"""Exit levels: parsing, resolution, and when they fire.

All pure. The enforcement decision has to be reproducible from a row in a table,
so every test here is a function call with no database and no clock.
"""

from __future__ import annotations

import pytest

from thepit.trading import levels as lv

NOW = 1_800_000_000_000
COST_BP = 3.0


def _resolve(proposal: dict, *, side: str = "buy", entry: float = 100.0):
    levels, error = lv.parse(proposal)
    assert error is None, error
    return lv.resolve(levels, symbol="AAPL", side=side, entry_price=entry,
                      now_ms=NOW, round_trip_cost_bp=COST_BP)


# -- parsing -----------------------------------------------------------------


def test_terse_and_explicit_field_names_both_parse():
    """Models produce "stop" and "stop_price" interchangeably."""
    terse, _ = lv.parse({"stop": 99.0, "target": 102.0, "trigger": 99.5})
    explicit, _ = lv.parse({"stop_price": 99.0, "target_price": 102.0,
                            "trigger_price": 99.5})
    assert terse == explicit


def test_a_price_and_a_bp_distance_for_the_same_level_is_a_contradiction():
    """Preferring one silently would make enforcement stop matching intent."""
    _, error = lv.parse({"stop": 99.0, "stop_bp": 15})
    assert "not both" in error


def test_an_unparseable_level_is_an_error_not_a_shrug():
    """Ignoring it would leave the position open with no stop, which is the whole
    failure this exists to prevent."""
    _, error = lv.parse({"stop": "just below support"})
    assert "not a number" in error


def test_negative_and_zero_modifiers_are_refused():
    for bad in ({"trail_bp": 0}, {"time_stop_minutes": -4}, {"valid_minutes": 0},
                {"trigger": 0}):
        _, error = lv.parse(bad)
        assert error, bad


# -- resolution --------------------------------------------------------------


def test_bp_stop_resolves_against_the_fill_not_the_quote():
    plan, error = _resolve({"stop_bp": 30, "target_bp": 60}, entry=200.0)
    assert error is None
    assert plan.stop_price == 200.0 * (1 - 0.0030)
    assert plan.target_price == 200.0 * (1 + 0.0060)


def test_a_bp_stop_is_a_distance_so_its_sign_carries_no_information():
    """-15 and 15 both mean fifteen basis points against me."""
    negative, _ = _resolve({"stop_bp": -15})
    positive, _ = _resolve({"stop_bp": 15})
    assert negative.stop_price == positive.stop_price < 100.0


def test_an_opening_order_without_a_stop_is_refused():
    plan, error = _resolve({"target_bp": 60})
    assert plan is None
    assert "must carry a stop" in error


def test_a_long_stop_above_the_entry_is_refused():
    plan, error = _resolve({"stop": 101.0})
    assert plan is None
    assert "not below" in error


def test_a_short_stop_below_the_entry_is_refused():
    plan, error = _resolve({"stop": 99.0}, side="sell")
    assert plan is None
    assert "not above" in error


def test_a_stop_inside_the_trading_cost_is_refused():
    """Otherwise noise closes the position and the session pays the round trip
    for nothing, repeatedly."""
    plan, error = _resolve({"stop_bp": 4})   # 4bp against a 3bp round trip
    assert plan is None
    assert "trading cost" in error

    ok, error = _resolve({"stop_bp": 8})
    assert error is None and ok is not None


def test_a_target_on_the_losing_side_is_refused():
    plan, error = _resolve({"stop_bp": 30, "target": 99.0})
    assert plan is None
    assert "not above" in error


def test_a_time_stop_becomes_an_absolute_deadline():
    plan, _ = _resolve({"stop_bp": 30, "time_stop_minutes": 4})
    assert plan.time_stop_ms == NOW + 4 * 60_000


def test_short_levels_mirror_long_ones():
    plan, error = _resolve({"stop_bp": 30, "target_bp": 60}, side="sell", entry=100.0)
    assert error is None
    assert plan.stop_price > 100.0 > plan.target_price
    assert plan.close_side == "buy"


