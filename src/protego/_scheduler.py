from __future__ import annotations

import itertools
import threading
from bisect import insort
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum, auto
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._protego import Protego

_ONE_DAY = timedelta(days=1)

# A reservation search never looks further than this into the future and
# never iterates more than this many times, so reserve() always terminates.
_MAX_SEARCH_SPAN = timedelta(days=370)
_MAX_ITERATIONS = 10000


class SchedulerError(Exception):
    """Base class for scheduler errors."""


class NoSlotAvailable(SchedulerError):
    """No legal send time exists; the message explains why."""


class UnknownTicket(SchedulerError, KeyError):
    """The ticket does not identify a known reservation."""


class TicketStateError(SchedulerError):
    """The ticket is not in the state required for the operation."""


class CommitTimeError(TicketStateError):
    """The commit did not happen at the scheduled ticket time."""


class Reservation(NamedTuple):
    """A ticket handed out by :meth:`Scheduler.reserve`."""

    ticket_id: int
    scheduled_at: datetime


class _State(Enum):
    PENDING = auto()
    COMMITTED = auto()
    CANCELLED = auto()
    EXPIRED = auto()


class _Ticket:
    __slots__ = ("scheduled_at", "state", "ticket_id")

    def __init__(self, ticket_id: int, scheduled_at: datetime) -> None:
        self.ticket_id = ticket_id
        self.scheduled_at = scheduled_at
        self.state = _State.PENDING


def _daily_window(
    start: time, end: time, day: date
) -> tuple[datetime, datetime] | None:
    """Return the half-open UTC window [start, end) for the given date.

    A period whose end is not after its start crosses midnight and closes
    the next day. An empty period (start == end) yields None.
    """
    if start == end:
        return None
    opens = datetime.combine(day, start, tzinfo=timezone.utc)
    closes = datetime.combine(day, end, tzinfo=timezone.utc)
    if end < start:
        closes += _ONE_DAY
    return opens, closes


