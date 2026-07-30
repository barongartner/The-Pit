"""Runs a session: plan, trade, flatten, review.

The loop is deliberately dull. Claude decides *what* on a slow tick; this module
decides *when* and enforces *whether*. Every order goes through the risk check
before it can become a fill, and there is no path around that -- `_submit` is
the only thing that writes to `orders`.

Two loops run at once. This one asks the model every few minutes. The fast loop
(`session/fastloop.py`) enforces the levels that model committed to every few
seconds, including while a 40-second model call is in flight. Before it existed,
nothing at all happened between policy ticks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from dataclasses import replace

from thepit.agent import claude, stub
from thepit.core.clock import Clock
from thepit.core.types import FeedTier, Quote
from thepit.engine.killswitch import KillSwitch
from thepit.session.config import SessionConfig
from thepit.session.fastloop import FastLoop
from thepit.session.prompt import build_plan_prompt
from thepit.trading import levels as lv
from thepit.trading.book import (
    Book, Fill, Limits, Reject, check, round_trip_cost_bp, simulate_fill,
)

log = logging.getLogger("thepit.session")

# How long to wait between flatten attempts, and how long to keep trying after a
# session has already failed. Five seconds matches the quote feed: retrying
# faster only re-reads the same stale price the risk layer just refused.
_FLATTEN_RETRY_MS = 5_000
_FLATTEN_GRACE_MS = 30_000

TICK_SCHEMA = """Return ONLY a JSON object, no prose around it:

{
  "assessment": "<=200 chars on what changed since your plan",
  "orders": [
    {"symbol":"AAPL","side":"buy","qty":0.0125,"reason":"<=120 chars","conviction":7,
     "stop":338.90,"target":341.40,"trigger":339.80,
     "trail_bp":20,"time_stop_minutes":4,"valid_minutes":10}
  ],
  "exits": [
    {"symbol":"AAPL","stop":339.60,"target":342.00}
  ],
  "cancel_pending": ["TSLA"]
}

"qty" may be fractional -- decimals are expected on a small account.

## Your levels are enforced in Python, every few seconds

Between these ticks a fast loop checks your levels against the tape without
asking you anything. It closes a position when its stop or target prints, expires
a time stop, and drags a trailing stop. You do not have to wait for your next
tick to be stopped out, and you cannot rely on being asked before a level fires.

- **Every order that opens or adds to a position must carry a stop.** No stop,
  the order is rejected. This is not advice.
- "stop" and "target" are prices; "stop_bp" and "target_bp" are distances from
  your fill in basis points. Use one form or the other, not both.
- **"trigger" is how you place a limit order.** It arms the entry and the fast
  loop fills it the moment that price prints, whether or not you are being asked
  anything. Without a trigger you are buying now, at whatever the tape says.
  There is no other resting-order mechanism: if you want to enter at a level,
  arm it on the tick you decide, not on the tick it arrives. Deciding to "wait
  for" a level and returning no orders means nothing is working and nothing
  will fill.
- "valid_minutes" expires an armed entry that never prints. "time_stop_minutes"
  flattens a position that has not worked by then.
- "trail_bp" moves the stop up behind the best price and never back down.
- "exits" revises the levels on a position you already hold, without trading.
- "cancel_pending" withdraws armed entries you no longer want.

You are expected to trade when a reasonable opportunity exists. You have a
short window and finishing flat earns nothing.

If you return an empty "orders" list, the "assessment" field must state exactly
what you are waiting for and what specifically would change your mind. "No clean
setup" is not an acceptable answer -- name the level or the condition.

