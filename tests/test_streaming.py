"""Tests for the on-demand streaming urlset parser in :mod:`usp.streaming`."""

import gzip
import io
import tracemalloc
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from usp.exceptions import (
    SitemapStreamFormatError,
    SitemapStreamTruncatedError,
)
from usp.objects.page import (
    SitemapImage,
    SitemapNewsStory,
    SitemapPage,
    SitemapPageChangeFrequency,
)
from usp.streaming import (
    DEFAULT_CHUNK_SIZE,
    SitemapUrlsetStream,
    iter_sitemap_pages,
)


class ReadCountingStream:
    """Binary stream that records every ``read()`` call and refuses to be closed by USP."""

    def __init__(self, data: bytes):
        self._buffer = io.BytesIO(data)
        self.read_count = 0
        self.read_sizes = []
        self.closed_by_callee = False

    def read(self, size: int = -1) -> bytes:
        self.read_count += 1
        self.read_sizes.append(size)
        return self._buffer.read(size)

    def close(self) -> None:
        self.closed_by_callee = True

    @property
    def position(self) -> int:
        return self._buffer.tell()

    @property
    def total_size(self) -> int:
        return self._buffer.getbuffer().nbytes


FULL_URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"
        xmlns:video="http://www.google.com/schemas/sitemap-video/1.1"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
    <url>
        <loc>https://example.org/first</loc>
        <lastmod>2024-01-02T03:04:05Z</lastmod>
        <changefreq>daily</changefreq>
        <priority>0.7</priority>
        <xhtml:link rel="alternate" hreflang="fr" href="https://example.org/fr/first"/>
        <image:image>
            <image:loc>https://example.org/first.jpg</image:loc>
            <image:caption>caption</image:caption>
        </image:image>
        <video:video>
            <video:thumbnail_loc>https://example.org/first-thumb.jpg</video:thumbnail_loc>
            <video:title>Video</video:title>
        </video:video>
        <news:news>
            <news:publication>
                <news:name>Example News</news:name>
                <news:language>en</news:language>
            </news:publication>
            <news:publication_date>2024-01-02T03:04:05Z</news:publication_date>
            <news:title>First story</news:title>
        </news:news>
    </url>
    <url>
        <loc>https://example.org/second</loc>
        <priority>0.2</priority>
    </url>
