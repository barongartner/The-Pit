"""Yahoo Finance chart feed.

**Rate limiter warning.** A burst of diagnostic requests across query1, query2
and the crumb endpoint earned an immediate 429 on every API host while the
landing page kept returning 200 -- which reads exactly like an IP block and is
not one (issue #13). Under normal polling this feed returns 200 consistently at
~64ms. Pace it (``FeedHttp(min_interval_s=...)``) and do not hammer it while
debugging.

Fidelity ceiling, which matters more than the rate limiter:

* No batch quote endpoint, so refreshing N symbols costs N requests.
* **No bid/ask at all.** :class:`~thepit.core.types.Quote` objects from this
  feed have ``has_book == False``, and a fill engine must refuse spread-cross
  pricing rather than invent a spread.
* Unknown and variable delay, no SLA.

That makes this a minutes-to-hours source. Nothing built on it can honestly
claim sub-minute execution.
"""

from __future__ import annotations

import json

from thepit.core.clock import Clock
from thepit.core.types import Bar, FeedTier, FeedUnavailable, FetchRecord, Quote
from thepit.feeds.http import FeedHttp

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Yahoo's interval vocabulary happens to match ours for the ranges we use.
_RANGE_FOR_TF = {
    "1m": "1d",
    "2m": "5d",
    "5m": "5d",
    "15m": "5d",
    "30m": "1mo",
    "1h": "3mo",
    "1d": "1y",
}


class YahooChartFeed:
    name = "yahoo"

    def __init__(self, http: FeedHttp, clock: Clock) -> None:
        self._http = http
        self._clock = clock

    def tier(self) -> FeedTier:
        # BARS, not QUOTES: there is no book in this payload. Stamping this
        # correctly is what stops a bar-derived backtest being averaged with a
        # quote-derived one. See issue #4.
        return FeedTier.BARS

    async def probe(self) -> None:
        res = await self._http.get(
            CHART_URL.format(symbol="AAPL"),
            source=self.name, kind="bars", symbols=("AAPL",),
            params={"range": "1d", "interval": "1m"}, record_raw=False,
        )
        if res.status == 429:
            raise FeedUnavailable(
                "Yahoo is rate limiting us (429). This is usually self-inflicted "
                "by a burst of requests rather than a standing block; back off "
                "and retry before concluding the feed is unavailable. See issue #13."
            )
        if not res.ok:
            raise FeedUnavailable(f"Yahoo unavailable: {res.record.error}")

    async def bars(
        self, symbol: str, tf: str, limit: int
    ) -> tuple[list[Bar], FetchRecord]:
        res = await self._http.get(
            CHART_URL.format(symbol=symbol),
            source=self.name, kind="bars", symbols=(symbol,),
            params={"range": _RANGE_FOR_TF.get(tf, "1d"), "interval": tf},
        )
        if not res.ok or res.text is None:
            return [], res.record
        try:
            return self._parse_bars(res.text, symbol, tf)[-limit:], res.record
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            from dataclasses import replace

            return [], replace(res.record, ok=False, error=f"parse failed: {exc}")

    async def quotes(
        self, symbols: list[str]
    ) -> tuple[dict[str, Quote], list[FetchRecord]]:
        """One request per symbol -- there is no batch endpoint.

        At ~250ms each that is 5s for twenty symbols, which is the real reason
        this system is minutes-to-hours rather than seconds.
        """
        out: dict[str, Quote] = {}
        records: list[FetchRecord] = []
        for symbol in symbols:
            res = await self._http.get(
                CHART_URL.format(symbol=symbol),
                source=self.name, kind="quote", symbols=(symbol,),
                params={"range": "1d", "interval": "1m"},
            )
            records.append(res.record)
            if not res.ok or res.text is None:
                continue
            try:
                meta = json.loads(res.text)["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                if price is None:
                    continue
                out[symbol] = Quote(
                    symbol=symbol,
                    ts_ms=int(meta.get("regularMarketTime", 0)) * 1000
                    or self._clock.now_ms(),
                    last=float(price),
                    source=self.name,
                    received_ms=self._clock.now_ms(),
                    # bid/ask deliberately omitted: this payload has no book,
                    # and a fabricated one is worse than a missing one.
                )
            except (ValueError, KeyError, TypeError, IndexError):
                continue
        return out, records

    def _parse_bars(self, text: str, symbol: str, tf: str) -> list[Bar]:
        result = json.loads(text)["chart"]["result"][0]
        stamps = result.get("timestamp") or []
        q = result["indicators"]["quote"][0]

        bars: list[Bar] = []
        for i, ts in enumerate(stamps):
            o, h, l, c, v = (
                q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
            )
            # Yahoo pads the array with nulls for minutes with no trades. Those
            # are absences, not zero-volume bars, and inventing a flat candle
            # would put fake structure into every indicator downstream.
            if None in (o, h, l, c):
                continue
            bars.append(
                Bar(symbol=symbol, tf=tf, ts_ms=int(ts) * 1000, o=float(o),
                    h=float(h), l=float(l), c=float(c), v=float(v or 0.0),
                    source=self.name)
            )
        return bars
