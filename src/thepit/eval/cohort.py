"""Which arm a session belongs to, and whether it can be scored at all.

Most of this file is about exclusion, which is where the honesty lives. Every
metric downstream divides by a count, and the fastest way to manufacture a result
at n=4 is to quietly include a session that should not be in the denominator.

Two exclusions have already bitten in principle and would bite silently:

**A session with no decisions is not an LLM session.** One that died before its
first model call has zero `decisions` rows, so a naive "not the stub" test labels
it LLM -- and failed sessions are exactly the ones with no decisions, so the
misclassification pushes the comparison in the flattering direction.

**Fill tiers must never be averaged.** NOTES.md: every fill records its `sim_tier`
so a bar-derived and a quote-derived run can never be averaged into one number.
Here that is a raise, not a docstring.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from thepit.eval import pnl as pnl_mod

# Written verbatim by runner._plan and runner._tick when use_stub is set.
STUB_PROMPT = "(deterministic baseline)"
# Written by stub.plan().
STUB_PLAN_PREFIX = "Deterministic baseline (no model)."


class Arm(StrEnum):
    LLM = "llm"
    BASELINE = "baseline"
    UNKNOWN = "unknown"


class MixedTierError(RuntimeError):
    """Two fill fidelities in one cohort. Averaging them would be meaningless."""


# Exclusion reasons, as constants so a report can group by them instead of
# matching prose.
NO_FILLS = "no_fills"
UNKNOWN_ARM = "unknown_arm"
CASH_MISMATCH = "cash_mismatch"
UNMARKABLE = "unmarkable"
MIXED_TIER = "mixed_tier"
IN_FLIGHT = "in_flight"
DONE_BUT_HOLDING = "done_but_holding"
BLINDED_UNMAPPED = "blinded_unmapped"


@dataclass(frozen=True, slots=True)
class SessionMeta:
    id: int
    arm: Arm
    status: str
    created_ms: int
    started_ms: int | None
    ends_ms: int | None
    mark_ms: int
    capital: float
    model: str | None
    research: str
    blinding: str
    duration_minutes: int
    policy_tick_minutes: int
    fast_loop_seconds: int
    universe: tuple[str, ...]
    tiers: tuple[str, ...]
    twin_of: int | None
    halt_reason: str | None
    excluded: tuple[str, ...]

    @property
    def scorable(self) -> bool:
        return not self.excluded

    @property
    def ended_holding(self) -> bool:
        """A halted session that could not close is a different animal from one
        that halted on its loss limit and closed cleanly. Pooling them mixes a
        real result with an unfalsifiable one."""
        return bool(self.halt_reason and "still holding" in self.halt_reason)


def classify(conn: sqlite3.Connection, session_id: int) -> Arm:
    """LLM, baseline, or unknown. Three-valued, never a boolean.

    Prefers `sessions.arm`, written by the runner at creation (migration 007).
    Falls back to inferring from the decisions table for rows that predate it --
    that inference string-matches an f-string, so rewording a prompt would
    silently reclassify history. New rows never take that path.
    """
    try:
        recorded = conn.execute(
            "SELECT arm FROM sessions WHERE id=?", (session_id,)).fetchone()
    except sqlite3.OperationalError:
        # Database predates migration 007. The eval is read-only and must not
        # be the thing that forces an upgrade -- fall through and infer.
        recorded = None
    if recorded is not None and recorded["arm"]:
        try:
            return Arm(recorded["arm"])
        except ValueError:
            pass

    row = conn.execute(
        "SELECT "
        " EXISTS(SELECT 1 FROM decisions d WHERE d.session_id=s.id AND d.prompt=?) "
        "   AS stub_decisions, "
        " COALESCE(s.plan LIKE ?, 0) AS stub_plan, "
        " EXISTS(SELECT 1 FROM decisions d WHERE d.session_id=s.id AND d.prompt<>?) "
        "   AS model_decisions "
        "FROM sessions s WHERE s.id=?",
        (STUB_PROMPT, STUB_PLAN_PREFIX + "%", STUB_PROMPT, session_id)).fetchone()
    if row is None:
        return Arm.UNKNOWN
    if row["stub_decisions"] or row["stub_plan"]:
        return Arm.BASELINE
    if row["model_decisions"]:
        return Arm.LLM
    return Arm.UNKNOWN


def meta(conn: sqlite3.Connection, session_id: int) -> SessionMeta:
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(f"no session {session_id}")

    config = json.loads(row["config"] or "{}")
    universe = tuple(json.loads(row["universe"] or "[]") or config.get("symbols") or ())
    tiers = tuple(sorted(
        r["sim_tier"] for r in conn.execute(
            "SELECT DISTINCT sim_tier FROM fills WHERE session_id=?", (session_id,))))

    money = pnl_mod.session_pnl(conn, session_id)
    arm = classify(conn, session_id)
    twin_of = row["twin_of"] if "twin_of" in row.keys() else None

    excluded: list[str] = []
    if row["status"] in ("running", "flattening", "planned"):
        excluded.append(IN_FLIGHT)
    if arm is Arm.UNKNOWN:
        excluded.append(UNKNOWN_ARM)
    if money.n_fills == 0:
        # Not a defect -- a flat session is a real outcome and is counted in the
        # flat-rate figure. It is excluded only from P&L statistics, which it
        # would drag toward zero with no information.
        excluded.append(NO_FILLS)
    if money.unmarkable:
        excluded.append(UNMARKABLE)
    if abs(money.discrepancy) >= pnl_mod.CASH_TOLERANCE:
        excluded.append(CASH_MISMATCH)
    if len(tiers) > 1:
        excluded.append(MIXED_TIER)
    if row["status"] == "done" and money.open_qty:
        # `_finish` refuses to write this state, so a row like it predates that
        # fix or its cache diverged. Either way it is not evidence.
        excluded.append(DONE_BUT_HOLDING)
    if config.get("blinding") not in (None, "real"):
        # Blinded arms are unscorable rather than merely unrecorded: the label
        # mapping is never persisted and the order path never inverts it, so an
        # anonymized session's orders could only ever be rejected.
        excluded.append(BLINDED_UNMAPPED)

    return SessionMeta(
        id=session_id, arm=arm, status=row["status"],
        created_ms=row["created_ms"], started_ms=row["started_ms"],
        ends_ms=row["ends_ms"], mark_ms=money.mark_ms, capital=money.capital,
        model=config.get("model"), research=config.get("research", "unknown"),
        blinding=config.get("blinding", "unknown"),
        duration_minutes=int(config.get("duration_minutes", 0)),
        policy_tick_minutes=int(config.get("policy_tick_minutes", 0)),
        fast_loop_seconds=int(config.get("fast_loop_seconds", 0)),
        universe=universe, tiers=tiers, twin_of=twin_of,
        halt_reason=row["halt_reason"], excluded=tuple(excluded),
    )


def all_meta(conn: sqlite3.Connection, *, limit: int = 200) -> list[SessionMeta]:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM sessions ORDER BY id DESC LIMIT ?", (limit,))]
    return [meta(conn, i) for i in reversed(ids)]


def scorable(metas: list[SessionMeta]) -> list[SessionMeta]:
    return [m for m in metas if m.scorable]


def exclusions(metas: list[SessionMeta]) -> dict[str, int]:
    """Reason to count, for printing. A report that silently drops sessions is
    indistinguishable from one that had none to drop."""
    counts: dict[str, int] = {}
    for m in metas:
        for reason in m.excluded:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def require_single_tier(metas: list[SessionMeta]) -> str:
    """The one fill fidelity behind a cohort, or a raise.

    Cost and slippage figures averaged across tiers are not a number. This is the
    guard NOTES.md asks for, as code rather than as a promise.
    """
    tiers = {t for m in metas for t in m.tiers}
    if len(tiers) > 1:
        raise MixedTierError(
            f"cohort mixes fill tiers {sorted(tiers)}; bar-derived and "
            f"quote-derived fills cannot be averaged into one number"
        )
    return next(iter(tiers), "none")


def pair(
    metas: list[SessionMeta], *, overlap: float = 0.9
) -> tuple[list[tuple[SessionMeta, SessionMeta]], list[SessionMeta]]:
    """LLM/baseline pairs.

    Two sources, and they are not equivalent:

    **`twin_of` (migration 007).** A real pair. Both arms were spawned together
    on the same symbols, the same capital and the same quote snapshot, so the
    day's market move is common to both and subtracts out of the difference.

    **Wall-clock overlap.** The fallback for sessions recorded before twins
    existed. Two sessions on the same names over the same minutes saw *similar*
    tape, not the same tape. It is a substitute for a control and is labelled
    provisional wherever it is printed.

    Twinned pairs are matched first and their members are then unavailable to
    the fallback, so a real pair is never broken up to form a guessed one.
    """
    by_id = {m.id: m for m in metas}
    used: set[int] = set()
    pairs: list[tuple[SessionMeta, SessionMeta]] = []

    # Real pairs first.
    for m in metas:
        if m.twin_of is None or m.id in used or m.arm is not Arm.LLM:
            continue
        twin = by_id.get(m.twin_of)
        if twin is not None and twin.id not in used and twin.arm is Arm.BASELINE:
            pairs.append((m, twin))
            used.update({m.id, twin.id})

    llm = [m for m in metas if m.arm is Arm.LLM and m.id not in used]
    base = [m for m in metas if m.arm is Arm.BASELINE and m.id not in used]

    for a in llm:
        for b in base:
            if b.id in used or a.universe != b.universe:
                continue
            if _overlap_fraction(a, b) >= overlap:
                pairs.append((a, b))
                used.add(b.id)
                break

    paired_ids = {m.id for p in pairs for m in p}
    return pairs, [m for m in metas if m.id not in paired_ids]


def twinned(pairs: list[tuple[SessionMeta, SessionMeta]]) -> int:
    """How many pairs are real twins rather than wall-clock guesses.

    Printed beside the pair count so a paired difference is never read as
    stronger evidence than it is.
    """
    return sum(1 for a, b in pairs if a.twin_of == b.id or b.twin_of == a.id)


def _overlap_fraction(a: SessionMeta, b: SessionMeta) -> float:
    a0, a1 = a.started_ms or a.created_ms, a.mark_ms
    b0, b1 = b.started_ms or b.created_ms, b.mark_ms
    shorter = min(a1 - a0, b1 - b0)
    if shorter <= 0:
        return 0.0
    return max(0, min(a1, b1) - max(a0, b0)) / shorter
