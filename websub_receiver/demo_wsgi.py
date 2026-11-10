"""Port-free WSGI demonstration of :class:`WebSubReceiver`.

The script never opens a network socket: it builds WSGI ``environ`` mappings
and drives the application in-process.  It demonstrates, in order:

1. ``GET`` subscription confirmation,
2. a valid signed ``POST`` delivery,
3. a duplicate delivery (accepted, not stored again),
4. a database commit failure followed by a successful hub retry,
5. ``GET`` unsubscribe confirmation and rejection of later deliveries.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from .receiver import WebSubReceiver

TOPIC = "https://example.org/news.atom"
# The unguessable token stays out of all output in this demonstration.
CALLBACK_PATH = "/websub/callback/9f2c1e7d4a8b4f6e8c0d1a2b3c4d5e6f"
SECRET = "hmac-shared-secret-value"
MAX_BODY_BYTES = 4096


@dataclass
class Response:
    status: str
    headers: list[tuple[str, str]]
    body: bytes


class ListLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class Clock:
    """Injectable clock so lease expiry never requires real waiting."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FailingCommitConnection(sqlite3.Connection):
    """Connection whose first inbox-row commit raises, then works normally."""

    fail_next_commit = False

    def commit(self) -> None:
        if FailingCommitConnection.fail_next_commit and self.in_transaction:
            FailingCommitConnection.fail_next_commit = False
            self.rollback()
            raise sqlite3.OperationalError("injected write failure")
        super().commit()


def call_app(
    app: WebSubReceiver,
    method: str,
    path: str,
    body: bytes = b"",
    query: dict[str, str] | None = None,
    signature: str | None = None,
) -> Response:
    query_string = urlencode(query or {})
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "application/atom+xml",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if signature is not None:
        environ["HTTP_X_HUB_SIGNATURE"] = signature

    captured: dict[str, Any] = {}

    def start_response(
        status: str, headers: list[tuple[str, str]]
    ) -> None:
        captured["status"] = status
        captured["headers"] = headers

    chunks = app(environ, start_response)
    return Response(
        status=captured["status"],
        headers=captured["headers"],
        body=b"".join(chunks),
    )


def sign(body: bytes) -> str:
    digest = hmac.new(
        SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def feed_body(headline: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example News</title>
  <link href="{TOPIC}"/>
  <id>urn:uuid:example-feed</id>
  <updated>2026-11-10T00:00:00Z</updated>
  <entry>
    <title>{headline}</title>
    <id>urn:uuid:{headline}</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <content>Confidential article body for {headline}.</content>
  </entry>
</feed>
""".encode("utf-8")


def main() -> int:
    log_handler = ListLogHandler()
    logging.getLogger("websub_receiver").addHandler(log_handler)
    logging.getLogger("websub_receiver").setLevel(logging.DEBUG)

    clock = Clock()
    # A shared in-memory connection keeps state across per-request opens.
    database = sqlite3.connect(
        ":memory:", check_same_thread=False, factory=FailingCommitConnection
    )
    database.execute("PRAGMA foreign_keys = ON")
    receiver = WebSubReceiver(
        ":memory:",
        max_body_bytes=MAX_BODY_BYTES,
        clock=clock,
        connect=lambda _db: database,
    )

    subscription_id = receiver.register_pending(
        topic=TOPIC,
        callback_path=CALLBACK_PATH,
        secret=SECRET,
        mode="subscribe",
        challenge="challenge-subscribe",
    )
    print(f"[1] registered pending subscribe, subscription_id={subscription_id}")

    verify = call_app(
        receiver,
        "GET",
        CALLBACK_PATH,
        query={
            "hub.mode": "subscribe",
            "hub.topic": TOPIC,
            "hub.challenge": "challenge-subscribe",
            "hub.lease_seconds": "3600",
        },
    )
    headers = dict(verify.headers)
    assert verify.status == "200 OK"
    assert verify.body == b"challenge-subscribe"
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["X-Content-Type-Options"] == "nosniff"
    subscription = receiver.get_subscription(CALLBACK_PATH)
    assert subscription is not None and subscription.active
    print(
        f"[1] subscription confirmed, lease expires at "
        f"{subscription.lease_expires_at}"
    )

    first_feed = feed_body("first-headline")
    first_delivery = call_app(
        receiver,
        "POST",
        CALLBACK_PATH,
        body=first_feed,
        signature=sign(first_feed),
    )
    assert first_delivery.status == "200 OK"
    assert receiver.count_inbox(subscription_id) == 1
    print("[2] valid signed delivery stored")

    duplicate = call_app(
        receiver,
        "POST",
        CALLBACK_PATH,
        body=first_feed,
        signature=sign(first_feed),
    )
    assert duplicate.status == "200 OK"
    assert dict(duplicate.headers).get("X-WebSub-Duplicate") == "true"
    assert receiver.count_inbox(subscription_id) == 1
    print("[3] duplicate delivery acknowledged without a new inbox row")

    retry_feed = feed_body("second-headline")
    FailingCommitConnection.fail_next_commit = True
    failed = call_app(
        receiver,
        "POST",
        CALLBACK_PATH,
        body=retry_feed,
        signature=sign(retry_feed),
    )
    assert failed.status == "500 Internal Server Error"
    assert receiver.count_inbox(subscription_id) == 1
    print("[4a] first attempt failed during commit and stayed uncommitted")

    retried = call_app(
        receiver,
        "POST",
        CALLBACK_PATH,
        body=retry_feed,
        signature=sign(retry_feed),
    )
    assert retried.status == "200 OK"
    assert receiver.count_inbox(subscription_id) == 2
    print("[4b] hub retry after the failure stored the delivery")

    receiver.register_pending(
        topic=TOPIC,
        callback_path=CALLBACK_PATH,
        secret=SECRET,
        mode="unsubscribe",
        challenge="challenge-unsubscribe",
    )
    unsubscribe = call_app(
        receiver,
        "GET",
        CALLBACK_PATH,
        query={
            "hub.mode": "unsubscribe",
            "hub.topic": TOPIC,
            "hub.challenge": "challenge-unsubscribe",
        },
    )
    assert unsubscribe.status == "200 OK"
    assert unsubscribe.body == b"challenge-unsubscribe"
    assert not receiver.get_subscription(CALLBACK_PATH).active
    print("[5a] unsubscribe confirmed; lease cleared")

    after_unsubscribe = call_app(
        receiver,
        "POST",
        CALLBACK_PATH,
        body=retry_feed,
        signature=sign(retry_feed),
    )
    assert after_unsubscribe.status == "403 Forbidden"
    assert receiver.count_inbox(subscription_id) == 2
    print("[5b] delivery after unsubscribe rejected; inbox unchanged")

    sensitive_values = [
        CALLBACK_PATH,
        CALLBACK_PATH.rsplit("/", 1)[-1],
        SECRET,
        "Confidential article body",
        "first-headline",
        "second-headline",
    ]
    leaked = [
        message
        for message in log_handler.messages
        if any(value in message for value in sensitive_values)
    ]
    assert not leaked, f"sensitive data appeared in logs: {leaked}"

    total = receiver.count_inbox()
    print(f"[result] final inbox count: {total}")
    print("[result] logs contain no callback token, secret, or article text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
