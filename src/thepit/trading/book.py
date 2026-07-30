"""Paper trading: the book, the fill model, and the risk checks.

Deliberately one small module. These three things are read together constantly
-- a risk check needs position state, a fill changes it, sizing depends on both
-- and splitting them across three files would mean chasing the same logic
through three imports for no benefit at this size.

The fill model is **pessimistic on purpose**. Its job is not accuracy, which is
unreachable without a real book. Its job is to be wrong in a bounded, documented
direction, so a strategy that survives it has margin against reality.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from thepit.core.types import FeedTier, Quote
from thepit.store import db

# Slippage assumed when the feed has no bid/ask.
#
# Yahoo gives no book, so a spread has to be assumed. This was originally 5bp
# each way and that was badly wrong: the real spread on a liquid large cap is
# around 0.3bp, so 5bp was 15-30x reality. Combined with a prompt that told the
# model to skip anything under 10bp, it made trading mathematically irrational
# and the agent correctly sat out an entire session doing nothing.
#
# 1.5bp each way is ~5x the real spread on these names. Still conservative --
# assuming zero is how every fast strategy looks profitable on paper -- but no
# longer a hurdle that forbids trading. Revisit once Alpaca gives a real book
# and this can be measured instead of guessed (issue #11).
ASSUMED_SLIPPAGE_BP = 1.5

# Applied on top of a real crossed spread, for latency and impact. With an LLM
# in the loop the decision-to-order delay is seconds, not milliseconds.
QUOTE_SLIPPAGE_BP = 1.0


class Reject(StrEnum):
    KILLED = "kill switch engaged"
    HALTED = "session halted"
    NO_QUOTE = "no price available for this symbol"
    STALE_QUOTE = "price is too old to trade on"
    BAD_QTY = "quantity must be positive"
    POSITION_CAP = "exceeds max position size"
    CONCURRENT_CAP = "too many open positions"
    INSUFFICIENT_CASH = "not enough cash"
    NO_SHORTING = "shorting is disabled"
    LOSS_LIMIT = "session loss limit breached"
    CLOCK = "past the point where new positions may be opened"


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    qty: float
    avg_price: float
    realized: float = 0.0

    def value(self, price: float) -> float:
        return self.qty * price

    def unrealized(self, price: float) -> float:
        return (price - self.avg_price) * self.qty


@dataclass(frozen=True, slots=True)
class Limits:
    max_position_pct: float = 20.0
    max_concurrent: int = 3
    loss_limit_pct: float = 2.0
    allow_short: bool = False
    max_quote_age_s: float = 120.0


@dataclass(frozen=True, slots=True)
class Verdict:
    """The risk engine's answer. Carries no order.

    That is not stylistic: there is no field here that could hold a modified
    order, so "risk rejects, never silently resizes" is a property of the type
    rather than a rule someone has to remember.
    """

    ok: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    symbol: str
    side: str
    qty: float
    price: float        # what was actually paid
    ref_price: float    # the quote it was priced from
    cost: float         # modeled slippage in dollars
    tier: str


class Book:
    """One session's cash and positions."""

    def __init__(self, conn: sqlite3.Connection, session_id: int, cash: float) -> None:
        self._conn = conn
        self.session_id = session_id
        self.cash = cash
        self.positions: dict[str, Position] = {}

    # -- state ---------------------------------------------------------------

    def load(self) -> None:
        rows = self._conn.execute(
            "SELECT symbol, qty, avg_price, realized FROM positions WHERE session_id=?",
            (self.session_id,),
        ).fetchall()
        self.positions = {
            r["symbol"]: Position(r["symbol"], r["qty"], r["avg_price"], r["realized"])
            for r in rows
        }
        row = self._conn.execute(
            "SELECT cash FROM sessions WHERE id=?", (self.session_id,)
        ).fetchone()
        if row:
            self.cash = row["cash"]

    def equity(self, prices: dict[str, Quote]) -> float:
        """Cash plus positions marked to market.

        A position with no quote is valued at its **cost**, not at zero. Skipping
        it dropped its whole value out of equity, which read as a near-total loss
        and tripped the session loss limit off nothing but a missing tick -- and
        the quote dict is replaced wholesale every five seconds, so a symbol
        going briefly absent is ordinary rather than exotic.

        Valuing at cost is not a mark, it is a refusal to invent one. Callers that
        must know whether the book is really markable ask :meth:`unpriced`.
        """
        total = self.cash
        for sym, pos in self.positions.items():
            q = prices.get(sym)
            total += pos.value(q.last if q else pos.avg_price)
        return total

    def unpriced(self, prices: dict[str, Quote], *, now_ms: int | None = None,
                 max_age_s: float | None = None) -> list[str]:
        """Open symbols that cannot honestly be marked right now.

        Missing, or older than `max_age_s` when a clock is supplied. Any risk
        decision taken while this is non-empty is being taken on a number nobody
        can stand behind.
        """
        out: list[str] = []
        for sym, pos in self.positions.items():
            if abs(pos.qty) < 1e-9:
                continue
            q = prices.get(sym)
            if q is None:
                out.append(sym)
            elif now_ms is not None and max_age_s is not None \
                    and (now_ms - q.received_ms) / 1000 > max_age_s:
                out.append(sym)
        return out

    def open_count(self) -> int:
        return sum(1 for p in self.positions.values() if abs(p.qty) > 1e-9)

    # -- fills ---------------------------------------------------------------

    def apply(self, fill: Fill, ts_ms: int, order_id: int) -> None:
        """Book a fill. Updates cash, position, and realized P&L."""
        signed = fill.qty if fill.side == "buy" else -fill.qty
        prior = self.positions.get(fill.symbol)
        cash_delta = -signed * fill.price

        if prior is None or abs(prior.qty) < 1e-9:
            new = Position(fill.symbol, signed, fill.price,
                           prior.realized if prior else 0.0)
        elif (prior.qty > 0) == (signed > 0):
            # Adding to the position: weighted average cost.
            total = prior.qty + signed
            avg = (prior.avg_price * prior.qty + fill.price * signed) / total
            new = Position(fill.symbol, total, avg, prior.realized)
        else:
            # Reducing or reversing. Realize P&L on the closed portion only.
            closed = min(abs(signed), abs(prior.qty))
            direction = 1 if prior.qty > 0 else -1
            realized = (fill.price - prior.avg_price) * closed * direction
            remaining = prior.qty + signed
            avg = fill.price if abs(remaining) > 1e-9 and (
                (remaining > 0) != (prior.qty > 0)
            ) else prior.avg_price
            new = Position(fill.symbol, remaining, avg, prior.realized + realized)

        self.positions[fill.symbol] = new
        self.cash += cash_delta

        # BEGIN IMMEDIATE, not `with self._conn:`. The connections this project
        # opens set isolation_level=None, which turns the connection context
        # manager into a no-op: each of the three statements below committed on
        # its own, so a crash between them left cash and positions describing
        # different books, with the fill row possibly absent from both.
        with db.immediate(self._conn):
            self._conn.execute(
                "INSERT INTO fills (order_id,session_id,ts_ms,symbol,side,qty,price,"
                "ref_price,cost,sim_tier) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (order_id, self.session_id, ts_ms, fill.symbol, fill.side, fill.qty,
                 fill.price, fill.ref_price, fill.cost, fill.tier),
            )
            self._conn.execute(
                "INSERT INTO positions (session_id,symbol,qty,avg_price,realized) "
                "VALUES (?,?,?,?,?) ON CONFLICT(session_id,symbol) DO UPDATE SET "
                "qty=excluded.qty, avg_price=excluded.avg_price, "
                "realized=excluded.realized",
                (self.session_id, new.symbol, new.qty, new.avg_price, new.realized),
            )
            self._conn.execute(
                "UPDATE sessions SET cash=? WHERE id=?", (self.cash, self.session_id)
            )


