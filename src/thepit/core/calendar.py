"""US equity market sessions.

The poller needs "the market is closed" to be a *state*, not an error condition
-- closed is most of any 24-hour window, and a system that logs an error every
few seconds overnight has made its own logs useless.

Later stages need this to be genuinely correct rather than approximately so:
``flatten_before_close_minutes`` fired against a wrong early-close time means
carrying an unintended overnight position. Session/calendar handling is the
classic under-budgeted item in trading systems and it is not a footnote.

**Holiday data below is transcribed from the NYSE schedule and should be
verified against the exchange before any live trading.** It is deliberately a
plain table rather than a rule engine: "third Monday in January" style rules
break on the observance edge cases (a holiday falling on a Saturday is observed
the preceding Friday) and a table is auditable at a glance.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Pre/post market. Alpaca serves data across these; spreads are far wider and
# fills are correspondingly worse, which is why they are a separate state
# rather than folded into "open".
PREMARKET_OPEN = time(4, 0)
POSTMARKET_CLOSE = time(20, 0)

# NYSE full-day closures. Verify before live use.
HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; the 4th is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),
    date(2027, 6, 18),   # Juneteenth observed (the 19th is a Saturday)
    date(2027, 7, 5),    # Independence Day observed (the 4th is a Sunday)
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),  # Christmas observed (the 25th is a Saturday)
})

# 1:00pm ET closes.
EARLY_CLOSES: frozenset[date] = frozenset({
    date(2026, 11, 27),  # day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2027, 11, 26),
})


class SessionState(StrEnum):
    CLOSED = "closed"
    PRE = "pre"
    OPEN = "open"
    POST = "post"


def _et(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).astimezone(ET)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def close_time(d: date) -> time:
    return EARLY_CLOSE if d in EARLY_CLOSES else REGULAR_CLOSE


def state_at(ts_ms: int) -> SessionState:
    """Which session, if any, is running at this instant."""
    now = _et(ts_ms)
    d, t = now.date(), now.time()

    if not is_trading_day(d):
        return SessionState.CLOSED
    if t < PREMARKET_OPEN:
        return SessionState.CLOSED

    closes_at = close_time(d)
    if t < REGULAR_OPEN:
        return SessionState.PRE
    if t < closes_at:
        return SessionState.OPEN
    # An early close ends the day outright: there is no post-market session
    # after a 1pm close.
    if d in EARLY_CLOSES:
        return SessionState.CLOSED
    if t < POSTMARKET_CLOSE:
        return SessionState.POST
    return SessionState.CLOSED


def is_open(ts_ms: int) -> bool:
    """Regular hours only. Deliberately excludes pre and post."""
    return state_at(ts_ms) is SessionState.OPEN


def minutes_to_close(ts_ms: int) -> int | None:
    """Minutes until the regular close, or None when not in regular hours."""
    if not is_open(ts_ms):
        return None
    now = _et(ts_ms)
    closing = datetime.combine(now.date(), close_time(now.date()), tzinfo=ET)
    return max(0, int((closing - now).total_seconds() // 60))


def next_open_ms(ts_ms: int) -> int:
    """When regular trading next begins. Used to schedule the poller's sleep
    rather than spinning through a closed weekend at full cadence."""
    now = _et(ts_ms)
    d = now.date()

    if is_trading_day(d) and now.time() < REGULAR_OPEN:
        candidate = datetime.combine(d, REGULAR_OPEN, tzinfo=ET)
        return int(candidate.timestamp() * 1000)

    for offset in range(1, 12):
        nxt = d + timedelta(days=offset)
        if is_trading_day(nxt):
            candidate = datetime.combine(nxt, REGULAR_OPEN, tzinfo=ET)
            return int(candidate.timestamp() * 1000)
    raise RuntimeError(  # pragma: no cover - would mean 12 straight holidays
        "no trading day within 12 days; holiday table is probably wrong"
    )
