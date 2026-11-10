"""``usp check-alternates`` command: offline hreflang relationship checker."""

import argparse
import json
import sys

from usp.alternates import AlternateCheckInputError, check_alternates
from usp.cli._util import setup_logging

EXIT_OK = 0
EXIT_ISSUES_FOUND = 1
EXIT_INPUT_ERROR = 2


def register(subparsers):
    parser = subparsers.add_parser(
        "check-alternates",
        help="Check hreflang alternate links in downloaded sitemaps (offline)",
        description=(
            "Read already-downloaded sitemap files and verify multilingual "
            'rel="alternate" relationships without fetching page bodies.'
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "sitemaps",
        nargs="+",
        type=str,
        help="paths to downloaded sitemap XML files (plain or gzipped)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="write the JSON report to this file instead of standard output",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase output verbosity (-v=INFO, -vv=DEBUG)",
    )
    parser.add_argument(
        "-l",
        "--log-file",
        type=str,
        help="write log to this file and suppress console logging",
    )
    parser.set_defaults(func=check)


def _print_summary(report, stream) -> None:
    stream.write(
        f"Scanned {report.page_count} pages and {report.edge_count} alternate "
        f"edges in {report.group_count} language group(s) from "
        f"{len(report.source_files)} sitemap file(s).\n"
    )

    if report.ok:
        stream.write("OK: all alternate relationships are consistent.\n")
        return

    stream.write(f"Found {len(report.issues)} issue(s):\n")
    for issue in report.issues:
        stream.write(f"  [{issue.group_id}] {issue.code}: {issue.message}\n")


def check(args) -> None:
    setup_logging(args.verbose, args.log_file)

    try:
        report = check_alternates(args.sitemaps)
    except AlternateCheckInputError as ex:
        payload = {"status": "input_error", "error": str(ex)}
        _emit_json(payload, args.output)
        sys.stderr.write(f"INPUT ERROR: {ex}\n")
        sys.exit(EXIT_INPUT_ERROR)

    _emit_json(report.to_dict(), args.output)
    _print_summary(report, sys.stderr)
    sys.exit(EXIT_OK if report.ok else EXIT_ISSUES_FOUND)


def _emit_json(payload: dict, output_path: str | None) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=False)
    if output_path is None:
        sys.stdout.write(serialized + "\n")
        return
    try:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
    except OSError as ex:
        sys.stderr.write(
            f"INPUT ERROR: unable to write report to {output_path}: {ex}\n"
        )
        sys.exit(EXIT_INPUT_ERROR)
