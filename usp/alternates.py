"""Offline cross-checker for multilingual ``rel="alternate"`` relationships.

The checker reads already-downloaded sitemap files, parses them with the library's own
sitemap/page model (never fetching page bodies), aggregates every declared alternate edge
by absolute ``loc`` and verifies the reciprocity rules expected of hreflang clusters:

* a language declared on one page must point to exactly one target;
* the target page, when present in the supplied sitemaps, must point back;
* pages must identify themselves with a single language inside a language group;
* targets absent from the supplied inputs are reported as unverified, never as dead links.

All aggregation is based on set semantics, so swapping the order of the input files (or
of declarations within a file) does not change the problem set or its stable numbering.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .helpers import gunzip
from .objects.sitemap import (
    AbstractIndexSitemap,
    AbstractPagesSitemap,
    InvalidSitemap,
)
from .tree import sitemap_from_str

X_DEFAULT = "x-default"
"""Reserved hreflang label handled separately from ordinary language codes."""


class AlternateCheckInputError(Exception):
    """Raised when a supplied sitemap file cannot be read or parsed.

    The CLI maps this to the dedicated input-parsing-failure exit code.
    """


@dataclass(frozen=True, slots=True)
class AlternateEdge:
    """A single ``rel="alternate"`` declaration aggregated by absolute ``loc``."""

    source: str
    language: str
    target: str
    sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_x_default(self) -> bool:
        """Whether this edge carries the reserved ``x-default`` label."""
        return self.language == X_DEFAULT


def normalize_language(language: str) -> str:
    """Normalize an hreflang label for case-insensitive comparison.

    ``x-default`` is recognized as its own reserved label rather than a language.

    :param language: Raw ``hreflang`` value.
    :return: Lower-cased label.
    """
    return language.strip().lower()


@dataclass(slots=True)
class _PageRecord:
    """All declarations about one absolute ``loc`` found across the inputs."""

    loc: str
    source_files: set[str] = field(default_factory=set)
    language_targets: dict[str, set[str]] = field(default_factory=dict)
    edge_sources: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    def add(self, language: str, target: str, source_file: str) -> None:
        """Merge one alternate declaration, keeping every originating file."""
        self.language_targets.setdefault(language, set()).add(target)
        self.edge_sources.setdefault((language, target), set()).add(source_file)


@dataclass(frozen=True, slots=True)
class AlternateIssue:
    """One deterministic, de-duplicated problem in an alternate relationship group."""

    code: str
    group_id: str
    page: str
    language: str | None
    targets: tuple[str, ...]
    edges: tuple[AlternateEdge, ...]
    source_files: tuple[str, ...]
    message: str


@dataclass(slots=True)
class AlternateCheckReport:
    """Result of an offline alternate-relationship check."""

    source_files: tuple[str, ...]
    page_count: int
    edge_count: int
    group_count: int
    issues: list[AlternateIssue]

    @property
    def ok(self) -> bool:
        """Whether the inputs passed without any (including unverified) problems."""
        return not self.issues

    def to_dict(self) -> dict:
        """Serialize the report, including stable issue numbering, to plain data."""
        return {
            "status": "passed" if self.ok else "issues_found",
            "summary": {
                "source_files": list(self.source_files),
                "pages": self.page_count,
                "alternate_edges": self.edge_count,
                "language_groups": self.group_count,
                "issues": len(self.issues),
            },
            "issues": [
                {
                    "id": f"ALT-{index:04d}",
                    "code": issue.code,
                    "group_id": issue.group_id,
                    "message": issue.message,
                    "page": issue.page,
                    "language": issue.language,
                    "targets": list(issue.targets),
                    "edges": [
                        {
                            "source": edge.source,
                            "language": edge.language,
                            "target": edge.target,
                            "x_default": edge.is_x_default,
                            "source_files": list(edge.sources),
                        }
                        for edge in issue.edges
                    ],
                    "source_files": list(issue.source_files),
                }
                for index, issue in enumerate(self.issues, start=1)
            ],
        }


def _read_sitemap_content(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as ex:
        raise AlternateCheckInputError(
            f"Unable to read sitemap file {path}: {ex}"
        ) from ex

    if not raw:
        raise AlternateCheckInputError(f"Sitemap file {path} is empty")

    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gunzip(raw)
        except Exception as ex:
            raise AlternateCheckInputError(
                f"Unable to gunzip sitemap file {path}: {ex}"
            ) from ex

    return raw.decode("utf-8-sig", errors="replace")


def _load_records(paths: list[str]) -> dict[str, _PageRecord]:
    records: dict[str, _PageRecord] = {}

    for path in dict.fromkeys(paths):
        content = _read_sitemap_content(path)
        sitemap = sitemap_from_str(content)

        if isinstance(sitemap, InvalidSitemap):
            raise AlternateCheckInputError(
                f"Sitemap file {path} could not be parsed: {sitemap.reason}"
            )
        if isinstance(sitemap, AbstractIndexSitemap):
            raise AlternateCheckInputError(
                f"Sitemap file {path} is a sitemap index; pass the downloaded child "
                "sitemap files instead"
            )
        if not isinstance(sitemap, AbstractPagesSitemap):
            raise AlternateCheckInputError(
                f"Sitemap file {path} has unsupported type {type(sitemap).__name__}"
            )

        for page in sitemap.pages:
            record = records.setdefault(page.url, _PageRecord(loc=page.url))
            record.source_files.add(path)
            for raw_language, target in page.alternates or []:
                language = normalize_language(raw_language)
                record.add(language=language, target=target, source_file=path)

    return records


def _all_edges(records: dict[str, _PageRecord]) -> list[AlternateEdge]:
    edges: list[AlternateEdge] = []
    for loc in sorted(records):
        record = records[loc]
        for language, target in sorted(record.edge_sources):
            edges.append(
                AlternateEdge(
                    source=loc,
                    language=language,
                    target=target,
                    sources=tuple(sorted(record.edge_sources[(language, target)])),
                )
            )
    return edges


def _connected_groups(edges: list[AlternateEdge]) -> list[frozenset[str]]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.source, set()).add(edge.target)
        adjacency.setdefault(edge.target, set()).add(edge.source)

    groups: list[frozenset[str]] = []
    visited: set[str] = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        members: set[str] = set()
        queue = [start]
        while queue:
            node = queue.pop()
            if node in members:
                continue
            members.add(node)
            queue.extend(adjacency[node] - members)
        visited |= members
        groups.append(frozenset(members))
    return sorted(groups, key=lambda members: sorted(members))


def _group_id(members: frozenset[str]) -> str:
    digest = hashlib.sha1("|".join(sorted(members)).encode("utf-8")).hexdigest()
    return f"grp-{digest[:12]}"


def check_alternates(paths: list[str]) -> AlternateCheckReport:
    """Check hreflang alternate relationships across downloaded sitemap files.

    :param paths: Paths of already-downloaded sitemap files. Duplicates are ignored.
    :return: A deterministic report of every distinct problem found.
    :raises AlternateCheckInputError: If an input cannot be read or parsed as a page sitemap.
    """
    source_files = tuple(sorted(dict.fromkeys(paths)))
    records = _load_records(source_files)
    edges = _all_edges(records)
    groups = _connected_groups(edges)

    group_membership: dict[str, frozenset[str]] = {}
    for members in groups:
        for loc in members:
            group_membership[loc] = members

    def related_edges(members: frozenset[str]) -> list[AlternateEdge]:
        return [edge for edge in edges if edge.source in members]

    issues: list[AlternateIssue] = []

    # Conflicts first: one (case-insensitive) language pointing at several targets.
    # x-default is a reserved label and is excluded from language conflict detection.
    for loc in sorted(records):
        record = records[loc]
        for language in sorted(record.language_targets):
            if language == X_DEFAULT:
                continue
            targets = record.language_targets[language]
            if len(targets) < 2:
                continue
            members = group_membership.get(loc, frozenset((loc,)))
            group_edges = [
                edge
                for edge in related_edges(members)
                if edge.source == loc and edge.language == language
            ]
            issues.append(
                AlternateIssue(
                    code="conflicting_alternate_targets",
                    group_id=_group_id(members),
                    page=loc,
                    language=language,
                    targets=tuple(sorted(targets)),
                    edges=tuple(group_edges),
                    source_files=tuple(sorted(record.source_files)),
                    message=(
                        f"Page {loc} maps language '{language}' to multiple targets; "
                        "one language must identify exactly one page in the group"
                    ),
                )
            )

    source_language_conflicts: dict[str, set[str]] = {}
    for loc, record in records.items():
        for language, targets in record.language_targets.items():
            if language == X_DEFAULT or len(targets) != 1:
                continue
            target = next(iter(targets))
            source_language_conflicts.setdefault(target, set()).add(language)

    def self_language(loc: str) -> str | None:
        """The unique ordinary language a known page claims for itself."""
        if loc not in records:
            return None
        languages = {
            language
            for language, targets in records[loc].language_targets.items()
            if language != X_DEFAULT and loc in targets
        }
        return next(iter(languages)) if len(languages) == 1 else None

    seen_issues: set[tuple] = set()

    def add_issue(
        code: str,
        edge: AlternateEdge,
        message: str,
        targets: tuple[str, ...] | None = None,
    ) -> None:
        issue_key = (code, edge.source, edge.language, edge.target)
        if issue_key in seen_issues:
            return
        seen_issues.add(issue_key)
        record = records[edge.source]
        members = group_membership.get(edge.source, frozenset((edge.source,)))
        issues.append(
            AlternateIssue(
                code=code,
                group_id=_group_id(members),
                page=edge.source,
                language=edge.language,
                targets=targets or (edge.target,),
                edges=(
                    AlternateEdge(
                        source=edge.source,
                        language=edge.language,
                        target=edge.target,
                        sources=edge.sources,
                    ),
                ),
                source_files=tuple(sorted(record.source_files)),
                message=message,
            )
        )

    for edge in edges:
        if edge.target not in records:
            add_issue(
                code="alternate_target_unverified",
                edge=edge,
                message=(
                    f"Alternate target {edge.target} is not present in the supplied "
                    "sitemaps; its reciprocal link cannot be verified offline"
                ),
            )
            continue

        target_record = records[edge.target]
        if edge.target == edge.source:
            continue

        # Check 1 (self-reference): B must keep the same hreflang label and point it at B.
        self_targets = target_record.language_targets.get(edge.language, set())
        if edge.target not in self_targets:
            add_issue(
                code="missing_self_reference",
                edge=edge,
                message=(
                    f"Page {edge.source} declares {edge.language} -> {edge.target}, "
                    f"but {edge.target} does not identify itself with label "
                    f"'{edge.language}'"
                ),
            )
            continue

        if edge.is_x_default:
            continue

        # If the source page itself splits this language across several targets, a
        # missing group back-link is just a symptom of the reported conflict.
        if len(records[edge.source].language_targets.get(edge.language, ())) > 1:
            continue

        source_language = self_language(edge.source)
        if source_language is None:
            continue
        if len(source_language_conflicts.get(edge.source, set())) > 1:
            continue
        target_self_language = self_language(edge.target)
        if target_self_language is None:
            continue

        back_targets = target_record.language_targets.get(source_language, set())
        if edge.source not in back_targets:
            add_issue(
                code="language_group_mismatch",
                edge=edge,
                message=(
                    f"Page {edge.target} answers the {edge.language} link but does not "
                    f"map '{source_language}' back to {edge.source}; the language group "
                    "is inconsistent"
                ),
            )

    issues.sort(
        key=lambda issue: (
            issue.group_id,
            issue.code,
            issue.page,
            issue.language or "",
            issue.targets,
        )
    )

    return AlternateCheckReport(
        source_files=source_files,
        page_count=len(records),
        edge_count=len(edges),
        group_count=len(groups),
        issues=issues,
    )
