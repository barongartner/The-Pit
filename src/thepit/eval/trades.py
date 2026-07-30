"""Trades, folded out of the fill stream into flat-to-flat episodes.

A fill is not a trade. Three buys and a sell are one position with four legs, and
every interesting question -- did conviction predict the outcome, what closed the
position, how long was it held -- is a question about the episode, not the leg.

The fold is deliberately plain Python over an ordered query rather than SQL window
functions: the rule ("a new episode begins when quantity crosses back through
zero") is the thing worth reading, and it has to match `Book.apply` exactly. The
test that matters asserts an episode's net against `positions.realized`, which is
the independent implementation.

**Attribution comes from `orders.origin`, not from prose.** Before 005 it could
only be recovered by matching the wording of f-strings in fastloop.py and
runner.py, which made every attribution in the history one edit away from silently
becoming "the model did it". The prefix table is kept for rows written before that
column existed, in one place, marked.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

DUST = 1e-9


class Origin(StrEnum):
    MODEL = "model"                          # a policy tick's own order
    ARMED = "armed"                          # an entry that fired at its level
    FAST_LOOP_STOP = "fast_loop_stop"
    FAST_LOOP_TARGET = "fast_loop_target"
    FAST_LOOP_TIME_STOP = "fast_loop_time_stop"
    FLATTEN = "flatten"                      # the end-of-session close
    UNPROTECTED = "unprotected"              # emergency unwind of an unprotected fill
    UNKNOWN = "unknown"


# Legacy fallback, for orders written before `orders.origin` existed. The only
# place in the project that infers provenance from prose.
_LEGACY_PREFIXES: tuple[tuple[str, Origin], ...] = (
    ("fast loop stop:", Origin.FAST_LOOP_STOP),
    ("fast loop target:", Origin.FAST_LOOP_TARGET),
    ("fast loop time_stop:", Origin.FAST_LOOP_TIME_STOP),
    ("session flatten", Origin.FLATTEN),
    ("unprotected fill:", Origin.UNPROTECTED),
    ("armed at ", Origin.ARMED),
)

# Which origins close a position, and what to call the exit.
_EXIT_NAMES: dict[Origin, str] = {
    Origin.FAST_LOOP_STOP: "stop",
    Origin.FAST_LOOP_TARGET: "target",
    Origin.FAST_LOOP_TIME_STOP: "time_stop",
    Origin.FLATTEN: "flatten",
    Origin.UNPROTECTED: "unprotected",
    Origin.MODEL: "model",
    Origin.ARMED: "model",
    Origin.UNKNOWN: "unknown",
}


@dataclass(frozen=True, slots=True)
class Episode:
    """One position, from flat to flat."""

    session_id: int
    symbol: str
    long: bool
    opened_ms: int
    closed_ms: int | None
    legs: int
    peak_qty: float
    entry_price: float          # quantity-weighted over the opening legs
    net: float                  # realised P&L; 0.0 while still open
    costs: float
    conviction: int | None      # from the FIRST opening order
    conviction_conflict: bool   # later opening legs disagreed
    entry_origin: Origin
    exit: str
    open_at_end: bool
    tiers: tuple[str, ...]

    @property
    def gross(self) -> float:
        return self.net + self.costs

    @property
    def won(self) -> bool | None:
        return None if self.open_at_end else self.net > 0

    @property
    def held_s(self) -> float | None:
        if self.closed_ms is None:
            return None
        return (self.closed_ms - self.opened_ms) / 1000


def origin_of(row: sqlite3.Row) -> Origin:
    """The recorded origin, falling back to the reason prefix for old rows."""
    # `.keys()` is load-bearing on a sqlite3.Row: `x in row` tests values.
    recorded = row["origin"] if "origin" in row.keys() else None
    if recorded:
        try:
            return Origin(recorded)
        except ValueError:
            # An unrecognised value is reported, not silently bucketed as
            # 'model' -- that would be the exact failure the column exists to
            # prevent, reintroduced by a typo.
            return Origin.UNKNOWN
    reason = (row["reason"] or "") if "reason" in row.keys() else ""
    for prefix, origin in _LEGACY_PREFIXES:
        if reason.startswith(prefix):
            return origin
    return Origin.MODEL


def episodes(conn: sqlite3.Connection, session_id: int) -> list[Episode]:
    """Fold this session's fills into flat-to-flat episodes, per symbol."""
    rows = conn.execute(
        "SELECT f.id, f.order_id, f.ts_ms, f.symbol, f.side, f.qty, f.price, "
        "f.cost, f.sim_tier, o.conviction, o.reason, o.origin "
        "FROM fills f JOIN orders o ON o.id = f.order_id "
        "WHERE f.session_id=? ORDER BY f.symbol, f.ts_ms, f.id",
        (session_id,)).fetchall()

    out: list[Episode] = []
    current: dict | None = None
    symbol: str | None = None

    for r in rows:
        if r["symbol"] != symbol:
            # Symbols are independent books; a change of symbol cannot continue
            # an episode even if the previous one never closed.
            if current is not None:
                out.append(_finish(current, closed=False))
                current = None
            symbol = r["symbol"]

        signed = r["qty"] if r["side"] == "buy" else -r["qty"]
        if current is None:
            current = _start(session_id, r, signed)
        else:
            opening = (current["qty"] > 0) == (signed > 0)
            current["legs"] += 1
            current["costs"] += r["cost"]
            current["tiers"].add(r["sim_tier"])
            # Cash in and out. An episode that starts and ends flat has no
            # residual, so this sum IS its realised P&L -- and it already
            # contains slippage, because fills.price does.
            current["net"] += -signed * r["price"]
            if opening:
                current["entry_notional"] += abs(signed) * r["price"]
                current["entry_qty"] += abs(signed)
                if r["conviction"] is not None:
                    if current["conviction"] is None:
                        current["conviction"] = r["conviction"]
                    elif current["conviction"] != r["conviction"]:
                        current["conviction_conflict"] = True
            current["qty"] += signed
            current["peak_qty"] = max(current["peak_qty"], abs(current["qty"]))
            current["last_origin"] = origin_of(r)
            current["last_ms"] = r["ts_ms"]

            if abs(current["qty"]) < DUST:
                out.append(_finish(current, closed=True))
                current = None

    if current is not None:
        out.append(_finish(current, closed=False))
    return out


