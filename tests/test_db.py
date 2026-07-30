"""Storage layer tests.

Every CHECK constraint and structural guarantee gets a test that asserts it
*fires*. A constraint nobody has ever seen reject anything is a constraint you
do not know is wired up.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from thepit.store import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def test_migrate_is_idempotent(tmp_path):
    c = db.connect(tmp_path / "t.db")
    assert db.schema_version(c) == 0
    v1 = db.migrate(c)
    v2 = db.migrate(c)
    assert v1 == v2 > 0
    c.close()


def test_migrate_creates_expected_tables(conn):
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"bars", "ticks", "fetch_log", "news", "commands", "events", "meta"} <= names


def test_database_newer_than_code_refuses_to_run(conn):
    """Pointing old code at a newer database must fail loudly, not silently."""
    conn.execute("UPDATE meta SET v = '999' WHERE k = 'schema_version'")
    with pytest.raises(db.SchemaError, match="older than the database"):
        db.migrate(conn)


def test_assert_healthy_passes_on_fresh_db(conn):
    db.assert_healthy(conn)


def test_assert_healthy_rejects_unmigrated_db(tmp_path):
    c = db.connect(tmp_path / "t.db")
    with pytest.raises(db.SchemaError, match="schema version"):
        db.assert_healthy(c)
    c.close()


# ---------------------------------------------------------------------------
# Constraints. Each of these asserts the guard actually rejects.
# ---------------------------------------------------------------------------


def _bar(conn, **over):
    row = dict(
        symbol="AAPL", tf="1m", ts_ms=1, o=10.0, h=11.0, l=9.0, c=10.5,
        v=100.0, source="test", ingested_ms=2,
    )
    row.update(over)
    conn.execute(
        "INSERT INTO bars (symbol,tf,ts_ms,o,h,l,c,v,source,ingested_ms) "
        "VALUES (:symbol,:tf,:ts_ms,:o,:h,:l,:c,:v,:source,:ingested_ms)",
        row,
    )


def test_bar_accepts_valid_row(conn):
    _bar(conn)
    assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1


def test_bar_rejects_high_below_low(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _bar(conn, h=8.0, l=9.0)


def test_bar_rejects_close_outside_range(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _bar(conn, c=99.0)


def test_bar_rejects_negative_volume(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _bar(conn, v=-1.0)


def test_bar_rejects_unknown_timeframe(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _bar(conn, tf="3s")


def test_bars_from_two_sources_coexist(conn):
    """Feeds disagree. Keeping both is the point; last-writer-wins would hide it."""
    _bar(conn, source="yahoo", c=10.5)
    _bar(conn, source="alpaca", c=10.6)
    assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 2


def test_tick_rejects_crossed_book(conn):
    """bid > ask is bad data or a stale composite. Never store it as tradable."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ticks (symbol,ts_ms,last,bid,ask,source,received_ms) "
            "VALUES ('AAPL',1,10.0,11.0,10.0,'test',2)"
        )


def test_tick_allows_null_bid_ask(conn):
    """Yahoo gives no quote. Nullability here is load-bearing: the fill engine
    must refuse spread-cross pricing rather than invent a spread."""
    conn.execute(
        "INSERT INTO ticks (symbol,ts_ms,last,source,received_ms) "
        "VALUES ('AAPL',1,10.0,'yahoo',2)"
    )
    assert conn.execute("SELECT bid FROM ticks").fetchone()["bid"] is None


def test_fetch_log_failure_must_explain_itself(conn):
    """A gap in fetch_log must mean 'did not try', never 'tried and broke'."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fetch_log (ts_ms,source,kind,endpoint,symbols,ok) "
            "VALUES (1,'yahoo','bars','/x','[]',0)"
        )


def test_fetch_log_records_failures(conn):
    conn.execute(
        "INSERT INTO fetch_log (ts_ms,source,kind,endpoint,symbols,ok,error) "
        "VALUES (1,'yahoo','bars','/x','[]',0,'ConnectTimeout')"
    )
    assert conn.execute("SELECT COUNT(*) FROM fetch_log WHERE ok=0").fetchone()[0] == 1


def test_command_must_record_when_it_was_handled(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO commands (ts_ms,actor,kind,payload,status) "
            "VALUES (1,'operator','halt','{}','done')"
        )


def test_news_rejects_unknown_kind(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO news (id,published_ms,ingested_ms,source,external_id,kind,"
            "symbols,headline) VALUES ('a',1,2,'yahoo','x','rumour','[]','h')"
        )


# ---------------------------------------------------------------------------
# Single-writer discipline
# ---------------------------------------------------------------------------


def test_readonly_connection_cannot_write(tmp_path):
    """The API opens read-only so a stray write fails at the offending line
    rather than as intermittent SQLITE_BUSY under load."""
    path = tmp_path / "t.db"
    w = db.connect(path)
    db.migrate(w)
    w.close()

    r = db.connect(path, readonly=True)
    assert r.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        r.execute("INSERT INTO events (ts_ms,level,kind) VALUES (1,'info','x')")
    r.close()


def test_readonly_connection_refuses_missing_database(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        db.connect(tmp_path / "nope.db", readonly=True)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def test_immediate_rolls_back_on_exception(conn):
    with pytest.raises(ValueError):
        with db.immediate(conn):
            _bar(conn)
            raise ValueError("boom")
    assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0


def test_immediate_commits_on_success(conn):
    with db.immediate(conn):
        _bar(conn)
    assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1


def test_the_connection_context_manager_is_not_a_transaction(conn):
    """`with conn:` looks like one and is not, because these connections set
    isolation_level=None. Code that wrote a multi-row change under it committed
    each statement separately and rolled nothing back on failure -- which is what
    `Book.apply` was doing to fills, positions and cash.

    Pinned as a test because the trap is invisible at the call site.
    """
    with pytest.raises(ValueError):
        with conn:
            _bar(conn)
            raise ValueError("boom")
    assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1, (
        "`with conn:` rolled back — if this ever passes, the pragma changed")


def test_concurrent_writers_serialise_rather_than_corrupt(tmp_path):
    """Two writers contending for the lock: the second must WAIT for the first
    and then succeed, not fail.

    This is the test that proves BEGIN IMMEDIATE plus busy_timeout actually
    works. Without busy_timeout the second writer raises 'database is locked'
    instantly; without BEGIN IMMEDIATE a read-then-upgrade deadlocks.
    """
    path = tmp_path / "t.db"
    setup = db.connect(path)
    db.migrate(setup)
    setup.close()

    a = db.connect(path)
    b = db.connect(path)
    hold = threading.Event()
    result: list[object] = []

    def slow_writer():
        with db.immediate(a):
            a.execute("INSERT INTO events (ts_ms,level,kind) VALUES (1,'info','a')")
            hold.set()
            time.sleep(0.3)   # hold the write lock

    t = threading.Thread(target=slow_writer)
    t.start()
    hold.wait(timeout=2)

    started = time.monotonic()
    try:
        with db.immediate(b):
            b.execute("INSERT INTO events (ts_ms,level,kind) VALUES (2,'info','b')")
        result.append("ok")
    except sqlite3.OperationalError as e:   # pragma: no cover - the failure we assert against
        result.append(e)
    waited = time.monotonic() - started

    t.join()
    a.close()
    b.close()

    assert result == ["ok"], f"second writer failed instead of waiting: {result}"
    assert waited > 0.1, "second writer did not actually block on the lock"

    check = db.connect(path, readonly=True)
    assert check.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    check.close()


def test_checkpoint_runs(conn):
    _bar(conn)
    db.checkpoint(conn)
    db.assert_healthy(conn)
