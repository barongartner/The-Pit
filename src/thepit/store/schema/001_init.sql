-- 001_init.sql -- Stage 1: the data layer.
--
-- Conventions used throughout this schema:
--   * All times are integer milliseconds since the Unix epoch, UTC. Never local
--     time, never a text timestamp. Comparisons and range scans on integers are
--     unambiguous; on ISO strings they are a source of subtle bugs.
--   * `*_ms` names the instant. Where two instants exist for one row we keep
--     BOTH, because the gap between them is itself a measurement we will want.
--   * `source` is always recorded. Two feeds can disagree, and when they do we
--     need to know which one said what rather than silently keeping the last
--     writer.

-- No PRAGMA statements in migration files. Migrations run inside an explicit
-- transaction (see store/db.py::migrate) and most pragmas are either no-ops or
-- errors there. Connection pragmas belong in db.connect(), which is the single
-- place every connection passes through.

-- ---------------------------------------------------------------------------
-- meta: schema version and small singleton state.
-- ---------------------------------------------------------------------------
CREATE TABLE meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- bars: OHLCV candles.
--
-- `source` is part of the primary key on purpose. Yahoo and Alpaca will not
-- agree bar-for-bar (different venues, different aggregation). Overwriting one
-- with the other would silently corrupt the record; keeping both lets us
-- measure the disagreement, which is a real data-quality signal.
-- ---------------------------------------------------------------------------
CREATE TABLE bars (
  symbol      TEXT    NOT NULL,
  tf          TEXT    NOT NULL,          -- '1m' | '5m' | '1h' | '1d'
  ts_ms       INTEGER NOT NULL,          -- bar OPEN time, per the provider
  o           REAL    NOT NULL,
  h           REAL    NOT NULL,
  l           REAL    NOT NULL,
  c           REAL    NOT NULL,
  v           REAL    NOT NULL,
  source      TEXT    NOT NULL,
  ingested_ms INTEGER NOT NULL,          -- when WE saw it
  PRIMARY KEY (symbol, tf, ts_ms, source),

  -- A bar whose high is below its low, or whose open sits outside [low, high],
  -- is corrupt. Catch it at the boundary rather than three layers into a
  -- strategy where it will present as an inexplicable signal.
  CHECK (h >= l),
  CHECK (o >= l AND o <= h),
  CHECK (c >= l AND c <= h),
  CHECK (v >= 0),
  CHECK (tf IN ('1m', '2m', '5m', '15m', '30m', '1h', '1d'))
) WITHOUT ROWID;

CREATE INDEX ix_bars_symbol_ts ON bars(symbol, ts_ms);

-- ---------------------------------------------------------------------------
-- ticks: point-in-time quotes.
--
-- bid/ask are nullable because Yahoo does not provide them. That nullability is
-- load-bearing: the fill engine must refuse to do spread-cross pricing when the
-- spread is unknown rather than inventing one. See issue #4.
-- ---------------------------------------------------------------------------
CREATE TABLE ticks (
  symbol      TEXT    NOT NULL,
  ts_ms       INTEGER NOT NULL,          -- provider's timestamp for the quote
  last        REAL    NOT NULL,
  bid         REAL,
  ask         REAL,
  bid_size    REAL,
  ask_size    REAL,
  volume      REAL,
  source      TEXT    NOT NULL,
  received_ms INTEGER NOT NULL,          -- ts_ms -> received_ms is feed latency
  PRIMARY KEY (symbol, ts_ms, source),

  CHECK (last > 0),
  CHECK (bid IS NULL OR bid > 0),
  CHECK (ask IS NULL OR ask > 0),
  -- A crossed book (bid > ask) is either bad data or a stale composite. Either
  -- way we must not store it as if it were tradable.
  CHECK (bid IS NULL OR ask IS NULL OR bid <= ask)
) WITHOUT ROWID;

CREATE INDEX ix_ticks_symbol_ts ON ticks(symbol, ts_ms);

-- ---------------------------------------------------------------------------
-- fetch_log: one row per outbound request. This is the uptime proof and the
-- provenance of the whole dataset.
--
-- Failures are recorded as rows too, not just logged. "No row" must mean "we
-- did not try", never "we tried and it broke" -- otherwise a gap in this table
-- is ambiguous and the 24h uptime claim is unfalsifiable.
-- ---------------------------------------------------------------------------
CREATE TABLE fetch_log (
  id          INTEGER PRIMARY KEY,
  ts_ms       INTEGER NOT NULL,          -- when the request was sent
  source      TEXT    NOT NULL,          -- 'yahoo' | 'edgar' | 'alpaca'
  kind        TEXT    NOT NULL,          -- 'bars' | 'quote' | 'news'
  endpoint    TEXT    NOT NULL,
  symbols     TEXT    NOT NULL,          -- JSON array; '[]' for non-symbol fetches
  http_status INTEGER,                   -- NULL when the request never completed
  latency_ms  INTEGER,
  ok          INTEGER NOT NULL,          -- 0/1
  error       TEXT,                      -- exception class + message, truncated
  raw_path    TEXT,                      -- relative path into data/raw/, if kept

  CHECK (ok IN (0, 1)),
  CHECK (ok = 1 OR error IS NOT NULL)    -- a failure must say why
);

