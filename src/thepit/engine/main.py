"""Engine entrypoint. The only process that writes to the database.

    uv run python -m thepit.engine.main

Startup order matters and is deliberate:

1. Refuse to start if the kill switch is engaged. Booting into a killed state
   and beginning to poll would defeat the point of the switch surviving a crash.
2. Open the database, migrate, and run boot assertions. A system that boots into
   an inconsistent state and starts acting is strictly worse than one that will
   not boot.
3. Start the watchdog *before* any feed work, so a hang during startup is still
   covered.
4. Probe feeds. An unavailable feed is reported, not fatal -- news working while
   prices are blocked is a useful state and was literally day one here.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from thepit import config as cfg
from thepit.core.clock import SYSTEM_CLOCK
from thepit.engine.killswitch import KillSwitch, Watchdog
from thepit.engine.poller import Poller, PollerConfig
from thepit.feeds import edgar as edgar_mod
from thepit.feeds.alpaca import AlpacaFeed
from thepit.feeds.edgar import EdgarNewsFeed
from thepit.feeds import http as feed_http_mod
from thepit.feeds.http import FeedHttp
from thepit.feeds.recorder import RawRecorder
from thepit.feeds.yahoo import YahooChartFeed
from thepit.store import db

log = logging.getLogger("thepit.engine")

HEARTBEAT_INTERVAL_S = 5.0


async def run(config: cfg.Config) -> int:
    switch = KillSwitch(config.state_dir)
    switch.ensure_dir()

    if switch.engaged():
        log.error(
            "kill switch is engaged (%s). Refusing to start. "
            "Clear it with: rm %s",
            switch.dir / "KILL", switch.dir / "KILL",
        )
        return 2

    config.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(config.db_path)
    db.migrate(conn)
    db.assert_healthy(conn)
    log.info("database ready at %s", config.db_path)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()

    watchdog = Watchdog(
        switch,
        on_kill=lambda: loop.call_soon_threadsafe(stopping.set),
    )
    watchdog.start()

    recorder = RawRecorder(config.raw_dir, enabled=config.raw_recording)

    # Two HTTP clients, because SEC needs pacing and the price feed should not
    # be slowed down by someone else's rate limit.
    price_http = FeedHttp(SYSTEM_CLOCK, recorder)
    news_http = FeedHttp(
        SYSTEM_CLOCK, recorder,
        min_interval_s=edgar_mod.MIN_REQUEST_INTERVAL_S,
        timeout=feed_http_mod.SLOW_TIMEOUT,
    )

    price_feed = AlpacaFeed(price_http, SYSTEM_CLOCK)
    if not price_feed.configured:
        # Yahoo works fine (the earlier "blocked" diagnosis was wrong, see #13).
        # It is the fallback rather than the default only because it has no
        # batch endpoint and no bid/ask, so it caps the system at BARS tier.
        log.warning(
            "Alpaca credentials not set; using Yahoo. Works, but bars-tier only: "
            "no bid/ask and one request per symbol. See issue #11."
        )
        price_feed = YahooChartFeed(price_http, SYSTEM_CLOCK)

    news_feed = EdgarNewsFeed(news_http, SYSTEM_CLOCK)

    poller = Poller(
        conn, SYSTEM_CLOCK,
        PollerConfig(
            symbols=config.symbols,
            quote_interval_open_s=config.quote_interval_open_s,
            quote_interval_closed_s=config.quote_interval_closed_s,
            bar_interval_s=config.bar_interval_s,
            news_interval_s=config.news_interval_s,
        ),
        price_feed=price_feed,
        news_feed=news_feed,
    )

    for name, err in (await poller.probe_all()).items():
        log.info("feed %-8s %s", name, "OK" if err is None else f"UNAVAILABLE: {err}")

    # Windows' ProactorEventLoop does not implement add_signal_handler and
    # raises NotImplementedError. That is handled rather than merely suppressed:
    # without a handler, Ctrl-C surfaces as KeyboardInterrupt out of
    # asyncio.run(), which would skip the WAL checkpoint and the clean feed
    # shutdown below. See _run_with_interrupt.
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stopping.set)

    tasks = [
        asyncio.create_task(poller.run(), name="poller"),
        asyncio.create_task(_heartbeat(switch, stopping), name="heartbeat"),
        asyncio.create_task(_watch_kill(switch, stopping), name="killwatch"),
        asyncio.create_task(
            _retention(recorder, config, stopping), name="retention"
        ),
    ]

    log.info("engine running. Kill with: touch %s", switch.dir / "KILL")
    await stopping.wait()

    # Tell the watchdog we are shutting down on purpose, so it does not decide
    # we are wedged and hard-exit mid-cleanup.
    watchdog.acknowledge()
    log.info("shutting down")

    poller.stop()
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t

    await price_http.aclose()
    await news_http.aclose()
    db.checkpoint(conn)
    conn.close()
    watchdog.stop()
    log.info("stopped cleanly")
    return 0


async def _heartbeat(switch: KillSwitch, stopping: asyncio.Event) -> None:
    """Touch the heartbeat so an external supervisor can detect a wedge."""
    while not stopping.is_set():
        switch.beat()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=HEARTBEAT_INTERVAL_S)


async def _retention(
    recorder: RawRecorder, config: cfg.Config, stopping: asyncio.Event
) -> None:
    """Prune old recordings and report disk usage.

    Wired in from day one rather than added after the disk fills at 3am. The
    first measured rate was 988MB/day, which would have filled this Mac in three
    weeks; it is ~17MB/day now (issue #15), but the retention pass stays because
    every later stage adds a data source.
    """
    while not stopping.is_set():
        try:
            freed = recorder.prune_older_than(config.raw_retention_days)
            used = recorder.disk_usage_bytes()
            if freed:
                log.info("retention: freed %.1f MB", freed / 1e6)
            log.info("recordings on disk: %.1f MB", used / 1e6)
        except Exception as exc:  # noqa: BLE001 - housekeeping is never fatal
            log.warning("retention pass failed: %s", exc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=3600)


async def _watch_kill(switch: KillSwitch, stopping: asyncio.Event) -> None:
    """Responsiveness layer: notice the kill file within a second.

    The watchdog thread is the guarantee; this is the fast path for the normal
    case where the loop is healthy.
    """
    while not stopping.is_set():
        if switch.engaged():
            log.warning("kill switch engaged; stopping")
            stopping.set()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=1.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thepit-engine")
    # Mode is argv-only. Never a config key, never an environment variable, and
    # never mutable after startup.
    parser.add_argument(
        "--live", action="store_true",
        help="NOT IMPLEMENTED. Live trading is built but not armed.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.live:
        print(
            "Live mode is not armed. The live code path exists and is exercised "
            "against Alpaca's paper endpoint, but arming requires the confirmation "
            "ceremony, which is Stage 4.",
            file=sys.stderr,
        )
        return 2

    config = cfg.load(mode=cfg.Mode.PAPER)

    # On Windows, Ctrl-C arrives as KeyboardInterrupt rather than through a
    # signal handler, so run() never reaches its shutdown path. Engaging the
    # kill switch and letting the loop notice it takes the SAME route as every
    # other stop -- one shutdown path, exercised on both platforms, instead of
    # a POSIX path and a Windows path that drift apart.
    try:
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        log.warning("interrupted; the WAL may not have been checkpointed")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
