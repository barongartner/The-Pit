"""The fast loop, end to end against a real database and a real book.

These run the actual order path -- risk check, fill model, ledger -- with the
model replaced by hand-written tick JSON. That is deliberate: the value of this
feature is that a stop *executes*, and a test that stubbed out `_submit` would
pass while the position stayed open.

Time is a `FixedClock`, so a five-minute drift is one line and nothing sleeps.
"""

from __future__ import annotations

import pytest

from thepit.core.clock import FixedClock
from thepit.core.types import Bar, FeedTier, Quote
from thepit.session.config import SessionConfig
from thepit.session.runner import SessionRunner
from thepit.store import db
from thepit.store.repos import BarsRepo

NOW = 1_800_000_000_000
STOP_OPENING = NOW + 20 * 60_000


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    repo = BarsRepo(c)
    for sym, base in (("AAPL", 100.0), ("TSLA", 300.0)):
        repo.upsert_many(
            [Bar(sym, "1m", NOW - (60 - i) * 60_000, base, base + 0.5, base - 0.5,
                 base, 1000, "test") for i in range(60)],
            ingested_ms=NOW,
        )
    yield c
    c.close()


def quote(symbol: str, last: float, ts: int = NOW) -> Quote:
    return Quote(symbol=symbol, ts_ms=ts, last=last, source="test", received_ms=ts)


@pytest.fixture
def runner(conn):
    clock = FixedClock(NOW)
    cfg = SessionConfig(
        duration_minutes=30, capital=1_000.0, symbols=("AAPL", "TSLA"),
        policy_tick_minutes=5, max_position_pct=100.0, max_concurrent_positions=2,
        session_loss_limit_pct=60.0,
    )
    assert cfg.validate() == []
    r = SessionRunner(
        conn, clock, cfg, ["AAPL", "TSLA"],
        quotes={"AAPL": quote("AAPL", 100.0), "TSLA": quote("TSLA", 300.0)},
        tier=FeedTier.BARS,
    )
    r.create()
    return r


def mark(runner: SessionRunner, **prices: float) -> None:
    """Move the tape and the clock with it, so nothing looks stale."""
    now = runner._clock.now_ms()  # noqa: SLF001 - a test may drive the clock
    runner.update_quotes({s: quote(s, p, now) for s, p in prices.items()})


def advance(runner: SessionRunner, minutes: float, **prices: float) -> None:
    runner._clock.advance(int(minutes * 60_000))  # noqa: SLF001
    mark(runner, **prices)


def buy(runner: SessionRunner, symbol="AAPL", qty=5.0, **levels) -> None:
    runner._place({"symbol": symbol, "side": "buy", "qty": qty,  # noqa: SLF001
                   "reason": "test", **levels}, stop_opening_ms=STOP_OPENING)


def position(runner: SessionRunner, symbol="AAPL") -> float:
    pos = runner.book.positions.get(symbol)
    return pos.qty if pos else 0.0


def rejections(runner: SessionRunner) -> list[str]:
    return [r["reject_reason"] for r in runner._conn.execute(  # noqa: SLF001
        "SELECT reject_reason FROM orders WHERE status='rejected' ORDER BY id")]


# -- a stop is required, before the position exists ---------------------------


def test_an_opening_order_without_a_stop_is_rejected(runner):
    """The documented failure: a position existed with its stop living in prose
    in a reason field, and nothing could act on it."""
    buy(runner)
    assert position(runner) == 0.0
    assert any("must carry a stop" in r for r in rejections(runner))


def test_an_unusable_stop_is_caught_before_the_fill(runner):
    buy(runner, stop=101.0)          # above the entry, for a long
    assert position(runner) == 0.0
    assert any("not below" in r for r in rejections(runner))


def test_a_closing_order_needs_no_stop(runner):
    buy(runner, stop_bp=30)
    assert position(runner) == pytest.approx(5.0)
    runner._place({"symbol": "AAPL", "side": "sell", "qty": 5.0},  # noqa: SLF001
                  stop_opening_ms=STOP_OPENING)
    assert position(runner) == pytest.approx(0.0)


def test_a_fill_gets_a_plan_measured_from_the_price_actually_paid(runner):
    buy(runner, stop_bp=30)
    plan = runner.fast.plan("AAPL")
    fill = runner._conn.execute(  # noqa: SLF001
        "SELECT price FROM fills ORDER BY id DESC LIMIT 1").fetchone()["price"]
    assert plan.entry_price == pytest.approx(fill)
    assert plan.stop_price == pytest.approx(fill * (1 - 0.0030))


# -- enforcement between ticks ----------------------------------------------


def test_a_stop_closes_the_position_without_asking_the_model(runner):
    buy(runner, stop_bp=30, target_bp=60)
    advance(runner, 0.5, AAPL=99.5, TSLA=300.0)     # through the stop

    assert runner.fast.step() == ["stop:AAPL"]
    assert position(runner) == pytest.approx(0.0)
    assert runner.fast.plan("AAPL") is None