# -- firing ------------------------------------------------------------------


def test_a_long_fires_at_or_through_its_stop():
    plan, _ = _resolve({"stop": 99.0, "target": 102.0})
    assert lv.breached(plan, 99.5, NOW) is None
    assert lv.breached(plan, 99.0, NOW).kind == "stop"
    assert lv.breached(plan, 98.4, NOW).kind == "stop"


def test_a_long_fires_at_or_through_its_target():
    plan, _ = _resolve({"stop": 99.0, "target": 102.0})
    assert lv.breached(plan, 102.0, NOW).kind == "target"


def test_a_stop_and_a_target_in_the_same_interval_resolve_as_the_stop():
    """Five-second snapshots cannot show the path between them, so the adverse
    assumption is the honest one. Every choice in the other direction compounds
    into a paper result that flatters."""
    plan, _ = _resolve({"stop": 99.0, "target": 100.5})
    reachable_either_way = 98.0
    assert lv.breached(plan, reachable_either_way, NOW).kind == "stop"


def test_the_time_stop_fires_even_when_the_position_is_fine():
    plan, _ = _resolve({"stop": 99.0, "time_stop_minutes": 4})
    assert lv.breached(plan, 100.2, NOW + 3 * 60_000) is None
    assert lv.breached(plan, 100.2, NOW + 4 * 60_000).kind == "time_stop"


def test_a_short_fires_when_price_rises_through_its_stop():
    plan, _ = _resolve({"stop_bp": 30, "target_bp": 60}, side="sell")
    assert lv.breached(plan, 100.1, NOW) is None
    assert lv.breached(plan, plan.stop_price, NOW).kind == "stop"
    assert lv.breached(plan, plan.target_price, NOW).kind == "target"


# -- trailing ----------------------------------------------------------------


def test_a_trailing_stop_follows_the_high_water_mark():
    plan, _ = _resolve({"stop": 99.0, "trail_bp": 50})   # 50bp behind the best price
    moved = plan.trailed(101.0)
    assert moved.high_water == 101.0
    assert moved.stop_price == 101.0 * (1 - 0.0050)


def test_a_trailing_stop_never_retreats():
    """A stop that can move away is not a stop, it is a hope with a number."""
    plan, _ = _resolve({"stop": 99.0, "trail_bp": 50})
    up = plan.trailed(101.0)
    back_down = up.trailed(100.0)
    assert back_down.stop_price == up.stop_price
    assert back_down.high_water == up.high_water


def test_a_trail_that_would_loosen_the_original_stop_leaves_it_alone():
    plan, _ = _resolve({"stop": 99.9, "trail_bp": 200})
    assert plan.trailed(100.05).stop_price == 99.9


def test_trailing_is_a_no_op_without_trail_bp():
    plan, _ = _resolve({"stop": 99.0})
    assert plan.trailed(120.0) is plan


def test_a_short_trails_downward():
    plan, _ = _resolve({"stop_bp": 30, "trail_bp": 50}, side="sell")
    moved = plan.trailed(99.0)
    assert moved.high_water == 99.0
    assert moved.stop_price == pytest.approx(99.0 * (1 + 0.0050))
    assert moved.stop_price < plan.stop_price


# -- arming ------------------------------------------------------------------


def test_a_level_below_the_market_waits_for_a_pullback():
    assert lv.arm_direction("buy", 303.50, 304.82) == "at_or_below"
    assert not lv.triggered("at_or_below", 303.50, 304.82)
    assert lv.triggered("at_or_below", 303.50, 303.50)


def test_a_level_above_the_market_waits_for_a_breakout():
    assert lv.arm_direction("buy", 306.00, 304.82) == "at_or_above"
    assert not lv.triggered("at_or_above", 306.00, 304.82)
    assert lv.triggered("at_or_above", 306.00, 306.01)
