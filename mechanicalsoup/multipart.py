"""Streaming multipart/form-data encoder using only the standard library.

This module provides MultipartEncoder, an opt-in alternative to the
in-memory multipart encoding performed by Requests. It produces the
request body chunk by chunk, so that uploading a file of several
hundred MB does not require buffering the whole body in memory.
"""

import io
import mimetypes
import os
import uuid

#: Default size of the chunks read from file objects while streaming.
DEFAULT_CHUNK_SIZE = 64 * 1024


def _quote_header_param(value):
    """Quote a Content-Disposition parameter value per the HTML5 rules.

    Matches the behavior of urllib3's format_multipart_header_param:
    double-quote, CR, LF and non-ASCII characters are percent-encoded.
    """
    if not isinstance(value, bytes):
        value = str(value).encode("utf-8")
    result = []
    for byte in value:
        if byte in (0x22, 0x0D, 0x0A) or byte > 0x7F:
            result.append(f"%{byte:02X}")
        else:
            result.append(chr(byte))
    return "".join(result)


def _remaining_length(fileobj):
    """Return the number of bytes left in a seekable file object.

    The current position is restored after measuring. Return None if
    the object does not support seeking.
    """
    try:
        position = fileobj.tell()
    except (AttributeError, OSError):
        return None
    try:
        fileobj.seek(0, io.SEEK_END)
        end = fileobj.tell()
    except (AttributeError, OSError):
        return None
    finally:
        try:
            fileobj.seek(position, io.SEEK_SET)
        except (AttributeError, OSError):
            pass
    return end - position


class MultipartEncoder:
    """Iteratively encode form fields as a multipart/form-data body.

    :param fields: An ordered iterable of (name, value) pairs. Regular
        fields use a plain value (str or bytes). File fields use a
        (filename, fileobj) or (filename, fileobj, content_type) tuple,
        or a bare file object (the filename is then derived from its
        name attribute). Duplicate names and multiple files under the
        same name are preserved, and parts are emitted in the order
        given.
    :param boundary: The multipart boundary. A random one is generated
        if not specified.
    :param chunk_size: Maximum size of the chunks read from file objects.

    The encoder is an iterable of bytes suitable as the data argument
    of a Requests request. File objects are read at most once, in
    chunks of at most chunk_size bytes, are never spooled to disk, and
    are never closed by the encoder -- closing them remains the caller's
    responsibility, whether the upload succeeds, fails while reading,
    or the consumer stops reading the body early.

    len(encoder) is the total body length in bytes, provided that every
    file object is seekable (the remaining length is measured from the
    current position, which is restored afterwards). If any file is not
    seekable, len() raises TypeError, which makes Requests fall back to
    chunked transfer encoding instead of sending a Content-Length
    header.
    """

    def __init__(self, fields, boundary=None, chunk_size=DEFAULT_CHUNK_SIZE):
        self.fields = list(fields)
        self.boundary = boundary or uuid.uuid4().hex
        self.chunk_size = chunk_size
        self._len = None

    @property
    def content_type(self):
        """The value for the Content-Type header of the request."""
        return f"multipart/form-data; boundary={self.boundary}"


    def _parts(self):
        """Yield (header, content, is_file) for each field, in order."""
        boundary = self.boundary.encode("ascii")
        for name, value in self.fields:
            filename = None
            content_type = None
            if isinstance(value, (tuple, list)):
                filename = value[0]
                content = value[1]
                if len(value) > 2:
                    content_type = value[2]
            elif hasattr(value, "read"):
                content = value
                filename = os.path.basename(getattr(value, "name", "") or "")
            else:
                content = value

            disposition = f'form-data; name="{_quote_header_param(name)}"'
            if filename is not None:
                disposition += f'; filename="{_quote_header_param(filename)}"'
            header = b"--" + boundary + b"\r\n"
            header += f"Content-Disposition: {disposition}\r\n".encode("utf-8")
            if filename is not None:
                if content_type is None:
                    content_type = (
                        mimetypes.guess_type(filename)[0]
                        or "application/octet-stream"
                    )
                header += f"Content-Type: {content_type}\r\n".encode("utf-8")
            header += b"\r\n"

            if filename is None:
                if not isinstance(content, bytes):
                    content = str(content).encode("utf-8")
                yield header, content, False
            else:
                yield header, content, True


    def __iter__(self):
        """Yield the encoded body, chunk by chunk. Single pass only."""
        for header, content, is_file in self._parts():
            yield header
            if is_file:
                while True:
                    chunk = content.read(self.chunk_size)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    yield chunk
            else:
                yield content
            yield b"\r\n"
        yield b"--" + self.boundary.encode("ascii") + b"--\r\n"


    def __len__(self):
        """Total length of the encoded body in bytes.

        Raises TypeError if a file field is not seekable, in which case
        the length cannot be known without consuming it.
        """
        if self._len is not None:
            return self._len
        total = 0
        for header, content, is_file in self._parts():
            total += len(header) + len(b"\r\n")
            if is_file:
                remaining = _remaining_length(content)
                if remaining is None:
                    raise TypeError(
                        "Cannot determine the multipart body length: "
                        "a file field is not seekable."
                    )
                total += remaining
            else:
                total += len(content)
        total += len(self.boundary) + 6  # b"--" + boundary + b"--\r\n"
        self._len = total
        return total

    def __bool__(self):
        """Always truthy, even when the length is unknown.

        Requests evaluates the truthiness of the data argument (e.g.
        data or {}), which would otherwise raise TypeError from
        __len__ for non-seekable file fields.
        """
        return True
