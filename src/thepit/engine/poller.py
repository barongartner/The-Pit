"""The shared poller.

One poller, one cache, one recorder. Agents never touch the network -- they read
:class:`MarketView`. That is what makes the recorded dataset complete, keeps
rate-limit pressure proportional to the watchlist rather than to the number of
agents, and makes replay possible at all.

The design goal for Stage 1 is not throughput, it is **staying up for 24 hours
across a session boundary and an overnight close without supervision**. Three
consequences shape everything here:

* Closed markets are a state, not an error. Most of a 24-hour window is closed.
* A feed failing is expected. Feeds get degraded and retried with backoff; they
  do not raise into the loop and kill it.
* Nothing that only matters for the dataset is allowed to break the loop. The
  recorder swallows its own failures, and a parse error costs one cycle rather
  than the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from dataclasses import dataclass, field

from thepit.core import calendar
from thepit.core.clock import Clock
from thepit.core.types import FeedUnavailable, FetchRecord, Quote
from thepit.store import db
from thepit.store.repos import BarsRepo, EventsRepo, FetchLogRepo, NewsRepo, TicksRepo


@dataclass
class PollerConfig:
    symbols: list[str]
    # Quotes during regular hours. Alpaca batches, so this is one request.
    quote_interval_open_s: float = 5.0
    # Closed markets still move (pre/post, futures, news), but not enough to
    # justify the same cadence. This also keeps the machine cool overnight.
    quote_interval_closed_s: float = 300.0
    # 5 minutes, not 1. Each bars fetch returns the ENTIRE day of 1-minute
    # candles, so a 60s cadence re-downloads the same ~20KB payload five times
    # over for one new bar. At 300s no bar is ever missed -- the most recent one
    # is just up to five minutes late, which is irrelevant for a system whose
    # policy loop runs in minutes. Measured: this is the difference between
    # ~150MB/day and ~40MB/day of recording.
    bar_interval_s: float = 300.0
    bar_timeframe: str = "1m"
    bar_limit: int = 100
    # Form 4's firehose window is ~54 minutes; 10 minutes gives ~5x headroom.
    news_interval_s: float = 600.0
    # Consecutive failures before a feed is declared degraded. One failure is
    # weather; three is a problem worth an event on the dashboard.
    degrade_after: int = 3
    max_backoff_s: float = 300.0


@dataclass
class FeedHealth:
    name: str
    consecutive_failures: int = 0
    degraded: bool = False
    last_ok_ms: int = 0
    last_error: str | None = None

    def backoff_s(self, cap: float) -> float:
        """Exponential backoff, capped.

        Applied only after a feed is degraded. Retrying a blocked endpoint at
        full cadence is how a temporary block becomes a permanent one.
        """
        if self.consecutive_failures == 0:
            return 0.0
        return min(cap, 2.0 ** min(self.consecutive_failures, 8))


class MarketView:
    """In-memory latest-known state. The read surface for everything else.

    Deliberately not backed by a query: the dashboard polls this many times a
    second and the database is a single-writer resource the engine needs.
    """

    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}

    def update(self, quotes: dict[str, Quote]) -> None:
        self._quotes.update(quotes)

    def get(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def all(self) -> dict[str, Quote]:
        return dict(self._quotes)

    def stale_symbols(self, now_ms: int, max_age_ms: int) -> list[str]:
        """Symbols whose last quote is older than `max_age_ms`.

        A quote that has stopped updating looks exactly like a quiet market
        until you ask this question.
        """
        return [
            s for s, q in self._quotes.items()
            if now_ms - q.received_ms > max_age_ms
        ]


class Poller:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        config: PollerConfig,
        *,
        price_feed=None,
        news_feed=None,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._cfg = config
        self._price = price_feed
        self._news = news_feed

        self.view = MarketView()
        self.health: dict[str, FeedHealth] = {}
        if price_feed is not None:
            self.health[price_feed.name] = FeedHealth(price_feed.name)
        if news_feed is not None:
            self.health[news_feed.name] = FeedHealth(news_feed.name)

        self._bars = BarsRepo(conn)
        self._ticks = TicksRepo(conn)
        self._news_repo = NewsRepo(conn)
        self._fetch_log = FetchLogRepo(conn)
        self._events = EventsRepo(conn)
        self._stop = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------

    async def probe_all(self) -> dict[str, str | None]:
        """Check every feed at startup. Returns name -> error (None if healthy).

        Does not raise. A feed being unavailable is information the operator
        needs on the dashboard, not a reason to refuse to boot -- the news feed
        working while prices are blocked is a perfectly useful state, and it is
        exactly the state this project was in on day one.
        """
        out: dict[str, str | None] = {}
        for feed in (self._price, self._news):
            if feed is None:
                continue
            try:
                await feed.probe()
                out[feed.name] = None
                self._emit("info", "feed_ok", feed.name)
            except FeedUnavailable as exc:
                out[feed.name] = str(exc)
                self.health[feed.name].degraded = True
                self.health[feed.name].last_error = str(exc)
                self._emit("warn", "feed_unavailable", feed.name, {"reason": str(exc)})
            except Exception as exc:  # noqa: BLE001 - probe must not kill boot
                out[feed.name] = f"{type(exc).__name__}: {exc}"
                self._emit("error", "feed_probe_failed", feed.name,
                           {"error": out[feed.name]})
        return out

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Run every loop until stopped.

        Each loop is independent: news continuing while prices are blocked is a
        feature, not a degraded mode to be avoided.
        """
        tasks = [asyncio.create_task(self._quote_loop()),
                 asyncio.create_task(self._bar_loop()),
                 asyncio.create_task(self._news_loop())]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    # -- loops ---------------------------------------------------------------

    async def _quote_loop(self) -> None:
        if self._price is None:
            return
        while not self._stop.is_set():
            now = self._clock.now_ms()
            open_now = calendar.is_open(now)

            try:
                quotes, records = await self._price.quotes(self._cfg.symbols)
                self._ingest_quotes(quotes, records)
            except Exception as exc:  # noqa: BLE001 - the loop must not die
                self._note_failure(self._price.name, f"{type(exc).__name__}: {exc}")

            interval = (
                self._cfg.quote_interval_open_s if open_now
                else self._cfg.quote_interval_closed_s
            )
            await self._sleep(interval + self.health[self._price.name]
                              .backoff_s(self._cfg.max_backoff_s))

    async def _bar_loop(self) -> None:
        if self._price is None:
            return
        while not self._stop.is_set():
            try:
                by_symbol, records = await self._fetch_bars()
                ingested = self._clock.now_ms()
                with db.immediate(self._conn):
                    for rec in records:
                        self._fetch_log.record(rec)
                    for bars in by_symbol.values():
                        self._bars.upsert_many(bars, ingested)
            except Exception as exc:  # noqa: BLE001
                self._note_failure(self._price.name, f"bars: {type(exc).__name__}: {exc}")
            await self._sleep(self._cfg.bar_interval_s)

    async def _fetch_bars(self) -> tuple[dict[str, list], list[FetchRecord]]:
        """Fetch bars, batching when the feed supports it.

        Alpaca has a batch endpoint; Yahoo does not. Falling back to per-symbol
        requests rather than skipping bars entirely matters: an earlier version
        gated this whole loop on `hasattr(feed, "bars_many")`, which silently
        recorded zero bars for the entire first engine run. Nothing failed and
        nothing was logged -- the table was just empty.
        """
        assert self._price is not None
        tf, limit = self._cfg.bar_timeframe, self._cfg.bar_limit

        if hasattr(self._price, "bars_many"):
            return await self._price.bars_many(self._cfg.symbols, tf, limit)

        by_symbol: dict[str, list] = {}
        records: list[FetchRecord] = []
        for symbol in self._cfg.symbols:
            bars, rec = await self._price.bars(symbol, tf, limit)
            records.append(rec)
            if bars:
                by_symbol[symbol] = bars
        return by_symbol, records

    async def _news_loop(self) -> None:
        if self._news is None:
            return
        while not self._stop.is_set():
            try:
                # Watermark from the database, not from memory: a restart must
                # not re-ingest a week of filings, and must not skip the window
                # it was down for either.
                since = self._news_repo.latest_published_ms(self._news.name)
                items, records, warnings = await self._news.poll(
                    self._cfg.symbols, since
                )
                with db.immediate(self._conn):
                    for rec in records:
                        self._fetch_log.record(rec)
                    n = self._news_repo.upsert_many(items)
                    for w in warnings:
                        self._events.emit(self._clock.now_ms(), "warn",
                                          "news_window_saturated", self._news.name,
                                          {"detail": w})
                    if n:
                        self._events.emit(self._clock.now_ms(), "info", "news_ingested",
                                          self._news.name, {"count": n})
                self._note_result(self._news.name, ok=all(r.ok for r in records)
                                  if records else False)
            except Exception as exc:  # noqa: BLE001
                self._note_failure(self._news.name, f"{type(exc).__name__}: {exc}")
            await self._sleep(self._cfg.news_interval_s)

    # -- ingestion -----------------------------------------------------------

    def _ingest_quotes(
        self, quotes: dict[str, Quote], records: list[FetchRecord]
    ) -> None:
        with db.immediate(self._conn):
            for rec in records:
                self._fetch_log.record(rec)
            if quotes:
                self._ticks.insert_many(list(quotes.values()))
        self.view.update(quotes)
        self._note_result(
            self._price.name, ok=bool(records) and any(r.ok for r in records)
        )

    # -- health --------------------------------------------------------------

    def _note_result(self, feed: str, *, ok: bool) -> None:
        if ok:
            self._note_success(feed)
        else:
            self._note_failure(feed, "no successful fetch in cycle")

    def _note_success(self, feed: str) -> None:
        h = self.health[feed]
        h.last_ok_ms = self._clock.now_ms()
        was_degraded = h.degraded
        h.consecutive_failures = 0
        h.degraded = False
        h.last_error = None
        if was_degraded:
            # Recovery is as newsworthy as the outage: without this event the
            # uptime report cannot tell a resolved blip from an ongoing outage.
            self._emit("info", "feed_recovered", feed)

    def _note_failure(self, feed: str, error: str) -> None:
        h = self.health[feed]
        h.consecutive_failures += 1
        h.last_error = error
        if not h.degraded and h.consecutive_failures >= self._cfg.degrade_after:
            h.degraded = True
            self._emit("warn", "feed_degraded", feed,
                       {"failures": h.consecutive_failures, "error": error})

    def _emit(self, level: str, kind: str, subject: str, detail: dict | None = None
              ) -> None:
        self._events.emit(self._clock.now_ms(), level, kind, subject, detail)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately on stop.

        A plain asyncio.sleep would make shutdown take up to a full poll
        interval, which is five minutes overnight.
        """
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
