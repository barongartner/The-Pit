"""THE P&L. One definition, and this is it.

**P&L is never `cash - capital`.** While a position is open, that difference is
just the money currently sitting in stock. An interrupted session holding 8 NVDA
reported "-$3,060" by that arithmetic when its actual P&L was -$1.97. Two
separate ad-hoc queries made the mistake, and a third copy of the calculation
lived in the API until this module replaced all of them.

    P&L = (cash + positions marked to market) - starting capital

Three things this module does that a naive query does not:

**It rebuilds from fills.** `positions` is a cache -- 002_trading.sql says so --
and `sessions.cash` is a running total. Fills are the truth, so cash and quantity
are recomputed from them and the difference against the cached value is reported
as `discrepancy` rather than hidden. A session whose cache disagrees with its
fills is not scored; it is flagged.

**It marks at the session's own clock.** A finished session must be valued at the
prices that existed when it ended, not at today's. Scoring last Tuesday's session
against this morning's tape invents P&L out of the weekend.

**It refuses to guess.** A held symbol with no tick at or before the mark instant
is returned in `unmarkable` and the session is not scorable. Falling back to the
entry price -- which is what a live view has to do to avoid claiming a total loss
from a missing quote -- would report exactly zero unrealised P&L and hide the
entire position.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Below this, a quantity is zero. Fractional shares make an exact-zero test
# unsafe: a buy of 0.178042 and a sell of the same leaves float dust.
DUST = 1e-9

# Cash rebuilt from fills should match `sessions.cash` to the cent. More than
# this means the ledger and its cache disagree, and the session is not scorable.
CASH_TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class SessionPnL:
    session_id: int
    capital: float
    mark_ms: int          # the instant every held position was valued at

    cash: float           # rebuilt from fills
    held: float           # positions marked to market at mark_ms
    equity: float
    pnl: float
    pnl_bp: float         # of capital, so different account sizes compare

    costs: float          # modeled slippage already inside the fill prices
    gross: float          # pnl + costs: what it would have made for free
    notional: float       # traded volume at reference prices

    n_fills: int
    open_qty: dict[str, float]
    marks: dict[str, float]
    unmarkable: tuple[str, ...]
    discrepancy: float    # rebuilt cash minus the cached `sessions.cash`

    @property
    def realised(self) -> bool:
        """False while anything is open. It changes what the number means."""
        return not self.open_qty

    @property
    def scorable(self) -> bool:
        return not self.unmarkable and abs(self.discrepancy) < CASH_TOLERANCE


def mark_instant(row: sqlite3.Row) -> int:
    """When to value this session's positions.

    `finished_ms` for a session that ended, its last heartbeat for one that was
    interrupted, its scheduled end otherwise. Never "now": a session that ended
    on Friday priced at Monday's open is fiction.
    """
    for key in ("finished_ms", "heartbeat_ms", "ends_ms"):
        # `.keys()` is load-bearing: `key in row` on a sqlite3.Row tests its
        # VALUES, so dropping it silently changes what this checks.
        value = row[key] if key in row.keys() else None
        if value:
            return int(value)
    return int(row["created_ms"])


def mark_at(conn: sqlite3.Connection, symbol: str, at_ms: int) -> float | None:
    """Last traded price at or before `at_ms`. None rather than a guess.

    `ORDER BY source` breaks a two-feed tie deterministically -- the ticks
    primary key includes the source, so two providers stamping the same
    millisecond would otherwise make the mark depend on row order.
    """
    row = conn.execute(
        "SELECT last FROM ticks WHERE symbol=? AND ts_ms <= ? "
        "ORDER BY ts_ms DESC, source LIMIT 1", (symbol, at_ms)).fetchone()
    return float(row["last"]) if row else None


def open_from_fills(conn: sqlite3.Connection, session_id: int) -> dict[str, float]:
    """Signed quantity per symbol, from the fill stream rather than the cache."""
    rows = conn.execute(
        "SELECT symbol, SUM(CASE WHEN side='buy' THEN qty ELSE -qty END) AS qty "
        "FROM fills WHERE session_id=? GROUP BY symbol", (session_id,)).fetchall()
    return {r["symbol"]: r["qty"] for r in rows if abs(r["qty"]) > DUST}


def cash_from_fills(
    conn: sqlite3.Connection, session_id: int, capital: float
) -> tuple[float, float, float, int]:
    """Cash, costs, notional and fill count, rebuilt from fills.

    Costs are NOT subtracted again: `simulate_fill` puts slippage inside the fill
    price, so the cash figure has already paid it. Double-counting it is the
    easiest way to manufacture a loss that never happened.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN qty*price ELSE -qty*price END),0)"
        " AS spent, COALESCE(SUM(cost),0) AS costs, "
        "COALESCE(SUM(qty*ref_price),0) AS notional, COUNT(*) AS n "
        "FROM fills WHERE session_id=?", (session_id,)).fetchone()
    return capital - row["spent"], row["costs"], row["notional"], row["n"]


def session_pnl(
    conn: sqlite3.Connection, session_id: int, *, at_ms: int | None = None
) -> SessionPnL:
    """The money, for one session.

    `at_ms` overrides the mark instant. The live dashboard passes `now` to see a
    running session at current prices; scoring leaves it alone so a finished
    session is valued at its own clock.
    """
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(f"no session {session_id}")

    mark_ms = at_ms if at_ms is not None else mark_instant(row)
    capital = float(row["capital"])
    cash, costs, notional, n_fills = cash_from_fills(conn, session_id, capital)

    open_qty = open_from_fills(conn, session_id)
    marks: dict[str, float] = {}
    unmarkable: list[str] = []
    held = 0.0
    for symbol, qty in open_qty.items():
        price = mark_at(conn, symbol, mark_ms)
        if price is None:
            unmarkable.append(symbol)
            continue
        marks[symbol] = price
        held += qty * price

    equity = cash + held
    pnl = equity - capital
    return SessionPnL(
        session_id=session_id, capital=capital, mark_ms=mark_ms,
        cash=cash, held=held, equity=equity, pnl=pnl,
        pnl_bp=pnl / capital * 10_000 if capital else 0.0,
        costs=costs, gross=pnl + costs, notional=notional,
        n_fills=n_fills, open_qty=open_qty, marks=marks,
        unmarkable=tuple(sorted(unmarkable)),
        discrepancy=cash - float(row["cash"]),
    )
