"""Exceptions used by the sitemap parser."""


class SitemapException(Exception):
    """
    Problem due to which we can't run further, e.g. wrong input parameters.
    """

    pass


class SitemapXMLParsingException(Exception):
    """
    XML parsing exception to be handled gracefully.
    """

    pass


class GunzipException(Exception):
    """
    Error decompressing seemingly gzipped content.
    See :func:`usp.helpers.gunzip`.
    """

    pass


class StripURLToHomepageException(Exception):
    """
    Problem parsing a sitemap URL and stripping it to its homepage.
    See :func:`usp.helpers.strip_url_to_homepage`.
    """

    pass


class SitemapStreamException(SitemapException):
    """
    Base class for errors raised by the streaming urlset parser in
    :mod:`usp.streaming`.
    """

    pass


class SitemapStreamFormatError(SitemapStreamException):
    """
    The binary stream does not contain a single XML ``urlset`` document, for
    example because the root element is a ``sitemapindex`` or the XML is
    malformed in a way that prevents parsing.
    """

    pass


class SitemapStreamTruncatedError(SitemapStreamException):
    """
    The input stream ended before the XML ``urlset`` document was fully parsed.

    Pages already yielded before the truncation remain valid; the iterator's
    ``complete`` flag stays ``False``.
    """

    pass