</urlset>"""


def _expected_pages():
    return [
        SitemapPage(
            url="https://example.org/first",
            last_modified=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            change_frequency=SitemapPageChangeFrequency.DAILY,
            priority=Decimal("0.7"),
            news_story=SitemapNewsStory(
                title="First story",
                publish_date=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                publication_name="Example News",
                publication_language="en",
            ),
            images=[
                SitemapImage(
                    loc="https://example.org/first.jpg",
                    caption="caption",
                )
            ],
            alternates=[("fr", "https://example.org/fr/first")],
        ),
        SitemapPage(
            url="https://example.org/second",
            priority=Decimal("0.2"),
        ),
    ]


class TestSitemapUrlsetStream:
    def test_yields_pages_with_full_field_semantics(self):
        pages = list(iter_sitemap_pages(io.BytesIO(FULL_URLSET), chunk_size=64))
        assert pages == _expected_pages()

    def test_first_page_is_produced_before_input_is_fully_read(self):
        stream = ReadCountingStream(FULL_URLSET)
        iterator = iter_sitemap_pages(stream, chunk_size=64)

        first = next(iterator)

        assert first == _expected_pages()[0]
        # Only a handful of small chunks were needed for the first <url>.
        assert stream.read_count > 0
        assert stream.position < stream.total_size
        assert iterator.complete is False

        remaining = list(iterator)
        assert [first, *remaining] == _expected_pages()
        assert iterator.complete is True

    def test_caller_stream_is_never_closed(self):
        stream = ReadCountingStream(FULL_URLSET)
        list(iter_sitemap_pages(stream, chunk_size=64))
        assert stream.closed_by_callee is False

        gzip_stream = ReadCountingStream(gzip.compress(FULL_URLSET))
        list(iter_sitemap_pages(gzip_stream, chunk_size=64))
        assert gzip_stream.closed_by_callee is False

    def test_gzip_is_decompressed_on_demand(self):
        rows = b"".join(
            b"<url><loc>https://example.org/page/%d</loc></url>" % i
            for i in range(5000)
        )
        document = (
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + rows
            + b"</urlset>"
        )
        compressed = gzip.compress(document, compresslevel=6)
        stream = ReadCountingStream(compressed)
        iterator = iter_sitemap_pages(stream, chunk_size=256)

        first = next(iterator)

        assert first.url == "https://example.org/page/0"
        # On-demand decompression: the first page is available long before the whole
        # compressed archive has been read.
        assert stream.position < stream.total_size
        assert iterator.complete is False

        assert len(list(iterator)) == 4999
        assert iterator.complete is True

    def test_gzip_output_matches_plain_output(self):
        plain = list(iter_sitemap_pages(io.BytesIO(FULL_URLSET), chunk_size=37))
        gzipped = list(
            iter_sitemap_pages(io.BytesIO(gzip.compress(FULL_URLSET)), chunk_size=37)
        )
        assert gzipped == plain

    def test_gzip_reads_happen_in_small_chunks_not_whole_archive(self):
        rows = b"".join(
            b"<url><loc>https://example.org/page/%d</loc></url>" % i
            for i in range(20000)
        )
        document = (
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + rows
            + b"</urlset>"
        )
        compressed = gzip.compress(document, compresslevel=6)
        stream = ReadCountingStream(compressed)
        iterator = iter_sitemap_pages(stream, chunk_size=1024)

        next(iterator)

        # A small fraction of the compressed archive was enough for the first page.
        assert stream.position < stream.total_size / 2
        assert all(size <= 1024 for size in stream.read_sizes)

    def test_rejects_sitemapindex_without_emitting_pages(self):
        index = (
            b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<sitemap><loc>https://example.org/child.xml</loc></sitemap>"
            b"</sitemapindex>"
        )
        with pytest.raises(SitemapStreamFormatError, match="sitemapindex"):
            list(iter_sitemap_pages(io.BytesIO(index), chunk_size=16))

    @pytest.mark.parametrize(
        "document",
        [
            b'<rss version="2.0"><item/></rss>',
            b"<html><body>not a sitemap</body></html>",
            b'<feed xmlns="http://www.w3.org/2005/Atom"><entry/></feed>',
        ],
    )
    def test_rejects_other_root_elements(self, document):
        with pytest.raises(SitemapStreamFormatError, match="urlset root"):
            list(iter_sitemap_pages(io.BytesIO(document), chunk_size=16))

    def test_truncated_document_keeps_pages_already_produced(self):
        cut_at = FULL_URLSET.index(b"<url>", FULL_URLSET.index(b"<url>") + 1)
        truncated = FULL_URLSET[:cut_at]
        stream = ReadCountingStream(truncated)
        iterator = iter_sitemap_pages(stream, chunk_size=32)

        collected = []
        with pytest.raises(SitemapStreamTruncatedError):
            collected.extend(iterator)

        assert collected == [_expected_pages()[0]]
        assert iterator.complete is False
        assert iterator.closed is True

    def test_empty_stream_is_reported_as_truncated(self):
        with pytest.raises(SitemapStreamTruncatedError):
            list(iter_sitemap_pages(io.BytesIO(b""), chunk_size=16))

    def test_malformed_xml_inside_a_chunk_is_a_format_error(self):
        malformed = (
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>https://example.org/a</loc></url>"
            b"<url><</url></urlset>"
        )
        with pytest.raises(SitemapStreamFormatError):
            list(iter_sitemap_pages(io.BytesIO(malformed), chunk_size=4096))

    def test_early_break_stops_reading_and_marks_incomplete(self):
        stream = ReadCountingStream(FULL_URLSET)
        iterator = iter_sitemap_pages(stream, chunk_size=32)

        next(iterator)
        reads_after_first = stream.read_count
        iterator.close()

        assert stream.read_count == reads_after_first
        assert iterator.complete is False
        assert stream.closed_by_callee is False
        with pytest.raises(StopIteration):
            next(iterator)

    def test_context_manager_closes_iterator_not_stream(self):
        stream = ReadCountingStream(FULL_URLSET)
        with iter_sitemap_pages(stream, chunk_size=32) as iterator:
            next(iterator)
        assert iterator.closed is True
        assert iterator.complete is False
        assert stream.closed_by_callee is False

    def test_deduplication_matches_tree_parser_by_default(self):
        document = (
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>https://example.org/a</loc></url>"
            b"<url><loc>https://example.org/a</loc></url>"
            b"<url><loc>https://example.org/b</loc></url>"
            b"</urlset>"
        )
        pages = list(iter_sitemap_pages(io.BytesIO(document), chunk_size=16))
        assert [page.url for page in pages] == [
            "https://example.org/a",
            "https://example.org/b",
        ]

        pages_without_dedup = list(
            iter_sitemap_pages(io.BytesIO(document), chunk_size=16, deduplicate=False)
        )
        assert len(pages_without_dedup) == 3

    def test_explicit_gzip_flags(self):
        compressed = gzip.compress(FULL_URLSET)
        assert (
            list(iter_sitemap_pages(io.BytesIO(compressed), chunk_size=32, gzip=True))
            == _expected_pages()
        )
        assert (
            list(iter_sitemap_pages(io.BytesIO(FULL_URLSET), chunk_size=32, gzip=False))
            == _expected_pages()
        )

        with pytest.raises(SitemapStreamFormatError):
            list(iter_sitemap_pages(io.BytesIO(b"not gzip"), chunk_size=16, gzip=True))

    def test_doctype_and_entities_are_rejected(self):
        billion_laughs = (
            b"<!DOCTYPE lolz ["
            b'<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            b"]>"
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>&lol2;</loc></url></urlset>"
        )
        with pytest.raises(SitemapStreamFormatError, match="DOCTYPE"):
            list(iter_sitemap_pages(io.BytesIO(billion_laughs), chunk_size=32))

    def test_external_entity_reference_cannot_touch_the_filesystem(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET")
        document = (
            f'<!DOCTYPE urlset [<!ENTITY x SYSTEM "file://{secret}">]>'.encode()
            + b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + b"<url><loc>&x;</loc></url></urlset>"
        )
        with pytest.raises(SitemapStreamFormatError):
            list(iter_sitemap_pages(io.BytesIO(document), chunk_size=32))

    def test_default_chunk_size_constant(self):
        assert DEFAULT_CHUNK_SIZE == 64 * 1024
        assert SitemapUrlsetStream(io.BytesIO(b""))._chunk_size == DEFAULT_CHUNK_SIZE

    def test_invalid_chunk_size(self):
        with pytest.raises(ValueError, match="chunk_size"):
            SitemapUrlsetStream(io.BytesIO(b""), chunk_size=0)

    @pytest.mark.parametrize("url_count", [1000, 20000])
    def test_retained_memory_does_not_grow_with_document_size(self, url_count):
        rows = b"".join(
            b"<url><loc>https://example.org/page/%08d</loc></url>" % i
            for i in range(url_count)
        )
        document = (
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + rows
            + b"</urlset>"
        )

        # De-duplication keeps an O(n) URL set by design, so the bounded-memory
        # guarantee is measured with deduplicate=False: only the parser's own current
        # entry, read-ahead buffer and Expat state should count. Outputs are consumed
        # straight from the iterator and never collected into a list.
        iterator = iter_sitemap_pages(
            io.BytesIO(document), chunk_size=4096, deduplicate=False
        )
        tracemalloc.start()
        produced = 0
        for _page in iterator:
            produced += 1
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert produced == url_count
        # The 20x larger document must not inflate the parser's retained memory;
        # a loose ceiling leaves room for allocator noise while catching O(n) growth.
        assert peak < 200_000
