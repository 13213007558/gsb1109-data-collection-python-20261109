"""On-demand streaming parser for single XML ``urlset`` sitemaps.

Unlike :mod:`usp.tree`, which builds an in-memory (or disk-swapped) tree of
:class:`~usp.objects.page.SitemapPage` objects, this module consumes a binary
stream in small chunks and yields one page at a time as soon as its closing
``</url>`` tag is parsed. It is intended for back-end pipelines that persist
pages one by one, e.g. into a database, without ever materialising the whole
document.

Only a single XML ``urlset`` document is supported:

* ``sitemapindex`` documents and any other root element are rejected with
  :class:`~usp.exceptions.SitemapStreamFormatError`;
* index documents are not fetched or recursed into;
* RSS, Atom and plain-text sitemaps are out of scope here.

Gzip-compressed streams are decompressed incrementally as well: the compressed
input is read in small chunks and the full compressed document is never buffered
up-front.

The caller retains ownership of the passed binary stream and is responsible for
closing it; this module never closes the caller's stream.
"""

import struct
import xml.parsers.expat
import zlib
from collections.abc import Iterator

from .exceptions import (
    SitemapStreamFormatError,
    SitemapStreamTruncatedError,
    SitemapXMLParsingException,
)
from .fetch_parse import (
    StreamingPagesXMLSitemapParser,
    _create_hardened_xml_parser,
    _normalize_xml_element_name,
)
from .objects.page import SitemapPage

__all__ = [
    "iter_sitemap_pages",
    "SitemapUrlsetStream",
    "DEFAULT_CHUNK_SIZE",
]

DEFAULT_CHUNK_SIZE = 64 * 1024
"""Default number of compressed or plain bytes read from the input stream per ``read()`` call."""

_GZIP_MAGIC = b"\x1f\x8b"
_XML_NAMESPACE_SEPARATOR = " "