Equally: do not churn. Many marginal trades lose to costs. Take the best
available opportunity, size it properly, and let it work."""


class SessionRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        config: SessionConfig,
        symbols: list[str],
        *,
        quotes: dict[str, Quote],
        tier: FeedTier,
        kill: KillSwitch | None = None,
        use_stub: bool = False,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._cfg = config
        self._symbols = symbols
        self._quotes = quotes
        self._tier = tier
        self._kill = kill
        self.session_id: int | None = None
        self._claude_session: str | None = None
        self._halted: str | None = None
        self._book: Book | None = None
        self._fast: FastLoop | None = None
        # Said once per episode. At a one-second cadence the alternative buries
        # the activity log in the same line.
        self._blind_noted = False
        # The decision an order belongs to. Recorded on the order rather than
        # inferred from timestamps, which stopped working the moment the fast
        # loop began submitting between ticks.
        self._decision_id: int | None = None
        # When true, decisions come from the deterministic rule instead of a
        # model. This is both the fallback when the CLI is unavailable and the
        # control group the LLM has to beat.
        #
        # Constructor-only, because `create()` now WRITES the arm to the
        # database rather than leaving it to be inferred. Setting it afterwards
        # would record the wrong arm and, since the column outranks the
        # inference, record it authoritatively. A test caught exactly that.
        self._use_stub = use_stub

    @property
    def use_stub(self) -> bool:
        return self._use_stub

    @use_stub.setter
    def use_stub(self, value: bool) -> None:
        # `create()` writes `sessions.arm` from this, and the column outranks
        # the decisions-table inference. Flipping it afterwards would leave a
        # row that authoritatively claims the wrong arm -- which is worse than
        # the guesswork the column replaced. Pass it to the constructor.
        if self.session_id is not None and value != self._use_stub:
            raise RuntimeError(
                "use_stub cannot change after create(): sessions.arm is already "
                "written. Pass use_stub= to SessionRunner()."
            )
        self._use_stub = value

    # -- activity ------------------------------------------------------------

    def say(self, kind: str, message: str, *, pending: bool = False) -> int:
        """Write one line of running commentary.

        This is what the operator watches. Without it a 40-second model call and
        a crashed process look identical from the outside, which is exactly how
        a session was lost without anyone noticing.
        """
        cur = self._conn.execute(
            "INSERT INTO activity (session_id,ts_ms,kind,message,pending) "
            "VALUES (?,?,?,?,?)",
            (self.session_id, self._clock.now_ms(), kind, message,
             1 if pending else 0),
        )
        self._conn.commit()
        log.info("session %s: %s", self.session_id, message)
        return int(cur.lastrowid)

    def done_saying(self, activity_id: int, message: str | None = None) -> None:
        """Mark a pending line finished, so the UI stops counting up on it."""
        if message:
            self._conn.execute(
                "UPDATE activity SET pending=0, message=? WHERE id=?",
                (message, activity_id))
        else:
            self._conn.execute(
                "UPDATE activity SET pending=0 WHERE id=?", (activity_id,))
        self._conn.commit()

    def beat(self) -> None:
        """Prove this session is still being driven by a live process."""
        self._conn.execute(
            "UPDATE sessions SET heartbeat_ms=? WHERE id=?",
            (self._clock.now_ms(), self.session_id))
        self._conn.commit()

    async def _beat_until_cancelled(self) -> None:
        """Keep beating through a blocking await. Cancelled by the caller."""
        while True:
            with contextlib.suppress(Exception):
                self.beat()
            await asyncio.sleep(1.0)

    # -- lifecycle -----------------------------------------------------------

    def create(self, *, twin_of: int | None = None) -> int:
        """Create the session row.

        `arm` is written here rather than inferred later. `cohort.classify()`
        otherwise infers it by string-matching an f-string out of the decisions
        table, which is the fragility 005_provenance.sql removed everywhere
        else: reword one prompt and every historical session changes arm.
        """
        now = self._clock.now_ms()
        cur = self._conn.execute(
            "INSERT INTO sessions (created_ms,ends_ms,status,config,capital,cash,"
            "universe,arm,twin_of) VALUES (?,?,'planned',?,?,?,?,?,?)",
            (now, now + self._cfg.duration_minutes * 60_000,
             json.dumps(_config_json(self._cfg)), self._cfg.capital, self._cfg.capital,
             # The symbols actually passed to this runner. `config.symbols` is
             # empty on the default path, so the universe a session traded used
             # to be recoverable only from the text of its plan prompt.
             json.dumps(list(self._symbols)),
             "baseline" if self.use_stub else "llm", twin_of),
        )
        self._conn.commit()
        self.session_id = int(cur.lastrowid)
        self._book = Book(self._conn, self.session_id, self._cfg.capital)
        self._fast = FastLoop(
            self._conn, self._clock, self.session_id,
            quotes=lambda: self._quotes,
            positions=lambda: self.book.positions,
            submit=lambda proposal, can_open, origin="model": self._submit(
                proposal, can_open=can_open, origin=origin),
            say=self.say,
            snapshot=self._snapshot_equity,
            interval_s=self._cfg.fast_loop_seconds,
            round_trip_cost_bp=self._cost_bp() or 3.0,
        )
        return self.session_id

    @property
    def fast(self) -> FastLoop:
        assert self._fast is not None, "create() first"
        return self._fast

    def update_quotes(self, quotes: dict[str, Quote]) -> None:
        self._quotes = quotes

    @property
    def book(self) -> Book:
        assert self._book is not None
        return self._book

    async def run(self) -> None:
        assert self.session_id is not None
        now = self._clock.now_ms()
        ends = now + self._cfg.duration_minutes * 60_000
        stop_opening = ends - self._cfg.flatten_before_end_minutes * 60_000

        self._set_status("running", started_ms=now)
        self.beat()
        self.say("phase", f"Session started. {self._cfg.duration_minutes} minutes, "
                          f"{self._cfg.tick_count} ticks, "
                          f"${self._cfg.capital:,.0f}"
                          + (" (deterministic baseline)" if self.use_stub else
                             f" ({self._cfg.model})"))
        self.say("phase", f"Levels enforced every {self._cfg.fast_loop_seconds}s "
                          f"between ticks.")

        fast: asyncio.Task | None = None
        try:
            await self._plan()

            # Started after planning and cancelled before the flatten, so it is
            # alive for exactly the window in which positions can exist. It runs
            # concurrently with the model calls below -- that is the point: the
            # interval where nothing watched the book used to include the whole
            # 40 seconds the model spent thinking.
            fast = asyncio.create_task(self.fast.run())

            tick_no = 0
            while self._clock.now_ms() < stop_opening and not self._stopped():
                tick_no += 1
                await self._tick(stop_opening, tick_no)
                await self._sleep_until_next_tick(stop_opening)

            await _stop_task(fast)
            fast = None
            self._set_status("flattening")
            self.say("phase", "Session clock reached. Closing all positions.")
            # The flatten window exists so this can retry. A single attempt was
            # enough to end a session 'done' while still holding: the risk layer
            # fails closed on a quote older than 120s, the closing order was
            # rejected, and nothing looked again.
            stuck = await self._flatten_until_flat(deadline_ms=ends)
            await self._review(stuck)
            self._finish(stuck)
        except Exception as exc:  # noqa: BLE001 - a session must not take the engine down
            log.exception("session %s failed", self.session_id)
            self._halted = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                self.say("error", f"Session failed: {self._halted}")
                # Best effort, and bounded: the loop above is gone, so retrying
                # here is the last chance to end flat.
                stuck = await self._flatten_until_flat(
                    deadline_ms=self._clock.now_ms() + _FLATTEN_GRACE_MS)
                if stuck:
                    self.say("error", f"STILL HOLDING {', '.join(stuck)} after a "
                                      f"failed session. Close it by hand.")
            self._set_status("failed", halt_reason=self._halted)
        finally:
            # A failed session must not leave an enforcement task running against
            # a book nothing else is driving.
            if fast is not None:
                await _stop_task(fast)

    def _finish(self, stuck: list[str]) -> None:
        """Set the terminal status, and refuse to call a session done if it is not.

        A session that still holds stock is not finished, whatever the clock says.
        Recording it as 'done' hides an open position behind a status that reads
        as settled, and the P&L on that row is unrealised whether or not anyone
        notices.

        The reason a session stopped early is preserved rather than replaced. An
        earlier version overwrote it with the flatten outcome, so a run that hit
        its loss limit and then could not close ended up recorded as a flatten
        problem, with the loss limit -- the thing that actually happened --
        appearing nowhere in `sessions`, `orders` or `fills`.
        """
        eq = self.book.equity(self._quotes)
        pnl = eq - self._cfg.capital
        now = self._clock.now_ms()
        originating = self._halted
        # The last curve point must be the final equity. Without this the newest
        # `equity` row always predates the flatten.
        self._snapshot_equity(now)

        if not stuck:
            if originating:
                # It ended flat, but it did not run to the clock. 'done' would
                # read as an ordinary finish.
                self._set_status("halted", halt_reason=originating, finished_ms=now)
                self.say("phase", f"Halted and flat: {originating}. "
                                  f"P&L ${pnl:+,.2f}")
            else:
                self._set_status("done", finished_ms=now)
                self.say("phase", f"Done. P&L ${pnl:+,.2f}")
            return

        held = ", ".join(stuck)
        still_open = f"ended still holding {held}"
        self._halted = f"{originating}; {still_open}" if originating else still_open
        self.say("error", f"STILL HOLDING {held} — could not close it before the "
                          f"session clock ran out. Close it by hand.")
        self._set_status("halted", halt_reason=self._halted, finished_ms=now)
        self.say("phase", f"Ended holding {held}. Marked-to-market P&L "
                          f"${pnl:+,.2f} (not realised — the position is open)")

    def _stopped(self) -> bool:
        if self._kill is not None and self._kill.engaged():
            self._halted = "kill switch engaged"
            return True
        if self._halted:
            return True

        # A loss limit is only as good as the mark behind it. With a symbol
        # missing or its price minutes old, the number is fiction in both
        # directions: it can invent a breach, and it can hide one. Say so once
        # and skip the check -- the fast loop has already stopped enforcing
        # levels on that symbol for the same reason, and the flatten will report
        # what it could not close.
        blind = self.book.unpriced(
            self._quotes, now_ms=self._clock.now_ms(),
            max_age_s=Limits().max_quote_age_s)
        if blind:
            if not self._blind_noted:
                self._blind_noted = True
                self.say("error", f"Cannot mark {', '.join(blind)} — the loss limit "
                                  f"is not being evaluated while the price is stale")
            return False
        self._blind_noted = False

        # Mark-to-market, including unrealized. Realized-only would let a
        # session avoid the limit indefinitely by refusing to close losers,
        # which is exactly the behaviour the limit exists to stop.
        equity = self.book.equity(self._quotes)
        loss_pct = (self._cfg.capital - equity) / self._cfg.capital * 100
        if loss_pct >= self._cfg.session_loss_limit_pct:
            self._halted = f"loss limit hit: down {loss_pct:.2f}%"
            self.say("error", f"HALTING — {self._halted}")
            return True
        return False

    # -- phases --------------------------------------------------------------

    async def _plan(self) -> None:
        if self.use_stub:
            text = stub.plan(self._symbols)
            self._conn.execute(
                "INSERT INTO decisions (session_id,ts_ms,phase,prompt,response) "
                "VALUES (?,?,'plan','(deterministic baseline)',?)",
                (self.session_id, self._clock.now_ms(), text))
            self._conn.execute(
                "UPDATE sessions SET plan=?, plan_ms=? WHERE id=?",
                (text, self._clock.now_ms(), self.session_id))
            self._conn.commit()
            return

        prompt = build_plan_prompt(
            self._conn, self._cfg, self._symbols,
            now_ms=self._clock.now_ms(),
            round_trip_cost_bp=self._cost_bp(),
        )
        res = await self._ask(prompt, phase="plan")
        if res is None:
            return
        # Locked before any trade. Without this pre-commitment the reasoning
        # degrades into narration of whatever happened.
        self._conn.execute(
            "UPDATE sessions SET plan=?, plan_ms=? WHERE id=?",
            (res.text, self._clock.now_ms(), self.session_id),
        )
        self._conn.commit()

    async def _tick(self, stop_opening_ms: int, tick_no: int = 0) -> None:
        remaining = max(0, (stop_opening_ms - self._clock.now_ms()) // 60_000)
        self.beat()
        self.say("phase",
                 f"Tick {tick_no}/{self._cfg.tick_count} — {remaining}m left to open")

        if self.use_stub:
            text = stub.decide(
                self._conn, self._symbols, self._quotes,
                {s: p.qty for s, p in self.book.positions.items()},
                budget=self.book.equity(self._quotes)
                       * self._cfg.max_position_pct / 100 * 0.9)
            self._conn.execute(
                "INSERT INTO decisions (session_id,ts_ms,phase,prompt,response,parsed)"
                " VALUES (?,?,'tick','(deterministic baseline)',?,?)",
                (self.session_id, self._clock.now_ms(), text, text))
            self._conn.commit()
            self._apply_decision(json.loads(text), stop_opening_ms)
            return

        res = await self._ask(self._tick_prompt(remaining), phase="tick")
        if res is None:
            return

        parsed = claude.extract_json(res.text)
        if not parsed:
            log.warning("session %s: tick returned no JSON", self.session_id)
            return

        self._conn.execute(
            "UPDATE decisions SET parsed=? WHERE id=(SELECT MAX(id) FROM decisions "
            "WHERE session_id=?)",
            (json.dumps(parsed), self.session_id),
        )
        self._conn.commit()

        self._apply_decision(parsed, stop_opening_ms)

    def _apply_decision(self, parsed: dict, stop_opening_ms: int) -> None:
        """Act on one tick response: cancellations, then exits, then orders.

        That order matters. Revising a stop must happen before new exposure is
        taken, so a tick that both tightens a stop and opens a position cannot
        leave the old level enforced against the new one for an interval.
        """
        cancels = parsed.get("cancel_pending") or []
        if isinstance(cancels, str):
            cancels = [cancels]
        if cancels:
            self.fast.cancel_pending([str(s).upper() for s in cancels])

        for amendment in parsed.get("exits") or []:
            symbol = str(amendment.get("symbol", "")).upper()
            levels, error = lv.parse(amendment)
            if error is None:
                _, error = self.fast.amend(symbol, levels)
            if error:
                self.say("error", f"Could not revise {symbol} levels: {error}")

        for order in parsed.get("orders") or []:
            self._place(order, stop_opening_ms=stop_opening_ms)

    async def _review(self, stuck: list[str] | None = None) -> None:
        summary = self._summary(stuck)
        if self.use_stub:
            self._conn.execute(
                "UPDATE sessions SET review=? WHERE id=?",
                (f"Deterministic baseline, no self-review.\n\n{summary}",
                 self.session_id))
            self._conn.commit()
            return

        # Telling it that it is flat when it is not corrupts the one qualitative
        # output of the whole harness: the review reasons about a settled P&L that
        # is actually mark-to-market on an open position.
        opening = ("The session is over and you are flat.\n\n" if not stuck else
                   f"The session is over. It could not be closed: you are STILL "
                   f"HOLDING {', '.join(stuck)}, so the P&L below is marked to "
                   f"market, not realised.\n\n")
        prompt = (
            opening
            + "## Your plan\n"
            f"{self._plan_text() or '(none recorded)'}\n\n"
            "## What actually happened\n"
            f"{summary}\n\n"
            "**Under 600 characters. Four plain lines, no headings.**\n\n"
            "followed: <did you follow the plan, and exactly where you deviated>\n"
            "deviation: <did deviating help or hurt, with the number>\n"
            "conviction: <was it justified by what happened>\n"
            "next: <one specific change, not a platitude>\n\n"
            "Be blunt. A session that lost money and says why usefully is worth "
            "more than one that made money and cannot."
        )
        res = await self._ask(prompt, phase="review")
        if res is not None:
            self._conn.execute(
                "UPDATE sessions SET review=? WHERE id=?", (res.text, self.session_id)
            )
            self._conn.commit()

    # -- orders --------------------------------------------------------------

    def _place(self, proposal: dict, *, stop_opening_ms: int) -> None:
        """Route one proposed order: reject it, arm it, or submit it now.

        Everything about levels happens here, before `_submit`, because the two
        failures being prevented are only preventable up front: a position that
        exists with no stop, and an entry taken at a price the plan did not
        choose. Both were paid for -- see the notes in fastloop.py.
        """
        symbol = str(proposal.get("symbol", "")).upper()
        side = str(proposal.get("side", "")).lower()
        qty = _qty(proposal)

        if qty <= 0:
            # Caught here rather than by the risk layer, because an armed entry
            # never reaches the risk layer: it goes straight into a table whose
            # CHECK (qty > 0) raises, which took down the whole session from one
            # malformed field in one order object. Note that any key drift in the
            # model's JSON ("size", "shares") reads as 0 through _qty, so this is
            # not only about a literal zero.
            self._reject(proposal, str(Reject.BAD_QTY))
            return

        levels, error = lv.parse(proposal)
        if error:
            self._reject(proposal, f"unusable levels: {error}")
            return

        quote = self._quotes.get(symbol)
        opening = self._opens_exposure(symbol, side, qty)

        if not opening:
            if levels.trigger_price is not None:
                # A conditional exit is what an exit plan is for; honouring a
                # trigger here would create a second, competing mechanism.
                self.say("order", f"Ignoring the trigger on a reducing "
                                  f"{side} {symbol} — use \"exits\" to set levels")
            self._submit(proposal, can_open=False)
            return

        if not levels.has_stop:
            self._reject(proposal, "an opening order must carry a stop")
            return

        if quote is not None:
            # Validated before filling. Discovering that a stop sits on the wrong
            # side of the entry *after* the fill means unwinding a position that
            # should never have been opened.
            #
            # Against the price this order would actually get, not the quote: the
            # fill model moves the entry by the assumed slippage, and validating
            # against the raw last trade let a target land one slippage step
            # inside the fill -- passing here, then failing after the buy, and
            # unwinding for a guaranteed loss. For an armed entry the intended
            # entry is the trigger level itself.
            entry_estimate = (
                levels.trigger_price if levels.trigger_price is not None
                else simulate_fill(side, qty, quote, self._tier).price
            )
            _, why = lv.resolve(
                levels, symbol=symbol, side=side, entry_price=entry_estimate,
                now_ms=self._clock.now_ms(),
                round_trip_cost_bp=self._cost_bp() or 3.0,
            )
            if why:
                self._reject(proposal, f"unusable levels: {why}")
                return

            if levels.trigger_price is not None:
                direction = lv.arm_direction(side, levels.trigger_price, quote.last)
                if not lv.triggered(direction, levels.trigger_price, quote.last):
                    self.fast.arm(
                        symbol, side, qty, levels,
                        price_now=quote.last, expires_ms=stop_opening_ms,
                        reason=str(proposal.get("reason", "")),
                        conviction=_conviction(proposal),
                    )
                    return

        fill = self._submit(proposal, can_open=True)
        if fill is not None:
            self.fast.protect(fill, levels)

    def _opens_exposure(self, symbol: str, side: str, qty: float) -> bool:
        """Does this order add risk rather than reduce it?

        The same test the risk layer uses, and it has to be the same: a stop is
        required for opening exposure and would be nonsense on a closing order.
        """
        pos = self.book.positions.get(symbol)
        current = pos.qty if pos else 0.0
        projected = current + (qty if side == "buy" else -qty)
        return abs(projected) >= abs(current)

    def _reject(self, proposal: dict, reason: str) -> None:
        """Record a proposal that never reached the risk layer.

        Written to `orders` as rejected rather than dropped, for the same reason
        risk rejections are kept: "what did it want to do that it was not allowed
        to do" is the more interesting question. The intended levels are stored
        with it, so the counterfactual is at least priceable later.
        """
        levels, _ = lv.parse(proposal)
        self._conn.execute(
            "INSERT INTO orders (session_id,ts_ms,symbol,side,qty,status,reason,"
            "reject_reason,conviction,origin,decision_id,stop_price,target_price,"
            "trigger_price) VALUES (?,?,?,?,?,'rejected',?,?,?,?,?,?,?,?)",
            (self.session_id, self._clock.now_ms(),
             str(proposal.get("symbol", "")).upper(),
             "buy" if str(proposal.get("side", "")).lower() != "sell" else "sell",
             max(_qty(proposal), 0.0001), str(proposal.get("reason", ""))[:200],
             reason, _conviction(proposal), "model", self._decision_id,
             levels.stop_price, levels.target_price, levels.trigger_price),
        )
        self._conn.commit()
        self.say("order", f"REJECTED {proposal.get('side')} "
                          f"{proposal.get('symbol')} — {reason}")

    def _submit(self, proposal: dict, *, can_open: bool,
                origin: str = "model") -> Fill | None:
        """The only path from a proposal to a fill. Every order passes the risk
        check here; there is no other writer of `orders`.

        Returns the fill so the caller can attach exit levels to the price that
        was actually paid rather than the quote the decision was made on.

        `origin` is recorded rather than inferred. It used to be recoverable only
        by matching the wording of a reason string, which made every attribution
        in the history one edit away from silently becoming 'model'."""
        now = self._clock.now_ms()
        symbol = str(proposal.get("symbol", "")).upper()
        side = str(proposal.get("side", "")).lower()
        qty = _qty(proposal)
        reason = str(proposal.get("reason", ""))[:200]
        conviction = _conviction(proposal)
        levels, _ = lv.parse(proposal)

        cur = self._conn.execute(
            "INSERT INTO orders (session_id,ts_ms,symbol,side,qty,status,reason,"
            "conviction,origin,decision_id,stop_price,target_price,trigger_price) "
            "VALUES (?,?,?,?,?,'proposed',?,?,?,?,?,?,?)",
            (self.session_id, now, symbol, side, max(qty, 0.0001), reason, conviction,
             origin, self._decision_id, levels.stop_price, levels.target_price,
             levels.trigger_price),
        )
        order_id = int(cur.lastrowid)

        quote = self._quotes.get(symbol)
        verdict = check(
            side=side, symbol=symbol, qty=qty, book=self.book, quote=quote,
            limits=Limits(
                max_position_pct=self._cfg.max_position_pct,
                max_concurrent=self._cfg.max_concurrent_positions,
                loss_limit_pct=self._cfg.session_loss_limit_pct,
            ),
            equity=self.book.equity(self._quotes),
            starting_capital=self._cfg.capital,
            now_ms=now,
            can_open=can_open,
            killed=bool(self._kill and self._kill.engaged()),
            halted=bool(self._halted),
        )

        if not verdict.ok:
            # Kept, not dropped. "What did it want to do that it was not allowed
            # to do" is the more interesting question, and it is unanswerable if
            # rejections are discarded.
            self._conn.execute(
                "UPDATE orders SET status='rejected', reject_reason=? WHERE id=?",
                (verdict.reason, order_id),
            )
            self._conn.commit()
            self.say("order", f"REJECTED {side} {qty:g} {symbol} — {verdict.reason}")
            return None

        assert quote is not None
        fill = simulate_fill(side, qty, quote, self._tier)
        self.book.apply(fill, now, order_id, quote_ts_ms=quote.ts_ms)
        self._conn.execute(
            "UPDATE orders SET status='filled', filled_qty=?, avg_fill=? WHERE id=?",
            (qty, fill.price, order_id),
        )
        self._conn.commit()
        self._snapshot_equity(now)
        self.say("fill", f"FILLED {side} {qty:g} {symbol} @ {fill.price:.2f} "
                         f"(cost ${fill.cost:.2f})")
        return replace(fill, order_id=order_id)

    async def _flatten_until_flat(self, *, deadline_ms: int) -> list[str]:
        """Keep trying to close everything until flat or out of time.

        Returns the symbols still held, which must be empty for a session to be
        called done. Retrying matters because the risk layer fails closed on a
        stale quote -- correctly, trading on a dead feed is worse -- so a single
        attempt turns a five-minute feed hiccup into a session that reports
        'done' while still holding stock.

        The kill switch is the exception: it rejects every order by design, so
        spinning against it would waste the whole window. One attempt, then say
        so plainly.
        """
        killed = bool(self._kill and self._kill.engaged())
        stuck = self._flatten()
        if stuck and killed:
            self.say("error", f"Kill switch is engaged, so {', '.join(stuck)} "
                              f"cannot be closed by this session.")
            return stuck
        if not stuck:
            return stuck

        act = self.say("wait", f"Could not close {', '.join(stuck)} — retrying "
                               f"until the price refreshes", pending=True)
        while stuck and self._clock.now_ms() + _FLATTEN_RETRY_MS < deadline_ms:
            self.beat()
            await asyncio.sleep(_FLATTEN_RETRY_MS / 1000)
            # Only re-submit against a price the risk layer would accept.
            # Hammering a stale quote every five seconds writes a rejection row
            # each time, and those rows are supposed to mean "the agent wanted
            # something it was not allowed to have".
            stuck = self._flatten(only_tradeable=True)
        self.done_saying(act, "Closed everything" if not stuck
                              else f"Gave up trying to close {', '.join(stuck)}")
        return stuck

    def _flatten(self, *, only_tradeable: bool = False) -> list[str]:
        """One attempt to close everything. Returns what is still held.

        can_open=False, so only reducing orders pass.
        """
        # Armed entries first. An entry that triggers into the flatten would open
        # a position seconds before the session is required to be flat.
        if self._fast is not None:
            self.fast.cancel_pending()
        for symbol, pos in list(self.book.positions.items()):
            if abs(pos.qty) < 1e-9:
                continue
            if only_tradeable and not self._tradeable(symbol):
                continue
            self._submit(
                {"symbol": symbol, "side": "sell" if pos.qty > 0 else "buy",
                 "qty": abs(pos.qty), "reason": "session flatten"},
                can_open=False, origin="flatten",
            )
        return [s for s, p in self.book.positions.items() if abs(p.qty) > 1e-9]

    def _tradeable(self, symbol: str) -> bool:
        """Would the risk layer accept an order on this symbol's current price?

        The same staleness rule, asked before submitting rather than after being
        refused.
        """
        quote = self._quotes.get(symbol)
        if quote is None:
            return False
        age_s = (self._clock.now_ms() - quote.received_ms) / 1000
        return age_s <= Limits().max_quote_age_s

    # -- helpers -------------------------------------------------------------

    def _cost_bp(self) -> float | None:
        has_book = any(q.has_book for q in self._quotes.values())
        if self._tier is FeedTier.QUOTES and has_book:
            return round_trip_cost_bp(self._tier, True)
        # None makes the prompt say UNKNOWN explicitly rather than omit it. A
        # missing cost reads as "free".
        return round_trip_cost_bp(FeedTier.BARS, False)

    def _tick_prompt(self, minutes_left: int) -> str:
        eq = self.book.equity(self._quotes)
        pnl = eq - self._cfg.capital

        lines: list[str] = []

        # The plan, restated verbatim, every tick.
        #
        # It was previously absent entirely. The agent wrote a plan and then
        # never saw it again, relying on the resumed conversation to remember
        # it. It did not: in one session it planned entries at TSLA 303.50 /
        # NVDA 193.50, then bought at 304.82 / 194.80 and wrote in its own
        # review "Violated plan... Chasing late entries lost the session."
        #
        # Restating it costs a few hundred tokens and is the difference between
        # a plan and a wish.
        plan = self._plan_text()
        if plan:
            lines += ["## Your plan, which you committed to before this session",
                      plan.strip(), "",
                      "Do not enter at a price materially worse than you planned. "
                      "If your level never comes, that is exactly what your "
                      "stand-down condition is for. Chasing a missed entry turns "
                      "a good setup into a bad one.", ""]

        lines += [
            f"## {minutes_left} minutes left to open positions",
            "After that you will be flattened automatically.",
            "",
            f"Cash ${self.book.cash:,.2f} | Equity ${eq:,.2f} | "
            f"P&L ${pnl:+,.2f} ({pnl / self._cfg.capital * 100:+.2f}%)",
            f"Max position ${eq * self._cfg.max_position_pct / 100:,.2f} per symbol. "
            f"Fractional quantities allowed.",
            "",
        ]

        if self.book.positions and any(abs(p.qty) > 1e-9
                                       for p in self.book.positions.values()):
            lines.append("### Your positions")
            for sym, pos in self.book.positions.items():
                if abs(pos.qty) < 1e-9:
                    continue
                q = self._quotes.get(sym)
                if q:
                    lines.append(
                        f"- {sym}: {pos.qty:+.0f} @ {pos.avg_price:.2f}, "
                        f"now {q.last:.2f}, unrealized ${pos.unrealized(q.last):+,.2f}"
                    )
                    # The levels currently being enforced on it. Without this the
                    # model cannot tell a stop it set from one the fast loop has
                    # already trailed, and would revise against the wrong number.
                    plan = self.fast.plan(sym)
                    if plan:
                        lines.append(f"  levels being enforced: "
                                     f"{self.fast.describe(plan).split('levels: ', 1)[-1]}")
        else:
            lines.append("You have no open positions.")
        lines.append("")

        armed = self.fast.armed()
        if armed:
            lines.append("### Armed entries, waiting on a level")
            for a in armed:
                mins = max(0, (a.expires_ms - self._clock.now_ms()) // 60_000)
                lines.append(
                    f"- {a.side} {a.qty:g} {a.symbol} when price is "
                    f"{'at or below' if a.direction == 'at_or_below' else 'at or above'}"
                    f" {a.trigger_price:.2f} (expires in {mins}m)")
            lines.append("These fill without asking you. Cancel any you no longer "
                         "want with \"cancel_pending\".")
            lines.append("")
        elif not any(abs(p.qty) > 1e-9 for p in self.book.positions.values()):
            # THE failure this exists to stop.
            #
            # A session planned "NVDA $192.00 limit", then returned empty orders
            # at every tick with the assessment "NVDA limit entry $192.00 never
            # filled". It believed it had a resting order. It had never placed
            # one: zero rows in pending_entries, for the whole run. So it
            # watched the tape for fifteen minutes and did nothing, which is
            # exactly what the operator reported (issue #19).
            #
            # The `trigger` field was documented in a bullet among eight others
            # and nothing said, at the moment it mattered, that waiting is not
            # how you get a limit fill here.
            lines.append("### You are flat with nothing armed")
            lines.append(
                "There is no resting order. Nothing will happen before your next "
                "tick unless you place something now.")
            lines.append(
                "**If your plan has an entry level that has not printed yet, arm "
                "it NOW** with \"trigger\" set to that level. The fast loop fills "
                "it the second the price prints, without waiting for you.")
            lines.append(
                f"Waiting instead means you look again in "
                f"{self._cfg.policy_tick_minutes} minutes and the move has "
                f"already happened. \"I am waiting for X\" is not a resting "
                f"order; an armed entry is.")
            lines.append("")

        lines.append("### Prices now")
        for sym in self._symbols:
            q = self._quotes.get(sym)
            if q:
                age_s = (self._clock.now_ms() - q.received_ms) / 1000
                # An age, not a bare number. A price printed as current when it
                # is four minutes old is the model reasoning about a market that
                # has moved on, and it cannot tell from the number alone.
                stale = "" if age_s <= Limits().max_quote_age_s else \
                    f"  [{age_s:.0f}s OLD — orders on this will be rejected]"
                lines.append(f"- {sym}: {q.last:.2f}{stale}")
        lines.append("")

        rejects = self._conn.execute(
            "SELECT symbol,side,qty,reject_reason FROM orders WHERE session_id=? "
            "AND status='rejected' ORDER BY id DESC LIMIT 5",
            (self.session_id,),
        ).fetchall()
        if rejects:
            # Feeding rejections back stops the model proposing the same
            # impossible order every tick.
            lines.append("### Recently rejected (do not repeat these)")
            for r in rejects:
                lines.append(
                    f"- {r['side']} {r['qty']:.0f} {r['symbol']}: {r['reject_reason']}")
            lines.append("")

        lines.append(TICK_SCHEMA)
        return "\n".join(lines)

    async def _ask(self, prompt: str, *, phase: str):
        cur = self._conn.execute(
            "INSERT INTO decisions (session_id,ts_ms,phase,prompt) VALUES (?,?,?,?)",
            (self.session_id, self._clock.now_ms(), phase, prompt),
        )
        decision_id = int(cur.lastrowid)
        self._decision_id = decision_id
        self._conn.commit()

        label = {"plan": "Asking the model for a plan",
                 "tick": "Asking the model what to do",
                 "review": "Asking the model to review the session"}[phase]
        act = self.say("model", label + "…", pending=True)

        # A model call blocks this coroutine for up to three minutes, and the
        # only other heartbeat is between ticks -- so a session doing exactly what
        # it should looked dead to the reaper, which marked it 'interrupted' and
        # overwrote its halt reason while the task was still running.
        beating = asyncio.create_task(self._beat_until_cancelled())
        try:
            res = await claude.ask(
                prompt, model=self._cfg.model, effort=self._cfg.effort,
                session_id=self._claude_session,
            )
        except claude.ClaudeUnavailable as exc:
            self.done_saying(act, f"Model unavailable: {exc}")
            self._conn.execute(
                "UPDATE decisions SET error=? WHERE id=?", (str(exc), decision_id))
            self._conn.commit()
            self._halted = str(exc)
            return None
        finally:
            await _stop_task(beating)

        self._claude_session = res.session_id or self._claude_session
        self._conn.execute(
            "UPDATE decisions SET response=?,error=?,latency_ms=?,cost_usd=?,"
            "tokens_in=?,tokens_out=? WHERE id=?",
            (res.text, res.text if res.is_error else None, res.latency_ms,
             res.cost_usd, res.tokens_in, res.tokens_out, decision_id),
        )
        self._conn.commit()

        if res.is_error:
            self.done_saying(act, f"Model returned an error after "
                                  f"{res.latency_ms / 1000:.0f}s")
            return None
        self.done_saying(act, f"{label.replace('Asking', 'Asked')} — replied in "
                              f"{res.latency_ms / 1000:.0f}s "
                              f"({len(res.text):,} chars)")
        return res

    async def _sleep_until_next_tick(self, stop_opening_ms: int) -> None:
        target = min(
            self._clock.now_ms() + self._cfg.policy_tick_minutes * 60_000,
            stop_opening_ms,
        )
        secs = max(0, (target - self._clock.now_ms()) // 1000)
        if secs <= 0:
            return
        act = self.say("wait", f"Waiting {secs // 60}m{secs % 60:02d}s "
                               f"until the next tick", pending=True)
        while self._clock.now_ms() < target and not self._stopped():
            self.beat()
            await asyncio.sleep(1.0)
        self.done_saying(act, "Wait over")

    def _snapshot_equity(self, ts_ms: int) -> None:
        eq = self.book.equity(self._quotes)
        self._conn.execute(
            "INSERT INTO equity (session_id,ts_ms,cash,positions_value,equity) "
            "VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            (self.session_id, ts_ms, self.book.cash, eq - self.book.cash, eq),
        )
        self._conn.commit()

    def _plan_text(self) -> str | None:
        row = self._conn.execute(
            "SELECT plan FROM sessions WHERE id=?", (self.session_id,)).fetchone()
        return row["plan"] if row else None

    def _summary(self, stuck: list[str] | None = None) -> str:
        eq = self.book.equity(self._quotes)
        pnl = eq - self._cfg.capital
        fills = self._conn.execute(
            "SELECT side,qty,symbol,price FROM fills WHERE session_id=? ORDER BY ts_ms",
            (self.session_id,)).fetchall()
        rejects = self._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE session_id=? AND status='rejected'",
            (self.session_id,)).fetchone()[0]
        costs = self._conn.execute(
            "SELECT COALESCE(SUM(cost),0) FROM fills WHERE session_id=?",
            (self.session_id,)).fetchone()[0]

        label = "Final P&L" if not stuck else "Mark-to-market P&L (NOT realised)"
        lines = [
            f"{label}: ${pnl:+,.2f} ({pnl / self._cfg.capital * 100:+.2f}%)",
            f"Modeled trading costs: ${costs:,.2f}",
            f"Fills: {len(fills)} | Rejected orders: {rejects}",
        ]
        if stuck:
            lines.append(f"STILL OPEN: {', '.join(stuck)}")
        if self._halted:
            lines.append(f"HALTED: {self._halted}")
        for f in fills:
            lines.append(f"  {f['side']} {f['qty']:.0f} {f['symbol']} @ {f['price']:.2f}")
        return "\n".join(lines)

    def _set_status(self, status: str, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE sessions SET status=?{', ' + sets if sets else ''} WHERE id=?"
        self._conn.execute(sql, (status, *fields.values(), self.session_id))
        self._conn.commit()


def _qty(proposal: dict) -> float:
    try:
        return float(proposal.get("qty", 0))
    except (TypeError, ValueError):
        return 0.0


def _conviction(proposal: dict) -> int | None:
    value = proposal.get("conviction")
    return int(value) if isinstance(value, (int, float)) and 1 <= value <= 10 else None


async def _stop_task(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _config_json(cfg: SessionConfig) -> dict:
    return {
        "duration_minutes": cfg.duration_minutes, "capital": cfg.capital,
        "symbols": list(cfg.symbols), "policy_tick_minutes": cfg.policy_tick_minutes,
        "fast_loop_seconds": cfg.fast_loop_seconds,
        "max_position_pct": cfg.max_position_pct,
        "max_concurrent_positions": cfg.max_concurrent_positions,
        "session_loss_limit_pct": cfg.session_loss_limit_pct,
        "model": cfg.model, "effort": cfg.effort,
        "research": cfg.research.value, "blinding": cfg.blinding.value,
        # Recorded even though nothing spawns the twin yet: without it in the
        # config JSON, a later eval cannot tell an unpaired session from one whose
        # control was requested and never ran.
        "run_baseline": cfg.run_baseline,
        "notes": cfg.notes,
    }
