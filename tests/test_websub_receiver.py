"""Tests for the framework-free WebSub receiver."""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import sqlite3
import tempfile
from typing import Any

import pytest

from websub_receiver import WebSubError, WebSubReceiver
from websub_receiver import receiver as receiver_module

TOPIC = "https://example.org/topics/news"
PATH = "/websub/callback/unguessable-token"
SECRET = "the-shared-secret"

ATOM_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>News</title>
  <link href="https://example.org/topics/news"/>
  <id>urn:uuid:feed</id>
  <updated>2026-11-10T00:00:00Z</updated>
  <entry>
    <title>First</title>
    <id>urn:uuid:entry-1</id>
    <updated>2026-11-10T00:00:00Z</updated>
    <content>article body</content>
  </entry>
</feed>
"""


class MutableClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


class ListLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def signature(body: bytes, secret: str | bytes = SECRET) -> str:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def make_environ(
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
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
    for key, value in (headers or {}).items():
        environ[f"HTTP_{key.upper().replace('-', '_')}"] = value
    return environ


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def receiver(clock: MutableClock) -> WebSubReceiver:
    return WebSubReceiver(clock=clock, max_body_bytes=4096)


@pytest.fixture
def log_handler() -> ListLogHandler:
    handler = ListLogHandler()
    logger = logging.getLogger("websub_receiver.receiver")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)


def call(receiver: WebSubReceiver, environ: dict[str, Any]):
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    body = b"".join(receiver(environ, start_response))
    return captured["status"], dict(captured["headers"]), body


def verification_query(
    mode: str = "subscribe",
    topic: str = TOPIC,
    challenge: str = "ch",
    lease_seconds: str | None = "3600",
) -> str:
    from urllib.parse import urlencode

    params = {
        "hub.mode": mode,
        "hub.topic": topic,
        "hub.challenge": challenge,
    }
    if lease_seconds is not None:
        params["hub.lease_seconds"] = lease_seconds
    return urlencode(params)


def register(
    receiver: WebSubReceiver,
    mode: str = "subscribe",
    challenge: str = "ch",
    secret: str | bytes = SECRET,
    topic: str = TOPIC,
    path: str = PATH,
) -> int:
    return receiver.register_pending(topic, path, secret, mode, challenge)


def confirm_subscription(
    receiver: WebSubReceiver,
    clock: MutableClock,
    lease_seconds: int = 3600,
) -> int:
    subscription_id = register(receiver)
    status, headers, body = call(
        receiver,
        make_environ(
            "GET",
            PATH,
            verification_query(lease_seconds=str(lease_seconds)),
        ),
    )
    assert status == "200 OK"
    assert body == b"ch"
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["X-Content-Type-Options"] == "nosniff"
    subscription = receiver.get_subscription(PATH)
    assert subscription is not None
    assert subscription.lease_expires_at == clock.value + lease_seconds
    return subscription_id


class TestRegistration:
    def test_invalid_arguments_rejected(self) -> None:
        receiver = WebSubReceiver()
        with pytest.raises(WebSubError):
            receiver.register_pending("", PATH, SECRET, "subscribe", "ch")
        with pytest.raises(WebSubError):
            receiver.register_pending(TOPIC, "relative", SECRET, "subscribe", "ch")
        with pytest.raises(WebSubError):
            receiver.register_pending(TOPIC, PATH, SECRET, "delete", "ch")
        with pytest.raises(WebSubError):
            receiver.register_pending(TOPIC, PATH, 123, "subscribe", "ch")

    def test_constructor_limits_must_be_positive(self) -> None:
        with pytest.raises(WebSubError):
            WebSubReceiver(max_body_bytes=0)
        with pytest.raises(WebSubError):
            WebSubReceiver(pending_ttl_seconds=0)

    def test_renewal_preserves_lease_until_confirmed(
        self, receiver: WebSubReceiver, clock: MutableClock
    ) -> None:
        subscription_id = confirm_subscription(receiver, clock, lease_seconds=100)
        first_expiry = clock.value + 100

        clock.value += 50
        register(receiver, challenge="renewal")
        subscription = receiver.get_subscription(PATH)
        assert subscription.lease_expires_at == first_expiry

        clock.value += 10
        status, _, body = call(
            receiver,
            make_environ(
                "GET",
                PATH,
                verification_query(challenge="renewal", lease_seconds="200"),
            ),
        )
        assert status == "200 OK"
        assert body == b"renewal"
        renewed = receiver.get_subscription(PATH)
        assert renewed.lease_expires_at == clock.value + 200
        assert renewed.id == subscription_id

    def test_renewal_replaces_secret_only_after_confirmation(
        self, receiver: WebSubReceiver, clock: MutableClock
    ) -> None:
        confirm_subscription(receiver, clock, lease_seconds=100)
        old_body = ATOM_FEED
        status, _, _ = TestDelivery().post(receiver, body=old_body)
        assert status == "200 OK"

        clock.value += 10
        register(receiver, challenge="rotate", secret="rotated-secret")
        # Old secret still works while the new intent is merely pending.
        old_pending_body = ATOM_FEED.replace(b"First", b"Old")
        status, _, _ = TestDelivery().post(receiver, body=old_pending_body)
        assert status == "200 OK"

        call(
            receiver,
            make_environ(
                "GET",
                PATH,
                verification_query(challenge="rotate"),
            ),
        )
        rotated_body = ATOM_FEED.replace(b"First", b"Rotated")
        old_secret_status, _, _ = TestDelivery().post(
            receiver, body=rotated_body
        )
        assert old_secret_status == "403 Forbidden"
        new_secret_status, _, _ = TestDelivery().post(
            receiver,
            body=rotated_body,
            signature_header=signature(rotated_body, "rotated-secret"),
        )
        assert new_secret_status == "200 OK"

    def test_confirmed_intent_is_consumed(self, receiver, clock) -> None:
        confirm_subscription(receiver, clock)
        status, _, _ = call(
            receiver,
            make_environ("GET", PATH, verification_query()),
        )
        assert status == "404 Not Found"


class TestVerification:
    def test_challenge_echoed_verbatim(self, receiver, clock) -> None:
        register(receiver, challenge="verbatim-challenge")
        status, headers, body = call(
            receiver,
            make_environ(
                "GET",
                PATH,
                verification_query(challenge="verbatim-challenge"),
            ),
        )
        assert status == "200 OK"
        assert body == b"verbatim-challenge"
        assert headers["Content-Length"] == str(len(body))

    def test_wrong_path_does_not_match(self, receiver, clock) -> None:
        register(receiver)
        status, _, body = call(
            receiver,
            make_environ("GET", "/other/path", verification_query()),
        )
        assert status == "404 Not Found"
        assert body == b""
        assert receiver.get_subscription(PATH).lease_expires_at is None

    def test_wrong_topic_does_not_change_state(self, receiver, clock) -> None:
        register(receiver)
        status, _, _ = call(
            receiver,
            make_environ(
                "GET",
                PATH,
                verification_query(topic="https://example.org/other"),
            ),
        )
        assert status == "404 Not Found"
        assert receiver.get_subscription(PATH).lease_expires_at is None

    def test_wrong_mode_does_not_change_state(self, receiver, clock) -> None:
        register(receiver, mode="subscribe")
        status, _, _ = call(
            receiver,
            make_environ(
                "GET",
                PATH,
                verification_query(mode="unsubscribe", lease_seconds=None),
            ),
        )
        assert status == "404 Not Found"
        assert receiver.get_subscription(PATH).lease_expires_at is None

    def test_wrong_challenge_does_not_change_state(self, receiver, clock) -> None:
        register(receiver, challenge="real")
        status, _, _ = call(
            receiver,
            make_environ(
                "GET",
                PATH,
                verification_query(challenge="forged"),
            ),
        )
        assert status == "404 Not Found"
        assert receiver.get_subscription(PATH).lease_expires_at is None

    def test_expired_intent_does_not_confirm(
        self, receiver: WebSubReceiver, clock: MutableClock
    ) -> None:
        register(receiver)
        clock.value += receiver_module.DEFAULT_PENDING_TTL_SECONDS + 1
        status, _, _ = call(
            receiver,
            make_environ("GET", PATH, verification_query()),
        )
        assert status == "404 Not Found"
        assert receiver.get_subscription(PATH).lease_expires_at is None

    @pytest.mark.parametrize("lease_seconds", ["", "0", "-5", "soon"])
    def test_subscribe_requires_valid_lease(
        self, receiver: WebSubReceiver, clock: MutableClock, lease_seconds: str
    ) -> None:
        register(receiver)
        status, _, _ = call(
            receiver,
            make_environ(
                "GET",
                PATH,
                verification_query(lease_seconds=lease_seconds),
            ),
        )
        assert status == "404 Not Found"
        assert receiver.get_subscription(PATH).lease_expires_at is None

    def test_unsubscribe_clears_lease(
        self, receiver: WebSubReceiver, clock: MutableClock
    ) -> None:
        subscription_id = confirm_subscription(receiver, clock)
        register(receiver, mode="unsubscribe", challenge="bye")
        status, _, body = call(
            receiver,
            make_environ(
                "GET",
                PATH,
                verification_query(
                    mode="unsubscribe",
                    challenge="bye",
                    lease_seconds=None,
                ),
            ),
        )
        assert status == "200 OK"
        assert body == b"bye"
        subscription = receiver.get_subscription(PATH)
        assert subscription.id == subscription_id
        assert subscription.lease_expires_at is None
        assert not subscription.active

    def test_malformed_query_rejected(self, receiver, clock) -> None:
        register(receiver)
        status, _, _ = call(
            receiver,
            make_environ("GET", PATH, "hub.mode=subscribe"),
        )
        assert status == "404 Not Found"

    def test_only_get_and_post_allowed(self, receiver, clock) -> None:
        status, headers, _ = call(
            receiver, make_environ("PUT", PATH)
        )
        assert status == "405 Method Not Allowed"
        assert headers["Allow"] == "GET, POST"


class TestDelivery:
    def post(
        self,
        receiver: WebSubReceiver,
        body: bytes = ATOM_FEED,
        signature_header: str | None = None,
        path: str = PATH,
        content_length: int | None = None,
    ):
        headers = {}
        if signature_header is None:
            signature_header = signature(body)
        headers["X-Hub-Signature"] = signature_header
        environ = make_environ("POST", path, body=body, headers=headers)
        if content_length is not None:
            environ["CONTENT_LENGTH"] = str(content_length)
        return call(receiver, environ)

    def test_valid_delivery_stored(self, receiver, clock) -> None:
        subscription_id = confirm_subscription(receiver, clock)
        status, headers, _ = self.post(receiver)
        assert status == "200 OK"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert receiver.count_inbox(subscription_id) == 1
        item = receiver.list_inbox(subscription_id)[0]
        assert item.body_sha256 == hashlib.sha256(ATOM_FEED).hexdigest()
        assert item.parsed["entries"][0]["title"] == "First"
        assert item.received_at == clock.value

    def test_delivery_without_lease_rejected(self, receiver, clock) -> None:
        register(receiver)
        status, _, _ = self.post(receiver)
        assert status == "403 Forbidden"
        assert receiver.count_inbox() == 0

    def test_delivery_after_lease_expiry_rejected(
        self, receiver: WebSubReceiver, clock: MutableClock
    ) -> None:
        confirm_subscription(receiver, clock, lease_seconds=100)
        clock.value += 101
        status, _, _ = self.post(receiver)
        assert status == "403 Forbidden"
        assert receiver.count_inbox() == 0

    def test_renewal_window_keeps_old_lease_working(
        self, receiver: WebSubReceiver, clock: MutableClock
    ) -> None:
        confirm_subscription(receiver, clock, lease_seconds=100)
        clock.value += 50
        register(receiver, challenge="renew")
        status, _, _ = self.post(receiver)
        assert status == "200 OK"
        assert receiver.count_inbox() == 1

    def test_unsubscribed_delivery_rejected(self, receiver, clock) -> None:
        confirm_subscription(receiver, clock)
        register(receiver, mode="unsubscribe", challenge="bye")
        call(
            receiver,
            make_environ(
                "GET",
                PATH,
                verification_query(
                    mode="unsubscribe",
                    challenge="bye",
                    lease_seconds=None,
                ),
            ),
        )
        status, _, _ = self.post(receiver)
        assert status == "403 Forbidden"
        assert receiver.count_inbox() == 0

    def test_duplicate_body_returns_success_without_new_row(
        self, receiver, clock
    ) -> None:
        subscription_id = confirm_subscription(receiver, clock)
        first_status, first_headers, _ = self.post(receiver)
        second_status, second_headers, _ = self.post(receiver)
        assert first_status == second_status == "200 OK"
        assert "X-WebSub-Duplicate" not in first_headers
        assert second_headers["X-WebSub-Duplicate"] == "true"
        assert receiver.count_inbox(subscription_id) == 1

    def test_dedup_is_scoped_per_subscription(self, clock: MutableClock) -> None:
        receiver = WebSubReceiver(clock=clock, max_body_bytes=4096)
        other_path = "/websub/callback/second-token"
        for path in (PATH, other_path):
            receiver.register_pending(TOPIC, path, SECRET, "subscribe", "ch")
            call(
                receiver,
                make_environ("GET", path, verification_query()),
            )
        for path in (PATH, other_path):
            status, _, _ = TestDelivery().post(receiver, path=path)
            assert status == "200 OK"
        assert receiver.count_inbox() == 2

    def test_missing_signature_rejected(self, receiver, clock) -> None:
        confirm_subscription(receiver, clock)
        environ = make_environ("POST", PATH, body=ATOM_FEED)
        status, _, _ = call(receiver, environ)
        assert status == "403 Forbidden"
        assert receiver.count_inbox() == 0

    def test_non_sha256_algorithm_rejected(self, receiver, clock) -> None:
        confirm_subscription(receiver, clock)
        digest = hmac.new(
            SECRET.encode(), ATOM_FEED, hashlib.sha1
        ).hexdigest()
        status, _, _ = self.post(
            receiver, signature_header=f"sha1={digest}"
        )
        assert status == "403 Forbidden"
        assert receiver.count_inbox() == 0

    def test_bad_signature_rejected(self, receiver, clock) -> None:
        confirm_subscription(receiver, clock)
        bad_hex = "00" * 32
        status, _, _ = self.post(
            receiver, signature_header=f"sha256={bad_hex}"
        )
        assert status == "403 Forbidden"
        assert receiver.count_inbox() == 0

    def test_malformed_signature_header_rejected(self, receiver, clock) -> None:
        confirm_subscription(receiver, clock)
        status, _, _ = self.post(receiver, signature_header="garbage")
        assert status == "403 Forbidden"

    def test_non_hex_signature_rejected(self, receiver, clock) -> None:
        confirm_subscription(receiver, clock)
        status, _, _ = self.post(
            receiver, signature_header="sha256=not-hex"
        )
        assert status == "403 Forbidden"

    def test_constant_time_comparison_used(
        self, receiver, clock, monkeypatch
    ) -> None:
        confirm_subscription(receiver, clock)
        calls: list[tuple[str, str]] = []
        original = receiver_module.hmac.compare_digest

        def spy(a: str, b: str) -> bool:
            calls.append((a, b))
            return original(a, b)

        monkeypatch.setattr(receiver_module.hmac, "compare_digest", spy)
        status, _, _ = self.post(receiver)
        assert status == "200 OK"
        assert calls

    def test_oversize_content_length_rejected_without_reading(
        self, receiver, clock
    ) -> None:
        confirm_subscription(receiver, clock)
        body = b"x" * (receiver._max_body_bytes + 1)
        class ExplodingStream(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError("stream must not be read")

        environ = make_environ(
            "POST",
            PATH,
            body=b"ignored",
            headers={"X-Hub-Signature": signature(body)},
        )
        environ["CONTENT_LENGTH"] = str(len(body))
        environ["wsgi.input"] = ExplodingStream()
        status, _, _ = call(receiver, environ)
        assert status == "413 Payload Too Large"
        assert receiver.count_inbox() == 0

    def test_oversize_stream_without_content_length_rejected(
        self, receiver, clock
    ) -> None:
        confirm_subscription(receiver, clock)
        body = b"x" * (receiver._max_body_bytes + 10)
        environ = make_environ(
            "POST",
            PATH,
            body=body,
            headers={"X-Hub-Signature": signature(body)},
        )
        environ["CONTENT_LENGTH"] = "0"
        status, _, _ = call(receiver, environ)
        assert status == "413 Payload Too Large"
        assert receiver.count_inbox() == 0

    def test_body_is_capped_before_signature_check(
        self, receiver, clock, monkeypatch
    ) -> None:
        confirm_subscription(receiver, clock)
        monkeypatch.setattr(
            receiver,
            "_valid_signature",
            lambda *args: pytest.fail("signature checked before cap"),
        )
        body = b"x" * (receiver._max_body_bytes + 1)
        environ = make_environ(
            "POST",
            PATH,
            body=body,
            headers={"X-Hub-Signature": signature(body)},
        )
        status, _, _ = call(receiver, environ)
        assert status == "413 Payload Too Large"

    def test_bozo_feed_rejected(self, receiver, clock) -> None:
        confirm_subscription(receiver, clock)
        bad_body = b"<not a feed at all>>>"
        status, _, _ = self.post(receiver, body=bad_body)
        assert status == "422 Unprocessable Entity"
        assert receiver.count_inbox() == 0

    def test_parse_exception_rejected(self, receiver, clock, monkeypatch) -> None:
        confirm_subscription(receiver, clock)
        monkeypatch.setattr(
            receiver_module.feedparser,
            "parse",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        status, _, _ = self.post(receiver)
        assert status == "422 Unprocessable Entity"
        assert receiver.count_inbox() == 0

    def test_feedparser_runs_after_signature_verification(
        self, receiver, clock, monkeypatch
    ) -> None:
        confirm_subscription(receiver, clock)
        monkeypatch.setattr(
            receiver,
            "_valid_signature",
            lambda header, secret, body: False,
        )
        monkeypatch.setattr(
            receiver_module.feedparser,
            "parse",
            lambda *args, **kwargs: pytest.fail("parsed before verification"),
        )
        status, _, _ = self.post(receiver)
        assert status == "403 Forbidden"

    def test_commit_failure_returns_500_and_retry_succeeds(
        self, clock: MutableClock
    ) -> None:
        class FlakyConnection(sqlite3.Connection):
            should_fail = False

            def commit(self) -> None:
                if FlakyConnection.should_fail and self.in_transaction:
                    FlakyConnection.should_fail = False
                    self.rollback()
                    raise sqlite3.OperationalError("disk full")
                super().commit()

        connection = sqlite3.connect(
            ":memory:",
            check_same_thread=False,
            factory=FlakyConnection,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        receiver = WebSubReceiver(
            ":memory:", clock=clock, connect=lambda _db: connection
        )
        confirm_subscription(receiver, clock)
        FlakyConnection.should_fail = True
        failed_status, _, _ = TestDelivery().post(receiver)
        assert failed_status == "500 Internal Server Error"
        assert receiver.count_inbox() == 0
        retry_status, _, _ = TestDelivery().post(receiver)
        assert retry_status == "200 OK"
        assert receiver.count_inbox() == 1

    def test_logs_exclude_secret_callback_and_body(
        self, receiver, clock, log_handler: ListLogHandler
    ) -> None:
        confirm_subscription(receiver, clock)
        self.post(receiver)
        rendered = [
            logging.Formatter().format(record)
            for record in log_handler.records
        ]
        for message in rendered:
            assert SECRET not in message
            assert PATH not in message
            assert PATH.rsplit("/", 1)[-1] not in message
            assert "article body" not in message

    def test_file_database_persists_across_connections(
        self, clock: MutableClock
    ) -> None:
        with tempfile.TemporaryDirectory(dir=".") as directory:
            database_path = f"{directory}/websub.sqlite3"
            first = WebSubReceiver(str(database_path), clock=clock)
            subscription_id = confirm_subscription(first, clock)
            status, _, _ = TestDelivery().post(first)
            assert status == "200 OK"

            second = WebSubReceiver(str(database_path), clock=clock)
            assert second.get_subscription(PATH).active
            assert second.count_inbox(subscription_id) == 1
