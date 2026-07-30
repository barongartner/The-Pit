"""Exit levels: parse them, validate them, decide when they are breached.

Pure. No database, no clock, no quotes fetched -- everything arrives as an
argument, the same discipline `check()` in book.py follows, so the enforcement
decision is reproducible from a row in a table rather than from whatever the
process happened to be holding.

The point of this module is that a stop stops being prose. The model used to
write "stop -15bp" in a reason field, which no code could act on; it now returns
a field, this module turns it into an absolute price against the actual fill, and
the fast loop enforces it every few seconds.

Two conservative choices, both deliberate:

**A breach is checked before the trail is raised.** With 5-second snapshots the
path inside the interval is unknown. Raising the trailing stop with the same
price that might have breached the old one would let a position escape a stop it
plausibly hit, and every such choice compounds in the flattering direction.

**A stop and a target reached in the same interval resolve as the stop.** Same
reason: unknowable order, so assume the adverse one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# A stop closer than the round trip costs is not a stop, it is a fee schedule.
# Enforced as a multiple of the round-trip cost so it tightens automatically once
# a real bid/ask makes that number smaller (issue #11).
MIN_STOP_COST_MULTIPLE = 2.0


@dataclass(frozen=True, slots=True)
class Levels:
    """What the model asked for, unresolved.

    Prices and basis points are both accepted because both are natural: a level
    read off a chart is a price, a risk budget is basis points. Exactly one form
    of each is allowed -- being given 339.20 *and* -15bp is a contradiction, and
    silently preferring one is how enforcement stops matching intent.
    """

    stop_price: float | None = None
    stop_bp: float | None = None
    target_price: float | None = None
    target_bp: float | None = None
    trail_bp: float | None = None
    time_stop_minutes: float | None = None

    # Entry arming. Absent means "at market, now".
    trigger_price: float | None = None
    valid_minutes: float | None = None

    @property
    def has_stop(self) -> bool:
        return self.stop_price is not None or self.stop_bp is not None


@dataclass(frozen=True, slots=True)
class ExitPlan:
    """Levels resolved against a real fill. This is what gets enforced."""

    symbol: str
    long: bool
    entry_price: float
    stop_price: float
    target_price: float | None = None
    trail_bp: float | None = None
    time_stop_ms: int | None = None
    high_water: float = 0.0

    @property
    def close_side(self) -> str:
        return "sell" if self.long else "buy"

    def stop_bp(self) -> float:
        """Distance to the stop in basis points, always positive."""
        return abs(self.entry_price - self.stop_price) / self.entry_price * 10_000

    def trailed(self, price: float) -> ExitPlan:
        """Advance the high-water mark and drag the stop after it.

        The stop only ever moves in the direction that reduces risk. A trailing
        stop that can retreat is not a stop, it is a hope with a number attached.

        The trail is applied as an *invariant on the stored plan*, not as an event
        that fires when a new high prints. It used to return early when the high
        water had not moved, which meant anything that rewrote the stop -- an
        amendment, an add to the position -- silently un-trailed it until the
        price made a fresh high, and the plan then enforced a level far below the
        one it had already ratcheted to.
        """
        if self.trail_bp is None:
            return self
        high = max(self.high_water, price) if self.long else min(self.high_water, price)
        offset = high * self.trail_bp / 10_000
        candidate = high - offset if self.long else high + offset
        stop = max(self.stop_price, candidate) if self.long \
            else min(self.stop_price, candidate)
        if high == self.high_water and stop == self.stop_price:
            return self
        return replace(self, high_water=high, stop_price=stop)


@dataclass(frozen=True, slots=True)
class Breach:
    kind: str      # 'stop' | 'target' | 'time_stop'
    detail: str    # human-readable, with the number that caused it


def parse(proposal: dict) -> tuple[Levels, str | None]:
    """Pull the level fields out of one order object from a model response.

    Returns the levels and an error string. A malformed level is an error rather
    than something to ignore: an order whose stop failed to parse would otherwise
    become an unprotected position, which is the exact failure this exists to
    prevent.
    """
    out: dict[str, float | None] = {}
    for field in ("stop_price", "stop_bp", "target_price", "target_bp", "trail_bp",
                  "time_stop_minutes", "trigger_price", "valid_minutes"):
        # Both the terse form the schema documents ("stop") and the explicit one
        # ("stop_price") are read. Models reliably produce both.
        alias = field.removesuffix("_price")
        raw = proposal.get(field, proposal.get(alias))
        if raw is None or raw == "":
            out[field] = None
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return Levels(), f"{field} is not a number: {raw!r}"
        out[field] = value

    if out["stop_price"] is not None and out["stop_bp"] is not None:
        return Levels(), "give a stop as a price or in bp, not both"
    if out["target_price"] is not None and out["target_bp"] is not None:
        return Levels(), "give a target as a price or in bp, not both"

    levels = Levels(**out)  # type: ignore[arg-type]

    if levels.trail_bp is not None and levels.trail_bp <= 0:
        return Levels(), "trail_bp must be positive"
    if levels.time_stop_minutes is not None and levels.time_stop_minutes <= 0:
        return Levels(), "time_stop_minutes must be positive"
    if levels.trigger_price is not None and levels.trigger_price <= 0:
        return Levels(), "trigger must be a positive price"
    if levels.valid_minutes is not None and levels.valid_minutes <= 0:
        return Levels(), "valid_minutes must be positive"
    return levels, None


def resolve(
    levels: Levels,
    *,
    symbol: str,
    side: str,
    entry_price: float,
    now_ms: int,
    round_trip_cost_bp: float,
) -> tuple[ExitPlan | None, str | None]:
    """Turn stated levels into an enforceable plan, or say why they are unusable.

    Rejects rather than repairs. A stop the risk layer quietly moved to somewhere
    workable is a level nobody chose, and the whole value of a binding plan is
    that the number being enforced is the number that was committed to.
    """
    if entry_price <= 0:
        return None, "entry price must be positive"
    if not levels.has_stop:
        return None, "an opening order must carry a stop"

    long = side == "buy"
    sign = 1 if long else -1

    if levels.stop_price is not None:
        stop = levels.stop_price
    else:
        # A stop given in bp is a distance, so its sign carries no information.
        # -15 and 15 both mean "15bp against me".
        stop = entry_price * (1 - sign * abs(levels.stop_bp or 0.0) / 10_000)

    if levels.target_price is not None:
        target = levels.target_price
    elif levels.target_bp is not None:
        target = entry_price * (1 + sign * abs(levels.target_bp) / 10_000)
    else:
        target = None

    if stop <= 0:
        return None, "stop resolves to a non-positive price"
    if long and stop >= entry_price:
        return None, f"stop {stop:.2f} is not below the {entry_price:.2f} entry"
    if not long and stop <= entry_price:
        return None, f"stop {stop:.2f} is not above the {entry_price:.2f} entry"

    distance_bp = abs(entry_price - stop) / entry_price * 10_000
    floor_bp = round_trip_cost_bp * MIN_STOP_COST_MULTIPLE
    if distance_bp < floor_bp:
        # Otherwise ordinary noise closes the position and the session pays the
        # round trip for nothing, repeatedly.
        return None, (
            f"stop is {distance_bp:.1f}bp away, inside {floor_bp:.1f}bp of "
            f"trading cost -- noise would close it"
        )

    if target is not None:
        if long and target <= entry_price:
            return None, f"target {target:.2f} is not above the {entry_price:.2f} entry"
        if not long and target >= entry_price:
            return None, f"target {target:.2f} is not below the {entry_price:.2f} entry"
        target_bp = abs(target - entry_price) / entry_price * 10_000
        if target_bp < round_trip_cost_bp:
            # Arithmetic, not preference: a target closer than the round trip
            # cannot be profitable even when it is hit exactly. One such order
            # bought and unwound a position for a guaranteed loss.
            return None, (
                f"target is {target_bp:.1f}bp away, inside the "
                f"{round_trip_cost_bp:.1f}bp round trip -- hitting it still loses"
            )

    time_stop_ms = (
        now_ms + int(levels.time_stop_minutes * 60_000)
        if levels.time_stop_minutes is not None else None
    )

    return ExitPlan(
        symbol=symbol, long=long, entry_price=entry_price, stop_price=stop,
        target_price=target, trail_bp=levels.trail_bp, time_stop_ms=time_stop_ms,
        high_water=entry_price,
    ), None


def breached(plan: ExitPlan, price: float, now_ms: int) -> Breach | None:
    """Has this plan fired? Stop first, then target, then the clock.

    Ordered worst-case first on purpose: within one 5-second snapshot the path is
    unknown, and if both a stop and a target are reachable the honest assumption
    is the adverse one.
    """
    if plan.long:
        if price <= plan.stop_price:
            return Breach("stop", f"{price:.2f} at or through stop "
                                  f"{plan.stop_price:.2f} "
                                  f"({_move_bp(plan, price):+.0f}bp)")
        if plan.target_price is not None and price >= plan.target_price:
            return Breach("target", f"{price:.2f} at or through target "
                                    f"{plan.target_price:.2f} "
                                    f"({_move_bp(plan, price):+.0f}bp)")
    else:
        if price >= plan.stop_price:
            return Breach("stop", f"{price:.2f} at or through stop "
                                  f"{plan.stop_price:.2f} "
                                  f"({_move_bp(plan, price):+.0f}bp)")
        if plan.target_price is not None and price <= plan.target_price:
            return Breach("target", f"{price:.2f} at or through target "
                                    f"{plan.target_price:.2f} "
                                    f"({_move_bp(plan, price):+.0f}bp)")

    if plan.time_stop_ms is not None and now_ms >= plan.time_stop_ms:
        return Breach("time_stop", f"time stop reached at {price:.2f} "
                                   f"({_move_bp(plan, price):+.0f}bp)")
    return None


def triggered(direction: str, trigger_price: float, price: float) -> bool:
    """Has an armed entry level printed?"""
    if direction == "at_or_below":
        return price <= trigger_price
    return price >= trigger_price


def arm_direction(side: str, trigger_price: float, price_now: float) -> str:
    """Which way the price must cross for this trigger to fire.

    Inferred from where the level sits relative to the market rather than asked
    for: a buy below the market is waiting for a pullback, a buy above it is
    waiting for a breakout, and requiring the model to also state which is one
    more field to get wrong.
    """
    if trigger_price <= price_now:
        return "at_or_below"
    return "at_or_above"


def _move_bp(plan: ExitPlan, price: float) -> float:
    direction = 1 if plan.long else -1
    return (price - plan.entry_price) / plan.entry_price * 10_000 * direction