def simulate_fill(side: str, qty: float, quote: Quote, tier: FeedTier) -> Fill:
    """Price a market order pessimistically.

    With a real book: cross it -- buy at the ask, sell at the bid -- then add
    slippage for latency. Never fill at the mid, and never at last trade.

    Without a book: assume a spread. It has to be assumed rather than omitted,
    because assuming zero makes every fast strategy look profitable, and that is
    the single most common way a paper result lies to you.

    What this does NOT model, and cannot at this data tier: queue position,
    adverse selection (your resting orders filling exactly when the market is
    about to move against you), market impact beyond the constant, partial
    fills, borrow cost on shorts, and halts. All of those are unmodeled
    NEGATIVES -- reality is worse than this, not better.
    """
    if tier is FeedTier.QUOTES and quote.has_book:
        base = quote.ask if side == "buy" else quote.bid
        assert base is not None
        slip_bp = QUOTE_SLIPPAGE_BP
    else:
        base = quote.last
        slip_bp = ASSUMED_SLIPPAGE_BP

    direction = 1 if side == "buy" else -1
    price = base * (1 + direction * slip_bp / 10_000)
    cost = abs(price - quote.last) * qty

    return Fill(
        symbol=quote.symbol, side=side, qty=qty, price=price,
        ref_price=quote.last, cost=cost, tier=tier.value,
    )


