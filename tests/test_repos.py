"""Repository tests.

The centrepiece is :func:`test_news_as_of_never_leaks_the_future`. If that test
ever fails, every result this project has produced is worthless, so it is worth
more than the rest of this file put together.
"""

from __future__ import annotations

import inspect

import pytest

from thepit.core.types import Bar, FetchRecord, NewsItem, Quote
from thepit.store import db
from thepit.store.repos import (
    BarsRepo,
    EventsRepo,
    FetchLogRepo,
    NewsRepo,
    TicksRepo,
    news_id,
)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    yield c
    c.close()


def _news(pub_ms: int, ext: str, symbols=("AAPL",), source="edgar") -> NewsItem:
    return NewsItem(
        id=news_id(source, ext), published_ms=pub_ms, ingested_ms=pub_ms + 1000,
        source=source, external_id=ext, kind="filing", symbols=tuple(symbols),
        headline=f"headline {ext}",
    )


# ---------------------------------------------------------------------------
# Lookahead protection. The most important tests in the repository.
# ---------------------------------------------------------------------------


def test_news_as_of_never_leaks_the_future(conn):
    repo = NewsRepo(conn)
    repo.upsert_many([_news(1_000, "past"), _news(5_000, "future")])

    got = repo.as_of(3_000)
    assert [i.external_id for i in got] == ["past"]


def test_news_as_of_is_strict_not_inclusive(conn):
    """An item published in the same millisecond as the decision was not
    available to that decision."""
    repo = NewsRepo(conn)
    repo.upsert_many([_news(3_000, "simultaneous")])
    assert repo.as_of(3_000) == []
    assert len(repo.as_of(3_001)) == 1


def test_news_as_of_requires_the_cutoff_positionally(conn):
    """The guard is the signature. If someone adds a default to `as_of_ms`,
    every call site silently loses its lookahead protection -- so assert the
    shape of the signature itself, not just its behaviour."""
    sig = inspect.signature(NewsRepo.as_of)
    p = sig.parameters["as_of_ms"]
    assert p.default is inspect.Parameter.empty, "as_of_ms must not have a default"
    assert p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    # And there must be no other public read method that could bypass it.
    readers = [
        n for n, m in inspect.getmembers(NewsRepo, inspect.isfunction)
        if not n.startswith("_") and n not in {"upsert_many", "count", "latest_published_ms"}
    ]
    assert readers == ["as_of"], f"unexpected news read path(s): {readers}"


def test_news_as_of_filters_by_symbol(conn):
    repo = NewsRepo(conn)
    repo.upsert_many([
        _news(1_000, "a", symbols=("AAPL",)),
        _news(1_100, "b", symbols=("TSLA",)),
        _news(1_200, "c", symbols=("AAPL", "MSFT")),
    ])
    got = {i.external_id for i in repo.as_of(9_999, symbols=["AAPL"])}
    assert got == {"a", "c"}


def test_news_as_of_respects_since_floor(conn):
    repo = NewsRepo(conn)
    repo.upsert_many([_news(1_000, "old"), _news(8_000, "new")])
    got = repo.as_of(9_999, since_ms=5_000)
    assert [i.external_id for i in got] == ["new"]


# ---------------------------------------------------------------------------
# Dedupe / idempotency. The poller re-fetches overlapping windows constantly.
# ---------------------------------------------------------------------------


def test_news_id_is_stable_and_collision_resistant(conn):
    assert news_id("edgar", "x") == news_id("edgar", "x")
    assert news_id("edgar", "x") != news_id("yahoo", "x")
    # NUL separator: ("ab","c") and ("a","bc") must not collide.
    assert news_id("ab", "c") != news_id("a", "bc")


def test_news_upsert_is_idempotent(conn):
    repo = NewsRepo(conn)
    items = [_news(1_000, "a"), _news(2_000, "b")]
    repo.upsert_many(items)
    repo.upsert_many(items)
    assert repo.count() == 2


def test_bars_upsert_is_idempotent(conn):
    repo = BarsRepo(conn)
    bars = [Bar("AAPL", "1m", 60_000, 10, 11, 9, 10.5, 100, "test")]
    repo.upsert_many(bars, ingested_ms=1)
    repo.upsert_many(bars, ingested_ms=2)
    assert repo.count() == 1


