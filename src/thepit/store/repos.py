"""Repositories: the only code that writes SQL against the Stage 1 tables.

Two things here are load-bearing rather than merely tidy.

**Idempotent writes.** Every insert is ``ON CONFLICT DO NOTHING`` (or an explicit
upsert). The poller re-fetches overlapping windows constantly and a crash-restart
replays whatever was in flight, so writes must be safe to repeat. Anything that
would double-count is a bug that only shows up under the exact conditions you
cannot reproduce.

**Lookahead protection on news.** :meth:`NewsRepo.as_of` takes its cutoff as a
required positional argument. There is no overload without it and no default.
That is the enforcement -- a comment saying "remember to filter by publication
time" would be forgotten inside a month, and the resulting backtest would look
brilliant.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from thepit.core.types import Bar, FetchRecord, NewsItem, Quote


def news_id(source: str, external_id: str) -> str:
    """Stable id for deduplication across sources and restarts.

    NUL as the separator because it cannot occur in either component, so
    ``("ab", "c")`` and ``("a", "bc")`` cannot collide.
    """
    digest = hashlib.sha256(f"{source}\0{external_id}".encode()).hexdigest()
    return digest[:32]


class BarsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_many(self, bars: list[Bar], ingested_ms: int) -> int:
        """Insert bars, ignoring ones already recorded.

        DO NOTHING rather than an upsert: the primary key includes `source`, so
        a conflict means this exact provider already gave us this exact bar. The
        first version recorded is kept. Providers do revise recent bars, but
        silently rewriting history under a running strategy is worse than a
        stale final candle -- and the revision is visible in the raw recording
        if we ever need it.
        """
        if not bars:
            return 0
        rows = [
            (b.symbol, b.tf, b.ts_ms, b.o, b.h, b.l, b.c, b.v, b.source, ingested_ms)
            for b in bars
        ]
        cur = self._conn.executemany(
            "INSERT INTO bars (symbol,tf,ts_ms,o,h,l,c,v,source,ingested_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            rows,
        )
        return cur.rowcount

    def latest(self, symbol: str, tf: str, limit: int = 100) -> list[Bar]:
        rows = self._conn.execute(
            "SELECT * FROM bars WHERE symbol=? AND tf=? ORDER BY ts_ms DESC LIMIT ?",
            (symbol, tf, limit),
        ).fetchall()
        return [
            Bar(r["symbol"], r["tf"], r["ts_ms"], r["o"], r["h"], r["l"], r["c"],
                r["v"], r["source"])
            for r in reversed(rows)
        ]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]


class TicksRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_many(self, quotes: list[Quote]) -> int:
        if not quotes:
            return 0
        rows = [
            (q.symbol, q.ts_ms, q.last, q.bid, q.ask, q.bid_size, q.ask_size,
             q.volume, q.source, q.received_ms)
            for q in quotes
        ]
        cur = self._conn.executemany(
            "INSERT INTO ticks (symbol,ts_ms,last,bid,ask,bid_size,ask_size,volume,"
            "source,received_ms) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            rows,
        )
        return cur.rowcount

    def latest(self, symbol: str) -> Quote | None:
        r = self._conn.execute(
            "SELECT * FROM ticks WHERE symbol=? ORDER BY ts_ms DESC LIMIT 1", (symbol,)
        ).fetchone()
        if r is None:
            return None
        return Quote(
            symbol=r["symbol"], ts_ms=r["ts_ms"], last=r["last"], source=r["source"],
            received_ms=r["received_ms"], bid=r["bid"], ask=r["ask"],
            bid_size=r["bid_size"], ask_size=r["ask_size"], volume=r["volume"],
        )


class NewsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_many(self, items: list[NewsItem]) -> int:
        if not items:
            return 0
        rows = [
            (i.id, i.published_ms, i.ingested_ms, i.source, i.external_id, i.kind,
             json.dumps(list(i.symbols)), i.headline, i.summary, i.url)
            for i in items
        ]
        cur = self._conn.executemany(
            "INSERT INTO news (id,published_ms,ingested_ms,source,external_id,kind,"
            "symbols,headline,summary,url) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            rows,
        )
        return cur.rowcount

    # -- the read path -------------------------------------------------------
    #
    # THIS IS THE LOOKAHEAD GUARD. `as_of_ms` is positional and required.
    #
    # An agent reasoning at simulated time T may only see what was published
    # before T. In live-forward that is automatic and this argument is just
    # `now`. In replay it is the whole ballgame: without it, an agent "decides"
    # at 09:35 using a headline published at 15:50, and the resulting equity
    # curve is a fabrication that looks like genius.
    #
    # There is deliberately no convenience method that omits it.

    def as_of(
        self,
        as_of_ms: int,
        *,
        symbols: list[str] | None = None,
        limit: int = 50,
        since_ms: int = 0,
    ) -> list[NewsItem]:
        """Items published strictly before `as_of_ms`, newest first.

        Strict `<` rather than `<=`: an item published in the same millisecond a
        decision is made was not available to that decision.
        """
        sql = "SELECT * FROM news WHERE published_ms < ? AND published_ms >= ?"
        params: list[object] = [as_of_ms, since_ms]

        if symbols:
            # symbols is a JSON array; json_each is the portable way to test
            # membership without a junction table, and this table stays small
            # enough that the scan cost is irrelevant.
            sql += (
                " AND EXISTS (SELECT 1 FROM json_each(news.symbols) "
                "WHERE json_each.value IN (%s))" % ",".join("?" * len(symbols))
            )
            params.extend(symbols)

        sql += " ORDER BY published_ms DESC LIMIT ?"
        params.append(limit)

        return [self._row(r) for r in self._conn.execute(sql, params)]

    def latest_published_ms(self, source: str) -> int:
        """Watermark for incremental polling. 0 when we have nothing yet."""
        r = self._conn.execute(
            "SELECT MAX(published_ms) AS m FROM news WHERE source=?", (source,)
        ).fetchone()
        return int(r["m"] or 0)

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]

    @staticmethod
    def _row(r: sqlite3.Row) -> NewsItem:
        return NewsItem(
            id=r["id"], published_ms=r["published_ms"], ingested_ms=r["ingested_ms"],
            source=r["source"], external_id=r["external_id"], kind=r["kind"],
            symbols=tuple(json.loads(r["symbols"])), headline=r["headline"],
            summary=r["summary"], url=r["url"],
        )


class FetchLogRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, rec: FetchRecord) -> None:
        self._conn.execute(
            "INSERT INTO fetch_log (ts_ms,source,kind,endpoint,symbols,http_status,"
            "latency_ms,ok,error,raw_path) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rec.ts_ms, rec.source, rec.kind, rec.endpoint,
             json.dumps(list(rec.symbols)), rec.http_status, rec.latency_ms,
             1 if rec.ok else 0, rec.error, rec.raw_path),
        )

    def uptime(self, since_ms: int, until_ms: int) -> dict[str, object]:
        """Summary for the 24h uptime proof.

        Returns counts and latency percentiles rather than a single "uptime %",
        because a feed that returns 200s full of empty payloads is up by any
        naive measure and useless in fact.
        """
        rows = self._conn.execute(
            "SELECT source, ok, latency_ms FROM fetch_log "
            "WHERE ts_ms >= ? AND ts_ms <= ?",
            (since_ms, until_ms),
        ).fetchall()

        by_source: dict[str, dict[str, object]] = {}
        for r in rows:
            s = by_source.setdefault(
                r["source"], {"total": 0, "ok": 0, "failed": 0, "_lat": []}
            )
            s["total"] = int(s["total"]) + 1
            if r["ok"]:
                s["ok"] = int(s["ok"]) + 1
                if r["latency_ms"] is not None:
                    lat = s["_lat"]
                    assert isinstance(lat, list)
                    lat.append(r["latency_ms"])
            else:
                s["failed"] = int(s["failed"]) + 1

        for s in by_source.values():
            lat = sorted(s.pop("_lat"))  # type: ignore[arg-type]
            total = int(s["total"])
            s["success_rate"] = round(int(s["ok"]) / total, 4) if total else 0.0
            s["p50_ms"] = lat[len(lat) // 2] if lat else None
            s["p99_ms"] = lat[min(len(lat) - 1, int(len(lat) * 0.99))] if lat else None
        return by_source

    def gaps(self, since_ms: int, until_ms: int, max_gap_ms: int) -> list[tuple[int, int]]:
        """Windows longer than `max_gap_ms` with no successful fetch at all.

        This is the number that actually answers "did it stay up for 24 hours".
        A count of successes cannot: a feed can succeed 10,000 times and still
        have been dead for the three hours you care about.
        """
        rows = self._conn.execute(
            "SELECT ts_ms FROM fetch_log WHERE ok=1 AND ts_ms BETWEEN ? AND ? "
            "ORDER BY ts_ms",
            (since_ms, until_ms),
        ).fetchall()

        out: list[tuple[int, int]] = []
        prev = since_ms
        for r in rows:
            if r["ts_ms"] - prev > max_gap_ms:
                out.append((prev, r["ts_ms"]))
            prev = r["ts_ms"]
        if until_ms - prev > max_gap_ms:
            out.append((prev, until_ms))
        return out


class EventsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def emit(
        self,
        ts_ms: int,
        level: str,
        kind: str,
        subject: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO events (ts_ms,level,kind,subject,detail) VALUES (?,?,?,?,?)",
            (ts_ms, level, kind, subject, json.dumps(detail) if detail else None),
        )

    def recent(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events ORDER BY ts_ms DESC LIMIT ?", (limit,)
        ).fetchall()