def test_a_target_closes_the_position(runner):
    buy(runner, stop_bp=30, target_bp=60)
    advance(runner, 0.5, AAPL=100.8, TSLA=300.0)

    assert runner.fast.step() == ["target:AAPL"]
    assert position(runner) == pytest.approx(0.0)


def test_a_stop_five_minutes_from_the_next_tick_still_fires_now(runner):
    """This is the whole point of the second loop. On a 5-minute policy tick the
    old code checked this stop up to five minutes late, and session 4 lost $6
    with both positions drifting past their stated stops."""
    buy(runner, stop_bp=30)
    for i in range(1, 13):                          # 12 x 5s = one minute
        runner._clock.advance(5_000)                # noqa: SLF001
        mark(runner, AAPL=100.0 - i * 0.05, TSLA=300.0)
        if runner.fast.step():
            break
    # 30bp of 100 is 0.30, so it must have fired inside the first 40 seconds --
    # long before a policy tick would have looked.
    assert position(runner) == pytest.approx(0.0)
    assert runner._clock.now_ms() - NOW < 60_000    # noqa: SLF001


def test_the_time_stop_flattens_a_position_that_never_worked(runner):
    buy(runner, stop_bp=50, time_stop_minutes=4)
    advance(runner, 3, AAPL=100.05, TSLA=300.0)
    assert runner.fast.step() == []
    advance(runner, 1.5, AAPL=100.05, TSLA=300.0)
    assert runner.fast.step() == ["time_stop:AAPL"]
    assert position(runner) == pytest.approx(0.0)


def test_a_trailing_stop_ratchets_up_and_then_fires(runner):
    buy(runner, stop_bp=50, trail_bp=40)
    entry = runner.fast.plan("AAPL").entry_price

    advance(runner, 0.2, AAPL=101.0, TSLA=300.0)
    assert runner.fast.step() == ["trail:AAPL"]
    trailed = runner.fast.plan("AAPL").stop_price
    assert trailed == pytest.approx(101.0 * (1 - 0.0040))
    assert trailed > entry                          # now stopping out in profit

    advance(runner, 0.2, AAPL=100.5, TSLA=300.0)
    assert runner.fast.step() == ["stop:AAPL"]
    assert position(runner) == pytest.approx(0.0)


def test_a_dead_feed_stops_enforcement_loudly_rather_than_acting_on_a_stale_price(
    runner,
):
    buy(runner, stop_bp=30)
    runner._clock.advance(10 * 60_000)              # noqa: SLF001 - quotes go stale
    assert runner.fast.step() == []
    assert position(runner) == pytest.approx(5.0)

    messages = [r["message"] for r in runner._conn.execute(  # noqa: SLF001
        "SELECT message FROM activity WHERE kind='error'")]
    assert any("cannot be enforced against a dead feed" in m for m in messages)


def test_a_plan_closes_itself_when_the_model_exits_the_position(runner):
    buy(runner, stop_bp=30)
    runner._place({"symbol": "AAPL", "side": "sell", "qty": 5.0},  # noqa: SLF001
                  stop_opening_ms=STOP_OPENING)
    runner.fast.step()
    assert runner.fast.plan("AAPL") is None


# -- armed entries -----------------------------------------------------------


def test_an_entry_below_the_market_waits_instead_of_chasing(runner):
    """The recorded failure: planned TSLA at 303.50, asked again five minutes
    later at 304.82, bought there anyway. An armed level cannot be chased."""
    buy(runner, symbol="TSLA", qty=1.0, trigger=297.0, stop_bp=40)
    assert position(runner, "TSLA") == 0.0
    armed = runner.fast.armed()
    assert len(armed) == 1
    assert armed[0].direction == "at_or_below"

    advance(runner, 1, AAPL=100.0, TSLA=298.0)      # close, but not there
    assert runner.fast.step() == []
    assert position(runner, "TSLA") == 0.0

    advance(runner, 1, AAPL=100.0, TSLA=296.5)      # prints
    assert runner.fast.step() == ["entry:TSLA"]
    assert position(runner, "TSLA") == pytest.approx(1.0)
    assert runner.fast.armed() == []


def test_a_triggered_entry_gets_its_exit_levels_attached(runner):
    buy(runner, symbol="TSLA", qty=1.0, trigger=297.0, stop_bp=40, target_bp=80)
    advance(runner, 1, AAPL=100.0, TSLA=296.0)
    runner.fast.step()

    plan = runner.fast.plan("TSLA")
    assert plan is not None
    assert plan.stop_price == pytest.approx(plan.entry_price * (1 - 0.0040))
    assert plan.target_price == pytest.approx(plan.entry_price * (1 + 0.0080))


def test_a_trigger_already_satisfied_fills_immediately(runner):
    buy(runner, stop_bp=30, trigger=100.0)
    assert position(runner) == pytest.approx(5.0)
    assert runner.fast.armed() == []