class _GzipChunkDecoder:
    """Incremental, chunk-wise gzip decompressor.

    Wraps a raw binary stream and transparently decompresses gzip member(s),
    reading the underlying stream only in small chunks. Concatenated gzip
    members (valid per :rfc:`1952`) are supported.

    Invariant: a single :meth:`read` call performs at most one ``read()`` on the
    wrapped compressed stream. When one call cannot make progress without another
    underlying read, it returns the plaintext already available (possibly an empty
    byte string) and resumes on the next call.
    """

    _STATE_HEADER = "header"
    _STATE_DEFLATE = "deflate"
    _STATE_FOOTER = "footer"
    _STATE_MEMBER_END = "member_end"
    _STATE_DONE = "done"

    __slots__ = [
        "_stream",
        "_chunk_size",
        "_buffer",
        "_pending_out",
        "_state",
        "_decompressor",
        "_member_size",
        "_stream_exhausted",
        "_member_end_probed",
    ]

    def __init__(self, stream, chunk_size: int):
        self._stream = stream
        self._chunk_size = chunk_size
        self._buffer = b""
        self._pending_out = b""
        self._state = self._STATE_HEADER
        self._decompressor = None
        self._member_size = 0
        self._stream_exhausted = False
        self._member_end_probed = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._chunk_size

        # One underlying read is allowed per processing phase; entering a new phase
        # (header -> deflate -> footer) refreshes the allowance for that phase.
        can_pull = True
        current_phase = self._state

        def pull_once() -> bool:
            nonlocal can_pull
            if not can_pull:
                return False
            can_pull = False
            chunk = self._stream.read(self._chunk_size)
            if chunk:
                self._buffer += chunk
                return True
            self._stream_exhausted = True
            return False

        while self._state != self._STATE_DONE:
            if self._state != current_phase:
                current_phase = self._state
                can_pull = True
            if self._state == self._STATE_HEADER:
                while not self._consume_header():
                    if not pull_once():
                        if self._stream_exhausted:
                            raise SitemapStreamTruncatedError(
                                "Gzip stream ended in the middle of a gzip header."
                            )
                        return self._take_pending(size)
                self._decompressor = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
                self._member_size = 0
                self._state = self._STATE_DEFLATE

            elif self._state == self._STATE_DEFLATE:
                if not self._buffer:
                    if not pull_once():
                        if self._stream_exhausted:
                            raise SitemapStreamTruncatedError(
                                "Gzip stream ended before the end-of-stream marker was reached."
                            )
                        return self._take_pending(size)
                pending = self._buffer
                self._buffer = b""
                try:
                    # No output cap: zlib decides how much plaintext one compressed chunk
                    # yields. Plaintext stays in ``_pending_out`` (bounded by the ratio of
                    # one chunk) until consumed by callers.
                    decompressed = self._decompressor.decompress(pending)
                except zlib.error as ex:
                    raise SitemapStreamFormatError(
                        f"Unable to gunzip stream: {ex}"
                    ) from ex
                self._member_size += len(decompressed)
                self._pending_out += decompressed
                # Bytes past the deflate EOF are the gzip trailer / next member; anything
                # zlib could not consume in one go stays buffered too.
                self._buffer = (
                    self._decompressor.unconsumed_tail + self._decompressor.unused_data
                )
                if self._decompressor.eof:
                    self._state = self._STATE_FOOTER

            elif self._state == self._STATE_FOOTER:
                # 8-byte CRC32 + ISIZE trailer.
                if len(self._buffer) < 8 and not pull_once():
                    if self._stream_exhausted:
                        raise SitemapStreamTruncatedError(
                            "Gzip stream ended in the middle of its CRC/size trailer."
                        )
                    return self._take_pending(size)
                if len(self._buffer) < 8:
                    # Trailer incomplete but more compressed data is expected later.
                    return self._take_pending(size)
                expected_isize = struct.unpack("<I", self._buffer[4:8])[0]
                if self._member_size & 0xFFFFFFFF != expected_isize:
                    raise SitemapStreamFormatError(
                        "Gunzipped data size does not match the gzip ISIZE trailer."
                    )
                self._buffer = self._buffer[8:]
                if self._buffer:
                    # A concatenated gzip member starts immediately in buffered bytes.
                    self._state = self._STATE_HEADER
                else:
                    # The read allowance for this call was spent reaching the trailer;
                    # probe for a concatenated member (or clean EOF) on the next call.
                    self._state = self._STATE_MEMBER_END
                    self._member_end_probed = False
                    return self._take_pending(size)

            elif self._state == self._STATE_MEMBER_END:
                # Decide between clean EOF and a concatenated gzip member.
                if not self._member_end_probed:
                    if not pull_once():
                        if self._stream_exhausted:
                            self._state = self._STATE_DONE
                        else:
                            return self._take_pending(size)
                    else:
                        self._member_end_probed = True
                if self._stream_exhausted:
                    self._state = self._STATE_DONE
                else:
                    self._member_end_probed = False
                    self._state = self._STATE_HEADER

        return self._take_pending(size)

    def _take_pending(self, size: int) -> bytes:
        result = self._pending_out[:size]
        self._pending_out = self._pending_out[size:]
        return bytes(result)

    def _pull_more(self) -> bool:
        chunk = self._stream.read(self._chunk_size)
        if chunk:
            self._buffer += chunk
            return True
        self._stream_exhausted = True
        return False

    def _consume_header(self) -> bool:
        """Parse one RFC 1952 member header out of the buffer.

        Returns ``True`` when the full header was present, ``False`` if more
        compressed bytes are required.
        """
        buf = self._buffer
        if len(buf) < 10:
            return False
        if buf[:2] != _GZIP_MAGIC:
            raise SitemapStreamFormatError(
                "Stream was declared as gzip but does not start with gzip magic bytes."
            )
        if buf[2] != 8:
            raise SitemapStreamFormatError(
                f"Unsupported gzip compression method: {buf[2]}."
            )
        flags = buf[3]
        offset = 10

        if flags & 4:  # FEXTRA
            if len(buf) < offset + 2:
                return False
            extra_len = struct.unpack("<H", buf[offset : offset + 2])[0]
            offset += 2
            if len(buf) < offset + extra_len:
                return False
            offset += extra_len

        for flag_byte in (8, 16):  # FNAME, FCOMMENT: NUL-terminated strings
            if flags & flag_byte:
                end = buf.find(b"\x00", offset)
                if end == -1:
                    return False
                offset = end + 1

        if flags & 2:  # FHCRC
            if len(buf) < offset + 2:
                return False
            offset += 2

        self._buffer = buf[offset:]
        return True


class _PrefixReader:
    """Binary reader that serves a small buffered prefix before delegating to a stream.

    Used to feed the gzip-magic sniffed prefix back into the decompressor without
    ever closing (or owning) the caller's stream.
    """

    __slots__ = ["_stream", "_prefix", "_offset"]

    def __init__(self, stream, prefix: bytes):
        self._stream = stream
        self._prefix = prefix
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if self._offset < len(self._prefix):
            chunk = self._prefix[self._offset : self._offset + size]
            self._offset += len(chunk)
            if len(chunk) == size:
                return chunk
            rest = self._stream.read(size - len(chunk))
            return chunk + rest
        return self._stream.read(size)


