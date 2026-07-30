"""Eval module: P&L, episodes, enforcement, and the exclusions.

The tests that matter most here are not the ones asserting a formula. They are the
ones asserting that a small or dirty sample comes back empty-handed: a standard
deviation from two sessions, a correlation from three episodes, a mean over a
session whose cache disagrees with its fills. That is where a measurement harness
lies to its owner.
"""

from __future__ import annotations

import pytest

from thepit.core.clock import FixedClock
from thepit.core.types import Bar, FeedTier, Quote
from thepit.eval import cohort, enforcement, report, stats
from thepit.eval import pnl as pnl_mod
from thepit.eval.trades import Origin, episodes
from thepit.session.config import SessionConfig
from thepit.session.runner import SessionRunner
from thepit.store import db
from thepit.store.repos import BarsRepo

NOW = 1_800_000_000_000


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    yield c
    c.close()


def tick(conn, symbol, last, ts):
    conn.execute(
        "INSERT INTO ticks (symbol,ts_ms,last,source,received_ms) VALUES (?,?,?,?,?) "
        "ON CONFLICT DO NOTHING", (symbol, ts, last, "test", ts))
    conn.commit()


def bars(conn, symbol, base):
    BarsRepo(conn).upsert_many(
        [Bar(symbol, "1m", NOW - (60 - i) * 60_000, base, base + 0.5, base - 0.5,
             base, 1000, "test") for i in range(60)], ingested_ms=NOW)


def quote(symbol, last, ts=NOW):
    return Quote(symbol=symbol, ts_ms=ts, last=last, source="test", received_ms=ts)


def a_runner(conn, clock, *, capital=1_000.0, symbols=("AAPL",), **cfg_kw):
    for s in symbols:
        bars(conn, s, 100.0)
        tick(conn, s, 100.0, clock.now_ms())
    cfg = SessionConfig(
        duration_minutes=30, capital=capital, symbols=symbols,
        policy_tick_minutes=5, max_position_pct=100.0, max_concurrent_positions=2,
        session_loss_limit_pct=60.0, **cfg_kw)
    assert cfg.validate() == []
    r = SessionRunner(conn, clock, cfg, list(symbols),
                      quotes={s: quote(s, 100.0, clock.now_ms()) for s in symbols},
                      tier=FeedTier.BARS)
    r.create()
    return r


def trade(runner, clock, *, symbol="AAPL", qty=5.0, exit_price=None, **levels):
    """Buy, then move the price and let the fast loop or the flatten close it."""
    runner._place({"symbol": symbol, "side": "buy", "qty": qty,  # noqa: SLF001
                   "reason": "test", **levels},
                  stop_opening_ms=clock.now_ms() + 20 * 60_000)
    if exit_price is not None:
        clock.advance(60_000)
        runner.update_quotes({symbol: quote(symbol, exit_price, clock.now_ms())})
        tick(runner._conn, symbol, exit_price, clock.now_ms())  # noqa: SLF001
        runner.fast.step()


# -- the P&L, which is the whole point ----------------------------------------


def test_the_recorded_disaster_is_not_repeated(conn):
    """An interrupted session holding 8 NVDA reported "-$3,060" by
    `cash - capital` when its real P&L was -$1.97. Two separate ad-hoc queries
    made that mistake. This is the number that must never come back."""
    conn.execute(
        "INSERT INTO sessions (id,created_ms,ends_ms,finished_ms,status,config,"
        "capital,cash) VALUES (1,?,?,?,'halted','{}',3200,138.03)",
        (NOW, NOW, NOW))
    # 8 NVDA bought at 382.5, priced back at 382.5: the loss is the slippage only.
    conn.execute(
        "INSERT INTO orders (id,session_id,ts_ms,symbol,side,qty,status,origin) "
        "VALUES (1,1,?,'NVDA','buy',8,'filled','model')", (NOW - 1000,))
    conn.execute(
        "INSERT INTO fills (order_id,session_id,ts_ms,symbol,side,qty,price,"
        "ref_price,cost,sim_tier) VALUES (1,1,?,'NVDA','buy',8,382.74625,382.5,"
        "1.97,'bars')", (NOW - 1000,))
    conn.execute("UPDATE sessions SET cash = 3200 - 8*382.74625 WHERE id=1")
    tick(conn, "NVDA", 382.5, NOW)

    money = pnl_mod.session_pnl(conn, 1)
    assert money.cash - money.capital == pytest.approx(-3061.97, abs=0.01)
    assert money.pnl == pytest.approx(-1.97, abs=0.01)
    assert money.realised is False, "a position is open, so this is not settled"


