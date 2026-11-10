"""Budgeted, checkpointable sitemap crawl sessions.

Unlike :func:`usp.tree.sitemap_tree_for_homepage`, which builds the whole sitemap tree in a
single run, a :class:`CrawlSession` traverses the sitemap graph breadth-first and commits
results one document at a time. A *round* runs under a :class:`CrawlBudget` (maximum number
of documents and maximum cumulative decompressed bytes); when the budget runs out, the
session state can be saved to a versioned JSON checkpoint and resumed later -- possibly in
another process -- without re-fetching any completed document.

A sitemap document becomes a committable boundary only once it has been fully fetched and
parsed. If the byte budget cannot cover the document currently being read, its temporary
results are discarded (the bytes read so far still count towards the current round) and the
document is left on the frontier to be re-read with the next round's fresh budget.

Documents are identified by their final (post-redirect) URL. Shared sub-sitemaps are fetched
only once, but every parent/child reference is kept as an edge. Redirect loops, index
self-loops and failed documents get explicit statuses and never block other pending branches.

Out of scope (for now): fetching page content, lastmod-based incremental sync and any
business-level deletion logic.
"""

import argparse
import datetime
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import unquote_plus, urlparse

from .exceptions import GunzipException, SitemapException
from .fetch_parse import (
    IndexRobotsTxtSitemapParser,
    PlainTextSitemapParser,
    XMLSitemapParser,
)
from .helpers import gunzip, is_http_url
from .objects.page import (
    SitemapImage,
    SitemapNewsStory,
    SitemapPage,
    SitemapPageChangeFrequency,
)
from .objects.sitemap import (
    AbstractSitemap,
    IndexRobotsTxtSitemap,
    IndexWebsiteSitemap,
    IndexXMLSitemap,
    InvalidSitemap,
    PagesAtomSitemap,
    PagesRSSSitemap,
    PagesTextSitemap,
    PagesXMLSitemap,
)
from .web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientSuccessResponse,
    LocalWebClient,
    WebClientErrorResponse,
)
from .web_client.requests_client import RequestsWebClient

log = logging.getLogger(__name__)

CHECKPOINT_FORMAT = "usp-crawl-session"
"""Value of the ``format`` field in checkpoint files."""

CHECKPOINT_VERSION = 1
"""Checkpoint format version understood by this implementation."""

STATUS_COMPLETED = "completed"
"""Document was fetched and fully parsed."""

STATUS_FAILED = "failed"
"""Document could not be fetched or parsed; terminal, will not be retried."""

STATUS_REDIRECT_LOOP = "redirect_loop"
"""Fetching the document ended in a redirect loop; terminal."""

EDGE_SELF_LOOP = "self_loop"
"""Edge note for a sitemap index that references itself."""

_MAX_SITEMAP_SIZE = 100 * 1024 * 1024
"""Max. uncompressed sitemap size, mirrors :class:`~.SitemapFetcher`."""

_INDEX_KINDS = {
    "IndexXMLSitemap": IndexXMLSitemap,
    "IndexRobotsTxtSitemap": IndexRobotsTxtSitemap,
    "IndexWebsiteSitemap": IndexWebsiteSitemap,
}

_PAGES_KINDS = {
    "PagesXMLSitemap": PagesXMLSitemap,
    "PagesTextSitemap": PagesTextSitemap,
    "PagesRSSSitemap": PagesRSSSitemap,
    "PagesAtomSitemap": PagesAtomSitemap,
}


class CheckpointError(SitemapException):
    """Checkpoint file cannot be created or resumed from."""

    pass


@dataclass(frozen=True)
class CrawlBudget:
    """Per-round crawl budget.

    Both limits are optional; ``None`` means unbounded. The budget applies to a single
    round only -- every call to :meth:`CrawlSession.run_round` starts with a fresh budget.
    """

    max_documents: int | None = None
    """Maximum number of documents to commit in one round."""

    max_bytes: int | None = None
    """Maximum cumulative decompressed bytes to read in one round."""

    def __post_init__(self):
        if self.max_documents is not None and self.max_documents <= 0:
            raise SitemapException("max_documents must be positive or None.")
        if self.max_bytes is not None and self.max_bytes <= 0:
            raise SitemapException("max_bytes must be positive or None.")


