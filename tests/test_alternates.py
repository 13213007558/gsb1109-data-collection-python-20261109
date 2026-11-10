"""Tests for the offline hreflang alternate-relationship checker."""

import gzip
from pathlib import Path

import pytest

from usp.alternates import (
    X_DEFAULT,
    AlternateCheckInputError,
    check_alternates,
    normalize_language,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "alternates"
SITEMAP_A = str(FIXTURE_DIR / "sitemap_a.xml")
SITEMAP_B = str(FIXTURE_DIR / "sitemap_b.xml")


def _write_sitemap(
    tmp_path: Path, name: str, content: str, gzipped: bool = False
) -> str:
    path = tmp_path / name
    data = content.encode("utf-8")
    path.write_bytes(gzip.compress(data) if gzipped else data)
    return str(path)


CLEAN_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
    <url>
        <loc>https://example.com/en/page</loc>
        <xhtml:link rel="alternate" hreflang="en" href="https://example.com/en/page"/>
        <xhtml:link rel="alternate" hreflang="fr" href="https://example.com/fr/page"/>
    </url>
    <url>
        <loc>https://example.com/fr/page</loc>
        <xhtml:link rel="alternate" hreflang="en" href="https://example.com/en/page"/>
        <xhtml:link rel="alternate" hreflang="fr" href="https://example.com/fr/page"/>
    </url>
</urlset>
"""


def test_normalize_language_is_case_insensitive_and_x_default_is_reserved():
    assert normalize_language("EN-GB") == "en-gb"
    assert normalize_language(" X-Default ") == X_DEFAULT


def test_clean_cluster_passes(tmp_path):
    path = _write_sitemap(tmp_path, "clean.xml", CLEAN_SITEMAP)
    report = check_alternates([path])

    assert report.ok is True
    assert report.issues == []
    assert report.page_count == 2
    assert report.group_count == 1
    assert report.to_dict()["status"] == "passed"


def test_fixture_reports_exactly_one_of_each_issue_type():
    report = check_alternates([SITEMAP_A, SITEMAP_B])

    codes = [issue.code for issue in report.issues]
    assert sorted(codes) == sorted(
        [
            "alternate_target_unverified",
            "conflicting_alternate_targets",
            "language_group_mismatch",
        ]
    )

    by_code = {issue.code: issue for issue in report.issues}

    unverified = by_code["alternate_target_unverified"]
    assert list(unverified.targets) == [
        "https://external.example.org/es/contact",
    ]
    assert list(unverified.edges[0].sources) == [SITEMAP_A, SITEMAP_B]

    conflict = by_code["conflicting_alternate_targets"]
    assert conflict.page == "https://example.com/de/contact"
    assert conflict.language == "de"
    assert list(conflict.targets) == [
        "https://example.com/de-ch/contact",
        "https://example.com/de/contact",
    ]

    mismatch = by_code["language_group_mismatch"]
    assert mismatch.page == "https://example.com/en/contact"
    assert mismatch.language == "de"
    assert "en" in mismatch.message


def test_input_order_does_not_change_issues_or_numbering():
    forward = check_alternates([SITEMAP_A, SITEMAP_B]).to_dict()
    reversed_ = check_alternates([SITEMAP_B, SITEMAP_A]).to_dict()

    assert forward == reversed_
    assert [issue["id"] for issue in forward["issues"]] == [
        "ALT-0001",
        "ALT-0002",
        "ALT-0003",
    ]


def test_duplicate_declarations_are_reported_once(tmp_path):
    first = _write_sitemap(tmp_path, "a.xml", CLEAN_SITEMAP)
    second = _write_sitemap(tmp_path, "b.xml", CLEAN_SITEMAP)

    report = check_alternates([first, second])

    assert report.ok is True
    assert report.edge_count == 4


def test_external_target_is_unverified_not_dead(tmp_path):
    sitemap = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
    <url>
        <loc>https://example.com/en/page</loc>
        <xhtml:link rel="alternate" hreflang="en" href="https://example.com/en/page"/>
        <xhtml:link rel="alternate" hreflang="ja" href="https://external.test/ja/page"/>
    </url>
</urlset>
"""
    path = _write_sitemap(tmp_path, "external.xml", sitemap)

    report = check_alternates([path])

    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.code == "alternate_target_unverified"
    assert list(issue.targets) == ["https://external.test/ja/page"]


def test_missing_reciprocal_self_reference(tmp_path):
    sitemap = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
    <url>
        <loc>https://example.com/en/page</loc>
        <xhtml:link rel="alternate" hreflang="en" href="https://example.com/en/page"/>
        <xhtml:link rel="alternate" hreflang="fr" href="https://example.com/fr/page"/>
    </url>
    <url>
        <loc>https://example.com/fr/page</loc>
        <xhtml:link rel="alternate" hreflang="en" href="https://example.com/en/page"/>
    </url>
</urlset>
"""
    path = _write_sitemap(tmp_path, "missing_self.xml", sitemap)

    report = check_alternates([path])
    codes = {issue.code for issue in report.issues}
    assert "missing_self_reference" in codes


def test_gzipped_sitemap_is_supported(tmp_path):
    path = _write_sitemap(tmp_path, "clean.xml.gz", CLEAN_SITEMAP, gzipped=True)

    report = check_alternates([path])

    assert report.ok is True


def test_unparseable_input_raises_input_error(tmp_path):
    path = _write_sitemap(
        tmp_path,
        "broken.xml",
        '<?xml version="1.0"?><unknown-root><x/></unknown-root>',
    )

    with pytest.raises(AlternateCheckInputError):
        check_alternates([path])


def test_sitemap_index_is_rejected(tmp_path):
    index = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <sitemap><loc>https://example.com/child.xml</loc></sitemap>
</sitemapindex>
"""
    path = _write_sitemap(tmp_path, "index.xml", index)

    with pytest.raises(AlternateCheckInputError):
        check_alternates([path])


def test_missing_file_raises_input_error(tmp_path):
    with pytest.raises(AlternateCheckInputError):
        check_alternates([str(tmp_path / "does-not-exist.xml")])