def test_bars_from_different_sources_both_kept(conn):
    repo = BarsRepo(conn)
    repo.upsert_many([Bar("AAPL", "1m", 60_000, 10, 11, 9, 10.5, 100, "yahoo")], 1)
    repo.upsert_many([Bar("AAPL", "1m", 60_000, 10, 11, 9, 10.6, 100, "alpaca")], 1)
    assert repo.count() == 2


def test_bars_latest_returns_oldest_first(conn):
    repo = BarsRepo(conn)
    repo.upsert_many(
        [Bar("AAPL", "1m", t, 10, 11, 9, 10.5, 100, "test")
         for t in (60_000, 120_000, 180_000)],
        ingested_ms=1,
    )
    assert [b.ts_ms for b in repo.latest("AAPL", "1m")] == [60_000, 120_000, 180_000]


# ---------------------------------------------------------------------------
# Bar validation happens at the type boundary too, not only in SQL.
# ---------------------------------------------------------------------------


def test_bar_type_rejects_incoherent_ohlc():
    with pytest.raises(ValueError, match="incoherent"):
        Bar("AAPL", "1m", 1, o=99.0, h=11.0, l=9.0, c=10.0, v=1.0, source="x")


def test_bar_type_rejects_negative_volume():
    with pytest.raises(ValueError, match="negative volume"):
        Bar("AAPL", "1m", 1, 10, 11, 9, 10, -1.0, "x")


# ---------------------------------------------------------------------------
# Quote: no fabricated spreads.
# ---------------------------------------------------------------------------


def test_quote_without_book_reports_no_spread():
    q = Quote("AAPL", 1, 10.0, "yahoo", 2)
    assert q.has_book is False
    assert q.spread is None


def test_quote_exposes_feed_latency():
    q = Quote("AAPL", ts_ms=1_000, last=10.0, source="x", received_ms=1_250)
    assert q.age_ms == 250


def test_ticks_roundtrip_preserves_missing_book(conn):
    repo = TicksRepo(conn)
    repo.insert_many([Quote("AAPL", 1, 10.0, "yahoo", 2)])
    got = repo.latest("AAPL")
    assert got is not None and got.bid is None and got.has_book is False


# ---------------------------------------------------------------------------
# fetch_log: the uptime proof.
# ---------------------------------------------------------------------------


def _fetch(ts, ok=True, lat=100, source="yahoo"):
    return FetchRecord(
        ts_ms=ts, source=source, kind="bars", endpoint="/chart", symbols=("AAPL",),
        ok=ok, http_status=200 if ok else 429, latency_ms=lat if ok else None,
        error=None if ok else "429",
    )


def test_uptime_summary_separates_sources(conn):
    repo = FetchLogRepo(conn)
    for t in range(0, 5):
        repo.record(_fetch(t * 1000, ok=(t != 3)))
    repo.record(_fetch(9_000, source="edgar"))

    got = repo.uptime(0, 10_000)
    assert got["yahoo"]["total"] == 5
    assert got["yahoo"]["failed"] == 1
    assert got["yahoo"]["success_rate"] == 0.8
    assert got["edgar"]["success_rate"] == 1.0


def test_gaps_finds_the_window_where_nothing_succeeded(conn):
    """A count of successes cannot prove uptime: a feed can succeed 10,000 times
    and still have been dead for the three hours you care about."""
    repo = FetchLogRepo(conn)
    repo.record(_fetch(0))
    repo.record(_fetch(1_000))
    # nothing between 1s and 60s
    repo.record(_fetch(60_000))

    gaps = repo.gaps(0, 60_000, max_gap_ms=10_000)
    assert gaps == [(1_000, 60_000)]


def test_gaps_reports_a_trailing_outage(conn):
    repo = FetchLogRepo(conn)
    repo.record(_fetch(0))
    gaps = repo.gaps(0, 100_000, max_gap_ms=10_000)
    assert gaps == [(0, 100_000)]


def test_failed_fetches_are_recorded_not_dropped(conn):
    repo = FetchLogRepo(conn)
    repo.record(_fetch(1, ok=False))
    assert repo.uptime(0, 10)["yahoo"]["failed"] == 1


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_events_roundtrip(conn):
    repo = EventsRepo(conn)
    repo.emit(1, "warn", "feed_degraded", subject="yahoo", detail={"status": 429})
    rows = repo.recent()
    assert rows[0]["kind"] == "feed_degraded"
    assert rows[0]["subject"] == "yahoo"