def test_an_armed_entry_expires_unfilled(runner):
    buy(runner, symbol="TSLA", qty=1.0, trigger=290.0, stop_bp=40, valid_minutes=2)
    advance(runner, 3, AAPL=100.0, TSLA=299.0)
    assert runner.fast.step() == ["expired:TSLA"]
    assert runner.fast.armed() == []
    assert position(runner, "TSLA") == 0.0


def test_an_armed_entry_is_validated_when_it_is_armed_not_when_it_fills(runner):
    buy(runner, symbol="TSLA", qty=1.0, trigger=297.0, stop_bp=1)
    assert runner.fast.armed() == []
    assert any("trading cost" in r for r in rejections(runner))


def test_the_flatten_cancels_armed_entries(runner):
    """An entry triggering into the flatten would open a position seconds before
    the session is required to be flat."""
    buy(runner, symbol="TSLA", qty=1.0, trigger=297.0, stop_bp=40)
    runner._flatten()  # noqa: SLF001
    assert runner.fast.armed() == []


# -- revising levels without trading ----------------------------------------


def test_exits_can_be_tightened_without_a_round_trip(runner):
    buy(runner, stop_bp=50, target_bp=100)
    runner._apply_decision(  # noqa: SLF001
        {"exits": [{"symbol": "AAPL", "stop_bp": 20}]}, STOP_OPENING)
    plan = runner.fast.plan("AAPL")
    assert plan.stop_price == pytest.approx(plan.entry_price * (1 - 0.0020))
    # The target it did not mention survives.
    assert plan.target_price == pytest.approx(plan.entry_price * (1 + 0.0100))


def test_an_amendment_naming_only_a_target_cannot_delete_the_stop(runner):
    buy(runner, stop_bp=50)
    before = runner.fast.plan("AAPL").stop_price
    runner._apply_decision(  # noqa: SLF001
        {"exits": [{"symbol": "AAPL", "target_bp": 200}]}, STOP_OPENING)
    plan = runner.fast.plan("AAPL")
    assert plan.stop_price == pytest.approx(before)
    assert plan.target_price == pytest.approx(plan.entry_price * (1 + 0.0200))


def test_amending_a_position_that_does_not_exist_is_reported(runner):
    runner._apply_decision(  # noqa: SLF001
        {"exits": [{"symbol": "AAPL", "stop_bp": 20}]}, STOP_OPENING)
    messages = [r["message"] for r in runner._conn.execute(  # noqa: SLF001
        "SELECT message FROM activity WHERE kind='error'")]
    assert any("no active exit plan" in m for m in messages)


def test_cancel_pending_withdraws_an_armed_entry(runner):
    buy(runner, symbol="TSLA", qty=1.0, trigger=297.0, stop_bp=40)
    runner._apply_decision({"cancel_pending": ["TSLA"]}, STOP_OPENING)  # noqa: SLF001
    assert runner.fast.armed() == []
    advance(runner, 1, AAPL=100.0, TSLA=296.0)
    assert runner.fast.step() == []
    assert position(runner, "TSLA") == 0.0


# -- what the model is told --------------------------------------------------


def test_the_tick_prompt_shows_the_levels_being_enforced(runner):
    buy(runner, stop_bp=30, target_bp=60)
    text = runner._tick_prompt(10)  # noqa: SLF001
    assert "levels being enforced" in text
    assert "stop" in text


def test_the_tick_prompt_lists_armed_entries(runner):
    buy(runner, symbol="TSLA", qty=1.0, trigger=297.0, stop_bp=40)
    text = runner._tick_prompt(10)  # noqa: SLF001
    assert "Armed entries" in text
    assert "297.00" in text


def test_the_tick_schema_documents_the_stop_requirement_and_the_fast_loop():
    from thepit.session.runner import TICK_SCHEMA
    assert "must carry a stop" in TICK_SCHEMA
    assert "enforced in Python" in TICK_SCHEMA
    assert "trigger" in TICK_SCHEMA


def test_the_baseline_states_its_exits_as_levels_so_the_fast_loop_runs_them(conn):
    """The control group has to go through the identical execution path, or the
    comparison measures the harness rather than the agent."""
    import json

    from thepit.agent import stub

    # A move big enough for the rule to act on, so this asserts against a real
    # order rather than passing vacuously on an empty list. Its own symbol: the
    # fixture's bars are flat, and upserts do not overwrite them.
    BarsRepo(conn).upsert_many(
        [Bar("MSFT", "1m", NOW - (6 - i) * 60_000, 100.0 + i * 0.2,
             100.0 + i * 0.2, 100.0 + i * 0.2, 100.0 + i * 0.2, 1000, "test")
         for i in range(6)],
        ingested_ms=NOW,
    )
    text = stub.decide(conn, ["MSFT"], {"MSFT": quote("MSFT", 101.0)}, {},
                       budget=100.0)
    orders = json.loads(text)["orders"]
    assert orders, "the baseline found no trade on a 100bp move"
    assert all("stop_bp" in o and "target_bp" in o for o in orders)
