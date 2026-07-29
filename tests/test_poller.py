"""Calendar and poller tests.

The poller's job in Stage 1 is to survive 24 unattended hours, so most of these
are about failure: what happens when a feed breaks, recovers, breaks again, and
what happens overnight when the market is closed for sixteen hours straight.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from thepit.core import calendar
from thepit.core.calendar import SessionState
from thepit.core.clock import FixedClock
from thepit.core.types import FeedTier, FeedUnavailable, FetchRecord, Quote
from thepit.engine.poller import MarketView, Poller, PollerConfig
from thepit.store import db

ET = ZoneInfo("America/New_York")


def et_ms(y, mo, d, h, mi) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=ET).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "when,expected",
    [
        (et_ms(2026, 7, 29, 9, 29), SessionState.PRE),
        (et_ms(2026, 7, 29, 9, 30), SessionState.OPEN),
        (et_ms(2026, 7, 29, 15, 59), SessionState.OPEN),
        (et_ms(2026, 7, 29, 16, 0), SessionState.POST),
        (et_ms(2026, 7, 29, 20, 1), SessionState.CLOSED),
        (et_ms(2026, 7, 29, 3, 0), SessionState.CLOSED),
    ],
)
def test_session_boundaries(when, expected):
    assert calendar.state_at(when) is expected


def test_weekend_is_closed():
    assert calendar.state_at(et_ms(2026, 8, 1, 12, 0)) is SessionState.CLOSED  # Sat
    assert calendar.state_at(et_ms(2026, 8, 2, 12, 0)) is SessionState.CLOSED  # Sun


def test_holiday_is_closed():
    # 2026-07-03: Independence Day observed, since the 4th is a Saturday.
    assert not calendar.is_trading_day(datetime(2026, 7, 3).date())
    assert calendar.state_at(et_ms(2026, 7, 3, 12, 0)) is SessionState.CLOSED


def test_early_close_ends_the_day_outright():
    """A 1pm close has no post-market session after it. Getting this wrong means
    carrying an unintended overnight position."""
    xmas_eve = et_ms(2026, 12, 24, 13, 30)
    assert calendar.state_at(xmas_eve) is SessionState.CLOSED
    assert calendar.state_at(et_ms(2026, 12, 24, 12, 59)) is SessionState.OPEN


def test_minutes_to_close_respects_early_closes():
    assert calendar.minutes_to_close(et_ms(2026, 7, 29, 15, 30)) == 30
    assert calendar.minutes_to_close(et_ms(2026, 12, 24, 12, 30)) == 30
    assert calendar.minutes_to_close(et_ms(2026, 8, 1, 12, 0)) is None


def test_next_open_skips_weekends_and_holidays():
    # Friday evening -> Monday morning.
    nxt = calendar.next_open_ms(et_ms(2026, 7, 31, 18, 0))
    assert datetime.fromtimestamp(nxt / 1000, tz=ET).strftime("%a %H:%M") == "Mon 09:30"

    # The evening before a holiday skips it.
    nxt = calendar.next_open_ms(et_ms(2026, 7, 2, 18, 0))   # Thu, 3rd is a holiday
    assert datetime.fromtimestamp(nxt / 1000, tz=ET).date().day == 6  # Mon


def test_dst_transition_does_not_shift_the_open():
    """US DST ends 2026-11-01. The open stays 09:30 local on both sides."""
    for day in (10, 30):   # before and after
        ts = et_ms(2026, 11, day, 9, 30)
        if calendar.is_trading_day(datetime(2026, 11, day).date()):
            assert calendar.state_at(ts) is SessionState.OPEN


# ---------------------------------------------------------------------------
# MarketView
# ---------------------------------------------------------------------------


def test_market_view_detects_a_feed_that_stopped_updating():
    """A quote that stopped updating looks exactly like a quiet market until
    you ask this question."""
    v = MarketView()
    v.update({"AAPL": Quote("AAPL", 1_000, 10.0, "x", received_ms=1_000)})
    assert v.stale_symbols(now_ms=2_000, max_age_ms=5_000) == []
    assert v.stale_symbols(now_ms=9_000, max_age_ms=5_000) == ["AAPL"]


# ---------------------------------------------------------------------------
# Poller failure handling
# ---------------------------------------------------------------------------


class FakeFeed:
    """A price feed whose behaviour the test controls."""

    name = "fake"

    def __init__(self) -> None:
        self.mode = "ok"          # 'ok' | 'fail' | 'raise' | 'unavailable'
        self.calls = 0

    def tier(self) -> FeedTier:
        return FeedTier.QUOTES

    async def probe(self) -> None:
        if self.mode == "unavailable":
            raise FeedUnavailable("blocked")

    async def quotes(self, symbols):
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("transport exploded")
        rec = FetchRecord(
            ts_ms=1, source=self.name, kind="quote", endpoint="/q",
            symbols=tuple(symbols), ok=self.mode == "ok",
            http_status=200 if self.mode == "ok" else 500,
            error=None if self.mode == "ok" else "boom",
        )
        if self.mode != "ok":
            return {}, [rec]
        return (
            {s: Quote(s, 1_000, 10.0, self.name, received_ms=1_000) for s in symbols},
            [rec],
        )


@pytest.fixture
def poller(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    feed = FakeFeed()
    p = Poller(conn, FixedClock(1_800_000_000_000),
               PollerConfig(symbols=["AAPL"], degrade_after=3), price_feed=feed)
    yield p, feed, conn
    conn.close()


async def test_probe_records_unavailable_feed_without_raising(poller):
    """A feed being blocked is information for the dashboard, not a reason to
    refuse to boot. That was literally day one of this project."""
    p, feed, conn = poller
    feed.mode = "unavailable"

    result = await p.probe_all()

    assert result["fake"] == "blocked"
    assert p.health["fake"].degraded is True
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
    assert "feed_unavailable" in kinds


async def test_probe_emits_ok_for_healthy_feed(poller):
    p, _, conn = poller
    assert await p.probe_all() == {"fake": None}
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='feed_ok'"
    ).fetchone()[0] == 1


async def test_successful_cycle_persists_tick_and_fetch_log(poller):
    p, feed, conn = poller
    quotes, records = await feed.quotes(["AAPL"])
    p._ingest_quotes(quotes, records)

    assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0] == 1
    assert p.view.get("AAPL").last == 10.0


async def test_feed_degrades_only_after_repeated_failure(poller):
    """One failure is weather. Three is worth an event on the dashboard."""
    p, feed, conn = poller
    feed.mode = "fail"

    for _ in range(2):
        quotes, records = await feed.quotes(["AAPL"])
        p._ingest_quotes(quotes, records)
    assert p.health["fake"].degraded is False

    quotes, records = await feed.quotes(["AAPL"])
    p._ingest_quotes(quotes, records)
    assert p.health["fake"].degraded is True

    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='feed_degraded'"
    ).fetchone()[0] == 1


async def test_failed_fetches_are_still_recorded(poller):
    """A gap in fetch_log must mean 'did not try', never 'tried and broke'."""
    p, feed, conn = poller
    feed.mode = "fail"
    quotes, records = await feed.quotes(["AAPL"])
    p._ingest_quotes(quotes, records)
    assert conn.execute("SELECT COUNT(*) FROM fetch_log WHERE ok=0").fetchone()[0] == 1


async def test_recovery_emits_an_event(poller):
    """Without this, the uptime report cannot tell a resolved blip from an
    ongoing outage."""
    p, feed, conn = poller
    feed.mode = "fail"
    for _ in range(3):
        q, r = await feed.quotes(["AAPL"])
        p._ingest_quotes(q, r)
    assert p.health["fake"].degraded

    feed.mode = "ok"
    q, r = await feed.quotes(["AAPL"])
    p._ingest_quotes(q, r)

    assert p.health["fake"].degraded is False
    assert p.health["fake"].consecutive_failures == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='feed_recovered'"
    ).fetchone()[0] == 1


def test_backoff_grows_then_caps(poller):
    p, _, _ = poller
    h = p.health["fake"]
    assert h.backoff_s(300) == 0.0

    h.consecutive_failures = 3
    assert h.backoff_s(300) == 8.0

    h.consecutive_failures = 50
    assert h.backoff_s(300) == 256.0     # 2**8, under the cap
    assert h.backoff_s(100) == 100.0     # capped


async def test_transport_exception_does_not_kill_the_loop(poller):
    """The loop must survive anything a feed throws. 24h unattended is the
    whole requirement."""
    p, feed, conn = poller
    feed.mode = "raise"

    with pytest.raises(RuntimeError):
        await feed.quotes(["AAPL"])

    # The poller's own handler catches it and books a failure instead.
    p._note_failure("fake", "RuntimeError: transport exploded")
    assert p.health["fake"].consecutive_failures == 1