def test_a_finished_session_is_marked_at_its_own_clock(conn):
    """Scoring last week's session against this morning's tape invents P&L out of
    the days in between."""
    conn.execute(
        "INSERT INTO sessions (id,created_ms,ends_ms,finished_ms,status,config,"
        "capital,cash) VALUES (1,?,?,?,'halted','{}',1000,500)",
        (NOW, NOW, NOW))
    conn.execute(
        "INSERT INTO orders (id,session_id,ts_ms,symbol,side,qty,status) "
        "VALUES (1,1,?,'AAPL','buy',5,'filled')", (NOW,))
    conn.execute(
        "INSERT INTO fills (order_id,session_id,ts_ms,symbol,side,qty,price,"
        "ref_price,cost,sim_tier) VALUES (1,1,?,'AAPL','buy',5,100,100,0,'bars')",
        (NOW,))
    tick(conn, "AAPL", 100.0, NOW)
    tick(conn, "AAPL", 200.0, NOW + 86_400_000)     # a day later, doubled

    at_its_own_clock = pnl_mod.session_pnl(conn, 1)
    assert at_its_own_clock.marks["AAPL"] == 100.0
    later = pnl_mod.session_pnl(conn, 1, at_ms=NOW + 86_400_000)
    assert later.marks["AAPL"] == 200.0


def test_an_unmarkable_position_is_flagged_not_guessed(conn):
    """Falling back to the entry price reports exactly zero unrealised P&L, which
    hides the entire position."""
    conn.execute(
        "INSERT INTO sessions (id,created_ms,ends_ms,finished_ms,status,config,"
        "capital,cash) VALUES (1,?,?,?,'halted','{}',1000,500)", (NOW, NOW, NOW))
    conn.execute(
        "INSERT INTO orders (id,session_id,ts_ms,symbol,side,qty,status) "
        "VALUES (1,1,?,'AAPL','buy',5,'filled')", (NOW,))
    conn.execute(
        "INSERT INTO fills (order_id,session_id,ts_ms,symbol,side,qty,price,"
        "ref_price,cost,sim_tier) VALUES (1,1,?,'AAPL','buy',5,100,100,0,'bars')",
        (NOW,))
    conn.commit()   # no ticks at all

    money = pnl_mod.session_pnl(conn, 1)
    assert money.unmarkable == ("AAPL",)
    assert money.scorable is False


def test_a_cache_that_disagrees_with_the_fills_is_not_scored(conn):
    """`positions` and `sessions.cash` are caches; fills are the truth. A
    divergence is a flag, not something to average."""
    conn.execute(
        "INSERT INTO sessions (id,created_ms,ends_ms,finished_ms,status,config,"
        "capital,cash) VALUES (1,?,?,?,'done','{}',1000,999)", (NOW, NOW, NOW))
    conn.execute(
        "INSERT INTO orders (id,session_id,ts_ms,symbol,side,qty,status) "
        "VALUES (1,1,?,'AAPL','buy',5,'filled')", (NOW,))
    conn.execute(
        "INSERT INTO fills (order_id,session_id,ts_ms,symbol,side,qty,price,"
        "ref_price,cost,sim_tier) VALUES (1,1,?,'AAPL','buy',5,100,100,0,'bars')",
        (NOW,))
    tick(conn, "AAPL", 100.0, NOW)

    money = pnl_mod.session_pnl(conn, 1)
    assert money.discrepancy != pytest.approx(0.0, abs=0.01)
    assert money.scorable is False
    assert cohort.CASH_MISMATCH in cohort.meta(conn, 1).excluded


# -- episodes ------------------------------------------------------------------


def test_the_episode_fold_reproduces_the_books_own_realised_pnl(conn):
    """The independent check: `positions.realized` is computed by `Book.apply`,
    the episode fold is computed from the fill stream, and they must agree."""
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    trade(runner, clock, stop_bp=100, exit_price=99.0)

    realized = conn.execute(
        "SELECT realized FROM positions WHERE session_id=? AND symbol='AAPL'",
        (runner.session_id,)).fetchone()["realized"]
    eps = episodes(conn, runner.session_id)
    assert len(eps) == 1
    assert eps[0].net == pytest.approx(realized, abs=1e-9)
    assert eps[0].open_at_end is False
    assert eps[0].exit == "stop"


