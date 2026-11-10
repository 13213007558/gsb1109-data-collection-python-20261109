"""Tests for the opt-in streaming multipart/form-data submission.

Includes a small in-memory experiment comparing the memory footprint of
the default (Requests-encoded) submission with the streaming one. All
data is generated at runtime; no large attachments are stored in the
repository.
"""

import email
import email.policy
import hashlib
import io
import tracemalloc

import pytest
import setpath  # noqa: F401, must come before 'import mechanicalsoup'
import requests

import mechanicalsoup
from mechanicalsoup.multipart import MultipartEncoder

CHUNK_SIZE = 64 * 1024

UPLOAD_FORM = """
<html><body>
  <form method="post" enctype="multipart/form-data"
        action="mock://upload.test/post">
    <input type="text" name="tag" value="first">
    <input type="file" name="attachment">
    <input type="text" name="tag" value="second">
    <input type="file" name="attachment">
    <input type="text" name="note" value="done">
  </form>
</body></html>
"""

TEXT_ONLY_FORM = """
<html><body>
  <form method="post" enctype="multipart/form-data"
        action="mock://upload.test/post">
    <input type="text" name="alpha" value="1">
    <input type="text" name="alpha" value="2">
  </form>
</body></html>
"""

URLENCODED_FORM = """
<html><body>
  <form method="post" action="mock://upload.test/post">
    <input type="text" name="alpha" value="1">
  </form>
</body></html>
"""

SINGLE_FILE_FORM = """
<html><body>
  <form method="post" enctype="multipart/form-data"
        action="mock://upload.test/post">
    <input type="text" name="tag" value="first">
    <input type="file" name="attachment">
  </form>
</body></html>
"""


class FakeBigFile(io.RawIOBase):
    """A seekable, runtime-generated large file.

    Forbids whole-file reads (read(-1)/read()), records the size of
    every read, and generates deterministic content on the fly so that
    no large buffer is ever held in memory or on disk.
    """

    def __init__(self, size, name="big.bin"):
        self.size = size
        self._pos = 0
        self.name = name
        self.read_sizes = []

    @staticmethod
    def content_slice(position, length):
        out = bytearray()
        while length > 0:
            block_index = position // 64
            offset = position % 64
            block = hashlib.sha512(
                f"fake-big-file-{block_index}".encode("ascii")).digest()
            take = min(length, 64 - offset)
            out += block[offset:offset + take]
            position += take
            length -= take
        return bytes(out)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self.size + offset
        return self._pos

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError(
                "whole-file read attempted: the encoder must stream")
        self.read_sizes.append(size)
        length = min(size, self.size - self._pos)
        data = self.content_slice(self._pos, length)
        self._pos += length
        return data


class NonSeekableFile(io.RawIOBase):
    """A read-once stream: no seeking, no known length, no disk spool."""

    def __init__(self, payload, name="stream.bin"):
        self._payload = payload
        self._pos = 0
        self.name = name

    def readable(self):
        return True

    def seekable(self):
        return False

    def tell(self):
        raise io.UnsupportedOperation("not seekable")

    def seek(self, offset, whence=io.SEEK_SET):
        raise io.UnsupportedOperation("not seekable")

    def read(self, size=-1):
        if size is None or size < 0:
            size = 4096
        data = self._payload[self._pos:self._pos + size]
        self._pos += len(data)
        return data


class FailingFile(io.RawIOBase):
    """A file whose read() always fails."""

    def __init__(self, name="broken.bin"):
        self.name = name

    def readable(self):
        return True

    def read(self, size=-1):
        raise OSError("simulated read failure")


class CaptureAdapter(requests.adapters.HTTPAdapter):
    """A requests adapter that captures request bodies locally.

    Streaming bodies are consumed chunk by chunk; when keep_body is
    False, only a hash and the total length are retained so that memory
    usage stays flat (used by the memory-comparison experiment).
    """

    def __init__(self, page, keep_body=True):
        super().__init__()
        self.page = page
        self.keep_body = keep_body
        self.requests = []

    def send(self, request, **kwargs):
        if request.method == "GET":
            return self._respond(request, self.page.encode("utf-8"))

        body = request.body
        if body is None or isinstance(body, (bytes, str)):
            captured = body
        else:
            chunks = []
            total = 0
            digest = hashlib.sha256()
            for chunk in body:
                total += len(chunk)
                digest.update(chunk)
                if self.keep_body:
                    chunks.append(chunk)
            captured = (b"".join(chunks) if self.keep_body
                        else (total, digest.hexdigest()))
        self.requests.append((request, captured))
        return self._respond(request, b"<html><body>ok</body></html>")

    @staticmethod
    def _respond(request, content):
        response = requests.Response()
        response.status_code = 200
        response.reason = "OK"
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "text/html"
        response._content = content
        return response


