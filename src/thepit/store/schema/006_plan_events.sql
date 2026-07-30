-- 006_plan_events.sql -- the history of a level, not just its final value.
--
-- `exit_plans` is keyed (session_id, symbol) and upserted, so every amendment and
-- every trailing step overwrote the previous level and `fired_ms` was reset to
-- NULL whenever the symbol was re-entered. Only the final state of one plan per
-- symbol survived.
--
-- That made the eval module's flagship measurement wrong rather than merely
-- coarse. A session that stopped out and re-entered reported BOTH fires against
-- the last stop it ever held, which produced a lateness of 112 seconds for a loop
-- that acted within one second. Found by running `tradectl eval` on two real
-- sessions and disbelieving the number.
--
-- Append-only. Nothing here is ever updated, so "what level was in force at
-- 14:32:07" is answerable, and so is "did the agent tighten its stop as it
-- learned" -- which was previously invisible.
CREATE TABLE exit_plan_events (
  id           INTEGER PRIMARY KEY,
  session_id   INTEGER NOT NULL REFERENCES sessions(id),
  symbol       TEXT    NOT NULL,
  ts_ms        INTEGER NOT NULL,

  kind         TEXT    NOT NULL,   -- 'attached' | 'amended' | 'trailed' | 'fired' | 'closed'
  long         INTEGER,
  entry_price  REAL,
  stop_price   REAL,
  target_price REAL,
  trail_bp     REAL,
  high_water   REAL,
  detail       TEXT,               -- for 'fired': the breach that caused it

  CHECK (kind IN ('attached','amended','trailed','fired','closed'))
);

CREATE INDEX ix_plan_events ON exit_plan_events(session_id, symbol, ts_ms);
