"""The fast loop, end to end against a real database and a real book.

These run the actual order path -- risk check, fill model, ledger -- with the
model replaced by hand-written tick JSON. That is deliberate: the value of this
feature is that a stop *executes*, and a test that stubbed out `_submit` would
pass while the position stayed open.

Time is a `FixedClock`, so a five-minute drift is one line and nothing sleeps.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from thepit.core.clock import FixedClock
from thepit.core.types import Bar, FeedTier, Quote
from thepit.engine.killswitch import KillSwitch
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


# -- ending flat, or admitting it did not --------------------------------------


@pytest.fixture
def instant_sleep(runner, monkeypatch):
    """Make asyncio.sleep advance the FixedClock instead of the wall clock.

    Retries here are five seconds apart by design, so a real sleep would make
    these tests slower than the behaviour they check.
    """
    async def sleep(seconds, *a, **kw):
        runner._clock.advance(int(seconds * 1000))  # noqa: SLF001

    monkeypatch.setattr("asyncio.sleep", sleep)
    return runner


async def test_a_stale_feed_does_not_end_a_session_that_still_holds(instant_sleep):
    """The recorded failure: the risk layer fails closed on a quote older than
    120s, the closing order was rejected, and the session was marked 'done' while
    still holding stock. It retried nothing and admitted nothing."""
    runner = instant_sleep
    buy(runner, stop_bp=30)
    runner._clock.advance(10 * 60_000)  # noqa: SLF001 - the feed dies

    stuck = await runner._flatten_until_flat(  # noqa: SLF001
        deadline_ms=runner._clock.now_ms() + 30_000)  # noqa: SLF001
    assert stuck == ["AAPL"]
    stale = [r for r in rejections(runner) if "too old" in r]
    # Exactly one. Retrying into a dead feed must not fill the order table with
    # rejections that read as "the agent wanted something it could not have".
    assert len(stale) == 1

    runner._finish(stuck)  # noqa: SLF001
    row = runner._conn.execute(  # noqa: SLF001
        "SELECT status, halt_reason FROM sessions WHERE id=?",
        (runner.session_id,)).fetchone()
    assert row["status"] == "halted"
    assert "still holding AAPL" in row["halt_reason"]


async def test_the_flatten_retries_until_the_feed_comes_back(runner, monkeypatch):
    buy(runner, stop_bp=30)
    runner._clock.advance(10 * 60_000)  # noqa: SLF001 - the feed dies

    retries = 0

    async def sleep(seconds, *a, **kw):
        nonlocal retries
        retries += 1
        runner._clock.advance(int(seconds * 1000))  # noqa: SLF001
        mark(runner, AAPL=100.0, TSLA=300.0)        # the poller catches up

    monkeypatch.setattr("asyncio.sleep", sleep)
    stuck = await runner._flatten_until_flat(  # noqa: SLF001
        deadline_ms=runner._clock.now_ms() + 60_000)  # noqa: SLF001

    assert retries == 1, "one retry was enough once the price refreshed"
    assert stuck == []
    assert position(runner) == pytest.approx(0.0)


async def test_a_flat_session_is_recorded_as_done(instant_sleep):
    runner = instant_sleep
    buy(runner, stop_bp=30)
    stuck = await runner._flatten_until_flat(  # noqa: SLF001
        deadline_ms=runner._clock.now_ms() + 30_000)  # noqa: SLF001
    runner._finish(stuck)  # noqa: SLF001
    assert stuck == []
    assert runner._conn.execute(  # noqa: SLF001
        "SELECT status FROM sessions WHERE id=?",
        (runner.session_id,)).fetchone()["status"] == "done"


async def test_the_kill_switch_is_not_retried_against(instant_sleep, tmp_path):
    """It rejects every order by design, so spinning would burn the whole window
    and say nothing useful."""
    runner = instant_sleep
    buy(runner, stop_bp=30)

    kill = KillSwitch(tmp_path / "state")
    kill.engage("test")
    runner._kill = kill  # noqa: SLF001

    started = runner._clock.now_ms()  # noqa: SLF001
    stuck = await runner._flatten_until_flat(  # noqa: SLF001
        deadline_ms=started + 10 * 60_000)
    assert stuck == ["AAPL"]
    assert runner._clock.now_ms() == started, "it retried against the kill switch"  # noqa: SLF001
    messages = [r["message"] for r in runner._conn.execute(  # noqa: SLF001
        "SELECT message FROM activity WHERE kind='error'")]
    assert any("Kill switch is engaged" in m for m in messages)


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


# -- the audit's findings, pinned ---------------------------------------------


def test_a_zero_quantity_order_is_rejected_rather_than_killing_the_session(runner):
    """It used to reach `pending_entries` directly, whose CHECK (qty > 0) raised
    out of the tick and failed the whole session. One malformed field in one order
    object. Any key drift ("size", "shares") reads as 0 through the same path."""
    for bad in ({"qty": 0}, {"qty": -5}, {}):
        runner._place({"symbol": "AAPL", "side": "buy", "reason": "t",  # noqa: SLF001
                       "stop_bp": 30, "trigger": 99.0, **bad},
                      stop_opening_ms=STOP_OPENING)
    assert runner.fast.armed() == []
    assert position(runner) == 0.0
    assert len([r for r in rejections(runner) if "positive" in r]) == 3


def test_a_halted_session_can_still_close_its_position(runner):
    """The worst one the audit found, and it predates the fast loop: `halted` sat
    above the de-risking bypass in the risk layer, so the session loss limit
    locked the losing position open and every closing order for the rest of the
    window was rejected as 'session halted'."""
    buy(runner, stop_bp=30)
    runner._halted = "loss limit hit: down 61.00%"  # noqa: SLF001
    assert runner._flatten() == []  # noqa: SLF001
    assert position(runner) == pytest.approx(0.0)


def test_a_stop_that_cannot_execute_keeps_its_plan_active(runner, tmp_path):
    """Marking the plan 'fired' on a rejected order deleted the only thing
    watching a position that was already through its stop."""
    buy(runner, stop_bp=30)
    kill = KillSwitch(tmp_path / "state")
    kill.engage("test")
    runner._kill = kill  # noqa: SLF001

    advance(runner, 0.2, AAPL=99.5, TSLA=300.0)
    assert runner.fast.step() == ["stop-blocked:AAPL"]
    assert position(runner) == pytest.approx(5.0), "still open, as expected"
    assert runner.fast.plan("AAPL") is not None, "enforcement was deleted"

    kill.release()
    assert runner.fast.step() == ["stop:AAPL"]
    assert position(runner) == pytest.approx(0.0)


def test_adding_to_a_position_keeps_the_ratcheted_stop_and_the_blended_entry(runner):
    """A second fill used to overwrite the plan wholesale: the trailing stop
    retreated to wherever the new levels put it and risk was re-measured from the
    latest clip instead of the position's cost."""
    buy(runner, qty=2.0, stop_bp=50, trail_bp=40)
    advance(runner, 0.2, AAPL=102.0, TSLA=300.0)
    runner.fast.step()
    ratcheted = runner.fast.plan("AAPL").stop_price
    assert ratcheted == pytest.approx(102.0 * (1 - 0.0040))

    advance(runner, 0.2, AAPL=101.60, TSLA=300.0)
    buy(runner, qty=2.0, stop_bp=50)          # adds, with a looser stated stop
    plan = runner.fast.plan("AAPL")
    assert plan.stop_price == pytest.approx(ratcheted), "the trailed stop retreated"
    assert plan.high_water == pytest.approx(102.0), "the high-water mark reset"
    assert plan.entry_price == pytest.approx(
        runner.book.positions["AAPL"].avg_price), "risk measured off one clip"


