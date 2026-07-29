"""Alpaca market data feed.

The primary price source, promoted from fallback after Yahoo's API tier blocked
this network (issue #13).

Two properties that matter more than the rest of the adapter:

**It batches.** One request covers every watchlist symbol, where Yahoo needed
one per symbol. Twenty symbols go from ~5s to ~250ms, and the rate-limit
pressure drops by the same factor.

**It has a real book.** ``bid``/``ask``/sizes are present, so this feed reports
:data:`~thepit.core.types.FeedTier.QUOTES` and spread-cross fill pricing becomes
meaningful.

**But the free tier is IEX only -- roughly 2.5% of US equity volume, and not the
NBBO.** Crossing "the ask" against a single venue's book is neither conservative
nor optimistic, it is noise. Bar- and quote-derived results from this feed
validate logic, not edge, until a SIP subscription exists. Every fill records
the tier it was priced at so the two can never be silently averaged. See
issue #4.

Credentials come from environment variables whose names cannot overlap between
modes -- never one variable whose meaning depends on a flag. Pairing live keys
with the paper endpoint then fails with a 401 instead of trading real money.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace

from thepit.core.clock import Clock
from thepit.core.types import Bar, FeedTier, FeedUnavailable, FetchRecord, Quote
from thepit.feeds.http import FeedHttp

DATA_BASE = "https://data.alpaca.markets/v2/stocks"

# 'iex' is the free tier. 'sip' is the full consolidated tape and requires a
# paid subscription; the difference is the whole content of issue #4.
FEED_IEX = "iex"
FEED_SIP = "sip"

_TF_TO_ALPACA = {
    "1m": "1Min", "2m": "2Min", "5m": "5Min", "15m": "15Min",
    "30m": "30Min", "1h": "1Hour", "1d": "1Day",
}


class AlpacaFeed:
    name = "alpaca"

    def __init__(
        self,
        http: FeedHttp,
        clock: Clock,
        *,
        key_id: str | None = None,
        secret: str | None = None,
        data_feed: str = FEED_IEX,
    ) -> None:
        # Paper credentials only. There is deliberately no code path here that
        # can read ALPACA_LIVE_*; the live broker is a separate type with its
        # own constructor, so a mode mixup is not expressible rather than merely
        # unlikely.
        self._key_id = key_id or os.environ.get("ALPACA_PAPER_KEY_ID", "")
        self._secret = secret or os.environ.get("ALPACA_PAPER_SECRET", "")
        self._http = http
        self._clock = clock
        self._data_feed = data_feed

    def tier(self) -> FeedTier:
        return FeedTier.QUOTES

    @property
    def configured(self) -> bool:
        return bool(self._key_id and self._secret)

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key_id,
            "APCA-API-SECRET-KEY": self._secret,
        }

    async def probe(self) -> None:
        if not self.configured:
            raise FeedUnavailable(
                "Alpaca credentials not set. Export ALPACA_PAPER_KEY_ID and "
                "ALPACA_PAPER_SECRET (see issue #11)."
            )
        res = await self._http.get(
            f"{DATA_BASE}/quotes/latest",
            source=self.name, kind="quote", symbols=("AAPL",),
            params={"symbols": "AAPL", "feed": self._data_feed},
            headers=self._headers(), record_raw=False,
        )
        if res.status in (401, 403):
            raise FeedUnavailable(
                "Alpaca rejected the credentials. Check they are PAPER keys and "
                "that they were pasted whole."
            )
        if not res.ok:
            raise FeedUnavailable(f"Alpaca unavailable: {res.record.error}")

    async def quotes(
        self, symbols: list[str]
    ) -> tuple[dict[str, Quote], list[FetchRecord]]:
        """Latest quote for every symbol in ONE request."""
        if not symbols:
            return {}, []

        res = await self._http.get(
            f"{DATA_BASE}/quotes/latest",
            source=self.name, kind="quote", symbols=tuple(symbols),
            params={"symbols": ",".join(symbols), "feed": self._data_feed},
            headers=self._headers(),
        )
        if not res.ok or res.text is None:
            return {}, [res.record]

        try:
            payload = json.loads(res.text).get("quotes", {})
        except ValueError as exc:
            return {}, [replace(res.record, ok=False, error=f"parse failed: {exc}")]

        now = self._clock.now_ms()
        out: dict[str, Quote] = {}
        for symbol, q in payload.items():
            bid, ask = q.get("bp"), q.get("ap")
            # Alpaca reports 0 for "no quote on this venue right now", which is
            # not a price. Treating it as one would put a zero bid into the fill
            # engine, which is the kind of thing that looks like a 100% loss.
            bid = float(bid) if bid else None
            ask = float(ask) if ask else None
            if bid is None and ask is None:
                continue
            mid = (bid + ask) / 2 if bid and ask else (bid or ask)
            out[symbol] = Quote(
                symbol=symbol,
                ts_ms=_rfc3339_to_ms(q.get("t")) or now,
                last=float(mid),
                source=self.name,
                received_ms=now,
                bid=bid, ask=ask,
                bid_size=float(q["bs"]) if q.get("bs") else None,
                ask_size=float(q["as"]) if q.get("as") else None,
            )
        return out, [res.record]

    async def bars(
        self, symbol: str, tf: str, limit: int
    ) -> tuple[list[Bar], FetchRecord]:
        bars, records = await self.bars_many([symbol], tf, limit)
        return bars.get(symbol, []), records[0]

    async def bars_many(
        self, symbols: list[str], tf: str, limit: int
    ) -> tuple[dict[str, list[Bar]], list[FetchRecord]]:
        """Bars for every symbol in one request."""
        if not symbols:
            return {}, []

        res = await self._http.get(
            f"{DATA_BASE}/bars",
            source=self.name, kind="bars", symbols=tuple(symbols),
            params={
                "symbols": ",".join(symbols),
                "timeframe": _TF_TO_ALPACA.get(tf, "1Min"),
                "limit": limit * len(symbols),
                "feed": self._data_feed,
                "adjustment": "split",   # unadjusted splits read as a 75% loss
            },
            headers=self._headers(),
        )
        if not res.ok or res.text is None:
            return {}, [res.record]

        try:
            payload = json.loads(res.text).get("bars") or {}
        except ValueError as exc:
            return {}, [replace(res.record, ok=False, error=f"parse failed: {exc}")]

        out: dict[str, list[Bar]] = {}
        for symbol, rows in payload.items():
            parsed: list[Bar] = []
            for r in rows:
                ts = _rfc3339_to_ms(r.get("t"))
                if ts is None:
                    continue
                try:
                    parsed.append(
                        Bar(symbol=symbol, tf=tf, ts_ms=ts, o=float(r["o"]),
                            h=float(r["h"]), l=float(r["l"]), c=float(r["c"]),
                            v=float(r.get("v", 0.0)), source=self.name)
                    )
                except (KeyError, TypeError, ValueError):
                    # A malformed bar is dropped rather than failing the whole
                    # batch: one bad row should not cost nineteen good symbols.
                    continue
            out[symbol] = parsed[-limit:]
        return out, [res.record]


def _rfc3339_to_ms(s: str | None) -> int | None:
    """Alpaca timestamps are RFC-3339 with nanosecond precision and a `Z`.

    `datetime.fromisoformat` handles `Z` from 3.11 on, but not nine fractional
    digits, so the fraction is truncated to microseconds first.
    """
    if not s:
        return None
    from datetime import datetime

    try:
        if "." in s:
            head, _, tail = s.partition(".")
            digits = "".join(c for c in tail if c.isdigit())[:6]
            suffix = tail[len(digits):].lstrip("0123456789") or "Z"
            s = f"{head}.{digits.ljust(6, '0')}{suffix}"
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None