class _StreamUrlsetDriver:
    """Expat event driver that accepts a single ``urlset`` root and rejects anything else.

    Root-element validation happens as soon as the opening tag is parsed, so a
    ``sitemapindex`` (or an RSS/feed/unknown root) fails before any page could be
    emitted.
    """

    __slots__ = [
        "_url",
        "_parser",
        "_page_parser",
        "_is_non_ns_sitemap",
        "_root_accepted",
    ]

    def __init__(self, url: str | None, on_page, deduplicate: bool):
        self._url = url
        self._page_parser = StreamingPagesXMLSitemapParser(
            url=url, on_page=on_page, deduplicate=deduplicate
        )
        self._is_non_ns_sitemap = False
        self._root_accepted = False

        # Bytes are fed to Expat directly, so UTF-8 is the assumed document encoding
        # (a leading UTF-8 BOM is still accepted natively by Expat).
        parser = _create_hardened_xml_parser(
            namespace_separator=_XML_NAMESPACE_SEPARATOR,
            encoding="utf-8",
        )
        parser.StartElementHandler = self._on_start_element
        parser.EndElementHandler = self._on_end_element
        parser.CharacterDataHandler = self._page_parser.xml_char_data
        self._parser = parser

    @property
    def root_accepted(self) -> bool:
        return self._root_accepted

    def parse(self, data: bytes, is_final: bool) -> None:
        self._parser.Parse(data, is_final)

    def _normalize(self, name: str) -> str:
        normalized, self._is_non_ns_sitemap = _normalize_xml_element_name(
            name=name,
            namespace_separator=_XML_NAMESPACE_SEPARATOR,
            is_non_ns_sitemap=self._is_non_ns_sitemap,
        )
        return normalized

    def _on_start_element(self, name: str, attrs: dict[str, str]) -> None:
        normalized = self._normalize(name)

        if not self._root_accepted:
            if normalized != "sitemap:urlset":
                root_name = name.rsplit(_XML_NAMESPACE_SEPARATOR, 1)[-1]
                if normalized == "sitemap:sitemapindex":
                    raise SitemapStreamFormatError(
                        "Streaming parser only supports a single XML urlset document; "
                        "a sitemapindex was provided and index recursion is not supported."
                    )
                raise SitemapStreamFormatError(
                    f"Streaming parser only supports an XML urlset root element; got '{root_name}'."
                )
            self._root_accepted = True

        self._page_parser.xml_element_start(name=normalized, attrs=attrs)

    def _on_end_element(self, name: str) -> None:
        normalized = self._normalize(name)
        self._page_parser.xml_element_end(name=normalized)


