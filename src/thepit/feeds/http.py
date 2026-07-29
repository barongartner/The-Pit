"""Shared HTTP layer for feed adapters.

Every outbound request in this system goes through :class:`FeedHttp`. That is
what makes "record every fetch" a property of the architecture rather than a
thing each adapter has to remember.

It returns the response *and* a :class:`FetchRecord` describing what happened,
including for failures. Adapters do not write to the database; the poller takes
the record and persists it. That keeps the feed layer pure enough to test
without a database.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

import httpx

from thepit.core.clock import Clock
from thepit.core.types import FetchRecord
from thepit.feeds.recorder import RawRecorder

# Identify ourselves honestly.
#
# SEC EDGAR's access policy requires a User-Agent containing a **contact email**
# and enforces it: a UA carrying a project URL instead of an address gets a bare
# 403 from www.sec.gov with no explanation. Verified 2026-07-29.
#
# The address is read from the environment rather than hardcoded, because this
# repository is public and a source file is a worse place for it than a local
# env var. Falling back to a no-contact string is deliberate: SEC requests will
# fail loudly with a clear reason instead of the project shipping someone
# else's address as a default.
_CONTACT = os.environ.get("THEPIT_CONTACT_EMAIL", "").strip()

USER_AGENT = (
    f"ThePit/0.1 ({_CONTACT})" if _CONTACT
    else "ThePit/0.1 (set THEPIT_CONTACT_EMAIL)"
)


def has_contact() -> bool:
    """Whether a contact address is configured. SEC feeds require one."""
    return bool(_CONTACT)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# SEC serves an 800KB ticker map and multi-hundred-KB atom feeds, and its p50 is
# already ~1.8s. A 10s read timeout hit exactly that ceiling on the first live
# run (ReadTimeout at 10004ms). The failure was handled correctly -- recorded in
# fetch_log, feed degraded gracefully, recovered on the next cycle -- but the
# limit was simply wrong for the payload rather than a signal about SEC.
SLOW_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)


@dataclass(frozen=True, slots=True)
class FetchResult:
    record: FetchRecord
    status: int | None
    text: str | None

    @property
    def ok(self) -> bool:
        return self.record.ok


class FeedHttp:
    """Async HTTP client that times, records, and never raises on transport errors.

    Transport failures are returned as a `FetchResult` with `ok=False` rather
    than raised. A feed adapter's job is to report what happened; deciding
    whether a failure is worth degrading or halting over belongs to the poller,
    which is the only component with enough context to judge.
    """

    def __init__(
        self,
        clock: Clock,
        recorder: RawRecorder | None = None,
        *,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        user_agent: str = USER_AGENT,
        min_interval_s: float = 0.0,
    ) -> None:
        self._clock = clock
        self._recorder = recorder
        # Minimum spacing between requests from this client. SEC publishes a
        # 10 req/s ceiling but throttles bursts more aggressively than that
        # suggests: a browse-edgar call issued immediately after the 800KB
        # ticker-map download came back empty, then succeeded standalone a
        # second later. Pacing costs nothing here (we poll on a minutes-scale
        # cadence) and removes a whole class of intermittent empty results.
        self._min_interval_s = min_interval_s
        self._last_request_at = 0.0
        self._pace_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
            # A small pool: this is one process politely polling a handful of
            # endpoints, not a crawler.
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pace(self) -> None:
        """Sleep so consecutive requests are at least `min_interval_s` apart."""
        if self._min_interval_s <= 0:
            return
        async with self._pace_lock:
            wait = self._min_interval_s - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def get(
        self,
        url: str,
        *,
        source: str,
        kind: str,
        symbols: tuple[str, ...] = (),
        params: dict | None = None,
        headers: dict | None = None,
        record_raw: bool = True,
    ) -> FetchResult:
        """`fetch_log` records EVERY request; the raw body is only sampled.

        Rate limiting of the raw archive lives in RawRecorder, which keeps at
        most one sample per (source, kind) per hour. Uptime accounting is
        unaffected because it reads fetch_log, not the archive.
        """
        await self._pace()
        ts_ms = self._clock.now_ms()
        started = time.monotonic()

        try:
            resp = await self._client.get(url, params=params, headers=headers)
            latency_ms = int((time.monotonic() - started) * 1000)
            text = resp.text

            raw_path = None
            # Only record successful bodies by default. A megabyte of identical
            # "Too Many Requests" strings is not a dataset, and during an outage
            # that is exactly what we would otherwise accumulate. The failure is
            # still fully described in fetch_log.
            if record_raw and self._recorder is not None and resp.is_success:
                raw_path = self._recorder.record(
                    source, kind, text, ts_ms=ts_ms,
                    meta={"url": str(resp.url), "status": resp.status_code},
                )

            ok = resp.is_success
            return FetchResult(
                record=FetchRecord(
                    ts_ms=ts_ms, source=source, kind=kind, endpoint=_endpoint(url),
                    symbols=symbols, ok=ok, http_status=resp.status_code,
                    latency_ms=latency_ms,
                    error=None if ok else _truncate(f"HTTP {resp.status_code}: {text}"),
                    raw_path=raw_path,
                ),
                status=resp.status_code,
                text=text,
            )

        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return FetchResult(
                record=FetchRecord(
                    ts_ms=ts_ms, source=source, kind=kind, endpoint=_endpoint(url),
                    symbols=symbols, ok=False, http_status=None,
                    latency_ms=latency_ms,
                    # Class name plus message: "ConnectTimeout" alone is not
                    # enough to tell a DNS failure from a hung read at 3am.
                    error=_truncate(f"{type(exc).__name__}: {exc}"),
                ),
                status=None,
                text=None,
            )


def _endpoint(url: str) -> str:
    """Path only, so fetch_log groups by endpoint rather than by symbol."""
    without_scheme = url.split("://", 1)[-1]
    slash = without_scheme.find("/")
    return without_scheme[slash:].split("?", 1)[0] if slash != -1 else "/"


def _truncate(s: str, limit: int = 400) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"
