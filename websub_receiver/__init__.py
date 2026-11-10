"""A dependency-light W3C WebSub receiver built on :mod:`feedparser`.

The package implements the *receiving* side of WebSub only: subscription
confirmation (``GET``) and content distribution (``POST``).  Discovery and
issuing subscription requests to a hub are deliberately out of scope.
"""

from .receiver import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_PENDING_TTL_SECONDS,
    InboxItem,
    Subscription,
    WebSubError,
    WebSubReceiver,
)

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_PENDING_TTL_SECONDS",
    "InboxItem",
    "Subscription",
    "WebSubError",
    "WebSubReceiver",
]
