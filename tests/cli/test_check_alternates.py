"""Tests for the ``usp check-alternates`` CLI command."""

import json
import shlex
from pathlib import Path

import pytest

from usp.cli.cli import main as cli_main

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "alternates"
SITEMAP_A = str(FIXTURE_DIR / "sitemap_a.xml")
SITEMAP_B = str(FIXTURE_DIR / "sitemap_b.xml")


@pytest.fixture
def run_cli(capsys):
    def _run(args, expected_exit):
        with pytest.raises(SystemExit) as excinfo:
            cli_main(shlex.split(args))
        assert excinfo.value.code == expected_exit
        captured = capsys.readouterr()
        return captured.out, captured.err

    return _run


def _clean_sitemap(tmp_path: Path) -> str:
    content = """<?xml version="1.0" encoding="UTF-8"?>
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
    path = tmp_path / "clean.xml"
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_exit_code_zero_when_clean(run_cli, tmp_path):
    sitemap = _clean_sitemap(tmp_path)
    out, err = run_cli(f"check-alternates {sitemap}", expected_exit=0)

    payload = json.loads(out)
    assert payload["status"] == "passed"
    assert payload["summary"]["issues"] == 0
    assert "OK" in err


def test_exit_code_one_and_group_id_in_summary_when_issues_found(run_cli):
    out, err = run_cli(f"check-alternates {SITEMAP_A} {SITEMAP_B}", expected_exit=1)

    payload = json.loads(out)
    assert payload["status"] == "issues_found"
    assert len(payload["issues"]) == 3
    assert {issue["code"] for issue in payload["issues"]} == {
        "alternate_target_unverified",
        "conflicting_alternate_targets",
        "language_group_mismatch",
    }
    group_ids = {issue["group_id"] for issue in payload["issues"]}
    for group_id in group_ids:
        assert group_id in err


def test_shuffled_inputs_keep_identical_json_and_numbering(run_cli):
    forward_out, _ = run_cli(
        f"check-alternates {SITEMAP_A} {SITEMAP_B}", expected_exit=1
    )
    reversed_out, _ = run_cli(
        f"check-alternates {SITEMAP_B} {SITEMAP_A}", expected_exit=1
    )

    assert json.loads(forward_out) == json.loads(reversed_out)
    assert [issue["id"] for issue in json.loads(forward_out)["issues"]] == [
        "ALT-0001",
        "ALT-0002",
        "ALT-0003",
    ]


def test_exit_code_two_on_unparseable_input(run_cli, tmp_path):
    broken = tmp_path / "broken.xml"
    broken.write_text('<?xml version="1.0"?><unknown-root/>', encoding="utf-8")

    out, err = run_cli(f"check-alternates {broken}", expected_exit=2)

    payload = json.loads(out)
    assert payload["status"] == "input_error"
    assert "INPUT ERROR" in err


def test_exit_code_two_on_missing_file(run_cli, tmp_path):
    missing = tmp_path / "missing.xml"
    run_cli(f"check-alternates {missing}", expected_exit=2)


def test_report_can_be_written_to_file(run_cli, tmp_path):
    sitemap = _clean_sitemap(tmp_path)
    report_path = tmp_path / "report.json"
    out, _ = run_cli(
        f"check-alternates {sitemap} --output {report_path}", expected_exit=0
    )

    assert out == ""
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "passed"
