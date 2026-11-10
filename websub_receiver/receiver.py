"""W3C WebSub receiving endpoint backed by :mod:`sqlite3` and :mod:`feedparser`.

The receiver is a plain WSGI callable.  It does not depend on a web framework
and never talks to a hub itself; the caller registers pending subscription
operations out of band and the hub drives ``GET`` verification and ``POST``
content delivery.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import feedparser
from feedparser.util import FeedParserDict

logger = logging.getLogger(__name__)

DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024
DEFAULT_PENDING_TTL_SECONDS = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS websub_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    callback_path TEXT NOT NULL UNIQUE,
    secret BLOB NOT NULL,
    lease_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS websub_pending_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL UNIQUE
        REFERENCES websub_subscriptions(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('subscribe', 'unsubscribe')),
    topic TEXT NOT NULL,
    secret BLOB NOT NULL,
    challenge TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS websub_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL
        REFERENCES websub_subscriptions(id) ON DELETE CASCADE,
    body_sha256 TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    received_at REAL NOT NULL,
    UNIQUE (subscription_id, body_sha256)
);
"""

_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


class WebSubError(Exception):
    """Base class for receiver usage errors."""


@dataclass(frozen=True)
class Subscription:
    """Public view of a subscription. The shared secret is never exposed."""

    id: int
    topic: str
    callback_path: str
    lease_expires_at: float | None

    @property
    def active(self) -> bool:
        return self.lease_expires_at is not None


@dataclass(frozen=True)
class InboxItem:
    """One stored delivery, including the parsed feed payload."""

    id: int
    subscription_id: int
    body_sha256: str
    parsed: dict[str, Any]
    received_at: float


def _json_default(obj: Any) -> Any:
    if isinstance(obj, bytes | bytearray):
        return {"__type__": "bytes", "hex": bytes(obj).hex()}
    if isinstance(obj, set):
        return sorted(obj, key=repr)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, FeedParserDict):
        return dict(obj)
    return repr(obj)


def _to_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_json_default, ensure_ascii=False))


