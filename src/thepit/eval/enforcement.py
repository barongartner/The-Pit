"""Did the levels actually hold, and how late were they?

CLAUDE-READ-THIS.md and NOTES.md both claim that an enforced stop is not a venue
stop -- late by up to one interval plus feed latency, filled on a price that
already printed. This module is the only thing that puts a number on that claim,
which is the reason it exists: a documented limitation with no measurement behind
it is a sentence, not a finding.

Two numbers, kept separate on purpose:

**Slippage past the level**, decomposed. `detect_bp` is how far the tape had
already gone before the loop saw it; `model_bp` is what the fill model charged.
Only the second gets better with a real bid/ask (issue #11), so pooling them into
one figure would hide which half is fixable. Signed, never absolute: a gap through
a target in your favour must show as negative.

**Lateness in wall-clock time**, measured against the recorded tape rather than
against the loop's own timestamps. The loop submits inside the same step it
detects a breach, so its own clock says zero every time -- the honest question is
when the tape first printed through the level.

Both are only valid for non-trailing plans. `exit_plans` is keyed by symbol and
upserted, so a trailed plan's stored `stop_price` is the final trailed value, not
the level in force when it fired.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# The age at which both `fastloop._usable_price` and `book.check` refuse to act.
# A gap longer than this is time the loop could not have enforced anything.
MAX_QUOTE_AGE_S = 120.0


@dataclass(frozen=True, slots=True)
class LevelFill:
    session_id: int
    symbol: str
    kind: str            # 'stop' | 'target' | 'time_stop'
    level: float
    ref_price: float     # the quote the fill was priced from
    fill_price: float
    detect_bp: float     # tape past the level before the loop saw it
    model_bp: float      # what the fill model charged
    total_bp: float      # the whole miss, signed
    late_ms: int | None  # fired_ms minus the tape's first breach
    trailed: bool


@dataclass(frozen=True, slots=True)
class ArmedOutcome:
    triggered: int
    expired: int
    cancelled: int
    waiting: int
    distances_bp: tuple[float, ...]     # how far the level was when armed
    hit_rate: float | None              # triggered / (triggered + expired)


@dataclass(frozen=True, slots=True)
class Blindness:
    """Time the fast loop could not have enforced anything, per symbol."""

    symbol: str
    blind_s: float
    window_s: float

    @property
    def blind_pct(self) -> float:
        return self.blind_s / self.window_s * 100 if self.window_s else 0.0


def level_fills(conn: sqlite3.Connection, session_id: int) -> list[LevelFill]:
    """Every fired level, with how far past it the fill landed.

    Measured against `exit_plan_events`, not against `exit_plans`. The plans table
    is keyed by symbol and upserted, so a session that stopped out and re-entered
    had both fires compared to the last stop it ever held -- which reported a
    112-second lateness for a loop that acted inside one second.
    """
    # Paired in sequence per symbol rather than by timestamp. `_fire` writes its
    # event only after the closing order fills, so the k-th fire for a symbol is
    # the k-th fast-loop close for that symbol -- and matching on time instead
    # picks the wrong row whenever a fire and the next entry's attach land in the
    # same millisecond, which is most of the time under a test clock and possible
    # under a real one.
    fires: dict[str, list] = {}
    for e in conn.execute(
        "SELECT * FROM exit_plan_events WHERE session_id=? AND kind='fired' "
        "ORDER BY ts_ms, id", (session_id,)
    ):
        fires.setdefault(e["symbol"], []).append(e)

    closes: dict[str, list] = {}
    for o in conn.execute(
        "SELECT o.id, o.symbol, o.origin, f.ts_ms, f.ref_price, f.price "
        "FROM orders o JOIN fills f ON f.order_id=o.id "
        "WHERE o.session_id=? AND o.status='filled' AND o.origin LIKE 'fast_loop_%' "
        "ORDER BY f.ts_ms, o.id", (session_id,)
    ):
        closes.setdefault(o["symbol"], []).append(o)

    out: list[LevelFill] = []
    for symbol, events in fires.items():
        for event, o in zip(events, closes.get(symbol, []), strict=False):
            kind = (o["origin"] or "").removeprefix("fast_loop_")
            level = event["target_price"] if kind == "target" else event["stop_price"]
            if not level:
                continue
            long = bool(event["long"])
            # Signed so that "past the level against me" is positive and a gap in
            # my favour is negative. abs() here would report every gap as a cost.
            sign = 1 if long else -1
            detect = (level - o["ref_price"]) * sign / level * 10_000
            model = (o["ref_price"] - o["price"]) * sign / level * 10_000
            trailed = event["trail_bp"] is not None
            out.append(LevelFill(
                session_id=session_id, symbol=symbol, kind=kind or "unknown",
                level=level, ref_price=o["ref_price"], fill_price=o["price"],
                detect_bp=detect, model_bp=model, total_bp=detect + model,
                # A time stop is not a price event, and a trailed level cannot be
                # dated: the ratchet moved it while the tape moved too.
                late_ms=None if (trailed or kind == "time_stop") else _lateness(
                    conn, symbol, level,
                    # Which side of the level counts as a breach. A long's stop
                    # is below it and its target is above; using one direction for
                    # both searched the wrong half of the tape and reported tens
                    # of seconds of lateness for a target hit immediately.
                    below=long if kind == "stop" else not long,
                    since_ms=_level_set_at(conn, session_id, symbol, event["id"]),
                    until_ms=o["ts_ms"]),
                trailed=trailed,
            ))
    return sorted(out, key=lambda f: (f.symbol, f.level))


def _level_set_at(
    conn: sqlite3.Connection, session_id: int, symbol: str, fired_event_id: int
) -> int:
    """When the level that fired was set, as the lower bound for lateness.

    Without it the search runs back to the start of the session and finds a tick
    that breached a level which did not exist yet.
    """
    row = conn.execute(
        "SELECT ts_ms FROM exit_plan_events WHERE session_id=? AND symbol=? "
        "AND id < ? AND kind IN ('attached','amended','trailed') "
        "ORDER BY id DESC LIMIT 1", (session_id, symbol, fired_event_id)).fetchone()
    return int(row["ts_ms"]) if row else 0


def _lateness(
    conn: sqlite3.Connection, symbol: str, level: float,
    *, below: bool, since_ms: int, until_ms: int
) -> int | None:
    """Milliseconds between the tape printing through the level and the fill.

    Bounded below by when the level was set, so an earlier tick cannot be compared
    against a level that did not exist yet. Only as fine-grained as `ticks`, and
    roughly 10-15s of any figure here is structural: poller to ticks, ticks to the
    runner's 5s quote refresh, refresh to the loop's own interval.
    """
    row = conn.execute(
        "SELECT MIN(ts_ms) AS first_ms FROM ticks WHERE symbol=? "
        "AND ts_ms >= ? AND ts_ms <= ? AND "
        + ("last <= ?" if below else "last >= ?"),
        (symbol, since_ms, until_ms, level)).fetchone()
    if row is None or row["first_ms"] is None:
        return None
    return int(until_ms - row["first_ms"])


def armed_outcomes(conn: sqlite3.Connection, session_id: int) -> ArmedOutcome:
    """Whether armed levels print, and how far away they were when armed.

    A 0% hit rate at 80bp distances is a session that did nothing for a reason
    nobody would guess from its P&L -- and "finished flat" and "armed three levels
    that never printed" need different fixes.

    `cancelled` is reported beside the rate rather than inside it: one status
    covers the model withdrawing an entry, the flatten clearing them all, and a
    level that DID trigger into a rejected order. Only the third belongs in the
    denominator, and they are not currently distinguishable.
    """
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM pending_entries WHERE session_id=? "
        "GROUP BY status", (session_id,))}
    triggered = counts.get("triggered", 0)
    expired = counts.get("expired", 0)
    denom = triggered + expired

    distances: list[float] = []
    for r in conn.execute(
        "SELECT pe.trigger_price, (SELECT t.last FROM ticks t WHERE t.symbol=pe.symbol "
        " AND t.ts_ms <= pe.created_ms ORDER BY t.ts_ms DESC, t.source LIMIT 1) AS armed_at "
        "FROM pending_entries pe WHERE pe.session_id=?", (session_id,)
    ):
        if r["armed_at"]:
            distances.append(
                abs(r["trigger_price"] - r["armed_at"]) / r["armed_at"] * 10_000)

    return ArmedOutcome(
        triggered=triggered, expired=expired,
        cancelled=counts.get("cancelled", 0), waiting=counts.get("waiting", 0),
        distances_bp=tuple(round(d, 1) for d in distances),
        hit_rate=triggered / denom if denom else None,
    )


def blind_time(
    conn: sqlite3.Connection, symbols: list[str], since_ms: int, until_ms: int
) -> list[Blindness]:
    """Seconds in the window with no usable price, per symbol.

    A stop that never fired because the feed was dead for four minutes is not
    evidence about stops, and every slippage figure from such a session should be
    read as suspect. Same gap algorithm as `FetchLogRepo.gaps`, against ticks.
    """
    out: list[Blindness] = []
    window_s = max(0.0, (until_ms - since_ms) / 1000)
    for symbol in symbols:
        stamps = [r["ts_ms"] for r in conn.execute(
            "SELECT ts_ms FROM ticks WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
            "ORDER BY ts_ms", (symbol, since_ms, until_ms))]
        blind = 0.0
        prev = since_ms
        for ts in stamps:
            gap = (ts - prev) / 1000
            if gap > MAX_QUOTE_AGE_S:
                blind += gap
            prev = ts
        trailing = (until_ms - prev) / 1000
        if trailing > MAX_QUOTE_AGE_S:
            blind += trailing
        out.append(Blindness(symbol=symbol, blind_s=blind, window_s=window_s))
    return out
