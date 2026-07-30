-- 007_twin.sql -- link a session to the control it was run against.
--
-- The project exists to answer "does the LLM beat the deterministic baseline",
-- and until now nothing ran the control. `SessionConfig.run_baseline` was
-- parsed, recorded in the config JSON, and read by no code.
--
-- `cohort.pair()` compensated by matching sessions whose wall clocks overlapped
-- by 90% on the same universe. That is a substitute for a control, not a
-- control: the two sessions saw *similar* tape, not the same tape, and nothing
-- guaranteed they ran at all, let alone together.
--
-- The comparison this makes possible is paired, which matters more than it
-- sounds. Unpaired, the difference between arms is swamped by whatever the
-- market did that day; the day is a shared confounder worth far more basis
-- points than any edge being measured. Paired, the market move is common to
-- both arms and subtracts out.
--
-- NOTE: sessions recorded before this migration cannot be paired
-- retroactively. There is no way to reconstruct which control a session would
-- have had. They stay in the wall-clock fallback and are labelled provisional.

ALTER TABLE sessions ADD COLUMN twin_of INTEGER REFERENCES sessions(id);

-- Which side of the pair this row is. Denormalised on purpose: `classify()`
-- currently infers the arm by looking for '(deterministic baseline)' in the
-- decisions table, which is string-matching an f-string -- exactly the fragility
-- 005_provenance.sql was written to remove everywhere else.
ALTER TABLE sessions ADD COLUMN arm TEXT;

-- Partial index: only twinned rows are looked up this way, and most are not.
CREATE INDEX ix_sessions_twin ON sessions(twin_of) WHERE twin_of IS NOT NULL;
