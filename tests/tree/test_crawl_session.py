"""Offline tests for usp.crawl_session: budgeted rounds, checkpoints and resume.

The fixture graph (all served from memory, no network):

    sitemap_index.xml
    |-- a.xml                 (pages a1, a2)
    |-- b_index.xml
    |   |-- shared.xml        (page s1)         <--+ diamond: two parents
    |   `-- b.xml             (page b1)          |
    |-- c_index.xml                              |
    |   |-- shared.xml        (same document) ---+
    |   `-- gz.xml.gz         (gzipped pages g1, g2)
    |-- redirect_to_a.xml     (301 -> a.xml)
    |-- loop.xml              (index referencing itself and a.xml)
    |-- flaky.xml             (raises mid-read)
    `-- rloop.xml             (redirect loop)
"""

import json
from dataclasses import dataclass

import pytest

from tests.helpers import gzip
from usp.crawl_session import (
    CHECKPOINT_VERSION,
    EDGE_SELF_LOOP,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REDIRECT_LOOP,
    CheckpointError,
    CrawlBudget,
    CrawlSession,
)
from usp.web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientSuccessResponse,
    WebClientErrorResponse,
)

BASE = "http://example.com"
ENTRY_URL = f"{BASE}/sitemap_index.xml"


class PartialReadError(Exception):
    """Simulates a connection error in the middle of reading a response body."""

    def __init__(self, message: str, bytes_read: int):
        super().__init__(message)
        self.bytes_read = bytes_read


class FixtureResponse(AbstractWebClientSuccessResponse):
    def __init__(self, url: str, data: bytes):
        self._url = url
        self._data = data

    def status_code(self) -> int:
        return 200

    def status_message(self) -> str:
        return "OK"

    def header(self, case_insensitive_name: str) -> str | None:
        if case_insensitive_name.lower() == "content-type":
            return "application/xml"
        return None

    def raw_data(self) -> bytes:
        return self._data

    def url(self) -> str:
        return self._url


@dataclass
class Fixture:
    content: bytes | None = None
    redirect_to: str | None = None
    error: Exception | None = None


class FixtureWebClient(AbstractWebClient):
    """Fully offline web client serving in-memory fixtures and logging requests."""

    def __init__(self, fixtures: dict[str, Fixture]):
        self._fixtures = fixtures
        self.request_log: list[str] = []

    def set_max_response_data_length(self, max_response_data_length) -> None:
        pass

    def request_log_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for url in self.request_log:
            counts[url] = counts.get(url, 0) + 1
        return counts

    def get(self, url: str):
        self.request_log.append(url)
        seen = {url}
        current = url
        for _ in range(10):
            fixture = self._fixtures.get(current)
            if fixture is None:
                return WebClientErrorResponse(message="404 Not Found", retryable=False)
            if fixture.redirect_to is not None:
                if fixture.redirect_to in seen:
                    return WebClientErrorResponse(
                        message=f"Exceeded redirects: redirect loop for {url}",
                        retryable=False,
                    )
                seen.add(fixture.redirect_to)
                current = fixture.redirect_to
                continue
            if fixture.error is not None:
                raise fixture.error
            return FixtureResponse(url=current, data=fixture.content)
        return WebClientErrorResponse(message="Exceeded 10 redirects", retryable=False)


def _urlset(*locs: str) -> bytes:
    urls = "\n".join(
        f"<url><loc>{BASE}/{loc}</loc><lastmod>2026-01-01T00:00:00+00:00</lastmod></url>"
        for loc in locs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    ).encode()


def _index(*paths: str) -> bytes:
    sitemaps = "\n".join(
        f"<sitemap><loc>{BASE}/{path}</loc></sitemap>" for path in paths
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{sitemaps}\n"
        "</sitemapindex>\n"
    ).encode()