class EarlyStopAdapter(CaptureAdapter):
    """Consumes only the first chunk of a streaming request body."""

    def send(self, request, **kwargs):
        if request.method == "GET":
            return self._respond(request, self.page.encode("utf-8"))
        body = request.body
        if body is not None and not isinstance(body, (bytes, str)):
            for _ in body:
                break
        self.requests.append((request, None))
        return self._respond(request, b"<html><body>ok</body></html>")


def make_browser(page, adapter_class=CaptureAdapter, **adapter_kwargs):
    adapter = adapter_class(page, **adapter_kwargs)
    browser = mechanicalsoup.StatefulBrowser(
        requests_adapters={"mock": adapter})
    browser.open("mock://upload.test/form")
    browser.select_form()
    return browser, adapter


def attach_files(browser, *files):
    inputs = browser.form.form.find_all("input", {"type": "file"})
    assert len(inputs) == len(files)
    for tag, fileobj in zip(inputs, files):
        tag["value"] = fileobj


def parse_multipart(content_type, body):
    """Parse a multipart body with the standard library only."""
    message = email.message_from_bytes(
        b"Content-Type: " + content_type.encode("ascii")
        + b"\r\n\r\n" + body,
        policy=email.policy.default)
    assert message.is_multipart()
    return [
        (part.get_param("name", header="content-disposition"),
         part.get_filename(),
         part.get_payload(decode=True))
        for part in message.iter_parts()
    ]


def test_streaming_multipart_order_duplicates_and_content():
    browser, adapter = make_browser(UPLOAD_FORM)
    big = FakeBigFile(CHUNK_SIZE * 10 + 123)
    # Start mid-stream: the remaining length is measured from here and
    # the position is restored after measuring.
    big.seek(7)
    small = io.BytesIO(b"small-file-content")
    small.name = "notes.txt"
    attach_files(browser, big, small)

    browser.submit_selected(stream_multipart=True)

    request, body = adapter.requests[0]
    # Seekable source: an exact Content-Length is sent.
    assert request.headers["Content-Length"] == str(len(body))
    assert "Transfer-Encoding" not in request.headers

    parts = parse_multipart(request.headers["Content-Type"], body)
    # Fields and files appear in form order, duplicates included.
    assert [part[0] for part in parts] == [
        "tag", "attachment", "tag", "attachment", "note"]
    assert parts[0][2] == b"first"
    assert parts[2][2] == b"second"
    assert parts[4][2] == b"done"
    # Both files use the same field name and keep their own content.
    assert parts[1][1] == "big.bin"
    assert parts[1][2] == FakeBigFile.content_slice(7, big.size - 7)
    assert parts[3][1] == "notes.txt"
    assert parts[3][2] == b"small-file-content"

    # The big file was streamed in bounded chunks, never read at once.
    assert big.read_sizes
    assert max(big.read_sizes) <= CHUNK_SIZE
    # The caller's files are still open after a successful upload.
    assert not big.closed
    assert not small.closed


def test_streaming_multipart_non_seekable_uses_chunked_encoding():
    browser, adapter = make_browser(UPLOAD_FORM)
    payload = FakeBigFile.content_slice(0, CHUNK_SIZE * 3 + 5)
    stream = NonSeekableFile(payload)
    attach_files(browser, stream, io.BytesIO(b"other"))

    browser.submit_selected(stream_multipart=True)

    request, body = adapter.requests[0]
    # Length is unknowable without consuming the source: it must not be
    # faked, chunked transfer encoding is used instead.
    assert "Content-Length" not in request.headers
    assert request.headers["Transfer-Encoding"] == "chunked"

    parts = parse_multipart(request.headers["Content-Type"], body)
    assert parts[1][2] == payload
    # The source was read exactly once, to its end, and left open.
    assert stream._pos == len(payload)
    assert not stream.closed