CREATE INDEX ix_fetch_ts     ON fetch_log(ts_ms);
CREATE INDEX ix_fetch_src_ts ON fetch_log(source, ts_ms);
-- Partial index: the uptime report scans failures far more often than successes,
-- and failures are the rare case.
CREATE INDEX ix_fetch_fail   ON fetch_log(ts_ms) WHERE ok = 0;

-- ---------------------------------------------------------------------------
-- news: headlines, filings, earnings.
--
-- THE CRITICAL COLUMN IS published_ms.
--
-- An agent at simulated time T may only ever see items with published_ms < T.
-- In live-forward that is automatic. In replay it is one missing join condition
-- away from a lookahead bug that makes any strategy look brilliant.
--
-- The repository read method takes `as_of_ms` as a REQUIRED positional argument
-- so there is no way to query this table without stating the cutoff. That is
-- the enforcement; this comment is only the explanation.
-- ---------------------------------------------------------------------------
CREATE TABLE news (
  id           TEXT    PRIMARY KEY,      -- sha256(source || '\0' || external_id)[:32]
  published_ms INTEGER NOT NULL,         -- the PUBLISHER's timestamp. Never ours.
  ingested_ms  INTEGER NOT NULL,         -- when we saw it. The gap is a measurement.
  source       TEXT    NOT NULL,         -- 'edgar' | 'yahoo' | 'alpaca'
  external_id  TEXT    NOT NULL,         -- provider's id, for dedupe + debugging
  kind         TEXT    NOT NULL,         -- 'news' | 'filing' | 'earnings'
  symbols      TEXT    NOT NULL,         -- JSON array; '[]' for macro items
  headline     TEXT    NOT NULL,
  summary      TEXT,
  url          TEXT,
  body_path    TEXT,                     -- relative path into data/raw/
  raw_path     TEXT,

  CHECK (kind IN ('news', 'filing', 'earnings')),
  CHECK (published_ms > 0)
);

-- Every read is bounded by as_of_ms, so this is THE index that matters.
CREATE INDEX ix_news_pub        ON news(published_ms);
CREATE INDEX ix_news_source_ext ON news(source, external_id);

-- ---------------------------------------------------------------------------
-- commands: the API's only write path.
--
-- The API process opens the database read-only. To change anything it appends a
-- command here (via the engine's socket) and the engine drains it. That keeps
-- the single-writer invariant that makes SQLite WAL safe at this scale, and it
-- gives every mutation a durable record before it is applied.
-- ---------------------------------------------------------------------------
CREATE TABLE commands (
  id         INTEGER PRIMARY KEY,
  ts_ms      INTEGER NOT NULL,
  actor      TEXT    NOT NULL,           -- 'operator' | 'system' | agent id
  kind       TEXT    NOT NULL,
  payload    TEXT    NOT NULL,           -- JSON
  status     TEXT    NOT NULL DEFAULT 'pending',
  result     TEXT,
  handled_ms INTEGER,

  CHECK (status IN ('pending', 'done', 'failed')),
  CHECK (status = 'pending' OR handled_ms IS NOT NULL)
);

CREATE INDEX ix_commands_pending ON commands(id) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- events: engine lifecycle and health.
--
-- Feeds the dashboard, the WebSocket stream, and the uptime report. In later
-- stages the append-only, hash-chained `audit_log` carries anything that could
-- ever be asked "why did that happen"; this table stays as the cheap
-- operational stream.
-- ---------------------------------------------------------------------------
CREATE TABLE events (
  id       INTEGER PRIMARY KEY,
  ts_ms    INTEGER NOT NULL,
  level    TEXT    NOT NULL,             -- 'info' | 'warn' | 'error'
  kind     TEXT    NOT NULL,             -- 'feed_degraded' | 'feed_ok' | 'session_open' ...
  subject  TEXT,                         -- symbol, source, agent id
  detail   TEXT,                         -- JSON

  CHECK (level IN ('info', 'warn', 'error'))
);

CREATE INDEX ix_events_ts   ON events(ts_ms);
CREATE INDEX ix_events_kind ON events(kind, ts_ms);
