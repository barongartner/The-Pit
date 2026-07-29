"""The kill switch.

A file on disk. Its **presence** means stop.

Every design choice here is about surviving the case where the rest of the
system is broken, so each one is worth stating explicitly.

**Why a file, not a database row.** The kill switch must be settable when the
database is locked, corrupt, or held open by a wedged writer -- which is exactly
the situation you are killing for. A control plane must not depend on the health
of the data plane. A file is also settable with ``touch`` from any shell, from
cron, or over SSH from a phone, with no Python and no dependencies.

**Why not an flock or an advisory lock.** Locks die with the process holding
them. An unclean crash must leave the kill *engaged*, not silently release it.

**Why presence and never content.** A JSON kill file that fails to parse is a
kill switch that fails open. Nothing here parses anything. Content, if present,
is a note for humans.

**Why it fails closed.** If the state directory cannot be read at all -- unmounted,
permissions, disk gone -- that counts as killed. Every ambiguity resolves toward
not trading.

**Why there is a thread and not just an asyncio task.** The requirement is that
this works when the process is *wedged*, and a wedged event loop is precisely the
case where an asyncio task never gets scheduled. The watchdog therefore runs in
its own OS thread and will ``os._exit`` the process if the loop stops
acknowledging. That is the layer people forget, and it is the only one that
actually satisfies the requirement.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

KILL = "KILL"
FLATTEN = "FLATTEN"
HEARTBEAT = "HEARTBEAT"

# Exit code used when the watchdog force-kills a wedged process. Distinct from
# any normal exit so a supervisor can tell what happened.
EXIT_WEDGED = 3


class KillSwitch:
    def __init__(self, state_dir: Path | str) -> None:
        self.dir = Path(state_dir).expanduser()

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- reads ---------------------------------------------------------------

    def engaged(self) -> bool:
        """True when trading must stop. Fails closed.

        `os.path.exists` rather than opening the file: we never want to depend
        on being able to *read* it, only on being able to see it. A full disk
        can prevent a read; it cannot hide a directory entry.
        """
        try:
            return (self.dir / KILL).exists()
        except OSError:
            # Directory unreadable. Treat as killed.
            return True

    def flatten_requested(self) -> bool:
        try:
            return (self.dir / FLATTEN).exists()
        except OSError:
            return True

    def engaged_at(self) -> float | None:
        try:
            return (self.dir / KILL).stat().st_mtime
        except OSError:
            return None

    # -- writes --------------------------------------------------------------

    def engage(self, reason: str = "") -> None:
        """Engage the kill. Writing the reason is best-effort and never fatal:
        the switch is the file existing, not what is in it."""
        self.ensure_dir()
        path = self.dir / KILL
        try:
            path.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {reason}\n")
        except OSError:
            path.touch()

    def request_flatten(self, reason: str = "") -> None:
        self.ensure_dir()
        try:
            (self.dir / FLATTEN).write_text(reason + "\n")
        except OSError:
            (self.dir / FLATTEN).touch()

    def release(self) -> None:
        """Clear the kill. Deliberately manual and deliberately not called by
        anything automatic -- recovery is a human decision."""
        (self.dir / KILL).unlink(missing_ok=True)
        (self.dir / FLATTEN).unlink(missing_ok=True)

    # -- liveness ------------------------------------------------------------

    def beat(self) -> None:
        """Touch the heartbeat. An external supervisor watches its mtime."""
        try:
            (self.dir / HEARTBEAT).touch()
        except OSError:
            pass

    def heartbeat_age_s(self) -> float | None:
        try:
            return time.time() - (self.dir / HEARTBEAT).stat().st_mtime
        except OSError:
            return None


class Watchdog:
    """Force-exits the process if the kill file appears and the main loop does
    not acknowledge it.

    Runs in an OS thread precisely so a blocked event loop cannot prevent it
    from running. On trigger it calls ``os._exit``, which skips atexit handlers,
    finalizers, and buffer flushes -- deliberately, because those are exactly
    the things that hang in a wedged process. Losing the last few log lines is
    an acceptable price for the process actually dying.
    """

    def __init__(
        self,
        switch: KillSwitch,
        *,
        poll_s: float = 1.0,
        grace_s: float = 5.0,
        on_kill: Callable[[], None] | None = None,
        exit_fn: Callable[[int], None] | None = None,
    ) -> None:
        self._switch = switch
        self._poll_s = poll_s
        self._grace_s = grace_s
        self._on_kill = on_kill
        self._exit = exit_fn or os._exit
        self._ack = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.triggered_at: float | None = None

    def acknowledge(self) -> None:
        """Called by the main loop once it has begun an orderly shutdown.

        This is what distinguishes "shutting down cleanly" from "wedged": if the
        loop is alive enough to call this, the watchdog stands down.
        """
        self._ack.set()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="killswitch-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._switch.engaged():
                if self.triggered_at is None:
                    self.triggered_at = time.monotonic()
                    if self._on_kill is not None:
                        try:
                            self._on_kill()
                        except Exception:  # noqa: BLE001 - never trust the callback
                            pass
                elif not self._ack.is_set():
                    if time.monotonic() - self.triggered_at >= self._grace_s:
                        # The loop has had its grace period and has not
                        # responded. It is wedged. Kill the process.
                        self._exit(EXIT_WEDGED)
                        return
            else:
                self.triggered_at = None
                self._ack.clear()

            self._stop.wait(self._poll_s)
