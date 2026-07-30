"""API tests: the reaper, and the guards on starting a session.

The API had no tests, which is how the reaper came to mark a session 'halted'
while leaving its exit plans 'active' and its armed entries 'waiting' -- telling
the dashboard that levels were being enforced by a process that had died.

Control endpoints are mounted only on the loopback listener, so `allow_control`
is the switch these tests flip to reach them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from thepit import config as cfg
from thepit.agent import claude as claude_mod
from thepit.api.main import SESSION_STALE_S, create_app
from thepit.core.clock import now_ms
from thepit.engine.killswitch import KillSwitch
from thepit.store import db


@pytest.fixture
def home(tmp_path):
    return cfg.Config(mode=cfg.Mode.PAPER, home=tmp_path, symbols=["AAPL"])


@pytest.fixture
def conn(home):
    c = db.connect(home.db_path)
    db.migrate(c)
    yield c
    c.close()


def a_session(conn, *, status="running", heartbeat_age_s=0, sid=1) -> int:
    now = now_ms()
    conn.execute(
        "INSERT INTO sessions (id,created_ms,ends_ms,status,config,capital,cash,"
        "heartbeat_ms) VALUES (?,?,?,?,'{}',20,10,?)",
        (sid, now, now + 600_000, status, now - int(heartbeat_age_s * 1000)))
    conn.execute(
        "INSERT INTO positions (session_id,symbol,qty,avg_price) VALUES (?,'AAPL',1,100)",
        (sid,))
    conn.execute(
        "INSERT INTO exit_plans (session_id,symbol,created_ms,updated_ms,long,"
        "entry_price,stop_price,high_water,status) "
        "VALUES (?,'AAPL',?,?,1,100,99.5,100,'active')", (sid, now, now))
    conn.execute(
        "INSERT INTO pending_entries (session_id,created_ms,symbol,side,qty,"
        "trigger_price,direction,expires_ms) "
        "VALUES (?,?,'TSLA','buy',1,297,'at_or_below',?)", (sid, now, now + 600_000))
    conn.commit()
    return sid


def statuses(conn, sid):
    return (
        conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()[0],
        conn.execute("SELECT status FROM exit_plans WHERE session_id=?",
                     (sid,)).fetchone()[0],
        conn.execute("SELECT status FROM pending_entries WHERE session_id=?",
                     (sid,)).fetchone()[0],
    )


# -- the reaper ---------------------------------------------------------------


def test_the_reaper_closes_the_levels_it_orphans(home, conn):
    """The process that would have enforced them is the one that died. Leaving
    them active told the dashboard a stop was being watched by nobody."""
    sid = a_session(conn, heartbeat_age_s=SESSION_STALE_S + 60)
    create_app(home, allow_control=True)     # reaps at startup

    session, plan, entry = statuses(conn, sid)
    assert session == "halted"
    assert plan == "closed"
    assert entry == "cancelled"


def test_a_live_session_is_left_alone(home, conn):
    sid = a_session(conn, heartbeat_age_s=5)
    create_app(home, allow_control=True)
    assert statuses(conn, sid) == ("running", "active", "waiting")


def test_a_session_mid_model_call_is_not_reaped(home, conn):
    """A model call blocks for up to 180s plus a 15s drain. At a 120s threshold
    the reaper marked healthy sessions 'interrupted' while the task was still
    running, and overwrote their halt reason on the way out."""
    assert SESSION_STALE_S > claude_mod.TIMEOUT_S + claude_mod.KILL_DRAIN_S
    sid = a_session(conn, heartbeat_age_s=claude_mod.TIMEOUT_S - 10)
    create_app(home, allow_control=True)
    assert statuses(conn, sid)[0] == "running"


def test_a_session_that_died_before_it_started_is_reaped_too(home, conn):
    """A task that dies during the planning call never reaches 'running' and
    never beats, so the row sat at 'planned' forever with nothing to notice."""
    now = now_ms()
    conn.execute(
        "INSERT INTO sessions (id,created_ms,ends_ms,status,config,capital,cash) "
        "VALUES (9,?,?,'planned','{}',20,20)",
        (now - (SESSION_STALE_S + 60) * 1000, now + 600_000))
    conn.commit()
    create_app(home, allow_control=True)
    assert conn.execute(
        "SELECT status FROM sessions WHERE id=9").fetchone()[0] == "halted"


def test_a_freshly_created_session_is_not_reaped_before_it_starts(home, conn):
    now = now_ms()
    conn.execute(
        "INSERT INTO sessions (id,created_ms,ends_ms,status,config,capital,cash) "
        "VALUES (10,?,?,'planned','{}',20,20)", (now, now + 600_000))
    conn.commit()
    create_app(home, allow_control=True)
    assert conn.execute(
        "SELECT status FROM sessions WHERE id=10").fetchone()[0] == "planned"


def test_the_read_only_listener_never_reaps(home, conn):
    """A LAN viewer must not write to the database, even to tidy it."""
    sid = a_session(conn, heartbeat_age_s=SESSION_STALE_S + 60)
    create_app(home, allow_control=False)
    assert statuses(conn, sid)[0] == "running"


# -- starting a session -------------------------------------------------------


def test_starting_a_session_with_the_kill_switch_engaged_is_refused(home, conn):
    """Otherwise it reports 'running', spends a model call per tick against the
    shared rate window, and has every order rejected."""
    KillSwitch(home.state_dir).engage("test")
    client = TestClient(create_app(home, allow_control=True))
    r = client.post("/api/control/session/start", json={"capital": 20})
    assert r.status_code == 409
    assert "kill switch" in r.json()["errors"][0]


def test_a_zero_length_flatten_window_is_refused(home, conn):
    """It leaves the closing orders one attempt at the buzzer, which is how a
    single stale quote ends a session still holding."""
    client = TestClient(create_app(home, allow_control=True))
    r = client.post("/api/control/session/start",
                    json={"capital": 20, "flatten_before_end_minutes": 0})
    assert r.status_code == 400
    assert any("flatten window" in e for e in r.json()["errors"])


# -- what the session view reports -------------------------------------------


def test_the_session_list_reports_liveness_rather_than_a_stale_status(home, conn):
    """`tradectl sessions` and the dashboard both read this. A dead session that
    still says 'running' with nothing marking it is how two were lost."""
    a_session(conn, heartbeat_age_s=SESSION_STALE_S + 60, sid=1)
    a_session(conn, heartbeat_age_s=2, sid=2)
    client = TestClient(create_app(home, allow_control=True))

    rows = {r["id"]: r for r in client.get("/api/sessions").json()}
    assert rows[1]["status"] == "halted"
    assert rows[1]["alive"] is False
    assert rows[2]["alive"] is True


def test_session_detail_carries_the_levels_being_enforced(home, conn):
    sid = a_session(conn, heartbeat_age_s=2)
    client = TestClient(create_app(home, allow_control=True))
    d = client.get(f"/api/sessions/{sid}").json()
    assert d["exit_plans"][0]["stop_price"] == 99.5
    assert d["pending_entries"][0]["trigger_price"] == 297.0
