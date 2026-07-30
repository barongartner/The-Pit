-- 004_levels.sql -- the levels a fast loop can enforce.
--
-- Until now the model returned *orders* and nothing existed between policy
-- ticks. A stop stated as "-15bp" lived in the reason field as prose, so it was
-- checked only when the model was next asked -- up to five minutes late. Session
-- 4 lost $6 with both positions drifting past their stated stops and nothing
-- watching. A 15bp stop honoured five minutes late is not a 15bp stop.
--
-- These two tables make a level machine-readable, so Python can enforce it on a
-- seconds cadence while the model thinks. See issue #18.

-- One active plan per open position. Written when the entry fills, amended when
-- the model revises it, and closed out when it fires.
--
-- Prices are absolute here even when the model expressed them in basis points:
-- resolution happens once, against the actual fill, so what gets enforced is
-- never re-derived from a drifting reference.
CREATE TABLE exit_plans (
  session_id   INTEGER NOT NULL REFERENCES sessions(id),
  symbol       TEXT    NOT NULL,
  created_ms   INTEGER NOT NULL,
  updated_ms   INTEGER NOT NULL,

  long         INTEGER NOT NULL,   -- 1 long, 0 short. Fixes which side is losing.
  entry_price  REAL    NOT NULL,
  stop_price   REAL    NOT NULL,   -- NOT NULL: an opening order without a stop is refused
  target_price REAL,
  trail_bp     REAL,               -- once set, the stop follows high_water and never retreats
  time_stop_ms INTEGER,            -- absolute deadline, from the entry fill
  high_water   REAL    NOT NULL,   -- best price seen since entry, for trailing

  status       TEXT    NOT NULL DEFAULT 'active',
  fired_ms     INTEGER,
  fired_reason TEXT,               -- 'stop' | 'target' | 'time_stop', with the number

  PRIMARY KEY (session_id, symbol),
  CHECK (status IN ('active','fired','closed')),
  CHECK (long IN (0,1)),
  CHECK (entry_price > 0 AND stop_price > 0),
  CHECK (trail_bp IS NULL OR trail_bp > 0)
) WITHOUT ROWID;

-- Entries armed at a level rather than taken at market.
--
-- The recorded failure this addresses: the model planned TSLA at 303.50, was
-- next asked five minutes later at 304.82, bought there anyway, and wrote in its
-- own review "Violated plan. Chasing late entries lost the session." An armed
-- level either prints or it does not; it cannot be chased.
--
-- A row here is an intent, not an order. When it triggers, the normal order path
-- runs -- risk check included -- and writes a row to `orders`, so the audit trail
-- of what was actually attempted stays in one place.
CREATE TABLE pending_entries (
  id            INTEGER PRIMARY KEY,
  session_id    INTEGER NOT NULL REFERENCES sessions(id),
  created_ms    INTEGER NOT NULL,
  symbol        TEXT    NOT NULL,
  side          TEXT    NOT NULL,
  qty           REAL    NOT NULL,

  trigger_price REAL    NOT NULL,
  direction     TEXT    NOT NULL,   -- which way the price has to cross to arm
  expires_ms    INTEGER NOT NULL,   -- never outlives the window in which opening is allowed

  -- The exit levels to attach when it fills, still in the form the model gave
  -- them. Basis points cannot be resolved until there is a fill to measure from.
  stop_price    REAL,
  stop_bp       REAL,
  target_price  REAL,
  target_bp     REAL,
  trail_bp      REAL,
  time_stop_minutes REAL,

  reason        TEXT,
  conviction    INTEGER,

  status        TEXT    NOT NULL DEFAULT 'waiting',
  resolved_ms   INTEGER,
  order_id      INTEGER REFERENCES orders(id),

  CHECK (side IN ('buy','sell')),
  CHECK (qty > 0),
  CHECK (trigger_price > 0),
  CHECK (direction IN ('at_or_below','at_or_above')),
  CHECK (status IN ('waiting','triggered','expired','cancelled'))
);

CREATE INDEX ix_pending_entries_session ON pending_entries(session_id, status);