def build_fixtures() -> dict[str, Fixture]:
    return {
        f"{BASE}/sitemap_index.xml": Fixture(
            content=_index(
                "a.xml",
                "b_index.xml",
                "c_index.xml",
                "redirect_to_a.xml",
                "loop.xml",
                "flaky.xml",
                "rloop.xml",
            )
        ),
        f"{BASE}/a.xml": Fixture(content=_urlset("news/a1.html", "news/a2.html")),
        f"{BASE}/b_index.xml": Fixture(content=_index("shared.xml", "b.xml")),
        f"{BASE}/b.xml": Fixture(content=_urlset("news/b1.html")),
        f"{BASE}/c_index.xml": Fixture(content=_index("shared.xml", "gz.xml.gz")),
        f"{BASE}/shared.xml": Fixture(content=_urlset("news/s1.html")),
        f"{BASE}/gz.xml.gz": Fixture(
            content=gzip(_urlset("news/g1.html", "news/g2.html"))
        ),
        f"{BASE}/redirect_to_a.xml": Fixture(redirect_to=f"{BASE}/a.xml"),
        f"{BASE}/loop.xml": Fixture(content=_index("loop.xml", "a.xml")),
        f"{BASE}/flaky.xml": Fixture(
            error=PartialReadError("connection reset mid-read", bytes_read=64)
        ),
        f"{BASE}/rloop.xml": Fixture(redirect_to=f"{BASE}/rloop.xml"),
    }


EXPECTED_PAGE_URLS = {
    f"{BASE}/news/a1.html",
    f"{BASE}/news/a2.html",
    f"{BASE}/news/b1.html",
    f"{BASE}/news/s1.html",
    f"{BASE}/news/g1.html",
    f"{BASE}/news/g2.html",
}


def run_baseline() -> tuple[CrawlSession, FixtureWebClient]:
    """Single uninterrupted round with an unbounded budget."""
    client = FixtureWebClient(build_fixtures())
    session = CrawlSession(entry_urls=ENTRY_URL, web_client=client)
    stats = session.run_round(CrawlBudget())
    assert not stats.budget_exhausted
    assert session.is_exhausted
    return session, client


class TestCrawlSessionBaseline:
    def test_uninterrupted_run(self):
        session, client = run_baseline()

        documents = session.documents
        assert documents[ENTRY_URL]["status"] == STATUS_COMPLETED
        assert documents[f"{BASE}/flaky.xml"]["status"] == STATUS_FAILED
        assert documents[f"{BASE}/rloop.xml"]["status"] == STATUS_REDIRECT_LOOP

        # Mid-read exception: partial bytes were charged to the round.
        assert documents[f"{BASE}/flaky.xml"]["bytes"] == 64

        # Gzipped document was decompressed and parsed.
        gz_children = documents[f"{BASE}/gz.xml.gz"]["pages"]
        assert {page["url"] for page in gz_children} == {
            f"{BASE}/news/g1.html",
            f"{BASE}/news/g2.html",
        }

        # Redirect identity: the document is committed under the final URL only.
        assert f"{BASE}/redirect_to_a.xml" not in documents
        assert documents[f"{BASE}/a.xml"]["status"] == STATUS_COMPLETED

        # Shared sub-sitemap fetched exactly once, but both references kept.
        assert client.request_log.count(f"{BASE}/shared.xml") == 1
        shared_edges = [
            (edge["parent"], edge["child"])
            for edge in session.edges
            if edge["child"] == f"{BASE}/shared.xml"
        ]
        assert shared_edges == [
            (f"{BASE}/b_index.xml", f"{BASE}/shared.xml"),
            (f"{BASE}/c_index.xml", f"{BASE}/shared.xml"),
        ]

        # Index self-loop has an explicit edge note and was not re-fetched.
        self_loop_edges = [
            edge
            for edge in session.edges
            if edge["parent"] == f"{BASE}/loop.xml"
            and edge["child"] == f"{BASE}/loop.xml"
        ]
        assert len(self_loop_edges) == 1
        assert self_loop_edges[0]["note"] == EDGE_SELF_LOOP
        assert client.request_log.count(f"{BASE}/loop.xml") == 1

        # Every document was fetched exactly once.
        for url, count in client.request_log_counts().items():
            assert count == 1, f"{url} was requested {count} times"

        # Failed branches did not stop the crawl; all pages were committed.
        assert {page.url for page in session.committed_pages()} == EXPECTED_PAGE_URLS


