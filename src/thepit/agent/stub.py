"""A deterministic agent that emits the same JSON an LLM would.

Two jobs, and the second is the important one.

**It proves the pipeline.** Sessions can run, fill, flatten and be watched
without depending on the CLI being logged in, an API being reachable, or a rate
window being open.

**It is the control group.** The honest benchmark for "did the LLM add value" is
not buy-and-hold -- that flatters or damns arbitrarily depending on the regime.
It is *this*: the identical execution engine, the identical risk layer, the
identical fill model, with the model replaced by a rule so simple it cannot be
said to be reasoning. If the LLM cannot beat a short-window momentum rule, the
Python did the work.

The rule is deliberately unimpressive: buy the strongest recent mover, exit on a
fixed stop or target. It is not meant to be good. It is meant to be a floor.
"""

from __future__ import annotations

import json
import sqlite3

from thepit.core.types import Quote
from thepit.store.repos import BarsRepo

# Enter only when the recent move is larger than the round trip costs, with room
# to spare. Without this the stub churns and loses to fees, which is exactly the
# failure the cost line in the prompt is meant to stop an LLM making.
MIN_MOVE_BP = 25.0
STOP_BP = 30.0
TARGET_BP = 60.0


def plan(symbols: list[str]) -> str:
    return (
        "Deterministic baseline (no model).\n\n"
        f"Rule: each tick, rank {', '.join(symbols)} by their last 5 minutes of "
        f"return. If the leader has moved more than {MIN_MOVE_BP:.0f}bp, buy it "
        f"with a {STOP_BP:.0f}bp stop and a {TARGET_BP:.0f}bp target attached. "
        "One position at a time. Flat at the end.\n\n"
        "This is a floor, not a strategy. Its purpose is to be beaten."
    )


def decide(
    conn: sqlite3.Connection,
    symbols: list[str],
    quotes: dict[str, Quote],
    positions: dict[str, float],
    budget: float = 1500.0,
) -> str:
    """Return the same JSON shape an LLM tick returns.

    Exits are stated as levels rather than executed here. The fast loop enforces
    them every few seconds, which is both better than this rule checking them
    once a tick and necessary for the comparison to mean anything: the control
    group has to run through the identical execution path, not a slower one.
    """
    orders: list[dict] = []

    holding = any(abs(q) > 1e-9 for q in positions.values())
    if not holding:
        ranked = sorted(
            ((s, _return_bp(conn, s, 5)) for s in symbols),
            key=lambda x: x[1] or -1e9, reverse=True,
        )
        if ranked and ranked[0][1] and ranked[0][1] >= MIN_MOVE_BP:
            symbol, move = ranked[0]
            quote = quotes.get(symbol)
            if quote:
                # Sized from the caller's budget; the risk layer holds the real
                # ceiling, and this must sit under it or every tick produces a
                # rejection instead of a trade.
                # Fractional. Whole shares are unbuyable on a small account:
                # int(4 / 194) is 0, and the session silently never trades.
                qty = round(budget / quote.last, 6)
                orders.append({
                    "symbol": symbol, "side": "buy", "qty": qty, "conviction": 4,
                    "reason": f"strongest 5m move, {move:+.0f}bp",
                    "stop_bp": STOP_BP, "target_bp": TARGET_BP,
                })

    return json.dumps({
        "assessment": f"baseline rule; {len(orders)} order(s)",
        "orders": orders,
    })


def _return_bp(conn: sqlite3.Connection, symbol: str, minutes: int) -> float | None:
    bars = BarsRepo(conn).latest(symbol, "1m", limit=minutes + 1)
    if len(bars) <= minutes:
        return None
    first, last = bars[0].c, bars[-1].c
    return (last - first) / first * 10_000 if first else None
