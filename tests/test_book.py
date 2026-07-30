"""Book, fill model, and risk checks.

The risk tests matter most: every one asserts a rejection actually fires.
"""

from __future__ import annotations

import pytest

from thepit.core.types import FeedTier, Quote
from thepit.store import db
from thepit.trading.book import (
    Book, Fill, Limits, check, round_trip_cost_bp, simulate_fill,
)

NOW = 1_800_000_000_000


def q(symbol="AAPL", last=100.0, bid=None, ask=None, age_s=0):
    return Quote(symbol, NOW, last, "test", NOW - int(age_s * 1000), bid=bid, ask=ask)


@pytest.fixture
def book(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    c.execute("INSERT INTO sessions (id,created_ms,status,config,capital,cash) "
              "VALUES (1,?,'running','{}',10000,10000)", (NOW,))
    c.commit()
    b = Book(c, 1, 10_000.0)
    yield b
    c.close()


def fill(book, symbol, side, qty, price):
    """Book a fill through a real order row.

    fills.order_id has a foreign key to orders, deliberately: a fill with no
    order is a fill from nowhere. Tests have to go through the same door.
    """
    cur = book._conn.execute(
        "INSERT INTO orders (session_id,ts_ms,symbol,side,qty,status) "
        "VALUES (1,?,?,?,?,'filled')", (NOW, symbol, side, qty))
    book.apply(Fill(symbol, side, qty, price, price, 0.0, "bars"), NOW,
               int(cur.lastrowid))


# -- fill model --------------------------------------------------------------


def test_buy_without_a_book_pays_above_last():
    """No bid/ask means a spread must be ASSUMED. Assuming zero is how every
    fast strategy looks profitable on paper."""
    f = simulate_fill("buy", 10, q(last=100.0), FeedTier.BARS)
    assert f.price > 100.0
    assert f.cost > 0


def test_sell_without_a_book_receives_below_last():
    f = simulate_fill("sell", 10, q(last=100.0), FeedTier.BARS)
    assert f.price < 100.0


def test_round_trip_is_never_free():
    buy = simulate_fill("buy", 10, q(last=100.0), FeedTier.BARS)
    sell = simulate_fill("sell", 10, q(last=100.0), FeedTier.BARS)
    assert sell.price < buy.price, "a round trip at an unchanged price made money"


def test_with_a_book_it_crosses_the_spread_not_the_mid():
    quote = q(last=100.0, bid=99.9, ask=100.1)
    buy = simulate_fill("buy", 10, quote, FeedTier.QUOTES)
    assert buy.price >= 100.1, "filled inside the spread"


def test_fill_records_its_data_tier():
    """A bar-derived and a quote-derived fill are not the same measurement and
    must never be averaged."""
    assert simulate_fill("buy", 1, q(), FeedTier.BARS).tier == "bars"
    assert simulate_fill("buy", 1, q(bid=99, ask=101), FeedTier.QUOTES).tier == "quotes"


def test_cost_is_higher_without_a_real_book():
    assert round_trip_cost_bp(FeedTier.BARS, False) > \
           round_trip_cost_bp(FeedTier.QUOTES, True)


# -- position accounting -----------------------------------------------------


def test_buy_then_sell_realizes_profit(book):
    fill(book, "AAPL", "buy", 10, 100.0)
    assert book.positions["AAPL"].qty == 10
    assert book.cash == pytest.approx(9000.0)

    fill(book, "AAPL", "sell", 10, 110.0)
    assert book.positions["AAPL"].qty == 0
    assert book.positions["AAPL"].realized == pytest.approx(100.0)
    assert book.cash == pytest.approx(10_100.0)


def test_averaging_up_weights_the_cost_basis(book):
    fill(book, "AAPL", "buy", 10, 100.0)
    fill(book, "AAPL", "buy", 10, 120.0)
    assert book.positions["AAPL"].avg_price == pytest.approx(110.0)


def test_partial_close_realizes_only_the_closed_part(book):
    fill(book, "AAPL", "buy", 10, 100.0)
    fill(book, "AAPL", "sell", 4, 110.0)
    pos = book.positions["AAPL"]
    assert pos.qty == 6
    assert pos.realized == pytest.approx(40.0)
    assert pos.avg_price == pytest.approx(100.0), "cost basis moved on a partial close"


def test_equity_includes_unrealized(book):
    fill(book, "AAPL", "buy", 10, 100.0)
    assert book.equity({"AAPL": q(last=120.0)}) == pytest.approx(10_200.0)


# -- risk: every one of these must actually reject ---------------------------


def _check(book, **over):
    kw = dict(
        side="buy", symbol="AAPL", qty=10, book=book, quote=q(),
        limits=Limits(), equity=10_000, starting_capital=10_000,
        now_ms=NOW, can_open=True,
    )
    kw.update(over)
    return check(**kw)


def test_accepts_a_reasonable_order(book):
    assert _check(book).ok


def test_kill_switch_beats_everything(book):
    assert not _check(book, killed=True).ok


def test_rejects_opening_when_halted(book):
    assert not _check(book, halted=True).ok


def test_rejects_missing_quote(book):
    assert not _check(book, quote=None).ok


def test_rejects_stale_quote(book):
    """Fails closed. Trading on a stale price is how you discover the feed died
    twenty minutes ago."""
    v = _check(book, quote=q(age_s=600))
    assert not v.ok and "too old" in v.reason


def test_rejects_oversized_position(book):
    v = _check(book, qty=100)   # $10,000 notional vs a 20% cap
    assert not v.ok and "max position" in v.reason


def test_rejects_unaffordable_order(book):
    book.cash = 100.0
    v = _check(book, qty=50)
    assert not v.ok


def test_rejects_shorting_by_default(book):
    v = _check(book, side="sell", qty=5)
    assert not v.ok and "hort" in v.reason


def test_rejects_when_loss_limit_breached(book):
    v = _check(book, equity=9_700)   # down 3% against a 2% limit
    assert not v.ok and "loss limit" in v.reason


def test_rejects_opening_after_the_clock(book):
    v = _check(book, can_open=False)
    assert not v.ok and "new positions" in v.reason


def test_rejects_too_many_concurrent_positions(book):
    for i, sym in enumerate(["A", "B", "C"]):
        fill(book, sym, "buy", 1, 10.0)
    v = _check(book, limits=Limits(max_concurrent=3))
    assert not v.ok and "open positions" in v.reason


def test_reducing_is_allowed_past_the_clock_and_the_loss_limit(book):
    """A risk control that prevents de-risking is not a risk control."""
    fill(book, "AAPL", "buy", 50, 100.0)
    v = _check(book, side="sell", qty=50, can_open=False, equity=9_000)
    assert v.ok, f"could not close a position: {v.reason}"


def test_reducing_is_allowed_while_halted(book):
    """The bug this pins: `halted` sat above the de-risking bypass, so the loss
    limit locked the losing position open. Every closing order for the rest of
    the window was rejected as 'session halted' by the control whose entire
    purpose was to stop the bleeding."""
    fill(book, "AAPL", "buy", 50, 100.0)
    v = _check(book, side="sell", qty=50, halted=True)
    assert v.ok, f"a halted session could not close its position: {v.reason}"


def test_the_kill_switch_still_blocks_even_a_reducing_order(book):
    """It is the brake. It stops everything, including de-risking, and the
    callers that need to unwind afterwards say so out loud instead of being
    quietly exempted."""
    fill(book, "AAPL", "buy", 50, 100.0)
    v = _check(book, side="sell", qty=50, killed=True)
    assert not v.ok and "kill switch" in v.reason


def test_risk_verdict_cannot_carry_a_modified_order():
    """The type has no field for one, so 'never silently resizes' is structural
    rather than a rule to remember."""
    from dataclasses import fields
    from thepit.trading.book import Verdict
    assert {f.name for f in fields(Verdict)} == {"ok", "reason"}