@dataclass
class CrawlRoundStats:
    """Statistics of a single :meth:`CrawlSession.run_round` call."""

    documents_committed: int = 0
    """Documents committed (fully parsed, or terminally failed) in this round."""

    bytes_read: int = 0
    """Cumulative decompressed bytes read in this round, including failed reads."""

    budget_exhausted: bool = False
    """True if the round stopped because the budget ran out."""


def _parse_static_content(url: str, content: str) -> AbstractSitemap:
    """Parse already-downloaded and decoded sitemap content.

    Sub-sitemaps are left un-fetched: child sitemaps of index documents come back as
    :class:`~.InvalidSitemap` placeholders whose URLs are the discovered child URLs.
    Mirrors the parser selection of :meth:`~.SitemapFetcher.sitemap`.
    """
    web_client = LocalWebClient()

    # MIME types are unpredictable, so peek into the content instead
    if content[:20].strip().startswith("<"):
        parser = XMLSitemapParser(
            url=url,
            content=content,
            recursion_level=0,
            web_client=web_client,
            parent_urls=set(),
        )
    elif url.endswith("/robots.txt"):
        parser = IndexRobotsTxtSitemapParser(
            url=url,
            content=content,
            recursion_level=0,
            web_client=web_client,
            parent_urls=set(),
        )
    else:
        parser = PlainTextSitemapParser(
            url=url,
            content=content,
            recursion_level=0,
            web_client=web_client,
            parent_urls=set(),
        )

    return parser.sitemap()


def _response_is_gzipped(url: str, response: AbstractWebClientSuccessResponse) -> bool:
    path = unquote_plus(urlparse(url).path).lower()
    content_type = (response.header("content-type") or "").lower()
    return path.endswith(".gz") or "gzip" in content_type


def _decode_response_content(
    url: str, response: AbstractWebClientSuccessResponse
) -> tuple[str, int]:
    """Decode (and gunzip if needed) response content.

    :return: Tuple of decoded text and the decompressed byte count.
    """
    data = response.raw_data()
    if _response_is_gzipped(url=url, response=response):
        try:
            data = gunzip(data, max_output_bytes=_MAX_SITEMAP_SIZE)
        except GunzipException as ex:
            log.warning(f"Unable to gunzip response for {url}: {ex}")
    return data.decode("utf-8-sig", errors="replace"), len(data)


def _page_to_checkpoint(page: SitemapPage) -> dict:
    """Serialize a page to plain JSON types (no Python objects)."""
    news_story = None
    if page.news_story is not None:
        story = page.news_story
        news_story = {
            "title": story.title,
            "publish_date": story.publish_date.isoformat()
            if story.publish_date
            else None,
            "publication_name": story.publication_name,
            "publication_language": story.publication_language,
            "access": story.access,
            "genres": list(story.genres),
            "keywords": list(story.keywords),
            "stock_tickers": list(story.stock_tickers),
        }

    images = None
    if page.images is not None:
        images = [
            {
                "loc": image.loc,
                "caption": image.caption,
                "geo_location": image.geo_location,
                "title": image.title,
                "license": image.license,
            }
            for image in page.images
        ]

    return {
        "url": page.url,
        "priority": str(page.priority),
        "last_modified": page.last_modified.isoformat() if page.last_modified else None,
        "change_frequency": page.change_frequency.value
        if page.change_frequency
        else None,
        "news_story": news_story,
        "images": images,
        "alternates": [list(alternate) for alternate in page.alternates]
        if page.alternates is not None
        else None,
    }