class SitemapUrlsetStream(Iterator[SitemapPage]):
    """Lazy, one-page-at-a-time iterator over a single XML ``urlset`` binary stream.

    The input stream is read in chunks of at most ``chunk_size`` bytes (gzip
    streams are decompressed incrementally as well), and each
    :class:`~usp.objects.page.SitemapPage` is yielded as soon as the closing
    ``</url>`` tag has been parsed. At most one page plus the current read-ahead
    chunk is retained by the iterator; previously yielded pages are never kept.

    The iterator distinguishes three end states:

    * a cleanly parsed document ends with :attr:`complete` set to ``True``;
    * a truncated input raises :class:`~usp.exceptions.SitemapStreamTruncatedError`,
      while pages already yielded stay valid and :attr:`complete` remains ``False``;
    * early termination (``break`` or :meth:`close`) leaves :attr:`complete`
      ``False`` and performs no further reads of the caller's stream.

    The passed stream is always owned by the caller and is never closed by this
    class, including on :meth:`close`, garbage collection or exceptions.

    :param stream: Binary stream exposing a ``read(size)`` method returning ``bytes``.
    :param url: Optional URL of the sitemap, used for log messages only.
    :param chunk_size: Maximum number of bytes pulled from ``stream`` per read.
    :param gzip: ``True`` forces gzip decompression, ``False`` forces plain XML,
        ``None`` (default) auto-detects gzip from the gzip magic bytes of the
        first chunk.
    :param deduplicate: Skip pages whose URL was already emitted, matching the
        tree parser's de-duplication. When ``False`` the parser keeps no
        per-URL state, giving fully bounded memory on arbitrarily large documents.
    """

    __slots__ = [
        "_stream",
        "_url",
        "_chunk_size",
        "_reader",
        "_driver",
        "_queue",
        "_complete",
        "_closed",
        "_pages_emitted",
        "_gzip_mode",
    ]

    def __init__(
        self,
        stream,
        url: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        gzip: bool | None = None,
        deduplicate: bool = True,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer.")

        self._stream = stream
        self._url = url
        self._chunk_size = chunk_size
        self._queue: list[SitemapPage] = []
        self._complete = False
        self._closed = False
        self._pages_emitted = 0
        self._reader = None

        self._driver = _StreamUrlsetDriver(
            url=url,
            on_page=self._queue.append,
            deduplicate=deduplicate,
        )

        self._gzip_mode = gzip

    @property
    def complete(self) -> bool:
        """Return ``True`` once the full ``urlset`` document has been parsed."""
        return self._complete

    @property
    def pages_emitted(self) -> int:
        """Number of pages yielded to the caller so far."""
        return self._pages_emitted

    @property
    def closed(self) -> bool:
        """Return ``True`` after :meth:`close` or terminal exhaustion of the iterator."""
        return self._closed

    def __iter__(self) -> "SitemapUrlsetStream":
        return self

    def __next__(self) -> SitemapPage:
        if self._closed:
            raise StopIteration

        while not self._queue:
            if self._complete:
                self._closed = True
                raise StopIteration
            self._pull_chunk()

        page = self._queue.pop(0)
        self._pages_emitted += 1
        return page

    def close(self) -> None:
        """Stop parsing without touching the caller's stream.

        No further ``read()`` calls are made on the input stream, which remains
        open and remains the caller's responsibility to close. Any pages not yet
        yielded are discarded and :attr:`complete` stays ``False``.
        """
        self._closed = True
        self._queue.clear()

    def __enter__(self) -> "SitemapUrlsetStream":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def _pull_chunk(self) -> None:
        if self._reader is None:
            self._init_reader()

        chunk = self._reader.read(self._chunk_size)
        is_final = not chunk
        try:
            self._driver.parse(chunk if chunk else b"", is_final=is_final)
            if is_final:
                self._complete = True
        except SitemapStreamFormatError:
            self._closed = True
            raise
        except SitemapStreamTruncatedError:
            self._closed = True
            raise
        except xml.parsers.expat.ExpatError as ex:
            self._closed = True
            if is_final:
                # Any parser failure while signalling EOF means the document was cut short.
                raise SitemapStreamTruncatedError(
                    f"XML urlset stream ended before the document was complete: {ex}"
                ) from ex
            raise SitemapStreamFormatError(
                f"Malformed XML in urlset stream: {ex}"
            ) from ex
        except SitemapXMLParsingException as ex:
            self._closed = True
            raise SitemapStreamFormatError(str(ex)) from ex

    def _init_reader(self) -> None:
        first_chunk = self._stream.read(self._chunk_size)
        if not first_chunk:
            self._reader = _EmptyReader()
            return

        is_gzip = self._gzip_mode
        if is_gzip is None:
            is_gzip = first_chunk.startswith(_GZIP_MAGIC)

        if is_gzip:
            if not first_chunk.startswith(_GZIP_MAGIC):
                raise SitemapStreamFormatError(
                    "Stream was declared as gzip but does not start with gzip magic bytes."
                )
            self._reader = _GzipChunkDecoder(
                stream=_PrefixReader(self._stream, first_chunk),
                chunk_size=self._chunk_size,
            )
        else:
            self._reader = _PrefixReader(self._stream, first_chunk)


class _EmptyReader:
    """Reader returned when the input stream was empty from the first read on."""

    __slots__ = ()

    def read(self, size: int = -1) -> bytes:
        return b""


def iter_sitemap_pages(
    stream,
    url: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    gzip: bool | None = None,
    deduplicate: bool = True,
) -> SitemapUrlsetStream:
    """Create a lazy iterator over the ``SitemapPage`` entries of one XML urlset stream.

    :param stream: Binary input stream (anything with a ``read(size) -> bytes`` method,
        e.g. a file object, :class:`io.BytesIO` or an HTTP response body). The stream
        is never closed by USP; closing it remains the caller's responsibility.
    :param url: Optional sitemap URL, used only for log messages.
    :param chunk_size: Maximum bytes read from ``stream`` per underlying read call.
    :param gzip: ``None`` auto-detects gzip via magic bytes, ``True`` forces gzip
        decompression, ``False`` treats the input as plain XML.
    :param deduplicate: Whether duplicate URLs are skipped (tree-API compatible default).
        Set to ``False`` for fully bounded retained memory.
    :return: Iterator of :class:`~usp.objects.page.SitemapPage`; see
        :class:`SitemapUrlsetStream` for lifecycle and error semantics.

    :raises SitemapStreamFormatError: The root element is not ``urlset`` (including
        ``sitemapindex``), or the stream is not valid gzip/XML.
    :raises SitemapStreamTruncatedError: The stream ended before the document was complete.
    """

    return SitemapUrlsetStream(
        stream=stream,
        url=url,
        chunk_size=chunk_size,
        gzip=gzip,
        deduplicate=deduplicate,
    )
