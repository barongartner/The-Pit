"""The API and dashboard server.

Runs as a **separate process** from the engine and opens the database
**read-only**. Splitting them means `uvicorn --reload` can restart this freely
while the engine keeps polling, which matters because the engine's entire job in
Stage 1 is uptime.

## Access model

This dashboard will eventually have buttons that move money, so the split is
enforced server-side rather than in the UI:

* **Control** endpoints are mounted only when bound to loopback. Not hidden, not
  permission-checked -- *not mounted*, so they 404 rather than 403 when reached
  from anywhere else. A UI check is not a security boundary and neither is a
  flag someone can flip.
* **Read-only** endpoints are all that a LAN listener ever serves.

Nothing here goes on the public internet.

## Why the WebSocket reads the database

`MarketView` lives in the engine process, so this one cannot see it. Polling the
read-only database once a second and pushing deltas is unglamorous, has no IPC
to debug, and is entirely adequate for one user on a laptop. If it ever is not,
the fix is a socket, not a rewrite.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from thepit import config as cfg
from thepit.core import calendar
from thepit.core.clock import SystemClock, now_ms
from thepit.engine.killswitch import KillSwitch
from thepit.store import db
from thepit.session.config import (
    Blinding, ResearchAccess, RiskProfile, SessionConfig, SessionReadiness,
)
from thepit.session.prompt import build_plan_prompt
from thepit.session.runner import SessionRunner
from thepit.agent import claude as claude_mod
from thepit.core.types import FeedTier, Quote
from thepit.eval import pnl as eval_pnl
from thepit.store.repos import BarsRepo, FetchLogRepo, NewsRepo

WEB_DIR = Path(__file__).resolve().parents[3] / "web"

# One second is plenty: the underlying feed updates every five seconds during
# regular hours and every five minutes when closed. Pushing faster would just
# heat the laptop.
PUSH_INTERVAL_S = 1.0


# A session whose heartbeat is older than this is not being driven by anything.
#
# It MUST outlast the slowest thing a live session can legitimately be doing,
# which is one model call: 180s of timeout plus a 15s drain. At 120s the reaper
# marked healthy sessions 'interrupted' mid-call and overwrote their halt reason
# while the task was still running. The runner also beats every second through a
# model call now; this margin is the belt to that braces.
SESSION_STALE_S = int(claude_mod.TIMEOUT_S + claude_mod.KILL_DRAIN_S + 60)


def _reap_orphans(config: cfg.Config) -> int:
    """Mark sessions whose driving process is gone.

    Sessions run as an asyncio task inside this process. Restart the API and the
    task dies -- but the row still said 'running', forever, with nothing to
    detect it. That is how a session was lost with no trace: it looked alive and
    was not.

    Called at startup and on every session read, so the state can never be a
    lie for longer than one poll.
    """
    conn = db.connect(config.db_path)
    try:
        now = now_ms()
        cutoff = now - SESSION_STALE_S * 1000
        dead = [r["id"] for r in conn.execute(
            "SELECT id FROM sessions WHERE "
            "(status IN ('running','flattening') "
            "  AND (heartbeat_ms IS NULL OR heartbeat_ms < ?)) "
            # 'planned' is included because a task that dies during the planning
            # call never reaches 'running' and never beats, so the row sat at
            # 'planned' forever with nothing able to notice. Gated on age so a
            # session created milliseconds ago is not reaped before it starts.
            "OR (status='planned' AND created_ms < ?)", (cutoff, cutoff))]
        if not dead:
            return 0

        marks = ",".join("?" * len(dead))
        with db.immediate(conn):
            conn.execute(
                "UPDATE sessions SET status='halted', "
                "halt_reason=COALESCE(halt_reason,"
                "'interrupted: the process driving this session stopped'), "
                f"finished_ms=? WHERE id IN ({marks})", (now, *dead))
            # Nothing is enforcing these any more -- the process that would have
            # was the one that died. Leaving them 'active' told the dashboard a
            # stop was being watched when no code was watching anything.
            conn.execute(
                f"UPDATE exit_plans SET status='closed', updated_ms=? "
                f"WHERE session_id IN ({marks}) AND status='active'", (now, *dead))
            conn.execute(
                f"UPDATE pending_entries SET status='cancelled', resolved_ms=? "
                f"WHERE session_id IN ({marks}) AND status='waiting'", (now, *dead))
        return len(dead)
    finally:
        conn.close()


def create_app(config: cfg.Config, *, allow_control: bool) -> FastAPI:
    app = FastAPI(title="The Pit", docs_url=None, redoc_url=None)

    if allow_control:
        # Only the writer reaps. A read-only LAN listener must not try.
        reaped = _reap_orphans(config)
        if reaped:
            print(f"marked {reaped} orphaned session(s) as interrupted")

    # Read-only at the URI level. A stray write raises at the offending line
    # instead of showing up as intermittent SQLITE_BUSY under load.
    def conn() -> sqlite3.Connection:
        return db.connect(config.db_path, readonly=True)

    read = APIRouter()

    @read.get("/api/status")
    def status() -> JSONResponse:
        c = conn()
        try:
            switch = KillSwitch(config.state_dir)
            hb = switch.heartbeat_age_s()
            return JSONResponse({
                "mode": config.mode.value,
                "session": calendar.state_at(now_ms()),
                "minutes_to_close": calendar.minutes_to_close(now_ms()),
                "kill_engaged": switch.engaged(),
                # The engine is a different process. A stale heartbeat is the
                # only way this one can tell the difference between "quiet
                # market" and "the engine died an hour ago".
                "engine_heartbeat_age_s": round(hb, 1) if hb is not None else None,
                "engine_alive": hb is not None and hb < 30,
                "symbols": config.symbols,
                "counts": {
                    t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("bars", "ticks", "news", "fetch_log", "events")
                },
                "control_enabled": allow_control,
            })
        finally:
            c.close()

    @read.get("/api/quotes")
    def quotes() -> JSONResponse:
        return JSONResponse(_latest_quotes(conn(), config.symbols, close=True))

    @read.get("/api/bars/{symbol}")
    def bars(symbol: str, tf: str = "1m", limit: int = 120) -> JSONResponse:
        c = conn()
        try:
            rows = c.execute(
                "SELECT ts_ms,o,h,l,c,v FROM bars WHERE symbol=? AND tf=? "
                "ORDER BY ts_ms DESC LIMIT ?",
                (symbol.upper(), tf, min(limit, 1000)),
            ).fetchall()
            return JSONResponse([dict(r) for r in reversed(rows)])
        finally:
            c.close()

    @read.get("/api/news")
    def news(limit: int = 30) -> JSONResponse:
        c = conn()
        try:
            # as_of is `now` here, which is the trivial case. The argument is
            # still required, because the guard is the signature -- see
            # store/repos.py.
            items = NewsRepo(c).as_of(now_ms(), limit=min(limit, 200))
            return JSONResponse([
                {"published_ms": i.published_ms, "symbols": list(i.symbols),
                 "headline": i.headline, "summary": i.summary, "url": i.url,
                 "source": i.source, "kind": i.kind}
                for i in items
            ])
        finally:
            c.close()

    @read.get("/api/health")
    def health(hours: int = 24) -> JSONResponse:
        """The uptime proof. Gaps matter more than the success count: a feed can
        succeed 10,000 times and still have been dead for the three hours you
        care about."""
        c = conn()
        try:
            until = now_ms()
            since = until - hours * 3_600_000
            repo = FetchLogRepo(c)

            # Measure from the first fetch we ever made, not from 24h ago.
            # Otherwise every fresh install reports a ~24 hour "gap" covering
            # the time before the engine existed -- technically true, and
            # useless. It also trains you to ignore the gap display, which is
            # the one number that actually proves uptime.
            first = c.execute("SELECT MIN(ts_ms) m FROM fetch_log").fetchone()["m"]
            partial = bool(first and first > since)
            if partial:
                since = first

            gaps = repo.gaps(since, until, max_gap_ms=600_000)
            events = [
                dict(r) for r in c.execute(
                    "SELECT ts_ms,level,kind,subject,detail FROM events "
                    "WHERE ts_ms >= ? ORDER BY ts_ms DESC LIMIT 50", (since,)
                )
            ]
            return JSONResponse({
                "window_hours": hours,
                "measured_from_ms": since,
                "partial_window": partial,
                "by_source": repo.uptime(since, until),
                "gaps": [{"from_ms": a, "to_ms": b, "minutes": round((b - a) / 60000, 1)}
                         for a, b in gaps],
                "events": events,
            })
        finally:
            c.close()

    @read.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        """Snapshot, then deltas.

        A client that reconnects must not have to reason about what it missed,
        so every connection starts with a full snapshot and only then receives
        changes.
        """
        await sock.accept()
        c = conn()
        last: dict[str, dict] = {}
        try:
            snapshot = _latest_quotes(c, config.symbols)
            last = {q["symbol"]: q for q in snapshot}
            await sock.send_text(json.dumps({"type": "snapshot", "quotes": snapshot,
                                             "session": calendar.state_at(now_ms())}))
            while True:
                await asyncio.sleep(PUSH_INTERVAL_S)
                current = _latest_quotes(c, config.symbols)
                changed = [
                    q for q in current
                    if last.get(q["symbol"], {}).get("ts_ms") != q["ts_ms"]
                ]
                last = {q["symbol"]: q for q in current}
                if changed:
                    await sock.send_text(json.dumps({"type": "delta", "quotes": changed}))
                else:
                    # Keeps proxies from idling the socket out, and lets the
                    # client distinguish "nothing changed" from "server gone".
                    await sock.send_text(json.dumps({"type": "heartbeat",
                                                     "ts_ms": now_ms()}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                c.close()

    @read.get("/api/sessions")
    def sessions() -> JSONResponse:
        # Reaped here too, not only in the detail view. This is the endpoint the
        # scoreboard reads, and a dead session that still says 'running' with
        # nothing marking it is how two sessions were lost unnoticed.
        if allow_control:
            _reap_orphans(config)
        c = conn()
        try:
            rows = c.execute(
                "SELECT id,created_ms,started_ms,finished_ms,status,capital,cash,"
                "halt_reason,heartbeat_ms FROM sessions ORDER BY id DESC LIMIT 25"
            ).fetchall()
            now = now_ms()
            out = []
            for r in rows:
                d = dict(r)
                # Computed rather than written, so a read-only listener that
                # cannot reap still shows the truth.
                d["alive"] = bool(
                    r["status"] in ("running", "flattening")
                    and r["heartbeat_ms"] is not None
                    and now - r["heartbeat_ms"] <= SESSION_STALE_S * 1000
                )
                out.append(d)
            return JSONResponse(out)
        finally:
            c.close()

    @read.get("/api/sessions/{sid}/activity")
    def session_activity(sid: int, after: int = 0) -> JSONResponse:
        c = conn()
        try:
            rows = c.execute(
                "SELECT id,ts_ms,kind,message,pending FROM activity "
                "WHERE session_id=? AND id > ? ORDER BY id LIMIT 200",
                (sid, after)).fetchall()
            return JSONResponse([dict(r) for r in rows])
        finally:
            c.close()

    @read.get("/api/sessions/{sid}")
    def session_detail(sid: int) -> JSONResponse:
        if allow_control:
            _reap_orphans(config)
        c = conn()
        try:
            s_row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if s_row is None:
                return JSONResponse({"error": "no such session"}, status_code=404)

            positions = [dict(r) for r in c.execute(
                "SELECT * FROM positions WHERE session_id=?", (sid,))]
            # The shared calculation, not a third local copy of it. The two
            # earlier ad-hoc versions are what produced a "-$3,060" P&L on a
            # session that was down $1.97. A live session is marked at the
            # current tape; a finished one at its own clock.
            live = s_row["status"] in ("running", "flattening")
            money = eval_pnl.session_pnl(c, sid, at_ms=now_ms() if live else None)

            stale = (
                s_row["status"] in ("running", "flattening")
                and (s_row["heartbeat_ms"] is None
                     or now_ms() - s_row["heartbeat_ms"] > SESSION_STALE_S * 1000)
            )
            return JSONResponse({
                "session": dict(s_row),
                "alive": not stale,
                "equity": money.equity,
                "pnl": money.pnl,
                # False while anything is open, which changes what the number
                # means. The UI says so rather than leaving it to be assumed.
                "pnl_realised": money.realised,
                "positions": positions,
                # What Python is enforcing right now, and what it is waiting on.
                # Both are the answer to "why did that close by itself", which is
                # otherwise only inferable from the activity log.
                "exit_plans": [dict(r) for r in c.execute(
                    "SELECT * FROM exit_plans WHERE session_id=? "
                    "ORDER BY status='active' DESC, symbol", (sid,))],
                "pending_entries": [dict(r) for r in c.execute(
                    "SELECT * FROM pending_entries WHERE session_id=? "
                    "ORDER BY status='waiting' DESC, id DESC LIMIT 25", (sid,))],
                # Rejected orders are included deliberately: "what did it want
                # to do that it could not" is the more interesting question.
                "orders": [dict(r) for r in c.execute(
                    "SELECT * FROM orders WHERE session_id=? ORDER BY id DESC LIMIT 50",
                    (sid,))],
                "fills": [dict(r) for r in c.execute(
                    "SELECT * FROM fills WHERE session_id=? ORDER BY id DESC LIMIT 50",
                    (sid,))],
                "decisions": [dict(r) for r in c.execute(
                    "SELECT id,ts_ms,phase,response,error,latency_ms,cost_usd "
                    "FROM decisions WHERE session_id=? ORDER BY id", (sid,))],
            })
        finally:
            c.close()

    @read.post("/api/session/preview")
    async def session_preview(payload: dict) -> JSONResponse:
        """Resolve a session config and render the exact prompt an agent would see.

        Deliberately on the READ router and deliberately named preview: it
        touches no capital and places no orders. It exists because the prompt is
        the highest-leverage text in the project and it should be reviewable
        before any of the machinery that could act on it exists.
        """
        cfg_obj, errors = _parse_session(payload, config.symbols)
        if errors:
            return JSONResponse({"errors": errors}, status_code=400)

        c = conn()
        try:
            symbols = list(cfg_obj.symbols) or config.symbols
            # No bid/ask on the current feed, so the round-trip cost is not
            # measurable. Passing None makes the prompt say so explicitly
            # rather than omit it -- a missing cost reads as "free".
            has_book = c.execute(
                "SELECT COUNT(*) FROM ticks WHERE bid IS NOT NULL"
            ).fetchone()[0] > 0

            prompt = build_plan_prompt(
                c, cfg_obj, symbols,
                now_ms=now_ms(),
                round_trip_cost_bp=None,
            )
            bars = BarsRepo(c)
            readiness = _readiness(cfg_obj, symbols, bars, has_book)
            return JSONResponse({
                "config": {
                    "duration_minutes": cfg_obj.duration_minutes,
                    "capital": cfg_obj.capital,
                    "symbols": symbols,
                    "policy_ticks": cfg_obj.tick_count,
                    "fast_loop_seconds": cfg_obj.fast_loop_seconds,
                    "model_calls": cfg_obj.model_calls,
                    "latency_s": cfg_obj.estimated_latency_s,
                    "trading_minutes": cfg_obj.trading_minutes,
                    "blinding": cfg_obj.blinding.value,
                    "research": cfg_obj.research.value,
                },
                "readiness": {
                    "can_execute": readiness.can_execute,
                    "missing": readiness.missing,
                    "warnings": readiness.warnings,
                },
                "prompt": prompt,
            })
        finally:
            c.close()

    app.include_router(read)

    if allow_control:
        control = APIRouter(prefix="/api/control")

        @control.post("/kill")
        def kill(reason: str = "dashboard") -> JSONResponse:
            KillSwitch(config.state_dir).engage(reason)
            return JSONResponse({"ok": True, "engaged": True})

        @control.post("/release")
        def release() -> JSONResponse:
            """Clearing the kill is deliberately a separate, explicit action.
            Recovery is a human decision, never automatic."""
            KillSwitch(config.state_dir).release()
            return JSONResponse({"ok": True, "engaged": False})

        @control.post("/session/start")
        async def session_start(payload: dict) -> JSONResponse:
            cfg_obj, errors = _parse_session(payload, config.symbols)
            if errors:
                return JSONResponse({"errors": errors}, status_code=400)

            switch = KillSwitch(config.state_dir)
            if switch.engaged():
                # Otherwise it reports 'running', spends a plan call plus a tick
                # call per interval against the shared rate window, and has every
                # single order rejected. Releasing the kill is a deliberate human
                # act; so is starting a session into it.
                return JSONResponse(
                    {"errors": [f"the kill switch is engaged: "
                                f"{switch.dir / 'KILL'}. Release it first."]},
                    status_code=409)

            wconn = db.connect(config.db_path)
            symbols = list(cfg_obj.symbols) or config.symbols
            quotes = _quotes_for(wconn, symbols)
            if not quotes:
                wconn.close()
                return JSONResponse(
                    {"errors": ["no prices available yet; let the engine run first"]},
                    status_code=400)

            use_stub = bool(payload.get("use_stub")) or not claude_mod.available()

            def build(stub: bool) -> SessionRunner:
                # Falls back to the deterministic baseline when no model is
                # reachable, rather than failing the session outright. It is the
                # control group anyway, so running it is never wasted.
                return SessionRunner(
                    wconn, SystemClock(), cfg_obj, symbols,
                    quotes=quotes, tier=FeedTier.BARS,
                    kill=KillSwitch(config.state_dir), use_stub=stub,
                )

            runner = build(use_stub)
            sid = runner.create()

            # The twin. Same symbols, same capital, same clock, and -- through
            # the shared refresher below -- the same quote snapshot at every
            # decision.
            #
            # Paired is the whole point. Unpaired, the difference between arms is
            # swamped by whatever the market did that day, which is a confounder
            # worth far more basis points than any edge being measured. Run
            # together on one tape, the market move is common to both and
            # subtracts out.
            #
            # Skipped when this session IS the baseline: a control needs no
            # control, and twinning one would just burn a second slot.
            twin: SessionRunner | None = None
            twin_id: int | None = None
            if cfg_obj.run_baseline and not use_stub:
                twin = build(True)
                twin_id = twin.create(twin_of=sid)
                wconn.execute("UPDATE sessions SET twin_of=? WHERE id=?",
                              (twin_id, sid))
                wconn.commit()

            async def drive() -> None:
                runners = [r for r in (runner, twin) if r is not None]
                try:
                    # Refresh quotes from the recorder while the session runs.
                    # The runner never fetches; it reads what the poller wrote.
                    # ONE refresher for both arms, so neither can win on a
                    # fresher tick than the other saw.
                    async def refresh() -> None:
                        while True:
                            await asyncio.sleep(5)
                            snapshot = _quotes_for(wconn, symbols)
                            for r in runners:
                                r.update_quotes(snapshot)

                    task = asyncio.create_task(refresh())
                    try:
                        # gather, not a task group: one arm failing must not
                        # cancel the other. A crashed baseline should cost the
                        # pairing, not the session.
                        await asyncio.gather(
                            *(r.run() for r in runners), return_exceptions=True)
                    finally:
                        task.cancel()
                finally:
                    with contextlib.suppress(Exception):
                        wconn.close()

            asyncio.create_task(drive())
            return JSONResponse({
                "session_id": sid, "twin_id": twin_id, "status": "running",
            })

        app.include_router(control)

    if WEB_DIR.exists():
        app.mount("/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="vendor")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


def _quotes_for(c: sqlite3.Connection, symbols: list[str]) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    for sym in set(symbols):
        r = c.execute(
            "SELECT * FROM ticks WHERE symbol=? ORDER BY ts_ms DESC LIMIT 1", (sym,)
        ).fetchone()
        if r:
            out[sym] = Quote(
                symbol=r["symbol"], ts_ms=r["ts_ms"], last=r["last"],
                source=r["source"], received_ms=r["received_ms"],
                bid=r["bid"], ask=r["ask"])
    return out


def _parse_session(
    payload: dict, default_symbols: list[str]
) -> tuple[SessionConfig, list[str]]:
    try:
        cfg_obj = SessionConfig(
            duration_minutes=int(payload.get("duration_minutes", 30)),
            capital=float(payload.get("capital", 10_000)),
            symbols=tuple(payload.get("symbols") or ()),
            policy_tick_minutes=int(payload.get("policy_tick_minutes", 5)),
            fast_loop_seconds=int(payload.get("fast_loop_seconds", 5)),
            max_position_pct=float(payload.get("max_position_pct", 20)),
            max_concurrent_positions=int(payload.get("max_concurrent_positions", 3)),
            session_loss_limit_pct=float(payload.get("session_loss_limit_pct", 2)),
            flatten_before_end_minutes=int(payload.get("flatten_before_end_minutes", 2)),
            model=str(payload.get("model", "sonnet")),
            effort=str(payload.get("effort", "medium")),
            research=ResearchAccess(payload.get("research", "ambient")),
            blinding=Blinding(payload.get("blinding", "real")),
            run_baseline=bool(payload.get("run_baseline", True)),
            notes=str(payload.get("notes", "")),
        )
        # A profile overrides the three risk fields wholesale. Applied after
        # construction so an explicit value can still win by sending
        # profile="custom".
        profile = payload.get("risk_profile")
        if profile and profile != "custom":
            cfg_obj = cfg_obj.with_profile(RiskProfile(profile))
    except (ValueError, TypeError) as exc:
        return SessionConfig(), [f"malformed config: {exc}"]
    return cfg_obj, cfg_obj.validate()


def _readiness(
    cfg_obj: SessionConfig, symbols: list[str], bars: BarsRepo, has_book: bool
) -> SessionReadiness:
    """What is missing before this session could actually trade.

    Separate from config validation: "you typed something wrong" and "this part
    of the system does not exist yet" are different failures and the UI must not
    present them the same way.
    """
    missing: list[str] = []
    warnings: list[str] = []

    if not claude_mod.available():
        missing.append(
            "the `claude` CLI is not on PATH, so no agent can be asked for a decision"
        )
    if not has_book:
        warnings.append(
            "No bid/ask in the data, so the round-trip cost cannot be measured. "
            "The prompt says so explicitly rather than implying trading is free. "
            "Connect Alpaca (issue #11) before any cost-sensitive session."
        )
    thin = [s for s in symbols if len(bars.latest(s, "1m", limit=30)) < 30]
    if thin:
        warnings.append(
            f"Thin bar history for {', '.join(thin[:5])}"
            + (f" and {len(thin) - 5} more" if len(thin) > 5 else "")
            + ". Let the recorder run longer before judging a plan built on it."
        )
    return SessionReadiness(can_execute=not missing, missing=missing,
                            warnings=warnings)


def _latest_quotes(
    c: sqlite3.Connection, symbols: list[str], *, close: bool = False
) -> list[dict]:
    """Latest tick per symbol, with the previous close for a change figure."""
    try:
        rows = c.execute(
            """
            SELECT t.symbol, t.ts_ms, t.last, t.bid, t.ask, t.source, t.received_ms
            FROM ticks t
            JOIN (SELECT symbol, MAX(ts_ms) AS m FROM ticks GROUP BY symbol) x
              ON x.symbol = t.symbol AND x.m = t.ts_ms
            """
        ).fetchall()
        out = []
        for r in rows:
            if r["symbol"] not in symbols:
                continue
            d = dict(r)
            first = c.execute(
                "SELECT c FROM bars WHERE symbol=? AND tf='1m' ORDER BY ts_ms LIMIT 1",
                (r["symbol"],),
            ).fetchone()
            d["open"] = first["c"] if first else None
            d["change_pct"] = (
                round((d["last"] - d["open"]) / d["open"] * 100, 2)
                if d["open"] else None
            )
            # Surfaced rather than hidden: a quote that stopped updating looks
            # exactly like a quiet market otherwise.
            d["age_s"] = round((now_ms() - r["received_ms"]) / 1000, 1)
            out.append(d)
        return sorted(out, key=lambda q: symbols.index(q["symbol"]))
    finally:
        if close:
            c.close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import uvicorn

    p = argparse.ArgumentParser(prog="thepit-api")
    p.add_argument(
        "--lan", action="store_true",
        help="Bind to all interfaces, READ ONLY. Control endpoints are not "
             "mounted. For viewing from another machine on your own network.",
    )
    p.add_argument("--port", type=int)
    args = p.parse_args(argv)

    config = cfg.load(mode=cfg.Mode.PAPER)

    # PORT is honoured so a supervisor can assign one. Explicit --port still wins.
    env_port = os.environ.get("PORT")

    if args.lan:
        host = "0.0.0.0"  # noqa: S104 - deliberate, read-only router only
        port = args.port or (int(env_port) if env_port else config.lan_port)
        allow_control = False
        print(f"LAN mode: READ ONLY on {host}:{port}. Control endpoints not mounted.")
    else:
        host = "127.0.0.1"
        port = args.port or (int(env_port) if env_port else config.api_port)
        allow_control = True

    uvicorn.run(create_app(config, allow_control=allow_control),
                host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