def _default_connect(database: str) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class WebSubReceiver:
    """WSGI application implementing the WebSub subscriber receiving protocol.

    Args:
        database: A sqlite database path, or ``":memory:"`` for tests.
        max_body_bytes: Hard cap on how many delivery bytes are ever read.
        pending_ttl_seconds: How long a registered intent stays confirmable.
        clock: Zero-argument callable returning the current epoch time. It is
            injected so tests can fast-forward leases without real waiting.
        connect: Optional factory ``(database) -> sqlite3.Connection``. It is
            invoked once per request, which makes storage failures injectable.
    """

    def __init__(
        self,
        database: str = ":memory:",
        *,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        pending_ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        connect: Callable[[str], sqlite3.Connection] | None = None,
    ) -> None:
        if max_body_bytes <= 0:
            raise WebSubError("max_body_bytes must be positive")
        if pending_ttl_seconds <= 0:
            raise WebSubError("pending_ttl_seconds must be positive")
        self._database = database
        self._max_body_bytes = max_body_bytes
        self._pending_ttl = pending_ttl_seconds
        self._clock = clock
        self._shared_connection: sqlite3.Connection | None = None

        if connect is not None and database == ":memory:":
            shared = connect(":memory:")
            self._shared_connection = shared
            self._connect = lambda _db: shared
        elif connect is not None:
            self._connect = connect
        elif database == ":memory:":
            shared = sqlite3.connect(":memory:", check_same_thread=False)
            shared.execute("PRAGMA foreign_keys = ON")
            self._shared_connection = shared
            self._connect = lambda _db: shared
        else:
            self._connect = _default_connect

        with self._database_connection() as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    @contextlib.contextmanager
    def _database_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(self._database)
        try:
            connection.executescript(_SCHEMA)
            yield connection
        finally:
            if connection is not self._shared_connection:
                connection.close()

    def register_pending(
        self,
        topic: str,
        callback_path: str,
        secret: str | bytes,
        mode: str,
        challenge: str,
    ) -> int:
        """Register a pending subscribe/unsubscribe intent for the caller.

        Returns the subscription id. The existing lease (if any) is left
        untouched until the hub confirms the intent.
        """
        if not isinstance(topic, str) or not topic:
            raise WebSubError("topic must be a non-empty string")
        if not isinstance(callback_path, str) or not callback_path.startswith("/"):
            raise WebSubError("callback_path must start with '/'")
        if mode not in ("subscribe", "unsubscribe"):
            raise WebSubError("mode must be 'subscribe' or 'unsubscribe'")
        if not isinstance(challenge, str) or not challenge:
            raise WebSubError("challenge must be a non-empty string")
        secret_bytes = self._coerce_secret(secret)
        now = self._clock()

        with self._database_connection() as connection:
            connection.execute(
                """
                INSERT INTO websub_subscriptions
                    (topic, callback_path, secret, lease_expires_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?)
                ON CONFLICT(callback_path) DO UPDATE SET
                    topic = excluded.topic,
                    updated_at = excluded.updated_at
                """,
                (topic, callback_path, secret_bytes, now, now),
            )
            subscription_id = connection.execute(
                "SELECT id FROM websub_subscriptions WHERE callback_path = ?",
                (callback_path,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO websub_pending_intents
                    (subscription_id, mode, topic, secret, challenge,
                     expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                    mode = excluded.mode,
                    topic = excluded.topic,
                    secret = excluded.secret,
                    challenge = excluded.challenge,
                    expires_at = excluded.expires_at
                """,
                (
                    subscription_id,
                    mode,
                    topic,
                    secret_bytes,
                    challenge,
                    now + self._pending_ttl,
                ),
            )
            connection.commit()
        logger.info(
            "pending intent registered subscription_id=%s mode=%s",
            subscription_id,
            mode,
        )
        return subscription_id

    @staticmethod
    def _coerce_secret(secret: str | bytes) -> bytes:
        if isinstance(secret, str):
            return secret.encode("utf-8")
        if isinstance(secret, bytes):
            return secret
        raise WebSubError("secret must be str or bytes")

    def get_subscription(self, callback_path: str) -> Subscription | None:
        """Return the subscription registered for ``callback_path``."""
        with self._database_connection() as connection:
            row = connection.execute(
                """
                SELECT id, topic, callback_path, lease_expires_at
                FROM websub_subscriptions
                WHERE callback_path = ?
                """,
                (callback_path,),
            ).fetchone()
        return self._row_to_subscription(row) if row is not None else None

    def count_inbox(self, subscription_id: int | None = None) -> int:
        """Count stored deliveries, optionally for one subscription."""
        with self._database_connection() as connection:
            if subscription_id is None:
                row = connection.execute(
                    "SELECT COUNT(*) FROM websub_inbox"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM websub_inbox WHERE subscription_id = ?",
                    (subscription_id,),
                ).fetchone()
        return int(row[0])

    def list_inbox(self, subscription_id: int | None = None) -> list[InboxItem]:
        """Return stored deliveries in arrival order."""
        with self._database_connection() as connection:
            if subscription_id is None:
                rows = connection.execute(
                    """
                    SELECT id, subscription_id, body_sha256, parsed_json,
                           received_at
                    FROM websub_inbox
                    ORDER BY id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, subscription_id, body_sha256, parsed_json,
                           received_at
                    FROM websub_inbox
                    WHERE subscription_id = ?
                    ORDER BY id
                    """,
                    (subscription_id,),
                ).fetchall()
        return [
            InboxItem(
                id=row[0],
                subscription_id=row[1],
                body_sha256=row[2],
                parsed=json.loads(row[3]),
                received_at=row[4],
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_subscription(row: sqlite3.Row | tuple[Any, ...]) -> Subscription:
        return Subscription(
            id=row[0],
            topic=row[1],
            callback_path=row[2],
            lease_expires_at=row[3],
        )

    # ------------------------------------------------------------------
    # WSGI plumbing
    # ------------------------------------------------------------------

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        try:
            method = environ.get("REQUEST_METHOD", "GET").upper()
            if method == "GET":
                status, body, extra_headers = self._handle_get(environ)
            elif method == "POST":
                status, body, extra_headers = self._handle_post(environ)
            else:
                status, body, extra_headers = (
                    "405 Method Not Allowed",
                    b"",
                    [("Allow", "GET, POST")],
                )
        except Exception:
            logger.exception("unhandled receiver error")
            status, body, extra_headers = "500 Internal Server Error", b"", []

        headers = [
            ("Content-Type", "application/octet-stream"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Length", str(len(body))),
        ]
        headers.extend(extra_headers)
        start_response(status, headers)
        return [body]

    @staticmethod
    def _single_query_value(
        query: dict[str, list[str]], key: str
    ) -> str | None:
        values = query.get(key)
        if not values:
            return None
        value = values[0]
        return value if value != "" else None

    def _handle_get(
        self, environ: dict[str, Any]
    ) -> tuple[str, bytes, list[tuple[str, str]]]:
        query = parse_qs(
            environ.get("QUERY_STRING", ""),
            keep_blank_values=True,
            strict_parsing=False,
        )
        mode = self._single_query_value(query, "hub.mode")
        topic = self._single_query_value(query, "hub.topic")
        challenge = self._single_query_value(query, "hub.challenge")
        lease_seconds_raw = self._single_query_value(query, "hub.lease_seconds")

        if mode not in ("subscribe", "unsubscribe") or not topic or not challenge:
            logger.info("verification rejected reason=malformed_query mode=%s", mode)
            return "404 Not Found", b"", []

        lease_seconds: int | None = None
        if mode == "subscribe":
            if lease_seconds_raw is None:
                logger.info(
                    "verification rejected reason=missing_lease_seconds"
                )
                return "404 Not Found", b"", []
            try:
                lease_seconds = int(lease_seconds_raw)
            except (TypeError, ValueError):
                logger.info(
                    "verification rejected reason=invalid_lease_seconds"
                )
                return "404 Not Found", b"", []
            if lease_seconds <= 0:
                logger.info(
                    "verification rejected reason=invalid_lease_seconds"
                )
                return "404 Not Found", b"", []

        path = environ.get("PATH_INFO", "")
        now = self._clock()
        try:
            with self._database_connection() as connection:
                row = connection.execute(
                    """
                    SELECT s.id, s.topic, s.secret, p.mode, p.topic, p.secret,
                           p.challenge, p.expires_at
                    FROM websub_pending_intents AS p
                    JOIN websub_subscriptions AS s ON s.id = p.subscription_id
                    WHERE s.callback_path = ? AND p.expires_at > ?
                    """,
                    (path, now),
                ).fetchone()
                if row is None:
                    logger.info(
                        "verification rejected reason=no_pending_intent"
                    )
                    return "404 Not Found", b"", []

                (
                    subscription_id,
                    subscription_topic,
                    _subscription_secret,
                    pending_mode,
                    pending_topic,
                    pending_secret,
                    pending_challenge,
                    _pending_expires,
                ) = row
                if (
                    pending_mode != mode
                    or pending_topic != topic
                    or subscription_topic != topic
                    or pending_challenge != challenge
                ):
                    logger.info(
                        "verification rejected reason=intent_mismatch "
                        "subscription_id=%s mode=%s",
                        subscription_id,
                        mode,
                    )
                    return "404 Not Found", b"", []

                if mode == "subscribe":
                    connection.execute(
                        """
                        UPDATE websub_subscriptions
                        SET secret = ?, lease_expires_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            pending_secret,
                            now + lease_seconds,
                            now,
                            subscription_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE websub_subscriptions
                        SET lease_expires_at = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, subscription_id),
                    )
                connection.execute(
                    "DELETE FROM websub_pending_intents WHERE subscription_id = ?",
                    (subscription_id,),
                )
                connection.commit()
        except sqlite3.Error:
            logger.exception("verification storage failed mode=%s", mode)
            return "500 Internal Server Error", b"", []

        logger.info(
            "verification confirmed subscription_id=%s mode=%s",
            subscription_id,
            mode,
        )
        return "200 OK", challenge.encode("utf-8"), []

    def _handle_post(
        self, environ: dict[str, Any]
    ) -> tuple[str, bytes, list[tuple[str, str]]]:
        path = environ.get("PATH_INFO", "")
        now = self._clock()
        try:
            with self._database_connection() as connection:
                subscription_row = connection.execute(
                    """
                    SELECT id, secret, lease_expires_at
                    FROM websub_subscriptions
                    WHERE callback_path = ? AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > ?
                    """,
                    (path, now),
                ).fetchone()
                if subscription_row is None:
                    logger.info("delivery rejected reason=no_active_lease")
                    return "403 Forbidden", b"", []
                subscription_id, secret, _lease_expires_at = subscription_row

                body, rejected = self._read_limited_body(environ)
                if rejected:
                    logger.info(
                        "delivery rejected reason=body_too_large "
                        "subscription_id=%s",
                        subscription_id,
                    )
                    return "413 Payload Too Large", b"", []

                signature_header = environ.get("HTTP_X_HUB_SIGNATURE", "")
                if not self._valid_signature(signature_header, secret, body):
                    logger.info(
                        "delivery rejected reason=bad_signature "
                        "subscription_id=%s",
                        subscription_id,
                    )
                    return "403 Forbidden", b"", []

                try:
                    parsed = feedparser.parse(body)
                except Exception:
                    logger.warning(
                        "delivery rejected reason=parse_exception "
                        "subscription_id=%s",
                        subscription_id,
                        exc_info=True,
                    )
                    return "422 Unprocessable Entity", b"", []

                if getattr(parsed, "bozo", False):
                    logger.info(
                        "delivery rejected reason=bozo subscription_id=%s",
                        subscription_id,
                    )
                    return "422 Unprocessable Entity", b"", []

                body_digest = hashlib.sha256(body).hexdigest()
                parsed_json = json.dumps(
                    _to_jsonable(parsed), ensure_ascii=False
                )
                try:
                    connection.execute(
                        """
                        INSERT INTO websub_inbox
                            (subscription_id, body_sha256, parsed_json,
                             received_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (subscription_id, body_digest, parsed_json, now),
                    )
                    connection.commit()
                except sqlite3.IntegrityError:
                    connection.rollback()
                    logger.info(
                        "delivery accepted as duplicate subscription_id=%s",
                        subscription_id,
                    )
                    return (
                        "200 OK",
                        b"",
                        [("X-WebSub-Duplicate", "true")],
                    )
                except sqlite3.Error:
                    connection.rollback()
                    logger.exception(
                        "delivery storage failed subscription_id=%s",
                        subscription_id,
                    )
                    return "500 Internal Server Error", b"", []
        except sqlite3.Error:
            logger.exception("delivery database error")
            return "500 Internal Server Error", b"", []

        logger.info(
            "delivery stored subscription_id=%s bytes=%s",
            subscription_id,
            len(body),
        )
        return "200 OK", b"", []

    def _read_limited_body(
        self, environ: dict[str, Any]
    ) -> tuple[bytes, bool]:
        """Read at most ``max_body_bytes + 1`` bytes from the WSGI stream.

        One extra byte is read so an oversized request is detected even when
        no ``CONTENT_LENGTH`` is supplied.
        """
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0) or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length > self._max_body_bytes:
            return b"", True

        stream = environ["wsgi.input"]
        remaining = self._max_body_bytes + 1
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = stream.read(min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > self._max_body_bytes:
            return b"", True
        return body, False

    def _valid_signature(
        self, signature_header: str, secret: bytes, body: bytes
    ) -> bool:
        if not signature_header or "=" not in signature_header:
            return False
        algorithm, _, provided_hex = signature_header.partition("=")
        if algorithm != "sha256":
            logger.info("signature rejected reason=unsupported_algorithm")
            return False
        if not _HEX_SHA256.fullmatch(provided_hex):
            return False
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided_hex.lower())