def test_three_legs_are_one_episode(conn):
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock, capital=100_000.0)
    trade(runner, clock, qty=2.0, stop_bp=200)
    trade(runner, clock, qty=3.0, stop_bp=200)
    clock.advance(60_000)
    runner.update_quotes({"AAPL": quote("AAPL", 101.0, clock.now_ms())})
    runner._flatten()  # noqa: SLF001

    eps = episodes(conn, runner.session_id)
    assert len(eps) == 1
    assert eps[0].legs == 3
    assert eps[0].peak_qty == pytest.approx(5.0)
    assert eps[0].exit == "flatten"


def test_an_open_episode_reports_no_pnl_rather_than_its_cash_flow(conn):
    """An unclosed position's cash flow is not P&L: the residual is still worth
    something. Counting it would report a total loss on every open trade."""
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    trade(runner, clock, stop_bp=100)

    ep = episodes(conn, runner.session_id)[0]
    assert ep.open_at_end is True
    assert ep.net == 0.0
    assert ep.won is None
    assert ep.exit == "open"


def test_origin_comes_from_the_column_not_the_prose(conn):
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    trade(runner, clock, stop_bp=100, exit_price=98.5)

    origins = [r["origin"] for r in conn.execute(
        "SELECT origin FROM orders WHERE session_id=? ORDER BY id",
        (runner.session_id,))]
    assert origins == ["model", "fast_loop_stop"]

    # And an unrecognised value is surfaced rather than bucketed as 'model'.
    conn.execute("UPDATE orders SET origin='typo' WHERE id=("
                 "SELECT MAX(id) FROM orders)")
    conn.commit()
    from thepit.eval.trades import origin_of
    row = conn.execute("SELECT origin, reason FROM orders WHERE id=("
                       "SELECT MAX(id) FROM orders)").fetchone()
    assert origin_of(row) is Origin.UNKNOWN


def test_an_armed_entry_is_linked_to_its_order(conn):
    """`pending_entries.order_id` existed and was never written, so attribution
    depended on matching the wording of a reason string."""
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    runner._place({"symbol": "AAPL", "side": "buy", "qty": 5.0,  # noqa: SLF001
                   "reason": "t", "stop_bp": 100, "trigger": 99.0},
                  stop_opening_ms=clock.now_ms() + 20 * 60_000)
    clock.advance(30_000)
    runner.update_quotes({"AAPL": quote("AAPL", 98.9, clock.now_ms())})
    runner.fast.step()

    row = conn.execute(
        "SELECT status, order_id FROM pending_entries WHERE session_id=?",
        (runner.session_id,)).fetchone()
    assert row["status"] == "triggered"
    assert row["order_id"] is not None


# -- enforcement ---------------------------------------------------------------


def test_level_slippage_separates_the_tape_from_the_fill_model(conn):
    """Only the fill-model half gets better with a real bid/ask, so pooling them
    into one number hides which half is fixable."""
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    trade(runner, clock, stop_bp=100, exit_price=98.5)   # gaps through the stop

    fills = enforcement.level_fills(conn, runner.session_id)
    assert len(fills) == 1
    f = fills[0]
    assert f.kind == "stop"
    assert f.detect_bp > 0, "the tape had already gone past the level"
    assert f.model_bp == pytest.approx(1.5, abs=0.2), "one side of assumed slippage"
    assert f.total_bp == pytest.approx(f.detect_bp + f.model_bp)


def test_a_target_gap_in_your_favour_is_negative_not_absolute(conn):
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    trade(runner, clock, stop_bp=100, target_bp=50, exit_price=101.5)

    f = enforcement.level_fills(conn, runner.session_id)[0]
    assert f.kind == "target"
    assert f.detect_bp < 0, "it printed through the target in our favour"
    # A long's target is ABOVE the level, and its stop below. Searching one
    # direction for both looked for a tick that never came and reported tens of
    # seconds of lateness on a target that was hit the moment it printed.
    assert f.late_ms == 0


def test_lateness_is_measured_against_the_tape(conn):
    """The loop submits inside the step that detects a breach, so its own clock
    says zero every time. The honest question is when the tape first printed."""
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    runner._place({"symbol": "AAPL", "side": "buy", "qty": 5.0,  # noqa: SLF001
                   "reason": "t", "stop_bp": 100},
                  stop_opening_ms=clock.now_ms() + 20 * 60_000)

    # The tape breaches a minute before the loop is next allowed to look.
    clock.advance(60_000)
    tick(conn, "AAPL", 98.9, clock.now_ms())
    clock.advance(60_000)
    runner.update_quotes({"AAPL": quote("AAPL", 98.8, clock.now_ms())})
    tick(conn, "AAPL", 98.8, clock.now_ms())
    runner.fast.step()

    f = enforcement.level_fills(conn, runner.session_id)[0]
    assert f.late_ms == pytest.approx(60_000, abs=1000)


