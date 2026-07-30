"""Assembles a session report and a cohort report. Formats nothing.

`tradectl eval` prints. This returns data, so the same numbers can go to a
terminal, a dashboard, or a test without a second implementation drifting from the
first.

Two rules the shape of this module enforces:

**Nothing is reported without its n.** Every rate carries a Wilson interval, every
comparison carries the number of sessions behind it and the number of sessions it
would actually need. At four completed sessions, a mean is a rounding of noise, and
the defence against reading it as a result is printing the arithmetic beside it.

**Cost drag is three numbers, because one ratio lies.** `costs / gross` reads as a
benefit on a losing session. Basis points of notional is comparable across
sessions and directly checkable against the 1.5bp/side assumption; breakeven bp is
what the session had to make to stand still.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from thepit.eval import cohort, enforcement, stats, trades
from thepit.eval import pnl as pnl_mod
from thepit.eval.cohort import Arm, SessionMeta
from thepit.eval.pnl import SessionPnL

# Default floor for reporting a correlation. Below it the number is printed as an
# n and withheld as a coefficient.
MIN_N_FOR_CORRELATION = 20

# The smallest P&L difference worth calling a difference on a $20 account.
DEFAULT_EFFECT_BP = 125.0    # 1.25% of capital


@dataclass(frozen=True, slots=True)
class Costs:
    costs: float
    gross: float
    notional: float
    capital: float
    round_trips: int

    @property
    def drag_pct(self) -> float | None:
        """Share of gross P&L eaten by costs. None on a losing session, where the
        ratio is negative and reads as a benefit."""
        return self.costs / self.gross * 100 if self.gross > 0 else None

    @property
    def bp_of_notional(self) -> float | None:
        """Directly checkable against ASSUMED_SLIPPAGE_BP (1.5bp per side)."""
        return self.costs / self.notional * 10_000 if self.notional else None

    @property
    def breakeven_bp(self) -> float:
        """What the session had to make to stand still."""
        return self.costs / self.capital * 10_000 if self.capital else 0.0


@dataclass(frozen=True, slots=True)
class Conviction:
    buckets: dict[int, dict[str, float]]
    tau: float | None
    p_value: float | None
    n: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class ModelUse:
    calls: int
    errors: int
    unparsed_ticks: int
    total_latency_ms: int
    p50_ms: float | None
    p95_ms: float | None
    thinking_share: float | None   # of the session's wall clock
    usd: float                     # not a trading cost; the CLI bills a subscription


@dataclass(frozen=True, slots=True)
class SessionReport:
    meta: SessionMeta
    money: SessionPnL
    costs: Costs
    episodes: list[trades.Episode]
    by_exit: dict[str, dict[str, float]]
    discipline: dict[str, int]
    unprotected: list[str]
    levels: list[enforcement.LevelFill]
    armed: enforcement.ArmedOutcome
    blindness: list[enforcement.Blindness]
    model: ModelUse
    rejections: dict[str, int]
    flat_reason: str | None

    @property
    def win_rate(self) -> tuple[float, tuple[float, float] | None] | None:
        closed = [e for e in self.episodes if not e.open_at_end]
        if not closed:
            return None
        wins = sum(1 for e in closed if e.net > 0)
        return wins / len(closed), stats.wilson(wins, len(closed))

    @property
    def median_hold_s(self) -> float | None:
        held = [e.held_s for e in self.episodes if e.held_s is not None]
        return stats.median(held)

    @property
    def turnover(self) -> float:
        return self.money.notional / self.money.capital if self.money.capital else 0.0


@dataclass(frozen=True, slots=True)
class ArmSummary:
    arm: Arm
    n: int
    mean_bp: float | None
    median_bp: float | None
    sd_bp: float | None
    positive: int


@dataclass(frozen=True, slots=True)
class CohortReport:
    tier: str
    sessions: list[SessionMeta]
    excluded: dict[str, int]
    arms: dict[Arm, ArmSummary]
    difference_bp: float | None
    permutation_p: float | None
    pairs: int
    twinned_pairs: int
    paired_p: float | None
    sessions_needed: int | None
    effect_bp: float
    flat_rate: tuple[float, tuple[float, float] | None] | None
    conviction: Conviction
    level_slippage: dict[str, float | None]
    lateness_ms: dict[str, float | None]
    notes: list[str] = field(default_factory=list)


def session_report(conn: sqlite3.Connection, session_id: int) -> SessionReport:
    meta = cohort.meta(conn, session_id)
    money = pnl_mod.session_pnl(conn, session_id)
    eps = trades.episodes(conn, session_id)
    closed = [e for e in eps if not e.open_at_end]

    since = meta.started_ms or meta.created_ms
    return SessionReport(
        meta=meta,
        money=money,
        costs=Costs(costs=money.costs, gross=money.gross, notional=money.notional,
                    capital=money.capital, round_trips=len(closed)),
        episodes=eps,
        by_exit=trades.by_exit(eps),
        discipline=trades.entry_discipline(conn, session_id),
        unprotected=trades.unprotected_fills(conn, session_id),
        levels=enforcement.level_fills(conn, session_id),
        armed=enforcement.armed_outcomes(conn, session_id),
        blindness=enforcement.blind_time(conn, list(meta.universe), since,
                                         meta.mark_ms),
        model=_model_use(conn, session_id, meta),
        rejections=_rejections(conn, session_id),
        flat_reason=_flat_reason(conn, session_id, money.n_fills),
    )


def cohort_report(
    conn: sqlite3.Connection, *, limit: int = 200, effect_bp: float = DEFAULT_EFFECT_BP,
    min_n_for_correlation: int = MIN_N_FOR_CORRELATION,
) -> CohortReport:
    metas = cohort.all_meta(conn, limit=limit)
    usable = cohort.scorable(metas)
    notes: list[str] = []

    try:
        tier = cohort.require_single_tier(usable)
    except cohort.MixedTierError as exc:
        # Not fatal for the whole report: drop to the majority tier and say so,
        # rather than refusing to print anything.
        tier = "MIXED"
        notes.append(str(exc))

    by_arm: dict[Arm, list[float]] = {Arm.LLM: [], Arm.BASELINE: []}
    for m in usable:
        if m.arm in by_arm:
            by_arm[m.arm].append(pnl_mod.session_pnl(conn, m.id).pnl_bp)

    arms = {
        arm: ArmSummary(
            arm=arm, n=len(xs), mean_bp=stats.mean(xs), median_bp=stats.median(xs),
            sd_bp=stats.stdev(xs) if len(xs) >= stats.MIN_N_FOR_SD else None,
            positive=sum(1 for x in xs if x > 0),
        )
        for arm, xs in by_arm.items()
    }

    llm, base = by_arm[Arm.LLM], by_arm[Arm.BASELINE]
    difference = (
        stats.mean(llm) - stats.mean(base)  # type: ignore[operator]
        if llm and base else None
    )

    pairs, _unpaired = cohort.pair(usable)
    paired_diffs = [
        pnl_mod.session_pnl(conn, a.id).pnl_bp - pnl_mod.session_pnl(conn, b.id).pnl_bp
        for a, b in pairs
    ]
    n_twinned = cohort.twinned(pairs)
    if not pairs:
        notes.append(
            "No LLM/baseline pairs, so the arm comparison is unpaired and "
            "confounded by whatever the market did on each day. Set "
            "run_baseline to spawn a twin."
        )
    elif n_twinned < len(pairs):
        # A wall-clock pair saw similar tape, not the same tape. Reporting the
        # two as one number would overstate the evidence.
        notes.append(
            f"{len(pairs) - n_twinned} of {len(pairs)} pairs are wall-clock "
            f"matches, not twins: those two sessions saw similar tape, not the "
            f"same tape. Sessions recorded before migration 007 cannot be "
            f"paired retroactively."
        )

    sd = arms[Arm.LLM].sd_bp or arms[Arm.BASELINE].sd_bp
    if sd is None:
        notes.append(
            f"Fewer than {stats.MIN_N_FOR_SD} scorable sessions in an arm, so "
            f"there is no usable spread and no sample-size estimate. One session "
            f"is a sample, not a result."
        )

    flat = [m for m in metas if pnl_mod.session_pnl(conn, m.id).n_fills == 0
            and cohort.IN_FLIGHT not in m.excluded]
    considered = [m for m in metas if cohort.IN_FLIGHT not in m.excluded]
    flat_rate = (
        (len(flat) / len(considered), stats.wilson(len(flat), len(considered)))
        if considered else None
    )

    return CohortReport(
        tier=tier, sessions=metas, excluded=cohort.exclusions(metas), arms=arms,
        difference_bp=difference,
        permutation_p=stats.permutation_p(llm, base),
        pairs=len(pairs), twinned_pairs=n_twinned,
        paired_p=stats.sign_test_p(paired_diffs),
        sessions_needed=stats.sessions_needed(sd, effect_bp), effect_bp=effect_bp,
        flat_rate=flat_rate,
        conviction=_conviction(conn, usable, min_n_for_correlation),
        level_slippage=_level_slippage(conn, usable),
        lateness_ms=_lateness(conn, usable),
        notes=notes,
    )


# -- pieces -------------------------------------------------------------------


def _model_use(
    conn: sqlite3.Connection, session_id: int, meta: SessionMeta
) -> ModelUse:
    rows = conn.execute(
        "SELECT phase, latency_ms, error, parsed, cost_usd FROM decisions "
        "WHERE session_id=?", (session_id,)).fetchall()
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    wall_ms = meta.duration_minutes * 60_000
    return ModelUse(
        calls=len(rows),
        errors=sum(1 for r in rows if r["error"]),
        # Invisible until now, and it looks exactly like prudence in the P&L: a
        # tick whose response did not parse placed no orders and said nothing.
        unparsed_ticks=sum(1 for r in rows
                           if r["phase"] == "tick" and r["parsed"] is None),
        total_latency_ms=sum(latencies),
        p50_ms=stats.percentile(latencies, 0.5),
        p95_ms=stats.percentile(latencies, 0.95),
        thinking_share=sum(latencies) / wall_ms if wall_ms else None,
        usd=sum(r["cost_usd"] or 0.0 for r in rows),
    )


def _rejections(conn: sqlite3.Connection, session_id: int) -> dict[str, int]:
    """What the harness refused, grouped.

    The most actionable output in the report: it distinguishes "the model
    declined" from "we forbade it". Whole-share sizing on a small account would
    have shown up here as every order rejected for insufficient cash.
    """
    return {r["reject_reason"]: r["n"] for r in conn.execute(
        "SELECT reject_reason, COUNT(*) AS n FROM orders WHERE session_id=? "
        "AND status='rejected' GROUP BY reject_reason ORDER BY n DESC",
        (session_id,))}


def _flat_reason(
    conn: sqlite3.Connection, session_id: int, n_fills: int
) -> str | None:
    """Why a session traded nothing. Both recorded causes were harness bugs."""
    if n_fills:
        return None
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM orders WHERE session_id=?) AS orders, "
        "(SELECT COUNT(*) FROM orders WHERE session_id=? AND status='rejected') "
        "  AS rejected, "
        "(SELECT COUNT(*) FROM decisions WHERE session_id=? AND phase='tick' "
        "  AND parsed IS NULL) AS unparsed, "
        "(SELECT COUNT(*) FROM pending_entries WHERE session_id=? "
        "  AND status='expired') AS expired",
        (session_id, session_id, session_id, session_id)).fetchone()
    if row["unparsed"]:
        return "broken_model"
    if row["expired"]:
        return "armed_but_never_printed"
    if row["orders"] and row["rejected"] == row["orders"]:
        return "all_rejected"
    if not row["orders"]:
        return "never_proposed"
    return "unknown"


def _conviction(
    conn: sqlite3.Connection, metas: list[SessionMeta], min_n: int
) -> Conviction:
    """Does stated conviction predict the realised outcome?

    Closed episodes only, attributed to the conviction on the FIRST opening order.
    Open episodes are excluded rather than counted as zero: an unresolved position
    has no outcome to correlate, and counting one manufactures a relationship out
    of bookkeeping.
    """
    convictions: list[float] = []
    nets: list[float] = []
    buckets: dict[int, dict[str, float]] = {}
    conflicts = 0

    for m in metas:
        for e in trades.episodes(conn, m.id):
            if e.open_at_end or e.conviction is None:
                continue
            conflicts += 1 if e.conviction_conflict else 0
            convictions.append(float(e.conviction))
            nets.append(e.net)
            b = buckets.setdefault(e.conviction, {"n": 0, "wins": 0, "net": 0.0})
            b["n"] += 1
            b["wins"] += 1 if e.net > 0 else 0
            b["net"] += e.net

    n = len(convictions)
    enough = n >= min_n
    return Conviction(
        buckets=dict(sorted(buckets.items())),
        tau=stats.kendall_tau_b(convictions, nets) if enough else None,
        p_value=None, n=n, conflicts=conflicts,
    )


def _level_slippage(
    conn: sqlite3.Connection, metas: list[SessionMeta]
) -> dict[str, float | None]:
    fills = [f for m in metas for f in enforcement.level_fills(conn, m.id)]
    detect = [f.detect_bp for f in fills]
    model = [f.model_bp for f in fills]
    total = [f.total_bp for f in fills]
    return {
        "n": float(len(fills)),
        "detect_bp_median": stats.median(detect),
        "model_bp_median": stats.median(model),
        "total_bp_median": stats.median(total),
        "total_bp_p95": stats.percentile(total, 0.95),
    }


def _lateness(
    conn: sqlite3.Connection, metas: list[SessionMeta]
) -> dict[str, float | None]:
    late = [f.late_ms for m in metas for f in enforcement.level_fills(conn, m.id)
            if f.late_ms is not None]
    return {
        "n": float(len(late)),
        "p50_ms": stats.percentile(late, 0.5),
        "p95_ms": stats.percentile(late, 0.95),
    }
