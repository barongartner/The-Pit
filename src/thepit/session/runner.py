"""Runs a session: plan, trade, flatten, review.

The loop is deliberately dull. Claude decides *what* on a slow tick; this module
decides *when* and enforces *whether*. Every order goes through the risk check
before it can become a fill, and there is no path around that -- `_submit` is
the only thing that writes to `orders`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3

from thepit.agent import claude, stub
from thepit.core.clock import Clock
from thepit.core.types import FeedTier, Quote
from thepit.engine.killswitch import KillSwitch
from thepit.session.config import SessionConfig
from thepit.session.prompt import build_plan_prompt
from thepit.trading.book import Book, Limits, check, round_trip_cost_bp, simulate_fill

log = logging.getLogger("thepit.session")

TICK_SCHEMA = """Return ONLY a JSON object, no prose around it:

{
  "assessment": "<=200 chars on what changed since your plan",
  "orders": [
    {"symbol":"AAPL","side":"buy","qty":10,"reason":"<=120 chars","conviction":7}
  ]
}

An empty "orders" list is a valid and often correct answer. You are not scored
on activity. If nothing meets your plan's criteria, return no orders."""


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
        # When true, decisions come from the deterministic rule instead of a
        # model. This is both the fallback when the CLI is unavailable and the
        # control group the LLM has to beat.
        self.use_stub = False

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

    # -- lifecycle -----------------------------------------------------------

    def create(self) -> int:
        now = self._clock.now_ms()
        cur = self._conn.execute(
            "INSERT INTO sessions (created_ms,ends_ms,status,config,capital,cash) "
            "VALUES (?,?,'planned',?,?,?)",
            (now, now + self._cfg.duration_minutes * 60_000,
             json.dumps(_config_json(self._cfg)), self._cfg.capital, self._cfg.capital),
        )
        self._conn.commit()
        self.session_id = int(cur.lastrowid)
        self._book = Book(self._conn, self.session_id, self._cfg.capital)
        return self.session_id

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

        try:
            await self._plan()

            tick_no = 0
            while self._clock.now_ms() < stop_opening and not self._stopped():
                tick_no += 1
                await self._tick(stop_opening, tick_no)
                await self._sleep_until_next_tick(stop_opening)

            self._set_status("flattening")
            self.say("phase", "Session clock reached. Closing all positions.")
            self._flatten()
            await self._review()
            self._set_status("done", finished_ms=self._clock.now_ms())
            eq = self.book.equity(self._quotes)
            self.say("phase", f"Done. P&L ${eq - self._cfg.capital:+,.2f}")
        except Exception as exc:  # noqa: BLE001 - a session must not take the engine down
            log.exception("session %s failed", self.session_id)
            self._halted = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                self.say("error", f"Session failed: {self._halted}")
                self._flatten()
            self._set_status("failed", halt_reason=self._halted)

    def _stopped(self) -> bool:
        if self._kill is not None and self._kill.engaged():
            self._halted = "kill switch engaged"
            return True
        if self._halted:
            return True
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
                {s: p.qty for s, p in self.book.positions.items()})
            self._conn.execute(
                "INSERT INTO decisions (session_id,ts_ms,phase,prompt,response,parsed)"
                " VALUES (?,?,'tick','(deterministic baseline)',?,?)",
                (self.session_id, self._clock.now_ms(), text, text))
            self._conn.commit()
            for order in (json.loads(text).get("orders") or []):
                self._submit(order, can_open=True)
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

        for order in parsed.get("orders") or []:
            self._submit(order, can_open=True)

    async def _review(self) -> None:
        summary = self._summary()
        if self.use_stub:
            self._conn.execute(
                "UPDATE sessions SET review=? WHERE id=?",
                (f"Deterministic baseline, no self-review.\n\n{summary}",
                 self.session_id))
            self._conn.commit()
            return

        prompt = (
            "The session is over and you are flat.\n\n"
            "## Your plan\n"
            f"{self._plan_text() or '(none recorded)'}\n\n"
            "## What actually happened\n"
            f"{summary}\n\n"
            "Answer in a few short paragraphs:\n"
            "1. Did you follow your plan? Where exactly did you deviate?\n"
            "2. Did deviating help or hurt?\n"
            "3. Was your conviction justified by the outcomes?\n"
            "4. What would you do differently, specifically?\n\n"
            "Be blunt. A session that lost money and says so usefully is worth "
            "more than one that made money and cannot say why."
        )
        res = await self._ask(prompt, phase="review")
        if res is not None:
            self._conn.execute(
                "UPDATE sessions SET review=? WHERE id=?", (res.text, self.session_id)
            )
            self._conn.commit()

    # -- orders --------------------------------------------------------------

    def _submit(self, proposal: dict, *, can_open: bool) -> None:
        """The only path from a proposal to a fill. Every order passes the risk
        check here; there is no other writer of `orders`."""
        now = self._clock.now_ms()
        symbol = str(proposal.get("symbol", "")).upper()
        side = str(proposal.get("side", "")).lower()
        try:
            qty = float(proposal.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0.0
        reason = str(proposal.get("reason", ""))[:200]
        conviction = proposal.get("conviction")
        conviction = int(conviction) if isinstance(conviction, (int, float)) and \
            1 <= conviction <= 10 else None

        cur = self._conn.execute(
            "INSERT INTO orders (session_id,ts_ms,symbol,side,qty,status,reason,"
            "conviction) VALUES (?,?,?,?,?,'proposed',?,?)",
            (self.session_id, now, symbol, side, max(qty, 0.0001), reason, conviction),
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
            return

        assert quote is not None
        fill = simulate_fill(side, qty, quote, self._tier)
        self.book.apply(fill, now, order_id)
        self._conn.execute(
            "UPDATE orders SET status='filled', filled_qty=?, avg_fill=? WHERE id=?",
            (qty, fill.price, order_id),
        )
        self._conn.commit()
        self._snapshot_equity(now)
        self.say("fill", f"FILLED {side} {qty:g} {symbol} @ {fill.price:.2f} "
                         f"(cost ${fill.cost:.2f})")

    def _flatten(self) -> None:
        """Close everything. can_open=False, so only reducing orders pass."""
        for symbol, pos in list(self.book.positions.items()):
            if abs(pos.qty) < 1e-9:
                continue
            self._submit(
                {"symbol": symbol, "side": "sell" if pos.qty > 0 else "buy",
                 "qty": abs(pos.qty), "reason": "session flatten"},
                can_open=False,
            )

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
        lines = [
            f"## {minutes_left} minutes left to open positions",
            "After that you will be flattened automatically.",
            "",
            f"Cash ${self.book.cash:,.2f} | Equity ${eq:,.2f} | "
            f"P&L ${pnl:+,.2f} ({pnl / self._cfg.capital * 100:+.2f}%)",
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
        else:
            lines.append("You have no open positions.")
        lines.append("")

        lines.append("### Prices now")
        for sym in self._symbols:
            q = self._quotes.get(sym)
            if q:
                lines.append(f"- {sym}: {q.last:.2f}")
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
        self._conn.commit()

        label = {"plan": "Asking the model for a plan",
                 "tick": "Asking the model what to do",
                 "review": "Asking the model to review the session"}[phase]
        act = self.say("model", label + "…", pending=True)

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

    def _summary(self) -> str:
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

        lines = [
            f"Final P&L: ${pnl:+,.2f} ({pnl / self._cfg.capital * 100:+.2f}%)",
            f"Modeled trading costs: ${costs:,.2f}",
            f"Fills: {len(fills)} | Rejected orders: {rejects}",
        ]
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


def _config_json(cfg: SessionConfig) -> dict:
    return {
        "duration_minutes": cfg.duration_minutes, "capital": cfg.capital,
        "symbols": list(cfg.symbols), "policy_tick_minutes": cfg.policy_tick_minutes,
        "max_position_pct": cfg.max_position_pct,
        "max_concurrent_positions": cfg.max_concurrent_positions,
        "session_loss_limit_pct": cfg.session_loss_limit_pct,
        "model": cfg.model, "effort": cfg.effort,
        "research": cfg.research.value, "blinding": cfg.blinding.value,
        "notes": cfg.notes,
    }