class TestCrawlSessionResume:
    def test_two_rounds_with_document_budget(self, tmp_path):
        """Run two rounds with a fresh session in between; merged == uninterrupted."""
        baseline, _ = run_baseline()

        checkpoint_path = str(tmp_path / "checkpoint.json")

        # Round 1: commit only the first 4 documents (index, a, b_index, c_index).
        client1 = FixtureWebClient(build_fixtures())
        session1 = CrawlSession(entry_urls=ENTRY_URL, web_client=client1)
        stats1 = session1.run_round(CrawlBudget(max_documents=4))
        assert stats1.budget_exhausted
        assert stats1.documents_committed == 4
        assert not session1.is_exhausted
        assert session1.frontier_urls == [
            f"{BASE}/redirect_to_a.xml",
            f"{BASE}/loop.xml",
            f"{BASE}/flaky.xml",
            f"{BASE}/rloop.xml",
            f"{BASE}/shared.xml",
            f"{BASE}/b.xml",
            f"{BASE}/gz.xml.gz",
        ]
        session1.save_checkpoint(checkpoint_path)

        # The checkpoint is plain, versioned JSON.
        with open(checkpoint_path, encoding="utf-8") as f:
            payload = json.load(f)
        assert payload["version"] == CHECKPOINT_VERSION
        assert payload["entry"]["urls"] == [ENTRY_URL]
        assert [item["url"] for item in payload["frontier"]] == (session1.frontier_urls)
        assert set(payload["documents"]) == {
            ENTRY_URL,
            f"{BASE}/a.xml",
            f"{BASE}/b_index.xml",
            f"{BASE}/c_index.xml",
        }

        # Round 2: a brand-new session resumes from the checkpoint.
        client2 = FixtureWebClient(build_fixtures())
        session2 = CrawlSession.load_checkpoint(
            checkpoint_path, web_client=client2, entry_urls=[ENTRY_URL]
        )
        stats2 = session2.run_round(CrawlBudget())
        assert not stats2.budget_exhausted
        assert session2.is_exhausted

        # Merged result is identical to the uninterrupted run.
        assert session2.sitemap_tree().to_dict() == baseline.sitemap_tree().to_dict()
        assert session2.committed_pages() == baseline.committed_pages()
        assert session2.edges == baseline.edges

        # Request log proves no completed document was re-fetched.
        combined_log = client1.request_log + client2.request_log
        for url in set(combined_log):
            assert combined_log.count(url) == 1, f"{url} was re-fetched"

    def test_byte_budget_discards_unfinished_document(self, tmp_path):
        """A document that doesn't fit the byte budget is discarded and re-read."""
        baseline, _ = run_baseline()
        fixtures = build_fixtures()

        def decompressed_size(url_path: str) -> int:
            content = fixtures[f"{BASE}/{url_path}"].content
            if url_path.endswith(".gz"):
                content = __import__("gzip").decompress(content)
            return len(content)

        # Round 1 budget covers exactly the first three documents plus one byte,
        # so the fourth document (c_index.xml) is read but doesn't fit.
        first_three = ["sitemap_index.xml", "a.xml", "b_index.xml"]
        max_bytes = sum(decompressed_size(path) for path in first_three) + 1

        checkpoint_path = str(tmp_path / "checkpoint.json")

        client1 = FixtureWebClient(build_fixtures())
        session1 = CrawlSession(entry_urls=ENTRY_URL, web_client=client1)
        stats1 = session1.run_round(CrawlBudget(max_bytes=max_bytes))

        assert stats1.budget_exhausted
        assert stats1.documents_committed == 3
        # The discarded read still counts towards this round's byte budget.
        assert stats1.bytes_read == max_bytes - 1 + decompressed_size("c_index.xml")
        # The unfinished document stays on the frontier.
        assert session1.frontier_urls[0] == f"{BASE}/c_index.xml"
        session1.save_checkpoint(checkpoint_path)

        # Round 2: fresh budget, new session; the discarded document is re-read.
        client2 = FixtureWebClient(build_fixtures())
        session2 = CrawlSession.load_checkpoint(
            checkpoint_path, web_client=client2, entry_urls=ENTRY_URL
        )
        stats2 = session2.run_round(CrawlBudget())
        assert session2.is_exhausted
        assert stats2.bytes_read > 0  # budget restarted for the new round

        # Merged result is identical to the uninterrupted run.
        assert session2.sitemap_tree().to_dict() == baseline.sitemap_tree().to_dict()
        assert session2.committed_pages() == baseline.committed_pages()

        # Only the discarded document was fetched twice; nothing else was re-fetched.
        combined_log = client1.request_log + client2.request_log
        for url in set(combined_log):
            expected = 2 if url == f"{BASE}/c_index.xml" else 1
            assert combined_log.count(url) == expected, (
                f"{url} requested {combined_log.count(url)} times"
            )


