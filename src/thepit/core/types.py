"""Core value types and the feed protocols.

Everything here is frozen. These objects cross module boundaries constantly and
a mutable value type is how a bar gets quietly rewritten three layers away from
where it was read.

The protocols are the seam that makes providers swappable. Assume any data
provider breaks, changes terms, or blocks you within six months -- that already
happened once during Stage 1 (see issue #13) -- so nothing above this layer may
know which provider it is talking to.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class FeedTier(StrEnum):
    """How much fidelity a feed can honestly support.

    This is not cosmetic. It is stamped onto every simulated fill, because a
    bar-based run and a quote-based run are not measuring the same thing and
    must never be averaged into one performance number.

    BARS   -- OHLCV candles only. No spread. A fill engine on this tier must
              refuse spread-cross pricing rather than invent a spread.
    QUOTES -- real bid/ask. Spread-cross pricing is meaningful. Still not the
              NBBO if the source is a single venue such as IEX.
    """

    BARS = "bars"
    QUOTES = "quotes"


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    tf: str
    ts_ms: int          # bar OPEN time, per the provider
    o: float
    h: float
    l: float
    c: float
    v: float
    source: str

    def __post_init__(self) -> None:
        # The database has matching CHECK constraints. Both exist on purpose:
        # this catches a bad parse at the boundary with a useful stack trace,
        # the CHECK catches anything that reaches the writer by another path.
        if not (self.l <= self.o <= self.h and self.l <= self.c <= self.h):
            raise ValueError(f"incoherent bar {self.symbol}@{self.ts_ms}: {self}")
        if self.v < 0:
            raise ValueError(f"negative volume {self.symbol}@{self.ts_ms}")


@dataclass(frozen=True, slots=True)
class Quote:
    """A point-in-time look at a symbol.

    `bid` and `ask` are optional and that optionality is load-bearing: some
    sources give no book at all. Callers must handle None rather than assuming
    a spread, which is why there is no `mid` property here -- computing one from
    a missing book is exactly the fabrication this type exists to prevent.
    """

    symbol: str
    ts_ms: int          # the provider's timestamp for the quote
    last: float
    source: str
    received_ms: int    # ts_ms -> received_ms is feed latency, and we want it
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    volume: float | None = None

    @property
    def has_book(self) -> bool:
        return self.bid is not None and self.ask is not None

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def age_ms(self) -> int:
        """How stale the provider said this was when we received it."""
        return self.received_ms - self.ts_ms


@dataclass(frozen=True, slots=True)
class NewsItem:
    """A headline, filing, or earnings entry.

    `published_ms` is the publisher's timestamp and `ingested_ms` is ours. Both
    are kept because the gap between them is a real measurement -- it is the
    answer to "how late is my news feed", which bounds what any news-reactive
    strategy could possibly have captured.
    """

    id: str             # sha256(source || NUL || external_id), truncated
    published_ms: int
    ingested_ms: int
    source: str
    external_id: str
    kind: str           # 'news' | 'filing' | 'earnings'
    symbols: tuple[str, ...]
    headline: str
    summary: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class FetchRecord:
    """One outbound request, success or failure.

    Failures are recorded as rows, not merely logged. "No row" must mean "we did
    not try", never "we tried and it broke" -- otherwise a gap in the record is
    ambiguous and the uptime claim is unfalsifiable.
    """

    ts_ms: int
    source: str
    kind: str           # 'bars' | 'quote' | 'news'
    endpoint: str
    symbols: tuple[str, ...]
    ok: bool
    http_status: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    raw_path: str | None = None


class FeedUnavailable(RuntimeError):
    """This feed cannot serve requests right now.

    Distinct from an ordinary transport error: it means the adapter has
    determined the provider will not serve us at all (blocked, unauthenticated,
    terms changed) rather than that one request failed. The poller demotes a
    feed on this rather than retrying it into a rate limit.
    """


@runtime_checkable
class PriceFeed(Protocol):
    """A source of prices. Deliberately small.

    Implementations must not cache, retry indefinitely, or write to the
    database. They translate a provider's wire format into our types and record
    what they did. Caching is the poller's job; storage is the recorder's.
    """

    name: str

    def tier(self) -> FeedTier: ...

    async def probe(self) -> None:
        """Verify the provider will actually serve us.

        Called at startup. Raises :class:`FeedUnavailable` with a human-readable
        reason if not. This exists because a feed that silently returns nothing
        looks identical to a quiet market, and we would rather fail at boot than
        discover it after a night of recording nothing.
        """
        ...

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Latest quote per symbol. Missing symbols are omitted, not faked."""
        ...

    async def bars(self, symbol: str, tf: str, limit: int) -> list[Bar]:
        """Most recent `limit` bars, oldest first."""
        ...


@runtime_checkable
class NewsFeed(Protocol):
    """A source of headlines, filings, or earnings entries."""

    name: str

    async def probe(self) -> None: ...

    async def poll(self, symbols: list[str], since_ms: int) -> list[NewsItem]:
        """Items published since `since_ms`, oldest first.

        Note this is `since_ms` (a floor on publication time) and not a cutoff.
        The lookahead protection lives on the *read* path in the repository,
        where `as_of_ms` is a required argument -- see store/repos.py.
        """
        ...
