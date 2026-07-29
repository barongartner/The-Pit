"""Time.

Every timestamp in this system is integer milliseconds since the Unix epoch, UTC.

The clock is an object rather than a module-level call to `time.time()` because
several things need to inject it: the risk engine must be a pure function of its
inputs (so the clock is an argument, not an ambient capability), and the replay
harness must be able to drive time from a recorded tape rather than the wall.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    """Wall-clock time. The default everywhere outside tests and replay."""

    __slots__ = ()

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class FixedClock:
    """A clock that only moves when you move it. For tests and replay.

    Deliberately mutable: replay advances it as it walks the tape, and tests use
    it to make time-dependent behaviour deterministic instead of flaky.
    """

    __slots__ = ("_now_ms",)

    def __init__(self, now_ms: int = 0) -> None:
        self._now_ms = now_ms

    def now_ms(self) -> int:
        return self._now_ms

    def set(self, now_ms: int) -> None:
        self._now_ms = now_ms

    def advance(self, ms: int) -> None:
        self._now_ms += ms


SYSTEM_CLOCK = SystemClock()


def now_ms() -> int:
    """Convenience for call sites that genuinely want the wall clock.

    Anything that could plausibly be replayed or unit-tested should take a
    `Clock` instead of calling this.
    """
    return SYSTEM_CLOCK.now_ms()