def test_two_fires_on_one_symbol_are_each_measured_against_their_own_level(conn):
    """`exit_plans` is upserted per symbol, so a session that stopped out and
    re-entered had both fires compared to the last stop it ever held. That
    reported 112 seconds of lateness for a loop that acted inside one second."""
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock, capital=100_000.0)
    trade(runner, clock, qty=5.0, stop_bp=100, exit_price=98.5)     # first stop
    trade(runner, clock, qty=5.0, stop_bp=100, exit_price=97.0)     # re-entry, second

    fills = enforcement.level_fills(conn, runner.session_id)
    assert len(fills) == 2, "both fires must be measured"
    assert fills[0].level != fills[1].level, "each fire has its own level"
    for f in fills:
        assert f.late_ms is not None
        assert f.late_ms < 90_000, (
            f"{f.late_ms}ms of lateness for a level set after the fill it is "
            f"being compared against")


def test_a_trailed_plan_is_labelled_because_its_stored_level_moved(conn):
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    trade(runner, clock, stop_bp=100, trail_bp=50)
    clock.advance(30_000)
    runner.update_quotes({"AAPL": quote("AAPL", 102.0, clock.now_ms())})
    tick(conn, "AAPL", 102.0, clock.now_ms())
    runner.fast.step()
    clock.advance(30_000)
    runner.update_quotes({"AAPL": quote("AAPL", 101.0, clock.now_ms())})
    tick(conn, "AAPL", 101.0, clock.now_ms())
    runner.fast.step()

    fills = enforcement.level_fills(conn, runner.session_id)
    assert fills and fills[0].trailed is True
    assert fills[0].late_ms is None, "a trailed level cannot be dated honestly"


def test_blind_time_counts_the_gaps_where_nothing_could_be_enforced(conn):
    tick(conn, "AAPL", 100.0, NOW)
    tick(conn, "AAPL", 100.0, NOW + 600_000)     # ten minutes of silence
    blind = enforcement.blind_time(conn, ["AAPL"], NOW, NOW + 600_000)[0]
    assert blind.blind_s == pytest.approx(600.0)
    assert blind.blind_pct == pytest.approx(100.0)


# -- cohort and exclusions -----------------------------------------------------


def test_a_session_with_no_decisions_is_unknown_not_llm(conn):
    """Failed sessions are exactly the ones with no decisions, so a naive
    "not the stub" test pushes the comparison in the flattering direction."""
    conn.execute(
        "INSERT INTO sessions (id,created_ms,ends_ms,status,config,capital,cash) "
        "VALUES (1,?,?,'failed','{}',20,20)", (NOW, NOW))
    conn.commit()
    assert cohort.classify(conn, 1) is cohort.Arm.UNKNOWN
    assert cohort.UNKNOWN_ARM in cohort.meta(conn, 1).excluded


def test_the_baseline_is_recognised_from_what_the_runner_writes(conn):
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    runner.use_stub = True
    conn.execute(
        "INSERT INTO decisions (session_id,ts_ms,phase,prompt) VALUES (?,?,'tick',?)",
        (runner.session_id, NOW, cohort.STUB_PROMPT))
    conn.commit()
    assert cohort.classify(conn, runner.session_id) is cohort.Arm.BASELINE


def test_mixed_fill_tiers_raise_rather_than_average(conn):
    """NOTES.md: a bar-derived and a quote-derived run can never be averaged into
    one number. Here that is a raise, not a docstring."""
    clock = FixedClock(NOW)
    a = a_runner(conn, clock)
    trade(a, clock, stop_bp=100, exit_price=99.0)
    conn.execute("UPDATE fills SET sim_tier='quotes' WHERE id=(SELECT MIN(id) FROM fills)")
    conn.commit()

    metas = cohort.all_meta(conn)
    with pytest.raises(cohort.MixedTierError):
        cohort.require_single_tier(metas)
    assert cohort.MIXED_TIER in metas[0].excluded