def test_an_amendment_cannot_silently_untrail_a_ratcheted_stop(runner):
    buy(runner, stop_bp=50, trail_bp=40)
    advance(runner, 0.2, AAPL=102.0, TSLA=300.0)
    runner.fast.step()
    ratcheted = runner.fast.plan("AAPL").stop_price

    runner._apply_decision(  # noqa: SLF001
        {"exits": [{"symbol": "AAPL", "stop_bp": 60}]}, STOP_OPENING)
    assert runner.fast.plan("AAPL").stop_price == pytest.approx(ratcheted)


def test_a_target_inside_the_fill_slippage_is_rejected_before_the_buy(runner):
    """It passed validation against the last trade, failed against the fill, and
    the position was bought and unwound for a guaranteed loss. The quote is
    100.00 and a buy fills at 100.015, so both of these are unreachable targets
    dressed as reachable ones."""
    buy(runner, stop_bp=30, target=100.01)      # behind the fill entirely
    buy(runner, stop_bp=30, target=100.03)      # ahead of it, but inside 3bp
    assert position(runner) == 0.0
    reasons = " ".join(rejections(runner))
    assert "not above" in reasons
    assert "round trip" in reasons


def test_an_armed_entry_is_validated_against_its_own_trigger_level(runner):
    """The intended entry is the level, not today's price: levels that are only
    sane relative to the trigger must survive being armed."""
    buy(runner, symbol="TSLA", qty=1.0, trigger=280.0, stop=278.0, target=284.0)
    armed = runner.fast.armed()
    assert len(armed) == 1
    advance(runner, 1, AAPL=100.0, TSLA=279.0)
    assert runner.fast.step() == ["entry:TSLA"]
    plan = runner.fast.plan("TSLA")
    assert plan.stop_price == pytest.approx(278.0)