def _parse_datetime(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    return datetime.datetime.fromisoformat(value)


def _page_from_checkpoint(data: dict) -> SitemapPage:
    """Deserialize a page saved by :func:`_page_to_checkpoint`."""
    news_story = None
    if data.get("news_story") is not None:
        story = data["news_story"]
        news_story = SitemapNewsStory(
            title=story["title"],
            publish_date=_parse_datetime(story.get("publish_date")),
            publication_name=story.get("publication_name"),
            publication_language=story.get("publication_language"),
            access=story.get("access"),
            genres=story.get("genres"),
            keywords=story.get("keywords"),
            stock_tickers=story.get("stock_tickers"),
        )

    images = None
    if data.get("images") is not None:
        images = [
            SitemapImage(
                loc=image["loc"],
                caption=image.get("caption"),
                geo_location=image.get("geo_location"),
                title=image.get("title"),
                license_=image.get("license"),
            )
            for image in data["images"]
        ]

    alternates = None
    if data.get("alternates") is not None:
        alternates = [tuple(alternate) for alternate in data["alternates"]]

    change_frequency = data.get("change_frequency")
    return SitemapPage(
        url=data["url"],
        priority=Decimal(data["priority"]),
        last_modified=_parse_datetime(data.get("last_modified")),
        change_frequency=SitemapPageChangeFrequency(change_frequency)
        if change_frequency
        else None,
        news_story=news_story,
        images=images,
        alternates=alternates,
    )


class CrawlSession:
    """A resumable, budgeted crawl over a graph of sitemap documents.

    :param entry_urls: One or more sitemap URLs to start the crawl from.
    :param web_client: Web client to fetch documents with; defaults to :class:`~.RequestsWebClient`.
    :param options: Opaque JSON-serializable options stored in the checkpoint. Resuming with
        different options is refused.
    """

    def __init__(
        self,
        entry_urls: str | list[str],
        web_client: AbstractWebClient | None = None,
        options: dict | None = None,
    ):
        if isinstance(entry_urls, str):
            entry_urls = [entry_urls]
        entry_urls = list(entry_urls)
        if not entry_urls:
            raise SitemapException("At least one entry URL is required.")
        for entry_url in entry_urls:
            if not is_http_url(entry_url):
                raise SitemapException(f"URL {entry_url} is not a HTTP(s) URL.")

        options = dict(options or {})
        try:
            json.dumps(options)
        except TypeError as ex:
            raise SitemapException(
                f"Session options must be JSON-serializable: {ex}"
            ) from ex

        if web_client is None:
            web_client = RequestsWebClient()
        web_client.set_max_response_data_length(_MAX_SITEMAP_SIZE)

        self._entry_urls = entry_urls
        self._options = options
        self._web_client = web_client

        # Frontier of pending documents, in FIFO (breadth-first) order.
        self._frontier: list[dict] = []
        self._frontier_by_url: dict[str, dict] = {}
        # Committed documents keyed by their final (post-redirect) URL.
        self._documents: dict[str, dict] = {}
        # Requested URL -> final URL, for documents fetched after a redirect.
        self._aliases: dict[str, str] = {}
        # Every discovered parent -> child reference.
        self._edges: list[dict] = []

        for entry_url in self._entry_urls:
            self._enqueue(entry_url, parent=None)

    def _enqueue(self, url: str, parent: str | None) -> None:
        existing = self._frontier_by_url.get(url)
        if existing is not None:
            if parent is not None and parent not in existing["parents"]:
                existing["parents"].append(parent)
            return
        item = {"url": url, "parents": [parent] if parent is not None else []}
        self._frontier.append(item)
        self._frontier_by_url[url] = item

    def _dequeue(self) -> dict:
        item = self._frontier.pop(0)
        del self._frontier_by_url[item["url"]]
        return item

    def _discover_edge(self, parent: str, child: str) -> None:
        resolved = self._aliases.get(child, child)
        note = EDGE_SELF_LOOP if resolved == parent else None
        self._edges.append({"parent": parent, "child": child, "note": note})

        if note == EDGE_SELF_LOOP:
            return
        if resolved in self._documents or child in self._aliases:
            # Already committed, or a known redirect to a committed document.
            return
        self._enqueue(child, parent=parent)

    def run_round(self, budget: CrawlBudget | None = None) -> CrawlRoundStats:
        """Process pending documents until the frontier empties or the budget runs out.

        :param budget: Budget for this round; defaults to an unbounded budget.
        :return: Statistics of the round.
        """
        budget = budget or CrawlBudget()
        stats = CrawlRoundStats()

        while self._frontier:
            if (
                budget.max_documents is not None
                and stats.documents_committed >= budget.max_documents
            ):
                stats.budget_exhausted = True
                break
            if budget.max_bytes is not None and stats.bytes_read >= budget.max_bytes:
                stats.budget_exhausted = True
                break

            item = self._frontier[0]
            url = item["url"]

            resolved = self._aliases.get(url, url)
            if resolved in self._documents or url in self._documents:
                # Already committed through another reference.
                self._dequeue()
                continue

            try:
                response = self._web_client.get(url)
            except Exception as ex:
                # E.g. a connection error in the middle of reading the response body.
                # The partial read still counts towards this round's byte budget.
                nbytes = int(getattr(ex, "bytes_read", 0) or 0)
                log.warning(f"Fetching {url} failed with an exception: {ex}")
                stats.bytes_read += nbytes
                stats.documents_committed += 1
                self._dequeue()
                self._commit_document(
                    url,
                    {
                        "url": url,
                        "status": STATUS_FAILED,
                        "reason": f"Unable to fetch sitemap from {url}: {ex}",
                        "kind": None,
                        "bytes": nbytes,
                        "children": [],
                        "pages": [],
                    },
                )
                continue

            if isinstance(response, WebClientErrorResponse):
                message = response.message()
                status = (
                    STATUS_REDIRECT_LOOP
                    if "redirect" in message.lower()
                    else STATUS_FAILED
                )
                log.warning(f"Fetching {url} failed: {message}")
                stats.documents_committed += 1
                self._dequeue()
                self._commit_document(
                    url,
                    {
                        "url": url,
                        "status": status,
                        "reason": f"Unable to fetch sitemap from {url}: {message}",
                        "kind": None,
                        "bytes": 0,
                        "children": [],
                        "pages": [],
                    },
                )
                continue

            assert isinstance(response, AbstractWebClientSuccessResponse)
            final_url = response.url()

            if final_url in self._documents:
                # Redirect to an already committed document; identity is the final URL.
                self._aliases[url] = final_url
                self._dequeue()
                continue

            content, nbytes = _decode_response_content(url=final_url, response=response)

            if (
                budget.max_bytes is not None
                and stats.bytes_read + nbytes > budget.max_bytes
            ):
                # The budget can't cover this document: discard its temporary results
                # and leave it on the frontier to be re-read next round. The bytes
                # read still count towards this round's budget.
                log.info(
                    f"Discarding {nbytes} bytes read from {final_url}: "
                    f"byte budget of {budget.max_bytes} bytes exhausted."
                )
                stats.bytes_read += nbytes
                stats.budget_exhausted = True
                break

            stats.bytes_read += nbytes
            if final_url != url:
                self._aliases[url] = final_url

            sitemap = _parse_static_content(url=final_url, content=content)
            self._dequeue()

            if isinstance(sitemap, InvalidSitemap):
                self._commit_document(
                    final_url,
                    {
                        "url": final_url,
                        "status": STATUS_FAILED,
                        "reason": sitemap.reason,
                        "kind": None,
                        "bytes": nbytes,
                        "children": [],
                        "pages": [],
                    },
                )
            else:
                children = [sub.url for sub in sitemap.sub_sitemaps]
                self._commit_document(
                    final_url,
                    {
                        "url": final_url,
                        "status": STATUS_COMPLETED,
                        "reason": None,
                        "kind": type(sitemap).__name__,
                        "bytes": nbytes,
                        "children": children,
                        "pages": [_page_to_checkpoint(page) for page in sitemap.pages],
                    },
                )
                for child_url in children:
                    self._discover_edge(parent=final_url, child=child_url)

            stats.documents_committed += 1

        return stats

    def _commit_document(self, final_url: str, record: dict) -> None:
        self._documents[final_url] = record

    @property
    def is_exhausted(self) -> bool:
        """True if there are no pending documents left."""
        return not self._frontier

    @property
    def frontier_urls(self) -> list[str]:
        """URLs of documents still pending, in processing order."""
        return [item["url"] for item in self._frontier]

    @property
    def documents(self) -> dict[str, dict]:
        """Committed documents keyed by final URL (shallow copy)."""
        return dict(self._documents)

    @property
    def edges(self) -> list[dict]:
        """Every discovered parent -> child reference."""
        return list(self._edges)

    def committed_pages(self) -> list[SitemapPage]:
        """All pages of committed documents, in commit order."""
        pages = []
        for record in self._documents.values():
            for page_data in record["pages"]:
                pages.append(_page_from_checkpoint(page_data))
        return pages

    # ------------------------------------------------------------------
    # Result tree
    # ------------------------------------------------------------------

    def sitemap_tree(self) -> AbstractSitemap:
        """Build a sitemap tree out of the documents committed so far.

        Pending documents are represented as :class:`~.InvalidSitemap` placeholders.
        Shared sub-sitemaps appear once per parent reference.

        :return: Root sitemap of the committed state.
        """
        nodes = [self._build_node(url, visiting=()) for url in self._entry_urls]
        if len(nodes) == 1:
            return nodes[0]
        return IndexWebsiteSitemap(url="usp-crawl-session://root", sub_sitemaps=nodes)

    def _build_node(self, url: str, visiting: tuple) -> AbstractSitemap:
        final_url = self._aliases.get(url, url)
        record = self._documents.get(final_url)

        if record is None:
            return InvalidSitemap(
                url=url, reason="Sitemap was not fetched (still pending or unknown)"
            )

        if final_url in visiting:
            return InvalidSitemap(
                url=final_url,
                reason=f"Recursion detected: sitemap index {final_url} references itself",
            )

        if record["status"] != STATUS_COMPLETED:
            return InvalidSitemap(url=final_url, reason=record["reason"])

        kind = record["kind"]
        if kind in _INDEX_KINDS:
            sub_sitemaps = [
                self._build_node(child_url, visiting=visiting + (final_url,))
                for child_url in record["children"]
            ]
            return _INDEX_KINDS[kind](url=final_url, sub_sitemaps=sub_sitemaps)

        pages = [_page_from_checkpoint(page) for page in record["pages"]]
        return _PAGES_KINDS[kind](url=final_url, pages=pages)

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    def to_checkpoint_dict(self) -> dict:
        """Serialize the session state to plain JSON types (no Python objects)."""
        return {
            "format": CHECKPOINT_FORMAT,
            "version": CHECKPOINT_VERSION,
            "entry": {
                "urls": list(self._entry_urls),
                "options": self._options,
            },
            "frontier": [dict(item) for item in self._frontier],
            "documents": self._documents,
            "aliases": self._aliases,
            "edges": [dict(edge) for edge in self._edges],
        }

    def save_checkpoint(self, path: str) -> None:
        """Save the session state to a versioned JSON checkpoint.

        The file is written to a temporary file first and then atomically moved over
        ``path``, so a crash mid-save cannot corrupt a previous checkpoint.

        :param path: Checkpoint file path.
        """
        payload = self.to_checkpoint_dict()
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(prefix=".usp-checkpoint-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(payload, tmp, indent=2, sort_keys=True)
                tmp.write("\n")
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        log.info(
            f"Saved checkpoint with {len(self._documents)} committed "
            f"documents and {len(self._frontier)} pending to {path}"
        )

    @classmethod
    def load_checkpoint(
        cls,
        path: str,
        web_client: AbstractWebClient | None = None,
        entry_urls: str | list[str] | None = None,
        options: dict | None = None,
    ) -> "CrawlSession":
        """Resume a session from a checkpoint file.

        :param path: Checkpoint file path.
        :param web_client: Web client to fetch the remaining documents with.
        :param entry_urls: Expected entry URLs; resuming is refused if they don't
            match the ones stored in the checkpoint. If ``None``, the stored entry
            URLs are used.
        :param options: Expected session options; resuming is refused if they don't
            match the stored ones. If ``None``, the stored options are used.
        :raises CheckpointError: If the file is corrupted, has an unsupported format
            or version, or the entry URLs / options don't match.
        """
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as ex:
            raise CheckpointError(
                f"Cannot resume from {path}: checkpoint is unreadable or corrupted: {ex}"
            ) from ex

        if not isinstance(payload, dict):
            raise CheckpointError(
                f"Cannot resume from {path}: checkpoint is corrupted "
                f"(top-level object is not a JSON object)."
            )

        if payload.get("format") != CHECKPOINT_FORMAT:
            raise CheckpointError(
                f"Cannot resume from {path}: not a usp crawl session checkpoint "
                f"(format is {payload.get('format')!r}, expected {CHECKPOINT_FORMAT!r})."
            )

        version = payload.get("version")
        if version != CHECKPOINT_VERSION:
            raise CheckpointError(
                f"Cannot resume from {path}: unsupported checkpoint version "
                f"{version!r} (expected {CHECKPOINT_VERSION})."
            )

        entry = payload.get("entry")
        if not isinstance(entry, dict) or not isinstance(entry.get("urls"), list):
            raise CheckpointError(
                f"Cannot resume from {path}: checkpoint is corrupted "
                f"(missing or invalid 'entry' section)."
            )
        stored_urls = entry["urls"]
        stored_options = entry.get("options") or {}
        if not all(isinstance(url, str) for url in stored_urls):
            raise CheckpointError(
                f"Cannot resume from {path}: checkpoint is corrupted "
                f"('entry.urls' is not a list of strings)."
            )
        if not isinstance(stored_options, dict):
            raise CheckpointError(
                f"Cannot resume from {path}: checkpoint is corrupted "
                f"('entry.options' is not an object)."
            )

        if entry_urls is not None:
            if isinstance(entry_urls, str):
                entry_urls = [entry_urls]
            if list(entry_urls) != stored_urls:
                raise CheckpointError(
                    f"Cannot resume from {path}: entry mismatch -- checkpoint was "
                    f"created for {stored_urls}, but resuming with {list(entry_urls)}."
                )

        if options is not None and dict(options) != stored_options:
            raise CheckpointError(
                f"Cannot resume from {path}: options mismatch -- checkpoint stores "
                f"{stored_options}, but resuming with {dict(options)}."
            )

        frontier = payload.get("frontier")
        documents = payload.get("documents")
        aliases = payload.get("aliases")
        edges = payload.get("edges")
        for name, value, expected_type in [
            ("frontier", frontier, list),
            ("documents", documents, dict),
            ("aliases", aliases, dict),
            ("edges", edges, list),
        ]:
            if not isinstance(value, expected_type):
                raise CheckpointError(
                    f"Cannot resume from {path}: checkpoint is corrupted "
                    f"('{name}' section is missing or invalid)."
                )

        session = cls(
            entry_urls=stored_urls,
            web_client=web_client,
            options=stored_options,
        )

        session._frontier = []
        session._frontier_by_url = {}
        for item in frontier:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise CheckpointError(
                    f"Cannot resume from {path}: checkpoint is corrupted "
                    f"(invalid frontier entry {item!r})."
                )
            parents = item.get("parents") or []
            normalized = {"url": item["url"], "parents": list(parents)}
            session._frontier.append(normalized)
            session._frontier_by_url[normalized["url"]] = normalized

        session._documents = documents
        session._aliases = dict(aliases)
        session._edges = [dict(edge) for edge in edges]

        log.info(
            f"Resumed checkpoint from {path} with {len(documents)} committed "
            f"documents and {len(session._frontier)} pending"
        )
        return session


def main(arg_list: list[str] | None = None) -> None:
    """Command line interface for crawl sessions.

    Start a new session::

        python -m usp.crawl_session run --entry https://example.com/sitemap.xml \\
            --checkpoint checkpoint.json --max-documents 200

    Resume it in another process::

        python -m usp.crawl_session resume --checkpoint checkpoint.json \\
            --max-documents 200
    """
    parser = argparse.ArgumentParser(
        prog="usp-crawl-session",
        description="Budgeted, checkpointable sitemap crawl sessions.",
    )
    subparsers = parser.add_subparsers(required=True, dest="command")

    run_parser = subparsers.add_parser("run", help="Start a new crawl session.")
    run_parser.add_argument(
        "--entry",
        action="append",
        required=True,
        help="Entry sitemap URL; may be given multiple times.",
    )
    run_parser.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Session option stored in the checkpoint; may be given multiple times.",
    )

    resume_parser = subparsers.add_parser(
        "resume", help="Resume a crawl session from a checkpoint."
    )
    resume_parser.add_argument(
        "--entry",
        action="append",
        default=None,
        help="Expected entry URL(s); resuming is refused on a mismatch.",
    )

    for subparser in (run_parser, resume_parser):
        subparser.add_argument(
            "--checkpoint", required=True, help="Checkpoint file path."
        )
        subparser.add_argument(
            "--max-documents",
            type=int,
            default=None,
            help="Maximum number of documents to commit in this round.",
        )
        subparser.add_argument(
            "--max-bytes",
            type=int,
            default=None,
            help="Maximum cumulative decompressed bytes to read in this round.",
        )

    args = parser.parse_args(arg_list)

    budget = CrawlBudget(max_documents=args.max_documents, max_bytes=args.max_bytes)

    if args.command == "run":
        options = {}
        for key_value in args.option:
            key, _, value = key_value.partition("=")
            options[key] = value
        session = CrawlSession(entry_urls=args.entry, options=options)
    else:
        session = CrawlSession.load_checkpoint(args.checkpoint, entry_urls=args.entry)

    stats = session.run_round(budget)
    session.save_checkpoint(args.checkpoint)

    sys.stdout.write(
        f"Committed {stats.documents_committed} documents "
        f"({stats.bytes_read} decompressed bytes) in this round; "
        f"{len(session.frontier_urls)} documents pending; "
        f"budget exhausted: {stats.budget_exhausted}; "
        f"checkpoint saved to {args.checkpoint}\n"
    )


if __name__ == "__main__":
    main()
