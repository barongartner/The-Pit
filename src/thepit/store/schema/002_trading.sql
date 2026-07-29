-- 002_trading.sql -- paper trading.
--
-- Session-centric on purpose. A session owns its capital, its positions, its
-- orders and its decisions. There is no separate long-lived agent object yet,
-- because sessions are what actually get run and a lifecycle abstraction with
-- one user and no users of it is just more to hold in your head.

CREATE TABLE sessions (
  id           INTEGER PRIMARY KEY,
  created_ms   INTEGER NOT NULL,
  started_ms   INTEGER,
  ends_ms      INTEGER,            -- hard clock. Flatten happens before this.
  finished_ms  INTEGER,

  status       TEXT NOT NULL DEFAULT 'planned',
  config       TEXT NOT NULL,      -- JSON: the SessionConfig it was created from
  capital      REAL NOT NULL,      -- starting cash
  cash         REAL NOT NULL,      -- current cash

  plan         TEXT,               -- the phase-1 plan, locked before any trade
  plan_ms      INTEGER,
  review       TEXT,               -- the phase-4 self-review
  halt_reason  TEXT,

  CHECK (status IN ('planned','running','flattening','done','halted','failed')),
  CHECK (capital > 0)
);

-- Positions. One row per symbol per session.
--
-- Derived from fills and treated as a cache: a boot check reconstructs it from
-- the fill stream and refuses to start if they disagree. Fills are the truth.
CREATE TABLE positions (
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  symbol     TEXT    NOT NULL,
  qty        REAL    NOT NULL,     -- negative is short
  avg_price  REAL    NOT NULL,
  realized   REAL    NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, symbol)
) WITHOUT ROWID;

CREATE TABLE orders (
  id           INTEGER PRIMARY KEY,
  session_id   INTEGER NOT NULL REFERENCES sessions(id),
  ts_ms        INTEGER NOT NULL,
  symbol       TEXT    NOT NULL,
  side         TEXT    NOT NULL,   -- 'buy' | 'sell'
  qty          REAL    NOT NULL,
  kind         TEXT    NOT NULL DEFAULT 'market',
  limit_price  REAL,

  -- The intent/allowed/filled triple. Keeping the REJECTED orders is the whole
  -- point: "what did it want to do that it was not allowed to do" is a more
  -- interesting question than "what did it do", and it is unanswerable if
  -- rejections are dropped on the floor.
  status       TEXT    NOT NULL DEFAULT 'proposed',
  reject_reason TEXT,
  filled_qty   REAL    NOT NULL DEFAULT 0,
  avg_fill     REAL,
  reason       TEXT,               -- the agent's stated reason
  conviction   INTEGER,            -- 1-10, scored against outcomes later

  CHECK (side IN ('buy','sell')),
  CHECK (qty > 0),
  CHECK (status IN ('proposed','rejected','filled','partial','cancelled')),
  CHECK (status <> 'rejected' OR reject_reason IS NOT NULL),
  CHECK (conviction IS NULL OR (conviction BETWEEN 1 AND 10))
);

CREATE INDEX ix_orders_session ON orders(session_id, ts_ms);

CREATE TABLE fills (
  id         INTEGER PRIMARY KEY,
  order_id   INTEGER NOT NULL REFERENCES orders(id),
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  ts_ms      INTEGER NOT NULL,
  symbol     TEXT    NOT NULL,
  side       TEXT    NOT NULL,
  qty        REAL    NOT NULL,
  price      REAL    NOT NULL,     -- what was actually paid, incl. slippage
  ref_price  REAL    NOT NULL,     -- the quote it was priced from
  cost       REAL    NOT NULL,     -- modeled spread + slippage, in dollars
  sim_tier   TEXT    NOT NULL,     -- 'bars' | 'quotes': the fidelity of this fill

  CHECK (qty > 0),
  CHECK (price > 0)
);

CREATE INDEX ix_fills_session ON fills(session_id, ts_ms);

-- Every LLM call: what it was asked, what it said, what that cost.
CREATE TABLE decisions (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES sessions(id),
  ts_ms       INTEGER NOT NULL,
  phase       TEXT    NOT NULL,    -- 'plan' | 'tick' | 'review'
  prompt      TEXT    NOT NULL,
  response    TEXT,
  parsed      TEXT,                -- the extracted JSON, when it parsed
  error       TEXT,
  latency_ms  INTEGER,
  cost_usd    REAL,
  tokens_in   INTEGER,
  tokens_out  INTEGER,

  CHECK (phase IN ('plan','tick','review'))
);

CREATE INDEX ix_decisions_session ON decisions(session_id, ts_ms);

-- Equity snapshots, for the curve.
CREATE TABLE equity (
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  ts_ms      INTEGER NOT NULL,
  cash       REAL    NOT NULL,
  positions_value REAL NOT NULL,
  equity     REAL    NOT NULL,
  PRIMARY KEY (session_id, ts_ms)
) WITHOUT ROWID;
