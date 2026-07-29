-- 003_activity.sql -- session activity log and liveness.
--
-- Two problems this fixes, both found by running a session and having no idea
-- what happened to it.
--
-- 1. A session is driven by an asyncio task inside the API process. Restart the
--    API and the task dies, but the row still says 'running' -- forever, with
--    nothing to detect it. `heartbeat_ms` makes a dead session detectable.
--
-- 2. During a 40-second model call, or a 3-minute wait between ticks, nothing
--    was visible. The panel looked frozen and identical to broken.

ALTER TABLE sessions ADD COLUMN heartbeat_ms INTEGER;

-- Human-readable running commentary. One row per thing that happens.
--
-- Deliberately separate from `decisions` (which stores prompts and responses)
-- and from `events` (engine-level health). This is the narrative: what is it
-- doing right now, and what did it just do.
CREATE TABLE activity (
  id         INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  ts_ms      INTEGER NOT NULL,
  kind       TEXT    NOT NULL,   -- 'phase' | 'model' | 'order' | 'fill' | 'wait' | 'error'
  message    TEXT    NOT NULL,

  -- Set when the line describes something ONGOING, cleared when it completes.
  -- That is what lets the UI show "asking the model (23s)" with a live counter
  -- instead of a stale line that might be from a minute ago.
  pending    INTEGER NOT NULL DEFAULT 0,

  CHECK (pending IN (0,1))
);

CREATE INDEX ix_activity_session ON activity(session_id, id);