def test_a_running_session_is_never_in_the_statistics(conn):
    conn.execute(
        "INSERT INTO sessions (id,created_ms,ends_ms,status,config,capital,cash,"
        "heartbeat_ms) VALUES (1,?,?,'running','{}',20,20,?)", (NOW, NOW, NOW))
    conn.commit()
    assert cohort.IN_FLIGHT in cohort.meta(conn, 1).excluded


# -- statistics that refuse to answer -----------------------------------------


def test_no_standard_deviation_from_one_session():
    assert stats.stdev([4.2]) is None
    assert stats.stdev([]) is None
    assert stats.stdev([1.0, 3.0]) == pytest.approx(1.4142, abs=1e-3)


def test_no_correlation_from_three_episodes():
    assert stats.kendall_tau_b([1, 2], [1, 2]) is None
    assert stats.kendall_tau_b([7, 7, 7, 7], [1, 2, 3, 4]) is None, "fully tied"
    assert stats.kendall_tau_b([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)


def test_the_win_rate_interval_is_wide_when_the_sample_is_small():
    low, high = stats.wilson(2, 3)
    assert low < 0.25 and high > 0.9, "2 of 3 is not a 67% win rate"
    assert stats.wilson(0, 0) is None


def test_the_required_sample_size_is_printed_as_arithmetic():
    """NOTES.md calls this arithmetic rather than pessimism."""
    assert stats.sessions_needed(67.0, 25.0) == 57
    assert stats.sessions_needed(None, 25.0) is None
    assert stats.sessions_needed(10.0, 0) is None


def test_a_permutation_p_is_never_exactly_zero():
    p = stats.permutation_p([100.0] * 5, [-100.0] * 5, trials=200)
    assert p is not None and p > 0


def test_the_sign_test_drops_zero_differences():
    assert stats.sign_test_p([0.0, 0.0]) is None
    assert stats.sign_test_p([1.0, 2.0, 3.0]) == pytest.approx(0.25)


# -- the assembled report ------------------------------------------------------


def test_a_session_report_holds_together(conn):
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    trade(runner, clock, stop_bp=100, exit_price=98.5)

    rep = report.session_report(conn, runner.session_id)
    assert rep.money.n_fills == 2
    assert rep.by_exit["stop"]["n"] == 1
    assert rep.discipline["at_market"] == 1
    assert rep.unprotected == []
    assert rep.costs.breakeven_bp > 0
    assert rep.win_rate is not None


def test_cost_drag_is_withheld_on_a_losing_session():
    """`costs / gross` is negative when gross is negative, which reads as a
    benefit."""
    losing = report.Costs(costs=0.3, gross=-2.0, notional=1000.0, capital=1000.0,
                          round_trips=1)
    assert losing.drag_pct is None
    assert losing.bp_of_notional == pytest.approx(3.0)
    assert losing.breakeven_bp == pytest.approx(3.0)


def test_a_flat_session_is_diagnosed_rather_than_averaged(conn):
    """Both recorded causes of flat sessions were harness bugs, and the P&L looks
    identical to prudence."""
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    runner._place({"symbol": "AAPL", "side": "buy", "qty": 5.0,  # noqa: SLF001
                   "reason": "no stop"}, stop_opening_ms=clock.now_ms() + 60_000)

    rep = report.session_report(conn, runner.session_id)
    assert rep.flat_reason == "all_rejected"
    assert "an opening order must carry a stop" in " ".join(rep.rejections)
    assert cohort.NO_FILLS in rep.meta.excluded


def test_the_cohort_report_says_what_it_cannot_measure(conn):
    clock = FixedClock(NOW)
    runner = a_runner(conn, clock)
    trade(runner, clock, stop_bp=100, exit_price=99.0)
    conn.execute(
        "INSERT INTO decisions (session_id,ts_ms,phase,prompt) VALUES (?,?,'tick','x')",
        (runner.session_id, NOW))
    conn.commit()

    conn.execute("UPDATE sessions SET status='done', finished_ms=? WHERE id=?",
                 (clock.now_ms(), runner.session_id))
    conn.commit()

    rep = report.cohort_report(conn)
    assert rep.arms[cohort.Arm.LLM].n == 1
    assert rep.arms[cohort.Arm.LLM].sd_bp is None, "one session has no spread"
    assert rep.difference_bp is None, "no baseline to compare against"
    assert rep.pairs == 0
    assert any("nothing in the schema links" in n for n in rep.notes)
    assert any("one session is a sample" in n.lower() for n in rep.notes)