def _start(session_id: int, r: sqlite3.Row, signed: float) -> dict:
    return {
        "session_id": session_id, "symbol": r["symbol"], "long": signed > 0,
        "opened_ms": r["ts_ms"], "last_ms": r["ts_ms"], "qty": signed,
        "peak_qty": abs(signed), "legs": 1,
        "net": -signed * r["price"], "costs": r["cost"],
        "entry_notional": abs(signed) * r["price"], "entry_qty": abs(signed),
        "conviction": r["conviction"], "conviction_conflict": False,
        "entry_origin": origin_of(r), "last_origin": origin_of(r),
        "tiers": {r["sim_tier"]},
    }


def _finish(state: dict, *, closed: bool) -> Episode:
    return Episode(
        session_id=state["session_id"], symbol=state["symbol"], long=state["long"],
        opened_ms=state["opened_ms"],
        closed_ms=state["last_ms"] if closed else None,
        legs=state["legs"], peak_qty=state["peak_qty"],
        entry_price=state["entry_notional"] / state["entry_qty"]
        if state["entry_qty"] else 0.0,
        # An open episode's cash flow is not P&L: the residual position is still
        # worth something. Left at zero and flagged, rather than reported.
        net=state["net"] if closed else 0.0,
        costs=state["costs"],
        conviction=state["conviction"],
        conviction_conflict=state["conviction_conflict"],
        entry_origin=state["entry_origin"],
        exit=_EXIT_NAMES.get(state["last_origin"], "unknown") if closed else "open",
        open_at_end=not closed, tiers=tuple(sorted(state["tiers"])),
    )


def by_exit(eps: list[Episode]) -> dict[str, dict[str, float]]:
    """P&L split by what closed the position.

    The honest attribution of a result: if every winner exited on a Python target
    and every loser on a Python stop, the levels did the work, not the reasoning.
    """
    out: dict[str, dict[str, float]] = {}
    for e in eps:
        if e.open_at_end:
            continue
        bucket = out.setdefault(e.exit, {"n": 0, "net": 0.0, "wins": 0})
        bucket["n"] += 1
        bucket["net"] += e.net
        bucket["wins"] += 1 if e.net > 0 else 0
    return out


def entry_discipline(conn: sqlite3.Connection, session_id: int) -> dict[str, int]:
    """Armed entries versus market orders, and unprotected fills.

    The recorded failure was "planned TSLA 303.50, bought 304.82, violated plan".
    This is the direct measurement of whether arming a level fixed it.
    """
    rows = conn.execute(
        "SELECT o.origin, o.reason, f.side FROM fills f "
        "JOIN orders o ON o.id=f.order_id WHERE f.session_id=?",
        (session_id,)).fetchall()
    counts = {"opening_fills": 0, "armed": 0, "at_market": 0, "unprotected": 0}
    for r in rows:
        origin = origin_of(r)
        if origin in (Origin.FLATTEN, Origin.FAST_LOOP_STOP,
                      Origin.FAST_LOOP_TARGET, Origin.FAST_LOOP_TIME_STOP):
            continue
        if origin is Origin.UNPROTECTED:
            counts["unprotected"] += 1
            continue
        counts["opening_fills"] += 1
        counts["armed" if origin is Origin.ARMED else "at_market"] += 1
    return counts


def unprotected_fills(conn: sqlite3.Connection, session_id: int) -> list[str]:
    """Opening fills with no exit plan of any kind. Should always be empty."""
    return [r["symbol"] for r in conn.execute(
        "SELECT DISTINCT f.symbol FROM fills f JOIN orders o ON o.id=f.order_id "
        "WHERE f.session_id=? AND COALESCE(o.origin,'model') IN ('model','armed') "
        "AND NOT EXISTS (SELECT 1 FROM exit_plans e WHERE e.session_id=f.session_id "
        "AND e.symbol=f.symbol)", (session_id,))]
