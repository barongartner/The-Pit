"""Measurement. Read-only, and the only place that decides what a number means.

`docs/NOTES.md` is the methodology; this package is its implementation. Nothing
here opens, writes to, or closes a database: every function takes a connection the
caller opened read-only, so an eval run can never be the thing that corrupts a
session it was scoring.

Start at `report.session_report` for one session and `report.cohort_report` for
all of them. `pnl.session_pnl` is the only definition of P&L in the project.
"""

from thepit.eval.cohort import Arm, MixedTierError, SessionMeta
from thepit.eval.pnl import SessionPnL, session_pnl
from thepit.eval.report import CohortReport, SessionReport, cohort_report, session_report

__all__ = [
    "Arm",
    "CohortReport",
    "MixedTierError",
    "SessionMeta",
    "SessionPnL",
    "SessionReport",
    "cohort_report",
    "session_pnl",
    "session_report",
]
