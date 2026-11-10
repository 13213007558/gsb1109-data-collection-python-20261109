"""Fully offline demo of the budgeted, resumable crawl session.

Runs a first round with a small document budget against an in-memory sitemap
graph (diamond sharing, self-loop, gzip document, mid-read failure), saves a
versioned JSON checkpoint, then resumes in a fresh session and finishes the
crawl -- without re-fetching any completed document.

Usage:

    python examples/crawl_session_demo.py

The checkpoint written to ``examples/checkpoint.sample.json`` can be inspected
directly, and a real crawl can be resumed with:

    python -m usp.crawl_session resume \
        --checkpoint examples/checkpoint.sample.json --max-documents 100
"""

import gzip
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from usp.crawl_session import CrawlBudget, CrawlSession  # noqa: E402
from usp.web_client.abstract_client import (  # noqa: E402
    AbstractWebClient,
    AbstractWebClientSuccessResponse,
    WebClientErrorResponse,
)

BASE = "http://example.com"
ENTRY_URL = f"{BASE}/sitemap_index.xml"
CHECKPOINT_PATH = os.path.join(os.path.dirname(__file__), "checkpoint.sample.json")


class FixtureResponse(AbstractWebClientSuccessResponse):
    def __init__(self, url, data):
        self._url = url
        self._data = data

    def status_code(self):
        return 200

    def status_message(self):
        return "OK"

    def header(self, case_insensitive_name):
        return None

    def raw_data(self):
        return self._data

    def url(self):
        return self._url


class FixtureWebClient(AbstractWebClient):
    """Offline web client: serves fixtures, follows redirects, logs requests."""

    def __init__(self, fixtures):
        self._fixtures = fixtures
        self.request_log = []

    def set_max_response_data_length(self, max_response_data_length):
        pass

    def get(self, url):
        self.request_log.append(url)
        seen = {url}
        current = url
        for _ in range(10):
            fixture = self._fixtures.get(current)
            if fixture is None:
                return WebClientErrorResponse("404 Not Found", retryable=False)
            if "redirect_to" in fixture:
                target = fixture["redirect_to"]
                if target in seen:
                    return WebClientErrorResponse(
                        f"Exceeded redirects: redirect loop for {url}",
                        retryable=False,
                    )
                seen.add(target)
                current = target
                continue
            if "error" in fixture:
                raise fixture["error"]
            return FixtureResponse(url=current, data=fixture["content"])
        return WebClientErrorResponse("Exceeded 10 redirects", retryable=False)


class PartialReadError(Exception):
    def __init__(self, message, bytes_read):
        super().__init__(message)
        self.bytes_read = bytes_read


def urlset(*locs):
    urls = "\n".join(f"<url><loc>{BASE}/{loc}</loc></url>" for loc in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n</urlset>\n"
    ).encode()


def index(*paths):
    sitemaps = "\n".join(f"<sitemap><loc>{BASE}/{p}</loc></sitemap>" for p in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{sitemaps}\n</sitemapindex>\n"
    ).encode()


def build_fixtures():
    return {
        f"{BASE}/sitemap_index.xml": {
            "content": index(
                "a.xml",
                "b_index.xml",
                "c_index.xml",
                "redirect_to_a.xml",
                "loop.xml",
                "flaky.xml",
                "rloop.xml",
            )
        },
        f"{BASE}/a.xml": {"content": urlset("news/a1.html", "news/a2.html")},
        f"{BASE}/b_index.xml": {"content": index("shared.xml", "b.xml")},
        f"{BASE}/b.xml": {"content": urlset("news/b1.html")},
        f"{BASE}/c_index.xml": {"content": index("shared.xml", "gz.xml.gz")},
        f"{BASE}/shared.xml": {"content": urlset("news/s1.html")},
        f"{BASE}/gz.xml.gz": {
            "content": gzip.compress(urlset("news/g1.html", "news/g2.html"))
        },
        f"{BASE}/redirect_to_a.xml": {"redirect_to": f"{BASE}/a.xml"},
        f"{BASE}/loop.xml": {"content": index("loop.xml", "a.xml")},
        f"{BASE}/flaky.xml": {
            "error": PartialReadError("connection reset mid-read", bytes_read=64)
        },
        f"{BASE}/rloop.xml": {"redirect_to": f"{BASE}/rloop.xml"},
    }


def out(message=""):
    sys.stdout.write(f"{message}\n")


def main():
    out("== Round 1: budget of 4 documents ==")
    client1 = FixtureWebClient(build_fixtures())
    session1 = CrawlSession(entry_urls=ENTRY_URL, web_client=client1)
    stats1 = session1.run_round(CrawlBudget(max_documents=4))
    session1.save_checkpoint(CHECKPOINT_PATH)
    out(
        f"committed={stats1.documents_committed} bytes={stats1.bytes_read} "
        f"pending={len(session1.frontier_urls)}"
    )
    out(f"checkpoint written to {CHECKPOINT_PATH}")
    out("resume a real crawl with:")
    out(
        "  python -m usp.crawl_session resume "
        f"--checkpoint {CHECKPOINT_PATH} --max-documents 100"
    )

    out("\n== Round 2: new session resumed from checkpoint ==")
    client2 = FixtureWebClient(build_fixtures())
    session2 = CrawlSession.load_checkpoint(CHECKPOINT_PATH, web_client=client2)
    stats2 = session2.run_round(CrawlBudget())
    out(
        f"committed={stats2.documents_committed} bytes={stats2.bytes_read} "
        f"exhausted={session2.is_exhausted}"
    )

    combined = client1.request_log + client2.request_log
    refetched = [url for url in set(combined) if combined.count(url) > 1]
    out(
        f"requests round 1: {len(client1.request_log)}, "
        f"round 2: {len(client2.request_log)}, re-fetched: {refetched or 'none'}"
    )

    out("\ncommitted pages:")
    for page in session2.committed_pages():
        out(f"  {page.url}")

    out("\ndocument statuses:")
    for url, record in session2.documents.items():
        out(f"  {record['status']:<14} {url}")


if __name__ == "__main__":
    main()