def test_a_fill_is_written_inside_a_real_transaction(runner, monkeypatch):
    """`with conn:` is a no-op on these connections (isolation_level=None), so
    the fill, the position and the cash each committed separately and a crash
    between them left them describing different books."""
    import contextlib as ctx

    from thepit.trading import book as book_mod

    open_transactions = []
    real = book_mod.db.immediate

    @ctx.contextmanager
    def spy(conn):
        with real(conn) as c:
            open_transactions.append(c.in_transaction)
            yield c

    monkeypatch.setattr(book_mod.db, "immediate", spy)
    buy(runner, stop_bp=30)
    assert open_transactions == [True], "the fill was not written atomically"


def test_the_loss_limit_is_not_evaluated_on_a_price_nobody_can_stand_behind(runner):
    """It used to read equity off an arbitrarily old quote, and `equity()` valued
    an unpriced position at zero — which invents a near-total loss and trips the
    limit off a missing tick."""
    buy(runner, qty=9.0, stop_bp=300)
    runner._cfg = replace(runner._cfg, session_loss_limit_pct=5.0)  # noqa: SLF001

    # A symbol vanishing from the dict is ordinary: the API replaces the whole
    # thing every five seconds from whatever ticks it finds.
    runner.update_quotes({"TSLA": quote("TSLA", 300.0, runner._clock.now_ms())})  # noqa: SLF001
    assert runner._stopped() is False, "halted on a phantom loss"  # noqa: SLF001
    assert runner.book.equity(runner._quotes) == pytest.approx(  # noqa: SLF001
        runner.book.cash + 9.0 * runner.book.positions["AAPL"].avg_price)

    # And a stale price is refused the same way.
    mark(runner, AAPL=100.0, TSLA=300.0)
    runner._clock.advance(10 * 60_000)  # noqa: SLF001
    assert runner._stopped() is False  # noqa: SLF001
    messages = [r["message"] for r in runner._conn.execute(  # noqa: SLF001
        "SELECT message FROM activity WHERE kind='error'")]
    assert any("Cannot mark AAPL" in m for m in messages)


def test_a_real_loss_still_halts_the_session(runner):
    """The guard above must not become a way to never halt."""
    buy(runner, qty=9.0, stop_bp=300)
    runner._cfg = replace(runner._cfg, session_loss_limit_pct=5.0)  # noqa: SLF001
    advance(runner, 0.5, AAPL=93.0, TSLA=300.0)
    assert runner._stopped() is True  # noqa: SLF001
    assert "loss limit" in runner._halted  # noqa: SLF001


def test_the_originating_halt_reason_survives_into_the_sessions_row(runner):
    """A run that hit its loss limit and then could not close was recorded as a
    flatten problem, with the loss limit appearing nowhere in the database."""
    buy(runner, stop_bp=30)
    runner._halted = "loss limit hit: down 6.31%"  # noqa: SLF001
    runner._finish(["AAPL"])  # noqa: SLF001
    reason = runner._conn.execute(  # noqa: SLF001
        "SELECT halt_reason FROM sessions WHERE id=?",
        (runner.session_id,)).fetchone()["halt_reason"]
    assert "loss limit hit" in reason
    assert "still holding AAPL" in reason


def test_a_session_that_halted_early_is_not_recorded_as_done(runner):
    """It ended flat, but it did not run to the clock, and 'done' reads as an
    ordinary finish."""
    runner._halted = "kill switch engaged"  # noqa: SLF001
    runner._finish([])  # noqa: SLF001
    row = runner._conn.execute(  # noqa: SLF001
        "SELECT status, halt_reason FROM sessions WHERE id=?",
        (runner.session_id,)).fetchone()
    assert row["status"] == "halted"
    assert row["halt_reason"] == "kill switch engaged"


async def test_the_review_is_not_told_it_is_flat_while_holding(runner):
    buy(runner, stop_bp=30)
    runner.use_stub = True
    await runner._review(["AAPL"])  # noqa: SLF001
    review = runner._conn.execute(  # noqa: SLF001
        "SELECT review FROM sessions WHERE id=?",
        (runner.session_id,)).fetchone()["review"]
    assert "NOT realised" in review
    assert "STILL OPEN: AAPL" in review


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
