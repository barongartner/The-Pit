-- 005_provenance.sql -- who caused this order, and what it was meant to do.
--
-- Written for the eval module, after an audit of what it could actually measure
-- from the schema. Six of its questions were answerable only by string-matching
-- f-strings out of `orders.reason` ('fast loop stop: ', 'armed at ', 'session
-- flatten'), which means any wording change silently reattributes every trade in
-- the history to the model.
--
-- The other half is intent. `orders` recorded quantity and, on rejection, a
-- reason -- but never the stop, target or trigger the order was meant to carry.
-- So "did it chase its planned entry" and "would that rejected order have made
-- money" were unanswerable for exactly the orders that matter.

-- Origin, as a column instead of a prose prefix.
--
-- No CHECK constraint: SQLite cannot add one by ALTER, and rebuilding the table
-- to gain one would be a bigger risk than the check is worth. `eval/trades.py`
-- validates the vocabulary, and an unrecognised value is reported rather than
-- silently bucketed as 'model'.
--
--   model               a policy tick's own order
--   armed               an entry that fired at its stated level
--   fast_loop_stop      a stop enforced between ticks
--   fast_loop_target    a target enforced between ticks
--   fast_loop_time_stop a time stop
--   flatten             the end-of-session close
--   unprotected         an emergency unwind of a fill whose levels did not resolve
ALTER TABLE orders ADD COLUMN origin TEXT;

-- Which decision produced it. Attribution by timestamp proximity breaks the
-- moment the fast loop submits between ticks, which is now most closes.
ALTER TABLE orders ADD COLUMN decision_id INTEGER REFERENCES decisions(id);

-- The intent, recorded even when the order is rejected. These are what the agent
-- asked for; `exit_plans` holds what was actually enforced after the fill.
ALTER TABLE orders ADD COLUMN stop_price REAL;
ALTER TABLE orders ADD COLUMN target_price REAL;
ALTER TABLE orders ADD COLUMN trigger_price REAL;

-- The identity of the quote a fill was priced from, not just its value.
--
-- `ref_price` says what the price was; this says WHEN it was, so the gap between
-- "the tape breached the level" and "we acted" becomes a measurement instead of
-- an estimate. The runner's quote dict refreshes on its own 5s timer, so a fill
-- can legitimately be priced off a tick several seconds older than the newest
-- one recorded.
ALTER TABLE fills ADD COLUMN quote_ts_ms INTEGER;

-- The symbols the session actually traded.
--
-- `sessions.config` records `SessionConfig.symbols`, which is empty whenever the
-- request omitted them -- the default path. The universe was then recoverable
-- only by reading the plan prompt text.
ALTER TABLE sessions ADD COLUMN universe TEXT;
