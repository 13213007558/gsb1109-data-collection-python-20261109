Streaming Urlset Parsing
========================

The :doc:`tree-based API <sitemap-tree>` is convenient when you want the whole
sitemap hierarchy as objects, but it still parses each XML document fully and
builds every :class:`~usp.objects.page.SitemapPage` before you can consume the
first one. For back-end jobs such as inserting URLs into a database one row at a
time, :mod:`usp.streaming` provides a truly on-demand iterator over a single XML
``urlset`` document.

Basic usage
-----------

Pass any binary stream (a file, :class:`io.BytesIO`, an HTTP response body, ...)
to :func:`~usp.streaming.iter_sitemap_pages`. Pages are yielded as soon as their
closing ``</url>`` tag is parsed, so the input is read incrementally in small
chunks and previously yielded pages are never retained by the parser:

.. code-block:: python

    from usp.streaming import iter_sitemap_pages

    with open("sitemap.xml", "rb") as stream:
        for page in iter_sitemap_pages(stream):
            insert_into_database(page)

The stream is always owned by the caller: USP never closes it, including when
the iterator is closed early or raises an error, so the ``with`` statement (or
the caller's own cleanup) remains in charge of the stream lifecycle.

Gzip
----

Gzip-compressed sitemaps are handled transparently. Gzip is auto-detected from
the gzip magic bytes of the first chunk, or forced with the ``gzip`` argument.
Decompression is incremental as well -- the compressed stream is pulled in small
chunks and the full compressed archive is never buffered up-front, unlike
:func:`gzip.GzipFile`, which reads ahead in fixed 128 KiB blocks.

.. code-block:: python

    import gzip

    from usp.streaming import iter_sitemap_pages

    with gzip.open("sitemap.xml.gz", "rb") as stream:
        for page in iter_sitemap_pages(stream):
            insert_into_database(page)

Scope and lifecycle
-------------------

The streaming entry point deliberately supports **one XML ``urlset`` document**
only:

* a ``sitemapindex`` (or any other root element such as ``rss`` or ``feed``)
  raises :class:`~usp.exceptions.SitemapStreamFormatError` before any page is
  yielded -- index documents are never fetched or recursed into;
* RSS, Atom and plain-text sitemaps remain tree-API only;
* the yielded :class:`~usp.objects.page.SitemapPage` objects use exactly the
  same construction logic as the tree parser, so ``lastmod``, ``priority``,
  ``changefreq``, Google News, image and ``xhtml:link`` alternates keep their
  existing meaning (video markup is accepted but has no dedicated page field,
  matching the tree parser).

Three end states are distinguished:

* a complete document finishes with :attr:`~usp.streaming.SitemapUrlsetStream.complete`
  set to ``True``;
* a truncated stream raises :class:`~usp.exceptions.SitemapStreamTruncatedError`,
  while the pages already yielded stay valid and ``complete`` stays ``False``;
* stopping early with ``break`` or :meth:`~usp.streaming.SitemapUrlsetStream.close`
  performs no further reads and leaves ``complete`` as ``False``.

.. code-block:: python

    from usp.exceptions import SitemapStreamTruncatedError
    from usp.streaming import iter_sitemap_pages

    iterator = iter_sitemap_pages(stream)
    try:
        for index, page in enumerate(iterator):
            if index >= 20:
                # Only need the first 20 URLs; the rest of the stream is never read.
                iterator.close()
                break
            insert_into_database(page)
    except SitemapStreamTruncatedError:
        # Pages produced before the cut-off have already been processed above.
        log.warning("Sitemap stream was truncated")

Memory behavior
---------------

The parser only retains the current ``<url>`` entry, a small read-ahead buffer
and the Expat parser state; it does not accumulate page objects. By default it
also keeps a set of seen URLs to mirror the tree parser's de-duplication, which
is the one intentionally O(n) structure. Pass ``deduplicate=False`` for fully
bounded retained memory when duplicate URLs do not need filtering:

.. code-block:: python

    for page in iter_sitemap_pages(stream, deduplicate=False):
        insert_into_database(page)

A reproduction script comparing the ``tracemalloc`` peak for 1,000 vs 20,000
URLs (without collecting the output into a list) ships in the repository:

.. code-block:: shell-session

    $ python scripts/streaming_memory_check.py

Security
--------

The same hardening as the tree parser applies: ``DOCTYPE`` and entity
declarations are rejected, parameter entities are disabled and external entity
references raise an error, so untrusted sitemaps cannot trigger file or network
access through the parser.
