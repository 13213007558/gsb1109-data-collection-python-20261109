import threading
from datetime import datetime, timedelta, timezone

import pytest

from protego import (
    CommitTimeError,
    NoSlotAvailable,
    Protego,
    Scheduler,
    TicketStateError,
    UnknownTicket,
)

UTC = timezone.utc


def dt(value):
    """Parse "YYYY-MM-DD HH:MM:SS" as a UTC datetime."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


class FakeClock:
    def __init__(self, now):
        self._now = now

    def __call__(self):
        return self._now

    def set(self, now):
        self._now = now

    def advance(self, **kwargs):
        self._now += timedelta(**kwargs)


def make_scheduler(content, user_agent='testbot', now=None):
    clock = FakeClock(now or dt('2026-11-10 12:00:00'))
    policy = Protego.parse(content)
    scheduler = Scheduler(policy, user_agent, 'https://example.com', clock=clock)
    return scheduler, clock


def _in_daily_period(moment, start, end):
    """Half-open UTC daily window check, crossing midnight if needed."""
    if start == end:
        return False
    for day in (moment.date(), moment.date() - timedelta(days=1)):
        opens = datetime.combine(day, start, tzinfo=UTC)
        closes = datetime.combine(day, end, tzinfo=UTC)
        if end < start:
            closes += timedelta(days=1)
        if opens <= moment < closes:
            return True
    return False


def check_constraints(policy, user_agent, times):
    """Check Crawl-delay, Request-rate and Visit-time on an actual send
    time sequence.
    """
    delay = policy.crawl_delay(user_agent)
    rate = policy.request_rate(user_agent)
    visit = policy.visit_time(user_agent)
    ordered = sorted(times)

    if delay:
        for earlier, later in zip(ordered, ordered[1:]):
            gap = (later - earlier).total_seconds()
            assert gap >= delay, f'adjacent sends {gap}s apart < crawl-delay {delay}'

    if rate:
        window = timedelta(seconds=rate.seconds)

        def in_period(moment):
            if rate.start_time is None:
                return True
            return _in_daily_period(moment, rate.start_time, rate.end_time)

        for send in ordered:
            if not in_period(send):
                continue
            count = sum(
                1 for other in ordered if send <= other < send + window and in_period(other)
            )
            assert count <= rate.requests, (
                f'{count} sends in rolling window starting at {send} '
                f'> request-rate {rate.requests}'
            )

    if visit:
        for send in ordered:
            assert _in_daily_period(send, visit.start_time, visit.end_time), (
                f'send at {send} outside visit-time '
                f'{visit.start_time:%H%M}-{visit.end_time:%H%M}'
            )


class TestReserve:
    def test_earliest_slots_and_unique_tickets(self):
        scheduler, _ = make_scheduler('User-agent: *\nCrawl-delay: 10')
        first = scheduler.reserve()
        second = scheduler.reserve()
        third = scheduler.reserve()
        assert first.scheduled_at == dt('2026-11-10 12:00:00')
        assert second.scheduled_at == dt('2026-11-10 12:00:10')
        assert third.scheduled_at == dt('2026-11-10 12:00:20')
        assert len({first.ticket_id, second.ticket_id, third.ticket_id}) == 3

    def test_no_constraints_returns_now(self):
        scheduler, _ = make_scheduler('User-agent: *\nDisallow: /private')
        assert scheduler.reserve().scheduled_at == dt('2026-11-10 12:00:00')

    def test_naive_now_rejected(self):
        scheduler, _ = make_scheduler('User-agent: *')
        with pytest.raises(ValueError, match='timezone-aware'):
            scheduler.reserve(datetime(2026, 11, 10, 12, 0, 0))

    def test_non_utc_now_normalized(self):
        scheduler, _ = make_scheduler('User-agent: *\nCrawl-delay: 5')
        east = timezone(timedelta(hours=8))
        now = datetime(2026, 11, 10, 20, 0, 0, tzinfo=east)
        reservation = scheduler.reserve(now)
        assert reservation.scheduled_at == dt('2026-11-10 12:00:00')

    def test_cross_midnight_visit_time(self):
        scheduler, _ = make_scheduler(
            'User-agent: *\nCrawl-delay: 10\nVisit-time: 2200-0200',
            now=dt('2026-11-10 23:59:45'),
        )
        times = [scheduler.reserve().scheduled_at for _ in range(4)]
        assert times == [
            dt('2026-11-10 23:59:45'),
            dt('2026-11-10 23:59:55'),
            dt('2026-11-11 00:00:05'),
            dt('2026-11-11 00:00:15'),
        ]

    def test_visit_window_end_is_exclusive(self):
        content = 'User-agent: *\nVisit-time: 0400-0845'
        scheduler, _ = make_scheduler(content, now=dt('2026-11-10 08:45:00'))
        assert scheduler.reserve().scheduled_at == dt('2026-11-11 04:00:00')

        scheduler, _ = make_scheduler(content, now=dt('2026-11-10 08:44:59'))
        assert scheduler.reserve().scheduled_at == dt('2026-11-10 08:44:59')

    def test_no_bunching_at_next_window_open(self):
        scheduler, _ = make_scheduler(
            'User-agent: *\nCrawl-delay: 30\nVisit-time: 2200-0200',
            now=dt('2026-11-10 03:00:00'),
        )
        times = [scheduler.reserve().scheduled_at for _ in range(3)]
        assert times == [
            dt('2026-11-10 22:00:00'),
            dt('2026-11-10 22:00:30'),
            dt('2026-11-10 22:01:00'),
        ]

    def test_request_rate_rolling_window_exact_expiry(self):
        scheduler, _ = make_scheduler('User-agent: *\nRequest-rate: 2/10s')
        times = [scheduler.reserve().scheduled_at for _ in range(4)]
        assert times == [
            dt('2026-11-10 12:00:00'),
            dt('2026-11-10 12:00:00'),
            # The first send leaves the rolling window exactly here.
            dt('2026-11-10 12:00:10'),
            dt('2026-11-10 12:00:10'),
        ]

    def test_request_rate_period_only_applies_inside(self):
        content = 'User-agent: *\nRequest-rate: 1/60s 0900-1000'
        scheduler, _ = make_scheduler(content, now=dt('2026-11-10 08:00:00'))
        first = scheduler.reserve()
        second = scheduler.reserve()
        assert first.scheduled_at == dt('2026-11-10 08:00:00')
        assert second.scheduled_at == dt('2026-11-10 08:00:00')

        scheduler, _ = make_scheduler(content, now=dt('2026-11-10 09:00:00'))
        times = [scheduler.reserve().scheduled_at for _ in range(3)]
        assert times == [
            dt('2026-11-10 09:00:00'),
            dt('2026-11-10 09:01:00'),
            dt('2026-11-10 09:02:00'),
        ]

    def test_empty_visit_time_reports_reason(self):
        scheduler, _ = make_scheduler('User-agent: *\nVisit-time: 0400-0400')
        with pytest.raises(NoSlotAvailable, match='empty window'):
            scheduler.reserve()

    def test_impossible_spacing_reports_reason(self):
        scheduler, _ = make_scheduler('User-agent: *\nCrawl-delay: 99999999')
        scheduler.reserve()
        with pytest.raises(NoSlotAvailable, match='no legal send time'):
            scheduler.reserve()


class TestCancel:
    def test_cancel_middle_ticket_frees_the_gap(self):
        scheduler, _ = make_scheduler('User-agent: *\nCrawl-delay: 10')
        first = scheduler.reserve()
        middle = scheduler.reserve()
        last = scheduler.reserve()
        assert scheduler.cancel(middle) is True
        # The freed slot is reused, other reservations do not move.
        refill = scheduler.reserve()
        assert refill.scheduled_at == middle.scheduled_at == dt('2026-11-10 12:00:10')
        assert first.scheduled_at == dt('2026-11-10 12:00:00')
        assert last.scheduled_at == dt('2026-11-10 12:00:20')

    def test_repeated_cancel_is_deterministic(self):
        scheduler, _ = make_scheduler('User-agent: *\nCrawl-delay: 10')
        reservation = scheduler.reserve()
        assert scheduler.cancel(reservation) is True
        assert scheduler.cancel(reservation) is False
        assert scheduler.cancel(reservation.ticket_id) is False

    def test_committed_ticket_cannot_be_cancelled(self):
        scheduler, clock = make_scheduler('User-agent: *\nCrawl-delay: 10')
        reservation = scheduler.reserve()
        clock.set(reservation.scheduled_at)
        scheduler.commit(reservation)
        assert scheduler.cancel(reservation) is False

    def test_unknown_ticket(self):
        scheduler, _ = make_scheduler('User-agent: *')
        with pytest.raises(UnknownTicket):
            scheduler.cancel(999)
        with pytest.raises(UnknownTicket):
            scheduler.commit(999)


class TestCommit:
    def test_commit_at_scheduled_time(self):
        scheduler, clock = make_scheduler('User-agent: *\nCrawl-delay: 10')
        reservation = scheduler.reserve()
        clock.set(reservation.scheduled_at)
        assert scheduler.commit(reservation) == reservation.scheduled_at
        with pytest.raises(TicketStateError, match='committed'):
            scheduler.commit(reservation)

    def test_early_commit_leaves_ticket_pending(self):
        scheduler, _ = make_scheduler('User-agent: *\nCrawl-delay: 10')
        scheduler.reserve()
        late = scheduler.reserve()
        with pytest.raises(CommitTimeError, match='not due'):
            scheduler.commit(late, at=dt('2026-11-10 12:00:00'))
        assert scheduler.commit(late, at=late.scheduled_at) == late.scheduled_at

    def test_late_commit_expires_and_preserves_spacing(self):
        scheduler, clock = make_scheduler('User-agent: *\nCrawl-delay: 10')
        first = scheduler.reserve()
        second = scheduler.reserve()
        clock.advance(seconds=5)
        with pytest.raises(CommitTimeError, match='reserve again'):
            scheduler.commit(first)
        # The expired ticket freed its slot, but the promised spacing to the
        # remaining ticket still holds, and that ticket did not move.
        rebooked = scheduler.reserve()
        assert rebooked.scheduled_at == dt('2026-11-10 12:00:20')
        assert second.scheduled_at == dt('2026-11-10 12:00:10')
        with pytest.raises(TicketStateError, match='expired'):
            scheduler.commit(first)

    def test_stale_ticket_expires_on_next_reserve(self):
        scheduler, clock = make_scheduler('User-agent: *\nCrawl-delay: 10')
        stale = scheduler.reserve()
        clock.advance(seconds=30)
        fresh = scheduler.reserve()
        assert fresh.scheduled_at == dt('2026-11-10 12:00:30')
        assert scheduler.cancel(stale) is False


class TestConcurrency:
    def test_concurrent_reserve_is_synchronized(self):
        scheduler, _ = make_scheduler('User-agent: *\nCrawl-delay: 1')
        barrier = threading.Barrier(8)
        reservations = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            reservation = scheduler.reserve()
            with lock:
                reservations.append(reservation)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len({r.ticket_id for r in reservations}) == 8
        assert sorted(r.scheduled_at for r in reservations) == [
            dt('2026-11-10 12:00:00') + timedelta(seconds=i) for i in range(8)
        ]


class TestSendSequence:
    @pytest.mark.parametrize(
        ('content', 'start'),
        [
            (
                'User-agent: *\nCrawl-delay: 10\nVisit-time: 2200-0200',
                dt('2026-11-10 21:59:30'),
            ),
            (
                'User-agent: *\nCrawl-delay: 4\nRequest-rate: 3/30s',
                dt('2026-11-10 12:00:00'),
            ),
            (
                'User-agent: *\nCrawl-delay: 5\nRequest-rate: 2/20s 0900-1300\n'
                'Visit-time: 0800-1400',
                dt('2026-11-10 08:59:50'),
            ),
        ],
    )
    def test_committed_sequence_satisfies_all_constraints(self, content, start):
        scheduler, clock = make_scheduler(content, now=start)
        policy = Protego.parse(content)
        sent = []
        for _ in range(10):
            reservation = scheduler.reserve()
            clock.set(reservation.scheduled_at)
            sent.append(scheduler.commit(reservation))
        check_constraints(policy, 'testbot', sent)

    def test_mixed_lifecycle_sequence(self):
        content = (
            'User-agent: *\nCrawl-delay: 10\nRequest-rate: 2/60s\n'
            'Visit-time: 2200-0200'
        )
        scheduler, clock = make_scheduler(content, now=dt('2026-11-10 21:59:50'))
        policy = Protego.parse(content)
        sent = []
        first = scheduler.reserve()
        second = scheduler.reserve()
        assert scheduler.cancel(second) is True
        clock.set(first.scheduled_at)
        sent.append(scheduler.commit(first))
        for _ in range(5):
            reservation = scheduler.reserve()
            clock.set(reservation.scheduled_at)
            sent.append(scheduler.commit(reservation))
        check_constraints(policy, 'testbot', sent)
