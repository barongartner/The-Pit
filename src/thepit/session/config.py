"""Session configuration.

A **session** is a bounded trading window: plan, execute, forced flatten, review.
See issue #17.

The objective is stated once, here, so it cannot drift: **maximum P&L over the
window, subject to the risk limits.** Everything else in this module exists to
serve that, including the parts that look like constraints -- an agent that
knows its hurdle and its clock earns more than one that does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class Blinding(StrEnum):
    """Symbol identity handling. See issue #3.

    REAL       -- tickers as-is.
    ANONYMIZED -- symbols replaced with opaque labels.
    MISLABELED -- real data served under a DIFFERENT real ticker. The sharpest
                  test: if behaviour tracks the label rather than the tape, that
                  is recall, demonstrated directly rather than inferred.
    """

    REAL = "real"
    ANONYMIZED = "anonymized"
    MISLABELED = "mislabeled"


class ResearchAccess(StrEnum):
    OFF = "off"                      # price action only
    AMBIENT = "ambient"              # watchlist news/filings pushed into context
    REQUESTED = "ambient+requested"  # plus harness-mediated lookups it asks for


class RiskProfile(StrEnum):
    """Preset risk postures.

    The defaults elsewhere in this file are tuned for capital preservation --
    20% per position, halt at a 2% session loss. On $20 of money you are willing
    to lose entirely, that is the wrong objective: a 2% limit is 40 cents, and a
    20% cap leaves five sixths of the account idle while the window expires.

    A correct call in an 8-minute session earned one cent because two thirds of
    the available capital sat unused. The agent's own review: "Reserve capital
    on an 8-minute session is just money left on the table."
    """

    PRESERVE = "preserve"    # capital preservation. Sensible on real money.
    BALANCED = "balanced"
    RISK_IT = "risk_it"      # money you can afford to lose entirely.


# max_position_pct, session_loss_limit_pct, max_concurrent_positions
PROFILES: dict[RiskProfile, tuple[float, float, int]] = {
    RiskProfile.PRESERVE: (20.0, 2.0, 3),
    RiskProfile.BALANCED: (50.0, 15.0, 2),
    # All-in on the single best idea, halt only near total loss. Concentration
    # is the point: diversifying $20 across three names guarantees that being
    # right is indistinguishable from being wrong.
    RiskProfile.RISK_IT: (100.0, 60.0, 1),
}


@dataclass(frozen=True, slots=True)
class SessionConfig:
    duration_minutes: int = 30
    capital: float = 10_000.0

    # Universe. Empty means "choose from the watchlist during planning", which
    # is itself a decision worth recording.
    symbols: tuple[str, ...] = ()

    # How often Claude re-thinks. Deterministic Python runs between ticks; the
    # model is never in a sub-minute execution path.
    policy_tick_minutes: int = 5

    # How often the fast loop enforces the levels the model committed to. No
    # model call, so seconds are achievable here in a way they are not above.
    #
    # 5 seconds matches the feed: Yahoo updates roughly that often and polling
    # faster would only re-read the same price with more heat. This is the
    # honest resolution of a stop in this system -- late by up to one interval
    # plus feed latency, which is not the same thing as a venue stop order.
    fast_loop_seconds: int = 5

    # Risk. These are ceilings the engine enforces, not suggestions in a prompt.
    max_position_pct: float = 20.0
    max_concurrent_positions: int = 3
    session_loss_limit_pct: float = 2.0

    # Flatten this many minutes before the session clock expires, so the
    # closing orders have room to fill rather than being fired at the buzzer.
    flatten_before_end_minutes: int = 2

    model: str = "sonnet"
    effort: str = "medium"

    research: ResearchAccess = ResearchAccess.AMBIENT
    blinding: Blinding = Blinding.REAL

    # Run the deterministic stub on the identical tape. Without this, "did the
    # LLM add anything" is unanswerable for that session.
    run_baseline: bool = True

    notes: str = ""

    def with_profile(self, profile: RiskProfile) -> "SessionConfig":
        pos, loss, concurrent = PROFILES[profile]
        return replace(self, max_position_pct=pos, session_loss_limit_pct=loss,
                       max_concurrent_positions=concurrent)

    def validate(self) -> list[str]:
        """Return reasons this config cannot run. Empty means it is coherent."""
        errors: list[str] = []

        if not 1 <= self.duration_minutes <= 390:
            errors.append("duration must be between 1 and 390 minutes (one session)")
        if self.capital <= 0:
            errors.append("capital must be positive")
        if self.policy_tick_minutes < 1:
            errors.append("policy tick must be at least 1 minute")
        if not 1 <= self.fast_loop_seconds <= 60:
            errors.append("fast loop interval must be between 1 and 60 seconds")
        if self.fast_loop_seconds >= self.policy_tick_minutes * 60:
            # Otherwise there is no enforcement *between* ticks, which is the
            # entire purpose of the second loop.
            errors.append(
                f"fast loop ({self.fast_loop_seconds}s) must be shorter than the "
                f"policy tick ({self.policy_tick_minutes}m)"
            )

        # A session that re-thinks once has no feedback loop; it is a single
        # decision wearing a session's clothes.
        if self.policy_tick_minutes * 2 > self.duration_minutes:
            errors.append(
                f"policy tick ({self.policy_tick_minutes}m) leaves fewer than two "
                f"ticks in a {self.duration_minutes}m session"
            )
        if self.flatten_before_end_minutes >= self.duration_minutes:
            errors.append("flatten window is longer than the session")
        if self.flatten_before_end_minutes < 1:
            # Zero leaves the closing orders no room at all: the flatten gets one
            # attempt at the buzzer, and a single stale quote then ends the
            # session still holding.
            errors.append("flatten window must be at least 1 minute")
        if not 0 < self.max_position_pct <= 100:
            errors.append("max position must be between 0 and 100 percent")
        if not 0 < self.session_loss_limit_pct <= 100:
            errors.append("session loss limit must be between 0 and 100 percent")
        if self.max_concurrent_positions < 1:
            errors.append("max concurrent positions must be at least 1")

        # Research would de-anonymize a blinded symbol in one lookup. Refusing
        # the combination beats trusting anyone to remember. See issue #3.
        if self.blinding is not Blinding.REAL and self.research is not ResearchAccess.OFF:
            errors.append(
                f"blinding={self.blinding} requires research=off: a single lookup "
                "identifies the symbol and invalidates the arm"
            )
        return errors

    @property
    def tick_count(self) -> int:
        return max(1, self.duration_minutes // self.policy_tick_minutes)

    @property
    def model_calls(self) -> int:
        """Total CLI invocations: one plan, one per tick, one review.

        Surfaced in the UI before the session starts, because these come out of
        the same 5-hour subscription window the operator uses for everything
        else. A misconfigured tick interval that locks you out of your own tools
        is a worse outcome than a bad trade, and it is entirely predictable up
        front.
        """
        return self.tick_count + 2

    @property
    def estimated_latency_s(self) -> int:
        """Wall time spent waiting on the model.

        ~6s per call measured against the CLI, most of it process startup rather
        than inference. At a 1-minute tick that is a tenth of every interval
        spent in subprocess spawn.
        """
        return self.model_calls * 6

    @property
    def trading_minutes(self) -> int:
        """Minutes available for opening positions, before the flatten window."""
        return max(0, self.duration_minutes - self.flatten_before_end_minutes)


@dataclass(frozen=True, slots=True)
class SessionReadiness:
    """Whether a session could actually run right now, and what is missing.

    Kept separate from validate(): a coherent config that cannot execute yet is
    a different failure from an incoherent one, and the UI must not conflate
    "you typed something wrong" with "this part of the system does not exist".
    """

    can_execute: bool
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
