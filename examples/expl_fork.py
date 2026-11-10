"""Demonstrate StatefulBrowser.fork() with two export flows on one page.

Scenario: a query page contains a single export form with a one-time
token and two submit buttons ("by region" / "by date"). Instead of
reusing one StatefulBrowser for both flows (the second flow would start
from whatever state the first flow left behind), we fork() the browser
twice. Each fork gets an independent copy of the page, form, cookies
and headers -- without re-GETting the page, so the one-time token stays
valid.

Everything runs offline against a local fake endpoint (requests_mock),
and the three resulting requests are printed with their fields, cookies
and the identity of the session that sent them.

Run with: python examples/expl_fork.py
"""

from urllib.parse import parse_qsl

import requests_mock

import mechanicalsoup

QUERY_PAGE = """
<html>
  <body>
    <form id="export" method="post" action="mock://export.local/submit">
      <input type="hidden" name="token" value="one-time-token-7f3a" />
      <input type="text" name="region" value="" />
      <input type="text" name="date" value="" />
      <input type="submit" name="by_region" value="Export by region" />
      <input type="submit" name="by_date" value="Export by date" />
    </form>
  </body>
</html>
"""


def main():
    # Local fake endpoint: a mock adapter mounted on the mock:// scheme.
    adapter = requests_mock.Adapter()
    adapter.register_uri("GET", "mock://export.local/query",
                         text=QUERY_PAGE,
                         headers={"Content-Type": "text/html"})
    adapter.register_uri("POST", "mock://export.local/submit", text="ok")

    parent = mechanicalsoup.StatefulBrowser(
        requests_adapters={"mock": adapter})
    parent.open("mock://export.local/query")
    parent.select_form("#export")
    parent.form.choose_submit("by_region")  # default export mode
    parent.session.cookies.set("session", "parent-session")
    parent.session.headers["X-Client"] = "export-bot"

    # Two independent branches from the current state. No HTTP request
    # is performed: the page (with its one-time token) is copied.
    region_branch = parent.fork()
    date_branch = parent.fork()
    session_ids = {
        "by-region branch": id(region_branch.session),
        "by-date branch": id(date_branch.session),
        "parent": id(parent.session),
    }

    # Branch 1: export by region.
    region_branch["region"] = "eu-west"
    region_branch.session.cookies.set("branch", "region")
    region_branch.submit_selected()

    # Branch 2: export by date, with its own submit button and cookies.
    date_branch["date"] = "2026-11-01..2026-11-09"
    date_branch.form.choose_submit("by_date")
    date_branch.session.cookies.set("branch", "date")
    date_branch.submit_selected()

    # Closing a branch never disturbs the parent: adapters and their
    # connection pools are shared but owned by the parent's session.
    region_branch.close()

    # The parent still holds its original state and submits it as-is.
    parent.submit_selected()

    posts = [r for r in adapter.request_history if r.method == "POST"]
    labels = ["by-region branch (closed)", "by-date branch", "parent"]
    for label, request in zip(labels, posts):
        print(f"--- POST from {label} ---")
        print(f"  session id     : "
              f"{session_ids[label.removesuffix(' (closed)')]}")
        print(f"  fields         : "
              f"{parse_qsl(request.text, keep_blank_values=True)}")
        print(f"  Cookie header  : {request.headers.get('Cookie')}")
        print(f"  X-Client header: {request.headers.get('X-Client')}")

    gets = [r for r in adapter.request_history if r.method == "GET"]
    print(f"GET requests issued: {len(gets)} "
          "(the page was never refetched for the forks)")
    print("parent session still open:", parent.session is not None)


if __name__ == "__main__":
    main()