class Scheduler:
    """Reserve send times that satisfy Crawl-delay, Request-rate and
    Visit-time on a single timeline.

    The scheduler performs no network I/O; it only keeps the timeline of
    reserved and committed send times for one (policy, user agent, origin)
    combination. All methods are synchronized, so a single instance can be
    shared between threads; no distributed storage is provided.
    """

    def __init__(
        self,
        policy: Protego,
        user_agent: str,
        origin: str = "",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._origin = origin
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )
        self._crawl_delay = policy.crawl_delay(user_agent)
        self._request_rate = policy.request_rate(user_agent)
        self._visit_time = policy.visit_time(user_agent)
        self._lock = threading.Lock()
        self._ticket_ids = itertools.count(1)
        self._tickets: dict[int, _Ticket] = {}
        # Sorted send times of pending and committed tickets.
        self._send_times: list[datetime] = []

    @property
    def user_agent(self) -> str:
        """Get the user agent this scheduler applies to."""
        return self._user_agent

    @property
    def origin(self) -> str:
        """Get the origin this scheduler applies to."""
        return self._origin

    def reserve(self, now: datetime | None = None) -> Reservation:
        """Return a ticket for the earliest legal send time at or after now.

        The returned time accounts for every ticket that is still pending or
        committed. Raises NoSlotAvailable when no legal send time exists.
        """
        with self._lock:
            moment = self._now(now)
            self._expire_stale(moment)
            scheduled_at = self._earliest_slot(moment)
            ticket_id = next(self._ticket_ids)
            self._tickets[ticket_id] = _Ticket(ticket_id, scheduled_at)
            insort(self._send_times, scheduled_at)
            return Reservation(ticket_id, scheduled_at)

    def cancel(self, ticket: int | Reservation) -> bool:
        """Cancel a pending ticket, freeing its capacity.

        Returns True when the ticket was pending and is now cancelled.
        Committed, expired and already cancelled tickets cannot be cancelled
        and return False, so repeated calls give a deterministic result.
        Cancelling never moves the send time of any other ticket.
        """
        with self._lock:
            t = self._ticket(ticket)
            if t.state is not _State.PENDING:
                return False
            t.state = _State.CANCELLED
            self._send_times.remove(t.scheduled_at)
            return True

    def commit(self, ticket: int | Reservation, at: datetime | None = None) -> datetime:
        """Confirm that the send happens exactly at the ticket time.

        A commit later than the scheduled time expires the ticket, freeing
        its capacity, and raises CommitTimeError; the send must be reserved
        again. A commit earlier than the scheduled time raises
        CommitTimeError and leaves the ticket pending.
        """
        with self._lock:
            t = self._ticket(ticket)
            moment = self._now(at)
            if t.state is not _State.PENDING:
                raise TicketStateError(
                    f"ticket {t.ticket_id} is {t.state.name.lower()}, not pending"
                )
            if moment > t.scheduled_at:
                t.state = _State.EXPIRED
                self._send_times.remove(t.scheduled_at)
                raise CommitTimeError(
                    f"ticket {t.ticket_id} was due at "
                    f"{t.scheduled_at.isoformat()}, reserve again"
                )
            if moment < t.scheduled_at:
                raise CommitTimeError(
                    f"ticket {t.ticket_id} is not due until "
                    f"{t.scheduled_at.isoformat()}"
                )
            t.state = _State.COMMITTED
            return t.scheduled_at

    def _now(self, moment: datetime | None) -> datetime:
        if moment is None:
            moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Scheduler needs a timezone-aware datetime")
        return moment.astimezone(timezone.utc)

    def _ticket(self, ticket: int | Reservation) -> _Ticket:
        ticket_id = ticket.ticket_id if isinstance(ticket, Reservation) else ticket
        try:
            return self._tickets[ticket_id]
        except KeyError:
            raise UnknownTicket(ticket_id) from None

    def _expire_stale(self, moment: datetime) -> None:
        """Expire pending tickets whose scheduled time has already passed."""
        for t in self._tickets.values():
            if t.state is _State.PENDING and t.scheduled_at < moment:
                t.state = _State.EXPIRED
                self._send_times.remove(t.scheduled_at)

    def _earliest_slot(self, moment: datetime) -> datetime:
        deadline = moment + _MAX_SEARCH_SPAN
        candidate = moment
        for _ in range(_MAX_ITERATIONS):
            nxt = self._apply_visit_time(candidate)
            nxt = self._apply_request_rate(nxt)
            nxt = self._apply_crawl_delay(nxt)
            if nxt == candidate:
                return candidate
            if nxt > deadline:
                break
            candidate = nxt
        raise NoSlotAvailable(
            f"no legal send time within {_MAX_SEARCH_SPAN.days} days of "
            f"{moment.isoformat()} for user agent {self._user_agent!r}"
        )

    def _apply_visit_time(self, candidate: datetime) -> datetime:
        vt = self._visit_time
        if vt is None:
            return candidate
        if vt.start_time == vt.end_time:
            raise NoSlotAvailable(
                f"visit-time {vt.start_time:%H%M}-{vt.end_time:%H%M} is an "
                "empty window, no legal send time exists"
            )
        day = candidate.date()
        for offset in (-1, 0, 1):
            window = _daily_window(vt.start_time, vt.end_time, day + offset * _ONE_DAY)
            if window is not None and window[0] <= candidate < window[1]:
                return candidate
        for offset in (0, 1, 2):
            window = _daily_window(vt.start_time, vt.end_time, day + offset * _ONE_DAY)
            if window is not None and candidate < window[0]:
                return window[0]
        raise NoSlotAvailable(  # unreachable for a non-empty daily window
            f"no upcoming visit-time window for user agent {self._user_agent!r}"
        )

    def _apply_request_rate(self, candidate: datetime) -> datetime:
        rr = self._request_rate
        if rr is None or not self._in_rate_period(candidate):
            return candidate
        window = timedelta(seconds=rr.seconds)
        since = candidate - window
        # A send exactly 'window' before the candidate has left the rolling
        # window; only sends in (candidate - window, candidate] count.
        recent = sorted(
            s
            for s in self._send_times
            if since < s <= candidate and self._in_rate_period(s)
        )
        if len(recent) < rr.requests:
            return candidate
        return recent[-rr.requests] + window

    def _apply_crawl_delay(self, candidate: datetime) -> datetime:
        delay = self._crawl_delay
        if not delay:
            return candidate
        spacing = timedelta(seconds=delay)
        for sent in self._send_times:
            if candidate - spacing < sent < candidate + spacing:
                candidate = max(candidate, sent + spacing)
        return candidate

    def _in_rate_period(self, moment: datetime) -> bool:
        rr = self._request_rate
        if rr is None or rr.start_time is None or rr.end_time is None:
            return True
        if rr.start_time == rr.end_time:
            # An empty period never applies the rate constraint.
            return False
        day = moment.date()
        for offset in (-1, 0):
            window = _daily_window(rr.start_time, rr.end_time, day + offset * _ONE_DAY)
            if window is not None and window[0] <= moment < window[1]:
                return True
        return False