def round_trip_cost_bp(tier: FeedTier, has_book: bool) -> float:
    """What a full round trip costs, for the prompt.

    This is the number the agent needs most: without it, nothing tells a model
    that a 5bp move is not worth capturing.
    """
    one_way = QUOTE_SLIPPAGE_BP if (tier is FeedTier.QUOTES and has_book) \
        else ASSUMED_SLIPPAGE_BP
    return one_way * 2


def check(
    *,
    side: str,
    symbol: str,
    qty: float,
    book: Book,
    quote: Quote | None,
    limits: Limits,
    equity: float,
    starting_capital: float,
    now_ms: int,
    can_open: bool,
    killed: bool = False,
    halted: bool = False,
) -> Verdict:
    """Accept or reject a proposed order. Pure: no I/O, no mutation.

    Checks run cheapest-and-most-absolute first, so a killed system rejects fast
    and the reason for any rejection is deterministic.

    Reducing an existing position is always allowed past the size, clock and
    halt checks. A risk control that prevents de-risking is not a risk control --
    and `halted` used to sit above that bypass, which meant the session loss
    limit locked the losing position open: every closing order for the rest of
    the window was rejected as "session halted" by the very control whose job was
    to stop the bleeding.

    The kill switch stays absolute. It is the brake, it is meant to stop
    everything including this, and the callers that need to unwind a position
    afterwards handle it explicitly rather than being quietly exempted here.
    """
    if killed:
        return Verdict(False, Reject.KILLED)
    if qty <= 0:
        return Verdict(False, Reject.BAD_QTY)
    if quote is None:
        return Verdict(False, Reject.NO_QUOTE)

    age_s = (now_ms - quote.received_ms) / 1000
    if age_s > limits.max_quote_age_s:
        # Fails closed. Trading on a stale price is how you discover the feed
        # died twenty minutes ago.
        return Verdict(False, f"{Reject.STALE_QUOTE} ({age_s:.0f}s)")

    current = book.positions.get(symbol)
    current_qty = current.qty if current else 0.0
    signed = qty if side == "buy" else -qty
    projected = current_qty + signed

    reducing = abs(projected) < abs(current_qty)

    if not reducing:
        if halted:
            return Verdict(False, Reject.HALTED)
        if not can_open:
            return Verdict(False, Reject.CLOCK)

        loss_pct = (starting_capital - equity) / starting_capital * 100
        if loss_pct >= limits.loss_limit_pct:
            return Verdict(False, f"{Reject.LOSS_LIMIT} (down {loss_pct:.1f}%)")

        if projected < 0 and not limits.allow_short:
            return Verdict(False, Reject.NO_SHORTING)

        notional = abs(projected) * quote.last
        cap = equity * limits.max_position_pct / 100
        if notional > cap:
            return Verdict(
                False,
                f"{Reject.POSITION_CAP}: ${notional:,.0f} > ${cap:,.0f} "
                f"({limits.max_position_pct:.0f}% of ${equity:,.0f})",
            )

        if abs(current_qty) < 1e-9 and book.open_count() >= limits.max_concurrent:
            return Verdict(False,
                           f"{Reject.CONCURRENT_CAP} ({limits.max_concurrent})")

    if side == "buy":
        # Buffer for the slippage the fill model will add. Checking against the
        # raw quote would let an order pass and then fail to be affordable.
        needed = qty * quote.last * 1.01
        if needed > book.cash:
            return Verdict(
                False,
                f"{Reject.INSUFFICIENT_CASH}: needs ${needed:,.0f}, "
                f"has ${book.cash:,.0f}",
            )

    return Verdict(True)
