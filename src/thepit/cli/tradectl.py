"""tradectl -- operate The Pit from a shell.

    uv run tradectl status
    uv run tradectl kill "reason"
    uv run tradectl release
    uv run tradectl uptime --hours 24

**Everything here works with the API process dead**, which is the whole point.
It talks to the database (read-only) and the state directory directly, never
over HTTP. If the dashboard is what you need to stop something, then the
dashboard being broken means you cannot stop it.

Killing does not even need this tool:

    touch ~/.thepit/state/KILL

That is deliberate. The kill switch is a file so it can be set from any shell,
from cron, or over SSH from a phone, with no Python and no dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from thepit import config as cfg
from thepit.core import calendar
from thepit.core.clock import now_ms
from thepit.engine.killswitch import KillSwitch
from thepit.store import db
from thepit.store.repos import FetchLogRepo, SessionsRepo

def _supports_colour() -> bool:
    """ANSI escapes only when they will actually render.

    Piping `tradectl status` into a file or grep should not litter it with
    escape sequences, and legacy Windows consoles render them as garbage.
    Windows Terminal and PowerShell 7 handle ANSI fine; cmd.exe on older builds
    does not, and there is no reliable way to tell them apart, so the safe
    default on Windows is off unless the environment advertises otherwise.
    """
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM"))
    return True


if _supports_colour():
    GREEN, RED, YELLOW, DIM, RESET = (
        "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
    )
else:
    GREEN = RED = YELLOW = DIM = RESET = ""


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def cmd_status(config: cfg.Config, args) -> int:
    switch = KillSwitch(config.state_dir)
    hb = switch.heartbeat_age_s()
    # A stale heartbeat is the only way to tell "quiet market" from "the engine
    # died three hours ago".
    alive = hb is not None and hb < 30

    print(f"mode      {config.mode.value.upper()}")
    print(f"session   {calendar.state_at(now_ms())}")
    print(
        "engine    "
        + (f"{GREEN}up{RESET} (heartbeat {hb:.0f}s ago)" if alive
           else f"{RED}DOWN{RESET}" + (f" (last beat {hb:.0f}s ago)" if hb else ""))
    )
    if switch.engaged():
        at = switch.engaged_at()
        when = datetime.fromtimestamp(at).strftime("%H:%M:%S") if at else "?"
        print(f"kill      {RED}ENGAGED{RESET} since {when}")
        print(f"          release with: rm {switch.dir / 'KILL'}")
    else:
        print(f"kill      {GREEN}clear{RESET}")

    if not config.db_path.exists():
        print(f"database  {DIM}not created yet{RESET}")
        return 0

    conn = db.connect(config.db_path, readonly=True)
    try:
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("bars", "ticks", "news", "fetch_log", "events")
        }
        print("database  " + "  ".join(f"{k}={v}" for k, v in counts.items()))

        row = conn.execute("SELECT MAX(received_ms) m FROM ticks").fetchone()
        if row and row["m"]:
            age = (now_ms() - row["m"]) / 1000
            colour = GREEN if age < 60 else RED
            print(f"last tick {colour}{age:.0f}s ago{RESET}")

        recent = conn.execute(
            "SELECT ts_ms,level,kind,subject FROM events ORDER BY ts_ms DESC LIMIT 5"
        ).fetchall()
        if recent:
            print("\nrecent events")
            for r in recent:
                mark = {"error": RED, "warn": YELLOW}.get(r["level"], DIM)
                print(f"  {DIM}{_ts(r['ts_ms'])}{RESET} {mark}{r['kind']}{RESET} "
                      f"{r['subject'] or ''}")
    finally:
        conn.close()
    return 0


def cmd_kill(config: cfg.Config, args) -> int:
    switch = KillSwitch(config.state_dir)
    switch.engage(args.reason)
    print(f"{RED}kill engaged{RESET}: {switch.dir / 'KILL'}")
    print("the engine stops within ~1s. Release with: tradectl release")
    return 0


def cmd_release(config: cfg.Config, args) -> int:
    switch = KillSwitch(config.state_dir)
    if not switch.engaged():
        print("kill was not engaged")
        return 0
    switch.release()
    print(f"{GREEN}kill released{RESET}. Restart the engine to resume.")
    return 0


def cmd_sessions(config: cfg.Config, args) -> int:
    """List sessions with P&L computed the ONLY correct way.

    `cash - capital` is not P&L. While a position is open that difference is
    just the money currently sitting in stock, and it reads as a catastrophic
    loss: an interrupted session holding 8 NVDA showed "-$3,060" when its actual
    P&L was -$1.97. Two separate ad-hoc queries made that mistake, so the
    correct calculation lives here instead of being retyped.

    P&L = (cash + positions marked to market) - starting capital.
    """
    if not config.db_path.exists():
        print("no database yet", file=sys.stderr)
        return 1

    conn = db.connect(config.db_path, readonly=True)
    try:
        repo = SessionsRepo(conn)
        marks = repo.marks()
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (args.limit,)
        ).fetchall()
        if not rows:
            print("no sessions yet")
            return 0

        print(f"{'id':>3}  {'status':10s} {'capital':>10s} {'equity':>10s} "
              f"{'P&L':>9s}  {'':2s} positions")
        for r in rows:
            # One calculation, in one place. See SessionsRepo: `cash - capital`
            # is not P&L, and reading it as P&L has already produced a "-$3,060"
            # on a session that was down $1.97.
            money = repo.pnl(r["id"], marks)
            positions = repo.positions(r["id"])
            colour = GREEN if money.pnl > 0 else RED if money.pnl < 0 else ""
            flag = "!" if r["status"] in ("halted", "failed") else " "
            desc = ", ".join(f"{p['symbol']} {p['qty']:g}" for p in positions) or "flat"
            # An open position means the number is not settled. Printing it
            # alongside a closed session's realised figure without saying so is
            # how a paper result flatters itself.
            mark = "" if money.realised else f" {DIM}(unrealised){RESET}"
            print(f"{r['id']:>3}  {r['status']:10s} {r['capital']:>10,.2f} "
                  f"{money.equity:>10,.2f} {colour}{money.pnl:>+9,.2f}{RESET}  "
                  f"{flag}  {desc}{mark}")
            if r["halt_reason"]:
                print(f"     {DIM}{r['halt_reason']}{RESET}")
            # An open position with no active plan is the state the fast loop
            # exists to prevent, so it is shown rather than left to be inferred.
            for p in positions:
                plan = conn.execute(
                    "SELECT stop_price, target_price, trail_bp FROM exit_plans "
                    "WHERE session_id=? AND symbol=? AND status='active'",
                    (r["id"], p["symbol"])).fetchone()
                if plan is None:
                    print(f"     {RED}{p['symbol']}: no exit plan{RESET}")
                    continue
                bits = [f"stop {plan['stop_price']:.2f}"]
                if plan["target_price"]:
                    bits.append(f"target {plan['target_price']:.2f}")
                if plan["trail_bp"]:
                    bits.append(f"trailing {plan['trail_bp']:.0f}bp")
                print(f"     {DIM}{p['symbol']}: {', '.join(bits)}{RESET}")
    finally:
        conn.close()
    return 0


def cmd_uptime(config: cfg.Config, args) -> int:
    """The 24h proof.

    Gaps are the number that matters. A feed can succeed ten thousand times and
    still have been dead for the three hours you cared about, so a success count
    alone cannot answer "did it stay up".
    """
    if not config.db_path.exists():
        print("no database yet", file=sys.stderr)
        return 1

    conn = db.connect(config.db_path, readonly=True)
    try:
        repo = FetchLogRepo(conn)
        until = now_ms()
        since = until - args.hours * 3_600_000

        first = conn.execute("SELECT MIN(ts_ms) m FROM fetch_log").fetchone()["m"]
        if first and first > since:
            # Otherwise the first run always reports a ~24h "gap" for the time
            # before the engine existed, which is true and useless.
            print(f"{DIM}note: only {(until - first) / 3_600_000:.1f}h of history "
                  f"exists; measuring from first fetch{RESET}\n")
            since = first

        summary = repo.uptime(since, until)
        if not summary:
            print("no fetches in window")
            return 1

        print(f"window    {_ts(since)} -> {_ts(until)} "
              f"({(until - since) / 3_600_000:.1f}h)\n")
        for source, s in sorted(summary.items()):
            rate = float(s["success_rate"]) * 100
            colour = GREEN if rate >= 99 else YELLOW if rate >= 95 else RED
            print(f"{source:8s} {colour}{rate:6.2f}%{RESET} of {s['total']:>6} fetches"
                  f"   p50 {s['p50_ms']}ms   p99 {s['p99_ms']}ms"
                  f"   {RED if s['failed'] else DIM}{s['failed']} failed{RESET}")

        gaps = repo.gaps(since, until, max_gap_ms=args.max_gap_min * 60_000)
        print()
        if gaps:
            print(f"{RED}{len(gaps)} gap(s) longer than {args.max_gap_min}min{RESET}")
            for a, b in gaps:
                print(f"  {_ts(a)} -> {_ts(b)}  ({(b - a) / 60_000:.1f} min)")
        else:
            print(f"{GREEN}no gap longer than {args.max_gap_min} minutes{RESET}")

        if args.json:
            print("\n" + json.dumps(
                {"by_source": summary,
                 "gaps": [{"from_ms": a, "to_ms": b} for a, b in gaps]},
                indent=2, default=str))
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tradectl", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="engine, kill switch, and data counts")

    k = sub.add_parser("kill", help="engage the global kill switch")
    k.add_argument("reason", nargs="?", default="tradectl")

    sub.add_parser("release", help="clear the kill switch (deliberate, manual)")

    ss = sub.add_parser("sessions", help="session list with correct P&L")
    ss.add_argument("--limit", type=int, default=10)

    u = sub.add_parser("uptime", help="feed uptime and gap report")
    u.add_argument("--hours", type=int, default=24)
    u.add_argument("--max-gap-min", type=int, default=10)
    u.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    config = cfg.load(mode=cfg.Mode.PAPER)

    return {
        "status": cmd_status, "kill": cmd_kill, "sessions": cmd_sessions,
        "release": cmd_release, "uptime": cmd_uptime,
    }[args.cmd](config, args)


if __name__ == "__main__":
    raise SystemExit(main())