class TestCheckpointValidation:
    def _make_checkpoint(self, tmp_path) -> str:
        client = FixtureWebClient(build_fixtures())
        session = CrawlSession(
            entry_urls=ENTRY_URL, web_client=client, options={"owner": "crawler-1"}
        )
        session.run_round(CrawlBudget(max_documents=2))
        path = str(tmp_path / "checkpoint.json")
        session.save_checkpoint(path)
        return path

    def test_corrupted_checkpoint_is_refused(self, tmp_path):
        path = str(tmp_path / "checkpoint.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        with pytest.raises(CheckpointError, match="corrupted"):
            CrawlSession.load_checkpoint(path)

    def test_unknown_format_is_refused(self, tmp_path):
        path = str(tmp_path / "checkpoint.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"format": "something-else", "version": 1}, f)
        with pytest.raises(CheckpointError, match="not a usp crawl session"):
            CrawlSession.load_checkpoint(path)

    def test_version_mismatch_is_refused(self, tmp_path):
        path = self._make_checkpoint(tmp_path)
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        payload["version"] = CHECKPOINT_VERSION + 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with pytest.raises(CheckpointError, match="version"):
            CrawlSession.load_checkpoint(path)

    def test_entry_mismatch_is_refused(self, tmp_path):
        path = self._make_checkpoint(tmp_path)
        with pytest.raises(CheckpointError, match="entry mismatch"):
            CrawlSession.load_checkpoint(
                path, entry_urls=["http://example.com/other.xml"]
            )

    def test_options_mismatch_is_refused(self, tmp_path):
        path = self._make_checkpoint(tmp_path)
        with pytest.raises(CheckpointError, match="options mismatch"):
            CrawlSession.load_checkpoint(path, options={"owner": "crawler-2"})

    def test_resume_with_stored_entry_and_options(self, tmp_path):
        path = self._make_checkpoint(tmp_path)
        session = CrawlSession.load_checkpoint(
            path,
            web_client=FixtureWebClient(build_fixtures()),
            entry_urls=ENTRY_URL,
            options={"owner": "crawler-1"},
        )
        stats = session.run_round(CrawlBudget())
        assert session.is_exhausted
        assert stats.documents_committed > 0

    def test_save_is_atomic_and_leaves_no_temp_files(self, tmp_path):
        path = self._make_checkpoint(tmp_path)
        leftovers = [p for p in tmp_path.iterdir() if p.name != "checkpoint.json"]
        assert leftovers == []
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        # Frontier, completed documents, discovery relations and committed pages
        # are all recorded as plain JSON.
        assert isinstance(payload["frontier"], list)
        assert isinstance(payload["documents"], dict)
        assert isinstance(payload["edges"], list)
        committed = payload["documents"][ENTRY_URL]
        assert committed["status"] == STATUS_COMPLETED
        assert isinstance(committed["children"], list)

    def test_non_serializable_options_are_rejected(self):
        with pytest.raises(Exception, match="JSON-serializable"):
            CrawlSession(
                entry_urls=ENTRY_URL,
                web_client=FixtureWebClient(build_fixtures()),
                options={"callback": object()},
            )
