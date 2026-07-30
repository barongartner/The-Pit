"""The fast loop: Python enforcing the levels the model committed to.

Two layers, which is what the session design was always for:

* **Slow loop (the model).** Strategy, watchlist, entry and exit *levels*, risk
  posture. Minutes apart, because a CLI call takes 9-40 seconds measured and no
  amount of wishing makes a model a sub-minute execution path.
* **Fast loop (this module).** Enforcement. Seconds apart, no model asked.

Before this existed, nothing happened between policy ticks. A stop set at -15bp
was checked when the model was next asked, which on a five-minute tick is up to
five minutes late -- and a 15bp stop honoured five minutes late is not a 15bp
stop. Session 4 lost $6 with both positions drifting past their stated stops.

What it does every interval, without asking anything:

1. Close a position whose stop or target has printed.
2. Fill an armed entry when its level prints, and attach that entry's exits.
3. Flatten a position that has run out of time on its own time stop.
4. Drag a trailing stop after the high-water mark.

It runs as its own asyncio task, so it keeps working *during* a 40-second model
call. That is most of the value: the interval where nothing was watching used to
include the exact moment the model was thinking about what to do.

**Honest resolution.** The Yahoo feed updates roughly every five seconds and has
no bid/ask, so this enforces against last-trade snapshots. It cannot see the path
between two snapshots, and a real venue stop would fill on ticks this never sees.
Enforcement here is *late by up to one interval plus feed latency*, not exact.
That is a large improvement on "late by up to one policy tick" and is not the
same thing as a real stop order.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace

from thepit.core.clock import Clock
from thepit.core.types import Quote
from thepit.trading import levels as lv
from thepit.trading.book import Fill

log = logging.getLogger("thepit.fastloop")

# Refusing to act on a quote older than this is the same rule the risk layer
# uses. A stop enforced against a dead feed is worse than one enforced late:
# it closes a position on a price that no longer exists.
MAX_QUOTE_AGE_S = 120.0


@dataclass(frozen=True, slots=True)
class Armed:
    """A pending entry, as stored."""

    id: int
    symbol: str
    side: str
    qty: float
    trigger_price: float
    direction: str
    expires_ms: int
    levels: lv.Levels
    reason: str
    conviction: int | None


SubmitFn = Callable[..., Fill | None]
SayFn = Callable[..., object]


class FastLoop:
    """Enforces stored levels on a seconds cadence.

    Holds no state of its own: plans live in the database, so what is being
    enforced is inspectable while the session runs and survives the loop being
    restarted. `step()` is synchronous and takes the time as an argument, which
    is what makes it testable without an event loop.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        session_id: int,
        *,
        quotes: Callable[[], dict[str, Quote]],
        positions: Callable[[], dict[str, float]],
        submit: SubmitFn,
        say: SayFn,
        interval_s: float = 5.0,
        round_trip_cost_bp: float = 3.0,
        max_quote_age_s: float = MAX_QUOTE_AGE_S,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._sid = session_id
        self._quotes = quotes
        self._positions = positions
        self._submit = submit
        self._say = say
        self.interval_s = interval_s
        self._cost_bp = round_trip_cost_bp
        self._max_age_s = max_quote_age_s
        self._stale: set[str] = set()
        self.ticks = 0

    # -- writing plans -------------------------------------------------------

    def attach(
        self, symbol: str, side: str, entry_price: float, levels: lv.Levels
    ) -> tuple[lv.ExitPlan | None, str | None]:
        """Resolve levels against a real fill and store the plan.

        Called immediately after an opening fill. The fill price, not the quote
        the decision was made on, is the reference -- otherwise a 15bp stop is
        measured from a price nobody traded at.
        """
        plan, error = lv.resolve(
            levels, symbol=symbol, side=side, entry_price=entry_price,
            now_ms=self._clock.now_ms(), round_trip_cost_bp=self._cost_bp,
        )
        if plan is None:
            return None, error
        self._write(plan)
        self._say("levels", self.describe(plan))
        return plan, None

    def amend(self, symbol: str, levels: lv.Levels) -> tuple[lv.ExitPlan | None, str | None]:
        """Revise an active plan without trading.

        The model needs a way to tighten a stop on new information, and making
        that require a round trip through a position would be an absurd tax. The
        original entry price stays the reference so revised levels mean the same
        thing the first ones did.
        """
        current = self.plan(symbol)
        if current is None:
            return None, f"no active exit plan for {symbol}"

        # Whatever the amendment does not mention is carried over. An amendment
        # that names only a target must not be a way to delete the stop.
        stated = lv.Levels(
            stop_price=levels.stop_price, stop_bp=levels.stop_bp,
            target_price=levels.target_price, target_bp=levels.target_bp,
            trail_bp=levels.trail_bp if levels.trail_bp is not None
            else current.trail_bp,
            time_stop_minutes=levels.time_stop_minutes,
        )
        if not stated.has_stop:
            stated = replace(stated, stop_price=current.stop_price)
        if stated.target_price is None and stated.target_bp is None:
            stated = replace(stated, target_price=current.target_price)

        plan, error = lv.resolve(
            stated, symbol=symbol, side="buy" if current.long else "sell",
            entry_price=current.entry_price, now_ms=self._clock.now_ms(),
            round_trip_cost_bp=self._cost_bp,
        )
        if plan is None:
            return None, error
        plan = replace(
            plan,
            high_water=current.high_water,
            # A new deadline only if one was stated; otherwise the original
            # clock keeps running rather than silently resetting.
            time_stop_ms=plan.time_stop_ms if levels.time_stop_minutes is not None
            else current.time_stop_ms,
        )
        self._write(plan)
        self._say("levels", "REVISED " + self.describe(plan))
        return plan, None

    def arm(
        self,
        symbol: str,
        side: str,
        qty: float,
        levels: lv.Levels,
        *,
        price_now: float,
        expires_ms: int,
        reason: str = "",
        conviction: int | None = None,
    ) -> Armed:
        """Store an entry waiting on a level.

        Nothing about it is an order yet. When it prints, the ordinary order path
        runs -- risk check, fill model, `orders` row -- so an armed entry can
        still be rejected for size or cash like anything else.
        """
        now = self._clock.now_ms()
        assert levels.trigger_price is not None
        direction = lv.arm_direction(side, levels.trigger_price, price_now)
        if levels.valid_minutes is not None:
            expires_ms = min(expires_ms, now + int(levels.valid_minutes * 60_000))

        cur = self._conn.execute(
            "INSERT INTO pending_entries (session_id,created_ms,symbol,side,qty,"
            "trigger_price,direction,expires_ms,stop_price,stop_bp,target_price,"
            "target_bp,trail_bp,time_stop_minutes,reason,conviction) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self._sid, now, symbol, side, qty, levels.trigger_price, direction,
             expires_ms, levels.stop_price, levels.stop_bp, levels.target_price,
             levels.target_bp, levels.trail_bp, levels.time_stop_minutes,
             reason[:200], conviction),
        )
        self._conn.commit()
        armed = Armed(int(cur.lastrowid), symbol, side, qty, levels.trigger_price,
                      direction, expires_ms, levels, reason, conviction)
        mins = max(0, (expires_ms - now) // 60_000)
        self._say("levels",
                  f"ARMED {side} {qty:g} {symbol} when price is "
                  f"{'at or below' if direction == 'at_or_below' else 'at or above'} "
                  f"{levels.trigger_price:.2f} (expires in {mins}m)")
        return armed

    def cancel_pending(self, symbols: list[str] | None = None) -> int:
        """Withdraw armed entries. All of them at flatten, or a named few."""
        sql = ("UPDATE pending_entries SET status='cancelled', resolved_ms=? "
               "WHERE session_id=? AND status='waiting'")
        params: list[object] = [self._clock.now_ms(), self._sid]
        if symbols:
            sql += f" AND symbol IN ({','.join('?' * len(symbols))})"
            params += [s.upper() for s in symbols]
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        if cur.rowcount:
            self._say("levels", f"Cancelled {cur.rowcount} armed entr"
                                f"{'y' if cur.rowcount == 1 else 'ies'}")
        return int(cur.rowcount)

    # -- reading -------------------------------------------------------------

    def plan(self, symbol: str) -> lv.ExitPlan | None:
        row = self._conn.execute(
            "SELECT * FROM exit_plans WHERE session_id=? AND symbol=? AND "
            "status='active'", (self._sid, symbol)).fetchone()
        return _plan_from_row(row) if row else None

    def plans(self) -> list[lv.ExitPlan]:
        return [_plan_from_row(r) for r in self._conn.execute(
            "SELECT * FROM exit_plans WHERE session_id=? AND status='active' "
            "ORDER BY symbol", (self._sid,))]

    def time_stop_ms(self, symbol: str) -> int | None:
        plan = self.plan(symbol)
        return plan.time_stop_ms if plan else None

    def armed(self) -> list[Armed]:
        return [_armed_from_row(r) for r in self._conn.execute(
            "SELECT * FROM pending_entries WHERE session_id=? AND status='waiting' "
            "ORDER BY id", (self._sid,))]

    def describe(self, plan: lv.ExitPlan) -> str:
        bits = [f"stop {plan.stop_price:.2f} ({plan.stop_bp():.0f}bp)"]
        if plan.target_price is not None:
            bits.append(f"target {plan.target_price:.2f}")
        if plan.trail_bp is not None:
            bits.append(f"trailing {plan.trail_bp:.0f}bp")
        if plan.time_stop_ms is not None:
            mins = max(0, (plan.time_stop_ms - self._clock.now_ms()) // 60_000)
            bits.append(f"time stop in {mins}m")
        return f"{plan.symbol} levels: " + ", ".join(bits)

    # -- the loop ------------------------------------------------------------

    async def run(self) -> None:
        """Enforce until cancelled. Never lets one bad interval end the loop.

        An exception here must not kill enforcement for the rest of the session,
        because the failure mode is silent and expensive: positions carry on with
        nobody watching and the session looks fine from the outside.
        """
        while True:
            try:
                self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - see above
                log.exception("fast loop step failed (session %s)", self._sid)
            await asyncio.sleep(self.interval_s)

    def step(self, *, can_open: bool = True) -> list[str]:
        """One enforcement pass. Returns what it did, for tests and logs."""
        now = self._clock.now_ms()
        quotes = self._quotes()
        positions = self._positions()
        acted: list[str] = []
        self.ticks += 1

        for plan in self.plans():
            qty = positions.get(plan.symbol, 0.0)
            if abs(qty) < 1e-9:
                # Closed by the model, by the flatten, or by an earlier pass.
                self._close_plan(plan.symbol, now)
                continue

            price = self._usable_price(plan.symbol, quotes, now)
            if price is None:
                continue

            hit = lv.breached(plan, price, now)
            if hit is not None:
                self._fire(plan, hit, qty, now)
                acted.append(f"{hit.kind}:{plan.symbol}")
                continue

            moved = plan.trailed(price)
            if moved.stop_price != plan.stop_price or moved.high_water != plan.high_water:
                self._write(moved)
                if moved.stop_price != plan.stop_price:
                    acted.append(f"trail:{plan.symbol}")
                    self._say("levels", f"{plan.symbol} trailing stop raised to "
                                        f"{moved.stop_price:.2f}")

        for entry in self.armed():
            if now >= entry.expires_ms:
                self._resolve_entry(entry.id, "expired", now)
                self._say("levels", f"Armed {entry.side} {entry.symbol} at "
                                    f"{entry.trigger_price:.2f} expired unfilled")
                acted.append(f"expired:{entry.symbol}")
                continue

            price = self._usable_price(entry.symbol, quotes, now)
            if price is None or not lv.triggered(entry.direction, entry.trigger_price,
                                                 price):
                continue
            if not can_open:
                self._resolve_entry(entry.id, "cancelled", now)
                continue
            acted.append(f"entry:{entry.symbol}")
            self._fill_armed(entry, price, now)

        return acted

    # -- internals -----------------------------------------------------------

    def _usable_price(
        self, symbol: str, quotes: dict[str, Quote], now: int
    ) -> float | None:
        quote = quotes.get(symbol)
        if quote is None:
            return None
        age_s = (now - quote.received_ms) / 1000
        if age_s > self._max_age_s:
            if symbol not in self._stale:
                self._stale.add(symbol)
                # Said once per episode rather than every interval, which at a
                # 5-second cadence would bury the activity log.
                self._say("error", f"{symbol} price is {age_s:.0f}s old — levels "
                                   f"cannot be enforced against a dead feed")
            return None
        self._stale.discard(symbol)
        return quote.last

    def _fire(self, plan: lv.ExitPlan, hit: lv.Breach, qty: float, now: int) -> None:
        self._say("order", f"{plan.symbol} {hit.kind.upper()} — {hit.detail}")
        # can_open=False: this is always a reduction, and it must pass even after
        # the clock has closed for new positions.
        fill = self._submit(
            {"symbol": plan.symbol, "side": plan.close_side, "qty": abs(qty),
             "reason": f"fast loop {hit.kind}: {hit.detail}"[:200]},
            can_open=False,
        )
        self._conn.execute(
            "UPDATE exit_plans SET status='fired', fired_ms=?, fired_reason=?, "
            "updated_ms=? WHERE session_id=? AND symbol=?",
            (now, f"{hit.kind}: {hit.detail}", now, self._sid, plan.symbol))
        self._conn.commit()
        if fill is None:
            # The order was rejected, so the position is still open with no plan
            # watching it. Loud, because it is the one outcome here that leaves
            # risk unmanaged.
            self._say("error", f"{plan.symbol} {hit.kind} could not be executed — "
                               f"position is still open and unprotected")

    def _fill_armed(self, entry: Armed, price: float, now: int) -> None:
        self._say("order", f"{entry.symbol} triggered at {price:.2f} — "
                           f"{entry.side} {entry.qty:g}")
        fill = self._submit(
            {"symbol": entry.symbol, "side": entry.side, "qty": entry.qty,
             "reason": f"armed at {entry.trigger_price:.2f}: {entry.reason}"[:200],
             "conviction": entry.conviction},
            can_open=True,
        )
        if fill is None:
            self._resolve_entry(entry.id, "cancelled", now)
            return
        self._resolve_entry(entry.id, "triggered", now)
        self.protect(fill, entry.levels)

    def protect(self, fill: Fill, levels: lv.Levels) -> lv.ExitPlan | None:
        """Attach a plan to a fresh opening fill, or close the position.

        A filled position with no enforceable stop is the exact state this module
        exists to prevent, so it is not allowed to persist for one interval. The
        levels were validated against the quote before the order went out, which
        makes this close to unreachable -- and carrying it silently if it ever
        happens is worse than an unwanted round trip.
        """
        plan, error = self.attach(fill.symbol, fill.side, fill.price, levels)
        if plan is not None:
            return plan
        self._say("error", f"{fill.symbol} filled but its exit levels do not resolve "
                           f"({error}). Closing immediately.")
        self._submit(
            {"symbol": fill.symbol, "side": "sell" if fill.side == "buy" else "buy",
             "qty": fill.qty, "reason": f"unprotected fill: {error}"[:200]},
            can_open=False,
        )
        return None

    def _resolve_entry(self, entry_id: int, status: str, now: int) -> None:
        self._conn.execute(
            "UPDATE pending_entries SET status=?, resolved_ms=? WHERE id=?",
            (status, now, entry_id))
        self._conn.commit()

    def _close_plan(self, symbol: str, now: int) -> None:
        self._conn.execute(
            "UPDATE exit_plans SET status='closed', updated_ms=? "
            "WHERE session_id=? AND symbol=? AND status='active'",
            (now, self._sid, symbol))
        self._conn.commit()

    def _write(self, plan: lv.ExitPlan) -> None:
        now = self._clock.now_ms()
        self._conn.execute(
            "INSERT INTO exit_plans (session_id,symbol,created_ms,updated_ms,long,"
            "entry_price,stop_price,target_price,trail_bp,time_stop_ms,high_water,"
            "status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'active') "
            "ON CONFLICT(session_id,symbol) DO UPDATE SET updated_ms=excluded.updated_ms,"
            "long=excluded.long, entry_price=excluded.entry_price,"
            "stop_price=excluded.stop_price, target_price=excluded.target_price,"
            "trail_bp=excluded.trail_bp, time_stop_ms=excluded.time_stop_ms,"
            "high_water=excluded.high_water, status='active', fired_ms=NULL,"
            "fired_reason=NULL",
            (self._sid, plan.symbol, now, now, 1 if plan.long else 0,
             plan.entry_price, plan.stop_price, plan.target_price, plan.trail_bp,
             plan.time_stop_ms, plan.high_water),
        )
        self._conn.commit()


def _plan_from_row(row: sqlite3.Row) -> lv.ExitPlan:
    return lv.ExitPlan(
        symbol=row["symbol"], long=bool(row["long"]), entry_price=row["entry_price"],
        stop_price=row["stop_price"], target_price=row["target_price"],
        trail_bp=row["trail_bp"], time_stop_ms=row["time_stop_ms"],
        high_water=row["high_water"],
    )


def _armed_from_row(row: sqlite3.Row) -> Armed:
    return Armed(
        id=row["id"], symbol=row["symbol"], side=row["side"], qty=row["qty"],
        trigger_price=row["trigger_price"], direction=row["direction"],
        expires_ms=row["expires_ms"],
        levels=lv.Levels(
            stop_price=row["stop_price"], stop_bp=row["stop_bp"],
            target_price=row["target_price"], target_bp=row["target_bp"],
            trail_bp=row["trail_bp"], time_stop_minutes=row["time_stop_minutes"],
        ),
        reason=row["reason"] or "", conviction=row["conviction"],
    )
