#!/usr/bin/env python
"""Reproduction script for the on-demand memory behavior of :mod:`usp.streaming`.

The script generates XML urlset documents of two sizes (1,000 and 20,000 URLs)
and measures the ``tracemalloc`` peak memory retained by the streaming parser
while every page is consumed and immediately discarded. Output pages are
deliberately *not* collected into a list, so the measured peak only covers
memory retained by the parser itself (current ``<url>`` entry, read-ahead
buffer, de-duplication state and Expat internals).

It also verifies that the first page is yielded before the input stream has
been read to the end, using a stream that records every ``read()`` call.

Default run (1,000 vs 20,000 entries) completes in well under ten seconds:

.. code-block:: shell-session

    $ python scripts/streaming_memory_check.py

Sizes can be overridden for ad-hoc experiments, e.g.::

    $ python scripts/streaming_memory_check.py --small 500 --large 100000
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import tracemalloc
from pathlib import Path

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from usp.streaming import iter_sitemap_pages  # noqa: E402


class ReadCountingStream:
    """Binary stream counting ``read()`` calls and bytes handed to the parser."""

    def __init__(self, data: bytes):
        self._buffer = io.BytesIO(data)
        self.read_count = 0
        self.total_size = len(data)

    def read(self, size: int = -1) -> bytes:
        self.read_count += 1
        return self._buffer.read(size)

    @property
    def position(self) -> int:
        return self._buffer.tell()


def build_urlset(url_count: int) -> bytes:
    rows = b"".join(
        b"<url>"
        b"<loc>https://example.org/section/page-%08d</loc>"
        b"<lastmod>2024-01-01T00:00:00Z</lastmod>"
        b"<changefreq>weekly</changefreq>"
        b"<priority>0.6</priority>"
        b"</url>" % i
        for i in range(url_count)
    )
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + rows
        + b"</urlset>"
    )


def measure_parser_peak(
    document: bytes, chunk_size: int, deduplicate: bool
) -> tuple[int, int, int]:
    """Consume the whole document discarding pages; return (peak bytes, pages, reads)."""
    stream = ReadCountingStream(document)
    iterator = iter_sitemap_pages(
        stream, chunk_size=chunk_size, deduplicate=deduplicate
    )

    tracemalloc.start()
    pages = 0
    for _page in iterator:
        # The page goes out of scope immediately; it is never appended to a container,
        # so tracemalloc only observes memory retained by the parser.
        pages += 1
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert iterator.complete is True
    return peak, pages, stream.read_count


def verify_first_page_is_lazy(document: bytes, chunk_size: int) -> tuple[int, int, int]:
    stream = ReadCountingStream(document)
    iterator = iter_sitemap_pages(stream, chunk_size=chunk_size)
    next(iterator)
    reads_for_first_page = stream.read_count
    position_for_first_page = stream.position
    # Drain the rest so the parser finishes cleanly.
    for _page in iterator:
        pass
    return reads_for_first_page, position_for_first_page, stream.total_size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--small", type=int, default=1000, help="Smaller URL count.")
    parser.add_argument("--large", type=int, default=20000, help="Larger URL count.")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=4096,
        help="Bytes read from the input stream per read() call.",
    )
    args = parser.parse_args()

    print(
        f"Comparing tracemalloc parser peaks for {args.small:,} vs {args.large:,} URLs "
        f"(chunk_size={args.chunk_size:,})\n"
    )

    started = time.perf_counter()
    results = {}
    for label, count in (("small", args.small), ("large", args.large)):
        document = build_urlset(count)
        # De-duplication is disabled for the headline measurement so that the only
        # retained state is the parser's own (current entry + read-ahead buffer). The
        # tree-compatible de-duplication mode intentionally keeps an O(n) URL set and
        # is reported separately for comparison.
        peak, pages, reads = measure_parser_peak(
            document, args.chunk_size, deduplicate=False
        )
        dedup_peak, _dedup_pages, _dedup_reads = measure_parser_peak(
            document, args.chunk_size, deduplicate=True
        )
        first_reads, first_position, total_size = verify_first_page_is_lazy(
            document, args.chunk_size
        )
        results[label] = {
            "count": count,
            "document_bytes": len(document),
            "peak": peak,
            "dedup_peak": dedup_peak,
            "reads": reads,
            "first_reads": first_reads,
            "first_position": first_position,
            "total_size": total_size,
            "pages": pages,
        }

    elapsed = time.perf_counter() - started

    for label, data in results.items():
        print(f"[{label}] {data['count']:,} URLs")
        print(f"  document size            : {data['document_bytes']:,} bytes")
        print(f"  pages produced           : {data['pages']:,}")
        print(f"  parser tracemalloc peak  : {data['peak']:,} bytes")
        print(
            f"  peak with de-duplication : {data['dedup_peak']:,} bytes "
            "(tree-compatible O(n) URL set)"
        )
        print(f"  total stream read() calls: {data['reads']:,}")
        print(
            "  first page after         : "
            f"{data['first_reads']:,} read(s), "
            f"{data['first_position']:,}/{data['total_size']:,} input bytes "
            f"({data['first_position'] / data['total_size']:.1%})"
        )
        print()

    small_peak = results["small"]["peak"]
    large_peak = results["large"]["peak"]
    ratio = large_peak / small_peak if small_peak else float("inf")
    print(f"Peak memory ratio (large / small): {ratio:.2f}x")
    print(f"Elapsed: {elapsed:.2f}s")

    # A streaming parser must retain roughly constant memory regardless of document size.
    if ratio > 2.0:
        print("\nFAIL: parser peak grew with document size; it should stay bounded.")
        return 1
    if results["large"]["first_position"] >= results["large"]["total_size"]:
        print("\nFAIL: first page required reading the whole input stream.")
        return 1
    print("\nOK: parser memory stays bounded and the first page is produced lazily.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
