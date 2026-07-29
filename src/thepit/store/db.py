"""SQLite access: pragmas, transactions, migrations, boot assertions.

Two rules this module exists to enforce.

**One writer.** The engine process is the only thing that writes. The API opens
its connections with ``mode=ro`` in the URI, so a stray write raises
``sqlite3.OperationalError`` immediately and loudly at the offending line,
instead of producing intermittent ``SQLITE_BUSY`` under load that is miserable
to trace back. Single-writer discipline is what makes WAL safe here, and a
convention that only lives in a comment is one careless commit from being
broken.

**Explicit transactions.** Python's sqlite3 has an implicit-transaction mode
whose behaviour is surprising. We turn it off (``isolation_level=None``) and
require callers to use :func:`immediate`, which issues ``BEGIN IMMEDIATE`` and
so takes the write lock up front rather than discovering the conflict partway
through and failing the upgrade from a read transaction.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_DIR = Path(__file__).parent / "schema"

# Wait this long for the write lock before raising. The engine's write
# transactions are short (a tick's worth of rows), so anything approaching this
# means something is genuinely wrong rather than merely contended.
BUSY_TIMEOUT_MS = 5_000

# Checkpoint the WAL once it passes this many pages (~4MB at the default page
# size). An unbounded WAL over a long session is a real operational problem with
# this stack, not a theoretical one.
WAL_AUTOCHECKPOINT_PAGES = 1_000


class SchemaError(RuntimeError):
    """The database on disk is not something we are willing to run against."""


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection with this project's pragmas applied.

    Pragmas are per-connection, not per-database, so every connection has to go
    through here. That is the reason this function exists rather than callers
    using ``sqlite3.connect`` directly.
    """
    path = Path(path)

    if readonly:
        # URI form is the only way to get a genuinely read-only handle. Note
        # that this does not create the file, which is correct: a reader asking
        # for a database that does not exist is a bug worth surfacing.
        uri = f"file:{path}?mode=ro"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{path}"

    conn = sqlite3.connect(
        uri,
        uri=True,
        # Disable the implicit transaction handling. See module docstring.
        isolation_level=None,
        timeout=BUSY_TIMEOUT_MS / 1000,
        # The engine touches its connection from an asyncio executor thread.
        # Serialisation is our job (single writer), not sqlite3's.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")

    if not readonly:
        # WAL survives across connections once set, but setting it is cheap and
        # makes a fresh database correct without a separate bootstrap step.
        conn.execute("PRAGMA journal_mode = WAL")
        # NORMAL means a power loss can cost the last few commits but cannot
        # corrupt the file. For recorded market data that is the right trade:
        # FULL would fsync on every tick and this machine has a spinning-rust-era
        # thermal budget.
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA wal_autocheckpoint = {WAL_AUTOCHECKPOINT_PAGES}")

    return conn


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction, taking the write lock up front.

    ``BEGIN IMMEDIATE`` rather than a bare ``BEGIN``: it acquires the write lock
    at the start instead of deferring until the first write, which avoids the
    case where a transaction reads, then tries to upgrade, then fails because
    someone else wrote in between.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Migrations
#
# Numbered .sql files plus a version row. No alembic: the whole mechanism is
# about thirty lines, it is fully inspectable, and the schema is small enough
# that the generated-migration machinery would cost more than it saves.
# ---------------------------------------------------------------------------

_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def _migration_files() -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for p in sorted(SCHEMA_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            raise SchemaError(
                f"migration filename {p.name!r} does not match NNN_snake_case.sql"
            )
        out.append((int(m.group(1)), p))

    # Gaps or duplicates mean two branches added a migration with the same
    # number and one silently won. Fail rather than guess.
    numbers = [n for n, _ in out]
    if numbers != list(range(1, len(numbers) + 1)):
        raise SchemaError(f"migration numbers must be gapless from 001, got {numbers}")
    return out


def schema_version(conn: sqlite3.Connection) -> int:
    """Current schema version, or 0 for an empty database."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()
    return int(row["v"]) if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any migrations newer than the database's current version.

    Each migration runs inside its own transaction, so a failure part-way
    through leaves the database at the last good version rather than in a
    half-migrated state.
    """
    current = schema_version(conn)
    files = _migration_files()

    if current > len(files):
        raise SchemaError(
            f"database is at schema version {current} but only {len(files)} migrations "
            "exist. This code is older than the database it is pointed at."
        )

    for number, path in files:
        if number <= current:
            continue

        # executescript() issues an implicit COMMIT before it runs and performs
        # no other transaction control, so wrapping the call in immediate()
        # would have its BEGIN silently discarded and the COMMIT would then
        # fail with "no transaction is active". The transaction has to live
        # inside the script text instead.
        #
        # `number` came from a regex-matched filename and is an int, so
        # interpolating it here cannot be an injection vector. executescript
        # does not accept parameters.
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{path.read_text()}\n"
            "INSERT INTO meta (k, v) VALUES ('schema_version', "
            f"'{number}') ON CONFLICT(k) DO UPDATE SET v = excluded.v;\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            # Leave the database at the last good version rather than
            # half-migrated. A partially applied migration that reports success
            # is how a schema quietly diverges from the code.
            conn.execute("ROLLBACK")
            raise
        current = number

    return current


# ---------------------------------------------------------------------------
# Boot assertions
#
# Run before the engine starts doing anything. The point is to refuse to start
# on a database we do not understand, rather than to start and corrupt it
# further. A trading system that boots into an inconsistent state and begins
# acting is strictly worse than one that will not boot.
# ---------------------------------------------------------------------------


def assert_healthy(conn: sqlite3.Connection) -> None:
    """Raise :class:`SchemaError` if the database is not safe to run against."""

    # quick_check is the cheap sibling of integrity_check: it skips the
    # (expensive) index-vs-table cross validation but still catches structural
    # corruption. At boot, cheap-and-run-always beats thorough-and-skipped.
    row = conn.execute("PRAGMA quick_check").fetchone()
    if row[0] != "ok":
        raise SchemaError(f"integrity check failed: {row[0]}")

    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SchemaError("foreign key violations present")

    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if journal.lower() != "wal":
        raise SchemaError(f"expected WAL journal mode, found {journal!r}")

    version = schema_version(conn)
    expected = len(_migration_files())
    if version != expected:
        raise SchemaError(
            f"schema version {version} but {expected} migrations exist. Run migrate()."
        )


def checkpoint(conn: sqlite3.Connection) -> None:
    """Truncate the WAL. Call at session close and on a slow timer.

    TRUNCATE rather than PASSIVE: we want the file actually shrunk, not merely
    marked reusable, because disk headroom on this machine is tight.
    """
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