def test_streaming_multipart_without_files_has_boundary():
    browser, adapter = make_browser(TEXT_ONLY_FORM)
    browser.submit_selected(stream_multipart=True)

    request, body = adapter.requests[0]
    content_type = request.headers["Content-Type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    parts = parse_multipart(content_type, body)
    assert [(name, content) for name, _, content in parts] == [
        ("alpha", b"1"), ("alpha", b"2")]


def test_streaming_multipart_read_error_does_not_close_file():
    browser, adapter = make_browser(UPLOAD_FORM)
    failing = FailingFile()
    attach_files(browser, failing, io.BytesIO(b"other"))

    with pytest.raises(OSError, match="simulated read failure"):
        browser.submit_selected(stream_multipart=True)
    assert not failing.closed


def test_streaming_multipart_early_body_close_does_not_close_file():
    browser, adapter = make_browser(UPLOAD_FORM, EarlyStopAdapter)
    big = FakeBigFile(CHUNK_SIZE * 4)
    attach_files(browser, big, io.BytesIO(b"other"))

    browser.submit_selected(stream_multipart=True)
    # The consumer stopped after the first chunk; the file is not closed.
    assert big.tell() < big.size
    assert not big.closed


def test_default_submission_is_unchanged():
    """Without the option, Requests encodes the body as before."""
    browser, adapter = make_browser(UPLOAD_FORM)
    attach_files(browser, io.BytesIO(b"aaa"), io.BytesIO(b"bbb"))
    browser.submit_selected()

    request, body = adapter.requests[0]
    assert request.headers["Content-Type"].startswith(
        "multipart/form-data; boundary=")
    parts = parse_multipart(request.headers["Content-Type"], body)
    names = sorted(part[0] for part in parts)
    # Requests keeps files in a dict, so same-name files collapse into
    # one part -- the historical behavior the streaming option fixes.
    assert names == ["attachment", "note", "tag", "tag"]


def test_stream_multipart_ignored_for_urlencoded_form():
    browser, adapter = make_browser(URLENCODED_FORM)
    browser.submit_selected(stream_multipart=True)

    request, body = adapter.requests[0]
    assert request.headers["Content-Type"] == (
        "application/x-www-form-urlencoded")
    assert body == "alpha=1"


def test_encoder_empty_fields_still_emits_boundary():
    encoder = MultipartEncoder([], boundary="BOUNDARY")
    assert b"".join(encoder) == b"--BOUNDARY--\r\n"
    assert len(encoder) == len(b"--BOUNDARY--\r\n")


def test_encoder_length_measurement_restores_file_position():
    fileobj = io.BytesIO(b"x" * 100)
    fileobj.seek(30)
    encoder = MultipartEncoder([("f", ("a.bin", fileobj))], boundary="B")
    length = len(encoder)
    assert fileobj.tell() == 30
    body = b"".join(encoder)
    assert len(body) == length
    assert fileobj.tell() == 100
    assert not fileobj.closed


def test_encoder_len_raises_for_non_seekable():
    encoder = MultipartEncoder(
        [("f", ("a.bin", NonSeekableFile(b"data")))], boundary="B")
    with pytest.raises(TypeError):
        len(encoder)
    # The body can still be produced, reading the source exactly once.
    assert b"data" in b"".join(encoder)


def measure_peak(func):
    tracemalloc.start()
    try:
        func()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_streaming_multipart_memory_comparison():
    """Small runtime experiment: streaming vs default memory footprint.

    The same 8 MB payload is uploaded both ways. The default path lets
    Requests read and encode the whole body in memory, while the
    streaming path keeps only small chunks alive at any time.
    """
    size = 8 * 1024 * 1024

    def run(streaming):
        adapter = CaptureAdapter(SINGLE_FILE_FORM, keep_body=False)
        browser = mechanicalsoup.StatefulBrowser(
            requests_adapters={"mock": adapter})
        browser.open("mock://upload.test/form")
        browser.select_form()
        if streaming:
            source = FakeBigFile(size)
        else:
            source = io.BytesIO(FakeBigFile.content_slice(0, size))
            source.name = "big.bin"
        attach_files(browser, source)
        peak = measure_peak(
            lambda: browser.submit_selected(stream_multipart=streaming))
        return peak, adapter.requests[0]

    peak_default, (default_request, default_body) = run(streaming=False)
    peak_stream, (stream_request, stream_body) = run(streaming=True)

    # The streaming submission peaks far below the default one and far
    # below the payload size.
    assert peak_stream < peak_default // 2
    assert peak_stream < size // 2

    # Both paths send the exact length they claim.
    total, _ = stream_body
    assert int(stream_request.headers["Content-Length"]) == total
    assert int(default_request.headers["Content-Length"]) == len(default_body)

    # And both transmit the same file content.
    parts = parse_multipart(
        default_request.headers["Content-Type"], default_body)
    assert parts[1][2] == FakeBigFile.content_slice(0, size)
