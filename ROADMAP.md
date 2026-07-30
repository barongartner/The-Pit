# ROADMAP

Everything known to be missing, broken, half-built, or worth building in The Pit,
with enough detail that a session which has never seen this repo can pick one item
and do it correctly.

Written 2026-07-30, from an adversarial sweep of every subsystem plus the audit
that followed the fast-loop work. Roughly 200 entries. It is not a plan to finish
them; it is a plan to never lose one.

---

## How to work from this file

**Read `CLAUDE-READ-THIS.md` first.** It is the project brief and it carries the
mistakes that have already been paid for. This file is the forward list; that one
is the memory.

**Pick from NOW before NEXT.** The NOW section is not "everything urgent", it is
"the things that make the other things possible or that are currently lying to
you". Inside NOW, the sequenced critical path below comes first.

**Work the checklist.** Every item has a box. Claim one by marking it `[~]` with
your date (`- [~] 2026-08-02 Title`), tick it `[x]` when it lands, and update the
progress count under [The checklist](#the-checklist). Two sessions doing the same
entry is the failure mode this document exists to prevent, and an unticked box is
the only thing standing between "we did that" and doing it twice.

**Do not tick a box without landing tests.** An entry is done when the behaviour it
describes is covered, not when the code compiles.

**Claim migration numbers here before you write the file.** `db._migration_files`
asserts migrations are gapless from 001, so two sessions both writing `007_*.sql`
means the system refuses to boot. Highest applied: **006**. Claimed and unwritten:
**007** (see the critical path — one migration carries the whole measurement
column set).

**Every entry needs evidence.** If you add one, cite `file:line` or a quote from a
project document. An entry with no evidence is an opinion and will be deleted.

**Update the honesty register.** If you make one of the "known lies" true, delete
it from that section in the same commit. If you find a new one, add it.

**Do not silently expand scope.** Land the entry you claimed. Put what you found on
the way into this file instead of into your branch.

---

## Where things stand — 2026-07-30

Paper trading runs end to end: plan, trade, levels enforced every 5 seconds between
model ticks, forced flatten, self-review. Prices and SEC filings record
continuously. `tradectl eval` scores what has run and says what it cannot score.
243 tests. Schema at 006. Roughly 11,700 lines of Python across 52 files.

What has actually been measured: almost nothing. A handful of sessions, one arm,
no pairing. Every number the eval prints today is a demonstration that the plumbing
works, not a result.

The three things standing between here and a first real answer, in order:

1. **Sessions run inside the API process**, so a restart kills them and the
   single-writer invariant is violated by the reader.
2. **Nothing spawns the baseline twin**, so LLM-versus-baseline is unpaired and
   confounded by whatever the market did that day.
3. **Nothing runs unattended**, so a cohort large enough to mean anything cannot be
   collected without Baron babysitting two terminals.

Everything else on this list is downstream of, or smaller than, those three.

---

## Hard rules

These are not preferences. Breaking one is worse than leaving the entry undone.

- **Never enable live trading, enter broker credentials, or build the arming
  ceremony.** That is Baron's own action. The Alpaca code path exists and is
  exercised against the paper endpoint only.
- **Never give trading or investment advice.** This is a software and measurement
  project. If an entry starts to read like a strategy recommendation, it is the
  wrong entry.
- **P&L is never `cash - capital`.** One implementation exists —
  `eval/pnl.py:session_pnl` — and tradectl, the API and the dashboard all call it.
  Do not write a fourth. The mistake has already produced a "−$3,060" on a session
  that was down $1.97.
- **Never make the paper result flatter itself.** When a modelling choice is
  arbitrary, take the pessimistic one and write down why. Adverse selection, queue
  position, market impact, partial fills, borrow cost and halts are all unmodelled
  and all in the same direction: reality is worse than the simulator.
- **Never average across `sim_tier`.** A bar-derived and a quote-derived fill are
  not the same measurement. `cohort.require_single_tier` raises; keep it that way.
- **A risk control that prevents de-risking is not a risk control.** This was paid
  for: `halted` sat above the de-risking bypass and the session loss limit locked
  the losing position open.
- **The repo is public.** The database, raw recordings, logs, journals and `.env`
  are gitignored and stay that way. Prompts and operator notes are not export
  fodder.

---

## The critical path

Ten items, in order. Each was assembled from several overlapping proposals — the
sweep found the same work described three or four different ways, and doing them
separately is how a third of a system gets built and called done.

- [ ] **1. Get sessions out of the API process** · large · unblocks six other entries
- [ ] **2. Migration 007, the whole measurement column set** · medium
- [ ] **3. Spawn the baseline twin** · large · highest value on the list
- [ ] **4. Record a shakedown cohort** · medium
- [ ] **5. Database lifecycle: retention, compaction, size, backup, restore** · medium
- [ ] **6. One flatten.py brake** · medium
- [ ] **7. Unattended operation** · medium
- [ ] **8. Make the tick prompt as informed as the plan prompt** · medium
- [ ] **9. The blinding path, whole** · large
- [ ] **10. Get the eval out of the terminal** · medium

The detail for each follows.

### 1. Get sessions out of the API process · large · unblocks six other entries

*Why:* `session_start` opens a **write** connection inside the process that is
supposed to be the read-only reader (`api/main.py:461`), and drives the session as
an asyncio task inside it (`main.py:481`). Restart the API and the session dies —
two have been lost that way. The `commands` table the schema was designed around
(`001_init.sql`) is drained by nobody, and `MarketView` (`engine/poller.py:78`) is
read by nothing outside tests.

*What:* (1) Move `SessionRunner` execution into the engine process, driven by the
`commands` table. (2) API control endpoints become command inserts, restoring
single-writer and making `MarketView` the runner's quote source. (3) Add
`tradectl session start` inserting the same command, giving a browserless path.
(4) Restart survival then becomes reachable, because the driving process is the one
holding the lock rather than the one being reloaded. Mid-session *resume* stays a
separate later entry.

*Risk:* Largest structural change on the list and it touches the risk path. Do it
before the baseline twin doubles the number of writers, and before any long cohort
is recorded — everything recorded under the old shape has a different concurrency
story.

### 2. Migration 007, carrying the whole measurement column set · medium

*Why:* At least five separate entries each want to add a column, and three of them
name "007". `db._migration_files` raises on a duplicate number, so the second one
to land stops the system from booting.

*What:* One migration adding `sessions.arm`, `sessions.experiment`,
`sessions.twin_of`, `sessions.uid`, `sessions.cli_version`, `sessions.risk_profile`
plus the three limit values, `decisions.model_id`, `decisions.attempts`,
`fetch_log.items`. Backfill what is derivable (arm from `cohort.classify`, limits
from the `config` JSON) in the same script. `eval/cohort.py` reads the columns and
keeps the string-matching classifier only as a fallback for pre-007 rows.
ALTER TABLE ADD COLUMN plus backfill UPDATEs only — no table rebuilds.

### 3. Spawn the baseline twin · large · the single highest-value item

*Why:* The project's central question is "does the LLM beat the deterministic
baseline", and nothing runs the control. `SessionConfig.run_baseline` is parsed,
recorded in the config JSON, and read by no code. `cohort.pair()` falls back to
matching sessions by overlapping wall clock, which is a substitute for a control,
not a control.

*What:* When `run_baseline` is set, start a second `SessionRunner` with
`use_stub=True` on the same symbols, capital, clock and quote refresher; record
`sessions.twin_of`; drive both from one task group so they start and end together.
Pair on the column, not on the clock.

*Blocked by:* nothing — but it must land before any cohort is recorded, because
sessions recorded without a twin cannot be paired retroactively.

### 4. Record a shakedown cohort before any measurement work · medium

*Why:* Everything downstream assumes sessions can be produced reliably on this
machine. Nobody has run more than a handful, and the ones that exist were produced
while the code was being changed underneath them.

*What:* Ten to twenty stub-only sessions on closed-market tape, unattended,
overnight. The deliverable is not P&L — it is the list of everything that broke.
Then freeze that corpus as a test fixture (entry: *Freeze a corpus of recorded
sessions*) so the eval's output can be asserted against known input.

### 5. The database lifecycle: retention, compaction, size, backup, restore · medium

*Why:* Only raw recordings are pruned. `ticks`, `bars`, `fetch_log` and `events`
grow forever on a machine with tight disk headroom, and **there is no backup of
anything, ever** — the recorded tape is the one asset this project accumulates.

*What:* Extend `engine/main.py::_retention` to prune the log tables past a
configurable horizon, schedule `VACUUM`, report database and WAL size to
`/api/status`, `tradectl status` and the dashboard, and take a
`sqlite3.Connection.backup()` to a dated file with a retention count. Include a
restore test that runs `db.assert_healthy` on the restored copy — an untested
backup is not a backup. Never `VACUUM` while a session is running or the market is
open; it takes an exclusive lock.

### 6. One flatten.py brake · medium

*Why:* The README describes it as an existing safety mechanism. It does not exist.
`KillSwitch.flatten_requested` and the FLATTEN file are defined with no callers.

*What:* A `flatten.py` at the repo root importing nothing from `thepit`, opening
the database with stdlib sqlite3, reading open positions for running sessions, and
closing them — or, at minimum, printing the exact positions and manual steps —
then engaging KILL. Enforce the no-import rule with a test that runs it in a
subprocess with `thepit` removed from the path. A brake that imports the thing it
is braking is not a brake.

### 7. Unattended operation · medium

*Why:* Nothing supervises either process. A reboot, a Windows Update restart or a
closed terminal silently ends data collection, and the gap only shows up later as a
hole in the tape.

*What:* A Windows Scheduled Task pair for engine and API, running **as Baron's
user** — a service account has no authenticated `claude` CLI, no
`THEPIT_CONTACT_EMAIL`, and a different `%USERPROFILE%`, so sessions would silently
fall back to the stub and be recorded as baseline arms. Plus rotating log files
(the API has no logging config at all) and an alert when the engine dies, a feed
goes dark, or a session ends holding.

### 8. Make the tick prompt as informed as the plan prompt · medium

*Why:* `session/prompt.py` is described as the highest-leverage file in the repo,
and the tick prompt — the one the model sees five times per session — gets a bare
list of last prices while the plan prompt gets returns, realized volatility and
range. Ambient news reaches the plan and never the tick.

*What:* Render `build_market_block` in the tick prompt; include news published
since the previous tick when research is not OFF, through the same sanitiser; move
`TICK_SCHEMA` out of `runner.py` into `prompt.py` and stamp every agent-facing text
with a version so a prompt change is dated and measurable. Watch the token cost
against the rate window.

### 9. The blinding path, whole · large

*Why:* Blinded arms cannot execute at all. `prompt._label_for` relabels for display
only; the order path never inverts the mapping, so an ANONYMIZED session's orders
can only be rejected, and a MISLABELED session's order executes on the wrong tape.
"Is it reasoning or recalling" is unanswerable until this works.

*What:* Persist the per-session map, invert it in `_place`/`_submit` before the
symbol reaches the risk layer, and apply the forward map to **every** surface the
agent reads — tick prices, positions, armed entries, rejection feedback, news.
Test by grepping the rendered prompt for every real ticker. A partial inversion is
worse than none: the session looks blinded, is not, and its data is silently
contaminated.

### 10. Get the eval out of the terminal · medium

*Why:* The report exists only as printed text. Nothing can chart it, diff two
cohorts, or hand it to anything else.

*What:* `to_dict()` on the report dataclasses plus `eval/serialize.py` (JSON and
CSV), keeping every `None` as null so "withheld because n is too small" survives
export; `GET /api/eval` on the read router; a dashboard cohort view. Version the
metric definitions in the same pass so a formula change is dated. Do not export
prompts or operator notes without an explicit flag.

---

## Large systems, later

Not on the critical path, big enough that they need to be one entry rather than
four.

- [ ] **The replay harness** · large
- [ ] **The dashboard rewrite** · large · partly blocked on Baron

### The replay harness · large

*Why:* Four separate proposals describe pieces of one system, and split up, a
session could build a third of it and call it done. It is the only way to price the
counterfactual of a rejected order, to run alternative baselines over recorded tape
at zero model cost, and to test a strategy change without spending market hours.

*What:* Drive `core.clock.Clock` from recorded time; read only through the
repositories, whose `as_of_ms` cutoff is already the lookahead guard; price
rejected orders against the recorded tape; re-run *recorded* LLM decisions rather
than calling the model.

*Understand before starting:* LLM replay is decision-replay and is **never
bit-exact** — `docs/NOTES.md` settles this. Classical strategies replay exactly;
model sessions replay their recorded outputs. Anyone rebuilding this expecting
determinism has misread the project.

*Blocked by:* the frozen corpus. A replay harness with nothing to replay is
untestable, and building it against synthetic fixtures means discovering on first
real use that the recorded data lacks a field it needs.

### The dashboard rewrite · large · partly blocked on Baron

Issue #12 wants Claude Design foundations and React. Before committing to that,
write the decision doc: the existing single page is 700 lines of plain HTML with
one vendored dependency, and roughly twenty of the backlog's UI entries improve it
without a rewrite. Stage any rewrite behind API parity — every view the new client
needs must exist as a JSON endpoint first — so the port is incremental rather than
a big bang with a half-working dashboard in the middle.

---

## Not doing, and why

Refusals belong in the plan too, or they get re-proposed every few weeks.

- **Enabling short selling.** The flag exists (`Limits.allow_short`) and is off.
  Turning it on with no bid/ask, no borrow cost, no locate and no halt modelling
  produces a P&L that is optimistic in a new and undocumented direction, and the
  reversal branch in `Book.apply` is the least-tested path in the ledger. Instead:
  hard-wire it False with a comment naming the missing pieces, keep the
  `NO_SHORTING` rejection so intent is still recorded, and add the sign-flip guard.
  Revisit only after a real book and a borrow-cost model exist.
- **Running the engine as a Windows service.** A service account has no
  authenticated `claude` CLI, no `THEPIT_CONTACT_EMAIL`, and a different
  `%USERPROFILE%`, so sessions would silently fall back to the stub, SEC would 403,
  and the data directory would move. Scheduled Tasks as Baron's user, or nothing.
- **Live trading, in any form.** Not a scheduling question. See Hard rules.
- **A day-trade counter.** The PDT rule was repealed 2026-06-04 (issue #1). Encode
  broker constraints, not repealed regulations.

---

## The experiment programme

The four questions `docs/NOTES.md` says are answerable, as experiments rather than
hopes. Each needs the critical path above; none should start before the shakedown
cohort exists.

- [ ] **EXP-001** — reasoning or recall
- [ ] **EXP-002** — does news access change decisions
- [ ] **EXP-003** — is conviction calibrated
- [ ] **EXP-004** — does the LLM beat the harness it is steering

| ID | Question | Design | Needs |
|---|---|---|---|
| EXP-001 | Reasoning or recall? | Three arms — real, anonymized, mislabeled — on one tape | Critical path #9 |
| EXP-002 | Does news access change decisions? | Paired OFF vs AMBIENT, same universe, same clock | News-to-decision linkage |
| EXP-003 | Is conviction calibrated? | Conviction versus realised return per episode, ≥20 episodes | Return-based correlation, not dollars |
| EXP-004 | Does the LLM beat the harness? | Paired LLM/stub twins | Critical path #3 |

Standing rules for all of them:

- **Pre-register the primary metric before the cohort runs.** A file the eval reads,
  committed before the first session. Without it, every later choice is a search.
- **One experiment per cohort report.** Scope the report to an experiment id, or
  the arms of different experiments get pooled into a mean of unrelated things.
- **Fix the schedule.** A cohort where the LLM arm ran at 10:00 and the baseline at
  15:30 measures the time of day.
- **Freeze the universe.** A watchlist that drifts mid-cohort makes the arms
  incomparable.
- **Print the minimum detectable effect before committing months.** The observed
  spread already implies dozens of sessions per arm; know the number before
  spending the time.
- **Have a stopping rule and obey it.** "Run twenty and one will look brilliant on
  noise alone" is in NOTES.md for a reason.

---

## The honesty register

Claims in this repo's own documents that are not currently true. Fix the code or
fix the claim; do not leave them.

- The README's architecture block describes `flatten.py` and a `commands` drain that
  do not exist. *(Marked "not built" as of 2026-07-30 — delete this line when they
  exist.)*
- The README claims CI fails the build on an `if live:` branch. There is no CI at
  all: no `.github/` directory.
- `002_trading.sql` says a boot check reconstructs positions from the fill stream
  and refuses to start on a mismatch. No such check exists; `Book.load` has no
  callers.
- `session/config.py:estimated_latency_s` uses 6 seconds per model call. The
  measured figure elsewhere in the docs is 9–40 seconds.
- The repo is public and has **no LICENSE file**.
- `tradectl` help lists commands the docstring does not, and vice versa. Keep one
  source.

---

## Blocked on Baron

Nothing here can be finished by an AI session alone.

- **Issue #11 — Alpaca paper account.** Unlocks a real bid/ask, which turns the
  assumed 1.5bp/side into a measured number and makes the fill model checkable.
  Until then every fill is bars-tier and the cost hurdle is an estimate.
- **Issue #12 — Claude Design foundations.** The dashboard rewrite. The staged
  alternative (improve the existing single page) is in the backlog and is not
  blocked.
- **A LICENSE decision.** Public repo, no license, so nobody — including Baron —
  has stated terms.
- **Whether to run unattended overnight at all**, given the machine is also his
  working PC.

---

---

## The checklist

Tick these. Every line links to its full entry below — why it matters, what to
do, and the evidence that it is real. The ten items of the critical path and the
four experiments have their own boxes above; these are everything else.

**Progress: 0 of 172.** Update the count when you tick one, so a glance at the
top of this file says whether the month moved.

### NOW — 51


*Trading, risk and the session loop*

- [ ] [Reject orders that flip a position's sign, instead of silently opening a naked short](#reject-orders-that-flip-a-positions-sign-instead-of-silently-opening-a-naked-short) · small
- [ ] [Make the fast loop sweep positions that have no active exit plan](#make-the-fast-loop-sweep-positions-that-have-no-active-exit-plan) · small
- [ ] [Stop printing fractional quantities as integers in every prompt and summary](#stop-printing-fractional-quantities-as-integers-in-every-prompt-and-summary) · small
- [ ] [Put position sizing arithmetic in the prompt instead of making the model do it](#put-position-sizing-arithmetic-in-the-prompt-instead-of-making-the-model-do-it) · small
- [ ] [Add an order rate limit and a re-entry cooldown after a stop fires](#add-an-order-rate-limit-and-a-re-entry-cooldown-after-a-stop-fires) · medium

*Measurement and the eval module*

- [ ] [Stop pooling fill tiers after MixedTierError is caught](#stop-pooling-fill-tiers-after-mixedtiererror-is-caught) · small
- [ ] [Mark a reaped session at its last heartbeat, not at the reap time](#mark-a-reaped-session-at-its-last-heartbeat-not-at-the-reap-time) · small
- [ ] [Refuse a mark from a tick that is hours older than the mark instant](#refuse-a-mark-from-a-tick-that-is-hours-older-than-the-mark-instant) · small

*Feeds, storage and the engine*

- [ ] [Reject quotes by the provider's timestamp, not just by when we polled](#reject-quotes-by-the-providers-timestamp-not-just-by-when-we-polled) · small
- [ ] [Drop or flag Yahoo's synthetic zero-volume closing candle](#drop-or-flag-yahoos-synthetic-zero-volume-closing-candle) · small
- [ ] [Make uptime gaps per-source; today one live feed masks another's outage](#make-uptime-gaps-per-source-today-one-live-feed-masks-anothers-outage) · small
- [ ] [Stop counting a cycle where most symbols failed as a healthy cycle](#stop-counting-a-cycle-where-most-symbols-failed-as-a-healthy-cycle) · small
- [ ] [Separate storage failures from feed failures in the poller loops](#separate-storage-failures-from-feed-failures-in-the-poller-loops) · small
- [ ] [Stop storing the in-progress bar as if it were final](#stop-storing-the-in-progress-bar-as-if-it-were-final) · medium
- [ ] [Give the news feed its own poll watermark instead of reusing the last stored filing](#give-the-news-feed-its-own-poll-watermark-instead-of-reusing-the-last-stored-filing) · medium
- [ ] [Derive the fill tier from the quote that priced the fill, not from a session constant](#derive-the-fill-tier-from-the-quote-that-priced-the-fill-not-from-a-session-constant) · medium
- [ ] [Pick one price source per symbol when two feeds are recording at once](#pick-one-price-source-per-symbol-when-two-feeds-are-recording-at-once) · medium

*Operations, packaging and the machine*

- [ ] [Fix setup.ps1 reporting success when uv sync or pytest actually failed](#fix-setupps1-reporting-success-when-uv-sync-or-pytest-actually-failed) · small
- [ ] [Add a LICENSE file -- the repo is public with no license at all](#add-a-license-file----the-repo-is-public-with-no-license-at-all) · small
- [ ] [Add ruff to the dev dependency group so `uv run ruff check` works](#add-ruff-to-the-dev-dependency-group-so-uv-run-ruff-check-works) · small
- [ ] [Create the CI workflow the README already claims exists](#create-the-ci-workflow-the-readme-already-claims-exists) · medium

*The dashboard*

- [ ] [Fix the Chg column: it measures from the oldest bar ever recorded](#fix-the-chg-column-it-measures-from-the-oldest-bar-ever-recorded) · small
- [ ] [Stop the quote Age column freezing when the feed dies](#stop-the-quote-age-column-freezing-when-the-feed-dies) · small
- [ ] [Disable Start on the read-only LAN listener instead of dead-ending it](#disable-start-on-the-read-only-lan-listener-instead-of-dead-ending-it) · small
- [ ] [Fix the footer: read-only overwrites KILL SWITCH ENGAGED, and neither resets](#fix-the-footer-read-only-overwrites-kill-switch-engaged-and-neither-resets) · small
- [ ] [Render fractional quantities to a fixed precision](#render-fractional-quantities-to-a-fixed-precision) · small
- [ ] [Escape model-authored and feed-authored text before it reaches innerHTML](#escape-model-authored-and-feed-authored-text-before-it-reaches-innerhtml) · small
- [ ] [Ship web/ in the wheel, or make its absence loud](#ship-web-in-the-wheel-or-make-its-absence-loud) · small
- [ ] [Show whether a session is scorable, not just its P&L](#show-whether-a-session-is-scorable-not-just-its-pl) · small
- [ ] [Test that control endpoints 404 on the LAN listener](#test-that-control-endpoints-404-on-the-lan-listener) · small
- [ ] [Stop rebuilding the session panel from an HTML string every 4 seconds](#stop-rebuilding-the-session-panel-from-an-html-string-every-4-seconds) · medium
- [ ] [Serve the equity curve and draw it](#serve-the-equity-curve-and-draw-it) · medium
- [ ] [Add a session picker so past sessions are reachable at all](#add-a-session-picker-so-past-sessions-are-reachable-at-all) · medium

*The research programme*

- [ ] [Refuse to substitute the baseline when the model is unavailable](#refuse-to-substitute-the-baseline-when-the-model-is-unavailable) · small
- [ ] [Correlate conviction against return, not dollars](#correlate-conviction-against-return-not-dollars) · small
- [ ] [Scope the cohort report to one experiment](#scope-the-cohort-report-to-one-experiment) · small
- [ ] [Shake the programme down on closed-market tape before spending market hours](#shake-the-programme-down-on-closed-market-tape-before-spending-market-hours) · small
- [ ] [Pre-register every experiment as a file the eval reads](#pre-register-every-experiment-as-a-file-the-eval-reads) · medium
- [ ] [Build decision-level behavioural metrics so the questions are not all gated on P&L](#build-decision-level-behavioural-metrics-so-the-questions-are-not-all-gated-on-pl) · large

*Cross-cutting: security, budget, contracts, hygiene*

- [ ] [Add an Origin/Host guard to the loopback control API](#add-an-originhost-guard-to-the-loopback-control-api) · small
- [ ] [Refuse to start a session that cannot finish before the close](#refuse-to-start-a-session-that-cannot-finish-before-the-close) · small
- [ ] [Record the resolved model identity, not the string "sonnet"](#record-the-resolved-model-identity-not-the-string-sonnet) · small
- [ ] [Count parsed items per fetch, so a 200 that yields nothing is visible](#count-parsed-items-per-fetch-so-a-200-that-yields-nothing-is-visible) · small
- [ ] [Take a single-instance lock in the engine](#take-a-single-instance-lock-in-the-engine) · small
- [ ] [Fix the doc claims that are already false, and add a test that keeps them true](#fix-the-doc-claims-that-are-already-false-and-add-a-test-that-keeps-them-true) · small
- [ ] [Make .claude/launch.json start the engine as well as the API](#make-claudelaunchjson-start-the-engine-as-well-as-the-api) · small
- [ ] [Verify the Windows deployment end to end and close issue #16](#verify-the-windows-deployment-end-to-end-and-close-issue-16) · small
- [ ] [Define a working agreement for ROADMAP.md itself](#define-a-working-agreement-for-roadmapmd-itself) · small
- [ ] [Budget the model rate window, and fail loudly when it runs out](#budget-the-model-rate-window-and-fail-loudly-when-it-runs-out) · medium
- [ ] [Preflight the claude CLI contract and record its version](#preflight-the-claude-cli-contract-and-record-its-version) · medium
- [ ] [Neutralise feed-authored text before it enters a prompt](#neutralise-feed-authored-text-before-it-enters-a-prompt) · medium

### NEXT — 75


*Trading, risk and the session loop*

- [ ] [Add a gross exposure cap to the risk layer](#add-a-gross-exposure-cap-to-the-risk-layer) · small
- [ ] [Stop the orphan reaper from abandoning open positions silently](#stop-the-orphan-reaper-from-abandoning-open-positions-silently) · small
- [ ] [Close the gaps in TICK_SCHEMA's vocabulary](#close-the-gaps-in-tick-schemas-vocabulary) · small
- [ ] [Move the whole agent-facing text into prompt.py and stamp it with a version](#move-the-whole-agent-facing-text-into-promptpy-and-stamp-it-with-a-version) · medium
- [ ] [Count armed entries against the concurrency cap and reserve their capital](#count-armed-entries-against-the-concurrency-cap-and-reserve-their-capital) · medium
- [ ] [Track drawdown across sessions and add a halt that survives a restart](#track-drawdown-across-sessions-and-add-a-halt-that-survives-a-restart) · medium
- [ ] [Extract an Agent protocol so the decision source is injectable](#extract-an-agent-protocol-so-the-decision-source-is-injectable) · medium
- [ ] [Implement the fills-versus-positions boot check the schema says exists](#implement-the-fills-versus-positions-boot-check-the-schema-says-exists) · medium
- [ ] [Bound concurrent sessions and make the writer story true](#bound-concurrent-sessions-and-make-the-writer-story-true) · medium

*Measurement and the eval module*

- [ ] [Make level_fills see pre-005 orders and stop silently dropping unpaired fires](#make-level-fills-see-pre-005-orders-and-stop-silently-dropping-unpaired-fires) · small
- [ ] [Detect unprotected fills against the plan-event history, not the upserted plans table](#detect-unprotected-fills-against-the-plan-event-history-not-the-upserted-plans-table) · small
- [ ] [Stop counting model-issued closing fills as at-market entries](#stop-counting-model-issued-closing-fills-as-at-market-entries) · small
- [ ] [Compute thinking share against the session's real wall clock](#compute-thinking-share-against-the-sessions-real-wall-clock) · small
- [ ] [Say when the cohort was truncated at the row limit](#say-when-the-cohort-was-truncated-at-the-row-limit) · small
- [ ] [Distinguish the three causes of a cancelled armed entry](#distinguish-the-three-causes-of-a-cancelled-armed-entry) · small
- [ ] [Pre-register a primary metric before a cohort runs](#pre-register-a-primary-metric-before-a-cohort-runs) · medium
- [ ] [Carry the risk limits on SessionMeta and refuse to pool arms across them](#carry-the-risk-limits-on-sessionmeta-and-refuse-to-pool-arms-across-them) · medium
- [ ] [Give every session a stable uid that survives a rebuilt database](#give-every-session-a-stable-uid-that-survives-a-rebuilt-database) · medium
- [ ] [Freeze a corpus of recorded sessions and assert the report against it](#freeze-a-corpus-of-recorded-sessions-and-assert-the-report-against-it) · medium

*Feeds, storage and the engine*

- [ ] [Checkpoint the WAL on a timer, not only on a clean shutdown](#checkpoint-the-wal-on-a-timer-not-only-on-a-clean-shutdown) · small
- [ ] [Detect schema drift by hashing applied migrations](#detect-schema-drift-by-hashing-applied-migrations) · small
- [ ] [Check the schema version on the read side before touching tables](#check-the-schema-version-on-the-read-side-before-touching-tables) · small
- [ ] [Assert the market calendar covers the date being traded](#assert-the-market-calendar-covers-the-date-being-traded) · small
- [ ] [Wake the poller at the session boundary instead of sleeping through the open](#wake-the-poller-at-the-session-boundary-instead-of-sleeping-through-the-open) · small
- [ ] [Handle 429 and Retry-After as a distinct condition from a transport failure](#handle-429-and-retry-after-as-a-distinct-condition-from-a-transport-failure) · small
- [ ] [Refresh the EDGAR ticker map and surface unresolved symbols](#refresh-the-edgar-ticker-map-and-surface-unresolved-symbols) · small
- [ ] [Make the raw archive readable, including after a hard kill](#make-the-raw-archive-readable-including-after-a-hard-kill) · small
- [ ] [Test the poller loops, not just the functions they call](#test-the-poller-loops-not-just-the-functions-they-call) · medium
- [ ] [Write unit tests for the two price feeds — there are none](#write-unit-tests-for-the-two-price-feeds-there-are-none) · medium
- [ ] [Detect and backfill holes in the bar series](#detect-and-backfill-holes-in-the-bar-series) · medium

*Operations, packaging and the machine*

- [ ] [Add the CI check that fails the build on a live-trading branch](#add-the-ci-check-that-fails-the-build-on-a-live-trading-branch) · small
- [ ] [Add `tradectl doctor`: one command that says why nothing is working](#add-tradectl-doctor-one-command-that-says-why-nothing-is-working) · small
- [ ] [Pick one version source, tag releases, and stop hardcoding the version in the User-Agent](#pick-one-version-source-tag-releases-and-stop-hardcoding-the-version-in-the-user-agent) · small
- [ ] [Pin the Python interpreter with .python-version -- the README's uv claim is not what happened here](#pin-the-python-interpreter-with-python-version----the-readmes-uv-claim-is-not-what-happened-here) · small
- [ ] [Make the Mac a read-only viewer: fix the dead lan_host config and document the network path](#make-the-mac-a-read-only-viewer-fix-the-dead-lan-host-config-and-document-the-network-path) · small
- [ ] [Make THEPIT_CONTACT_EMAIL survive a service account, and assert it at startup](#make-thepit-contact-email-survive-a-service-account-and-assert-it-at-startup) · small
- [ ] [Drain the `commands` table, or delete it and correct the README](#drain-the-commands-table-or-delete-it-and-correct-the-readme) · medium

*The dashboard*

- [ ] [Stop shipping every model response on the 4-second session poll](#stop-shipping-every-model-response-on-the-4-second-session-poll) · small
- [ ] [Show the rejection histogram instead of a bare count](#show-the-rejection-histogram-instead-of-a-bare-count) · small
- [ ] [Put the kill switch on the dashboard](#put-the-kill-switch-on-the-dashboard) · small
- [ ] [Render the health events and the fills the page already downloads](#render-the-health-events-and-the-fills-the-page-already-downloads) · small
- [ ] [Give every form control an accessible name](#give-every-form-control-an-accessible-name) · small
- [ ] [Make the quote table keyboard-operable](#make-the-quote-table-keyboard-operable) · small
- [ ] [Fix the two contrast failures: console timestamps and the LIVE badge](#fix-the-two-contrast-failures-console-timestamps-and-the-live-badge) · small
- [ ] [Honour prefers-reduced-motion and announce state changes](#honour-prefers-reduced-motion-and-announce-state-changes) · small
- [ ] [Implement the elapsed-time counter the schema promises](#implement-the-elapsed-time-counter-the-schema-promises) · small
- [ ] [Colour the console by the kinds actually emitted](#colour-the-console-by-the-kinds-actually-emitted) · small
- [ ] [Give the WebSocket client a heartbeat watchdog](#give-the-websocket-client-a-heartbeat-watchdog) · small
- [ ] [Extract the design tokens into a documented token block](#extract-the-design-tokens-into-a-documented-token-block) · small
- [ ] [Add a prompt and response viewer over the decisions table](#add-a-prompt-and-response-viewer-over-the-decisions-table) · medium
- [ ] [Expose the exit-plan event history as a timeline](#expose-the-exit-plan-event-history-as-a-timeline) · medium
- [ ] [Build the LLM-versus-baseline comparison view, honest about pairing](#build-the-llm-versus-baseline-comparison-view-honest-about-pairing) · medium
- [ ] [Cover the read endpoints the dashboard depends on](#cover-the-read-endpoints-the-dashboard-depends-on) · medium
- [ ] [Overlay fills, enforced levels and armed triggers on the price chart](#overlay-fills-enforced-levels-and-armed-triggers-on-the-price-chart) · large

*The research programme*

- [ ] [EXP-003: is stated conviction calibrated](#exp-003-is-stated-conviction-calibrated) · small
- [ ] [Freeze and version the trading universe](#freeze-and-version-the-trading-universe) · small
- [ ] [Report the minimum detectable effect before committing months of sessions](#report-the-minimum-detectable-effect-before-committing-months-of-sessions) · small
- [ ] [Record the regime each session ran in](#record-the-regime-each-session-ran-in) · small
- [ ] [EXP-001: reasoning versus recall, three blinding arms on one tape](#exp-001-reasoning-versus-recall-three-blinding-arms-on-one-tape) · medium
- [ ] [Record which news items each decision actually saw](#record-which-news-items-each-decision-actually-saw) · medium
- [ ] [EXP-002: does research access change decisions, paired OFF against AMBIENT](#exp-002-does-research-access-change-decisions-paired-off-against-ambient) · medium
- [ ] [EXP-004: does the LLM beat the harness it is steering](#exp-004-does-the-llm-beat-the-harness-it-is-steering) · medium
- [ ] [Fix the session schedule so time of day is not confounded with arm](#fix-the-session-schedule-so-time-of-day-is-not-confounded-with-arm) · medium
- [ ] [Add a stopping rule and stop reading results early](#add-a-stopping-rule-and-stop-reading-results-early) · medium
- [ ] [Replace the single momentum stub with a baseline suite](#replace-the-single-momentum-stub-with-a-baseline-suite) · large

*Cross-cutting: security, budget, contracts, hygiene*

- [ ] [Exclude sessions whose model output never parsed from the arm means](#exclude-sessions-whose-model-output-never-parsed-from-the-arm-means) · small
- [ ] [Report model spend and token use per session and per cohort](#report-model-spend-and-token-use-per-session-and-per-cohort) · small
- [ ] [Define, document and test what the kill switch does to an open position](#define-document-and-test-what-the-kill-switch-does-to-an-open-position) · small
- [ ] [Write docs/DATA-SOURCES.md: what each endpoint is, and on what terms](#write-docsdata-sourcesmd-what-each-endpoint-is-and-on-what-terms) · small
- [ ] [Assert the system clock at startup and record skew](#assert-the-system-clock-at-startup-and-record-skew) · small
- [ ] [Give the model call a retry and a stated policy for a mid-session outage](#give-the-model-call-a-retry-and-a-stated-policy-for-a-mid-session-outage) · small
- [ ] [Decide what the LAN listener is, and secure or document it accordingly](#decide-what-the-lan-listener-is-and-secure-or-document-it-accordingly) · small
- [ ] [Give session defaults one source of truth](#give-session-defaults-one-source-of-truth) · small
- [ ] [Turn on a real ruff rule set so the noqa codes mean something](#turn-on-a-real-ruff-rule-set-so-the-noqa-codes-mean-something) · small
- [ ] [Alert when the engine dies, a feed goes dark, or a session halts holding](#alert-when-the-engine-dies-a-feed-goes-dark-or-a-session-halts-holding) · medium

### LATER — 38


*Trading, risk and the session loop*

- [ ] [Cover multi-symbol and multi-position behaviour in the fast loop tests](#cover-multi-symbol-and-multi-position-behaviour-in-the-fast-loop-tests) · small
- [ ] [Property-test Book.apply against a replayed ledger](#property-test-bookapply-against-a-replayed-ledger) · small
- [ ] [Write docs/RISK.md and keep the README's "Not built" list honest](#write-docsriskmd-and-keep-the-readmes-not-built-list-honest) · small
- [ ] [Make prompt changes measurable with a variant registry](#make-prompt-changes-measurable-with-a-variant-registry) · medium
- [ ] [Model partial fills](#model-partial-fills) · large

*Measurement and the eval module*

- [ ] [Compute the conviction p-value or delete the field](#compute-the-conviction-p-value-or-delete-the-field) · small
- [ ] [Version the metric definitions so a formula change is dated](#version-the-metric-definitions-so-a-formula-change-is-dated) · small
- [ ] [Compute each session's numbers once per cohort run](#compute-each-sessions-numbers-once-per-cohort-run) · small
- [ ] [Count the metric family and adjust alpha as the metric count grows](#count-the-metric-family-and-adjust-alpha-as-the-metric-count-grows) · medium
- [ ] [Score the intra-session equity curve that is already being recorded](#score-the-intra-session-equity-curve-that-is-already-being-recorded) · medium
- [ ] [Fuzz the episode fold against the book's own realised P&L](#fuzz-the-episode-fold-against-the-books-own-realised-pl) · medium

*Feeds, storage and the engine*

- [ ] [Record and report feed latency, which the schema was built to measure](#record-and-report-feed-latency-which-the-schema-was-built-to-measure) · small
- [ ] [Wire MarketView into something or delete it](#wire-marketview-into-something-or-delete-it) · small
- [ ] [Measure the disagreement between two price sources](#measure-the-disagreement-between-two-price-sources) · medium
- [ ] [Decide what happens to stored bars across a split or dividend](#decide-what-happens-to-stored-bars-across-a-split-or-dividend) · large

*Operations, packaging and the machine*

- [ ] [Ship config.toml.example and .env.example -- both are referenced and neither exists](#ship-configtomlexample-and-envexample----both-are-referenced-and-neither-exists) · small
- [ ] [Correct the README claims that no longer match the repo](#correct-the-readme-claims-that-no-longer-match-the-repo) · small
- [ ] [Merge the fast-loop branch to main and set a branching convention](#merge-the-fast-loop-branch-to-main-and-set-a-branching-convention) · small
- [ ] [Add a reset/uninstall script for the runtime directory](#add-a-resetuninstall-script-for-the-runtime-directory) · small

*The dashboard*

- [ ] [Label the timezone on every rendered timestamp](#label-the-timezone-on-every-rendered-timestamp) · small
- [ ] [Write the decision doc on whether the React rewrite is worth it](#write-the-decision-doc-on-whether-the-react-rewrite-is-worth-it) · small
- [ ] [Record the vendored uPlot licence and pin its provenance](#record-the-vendored-uplot-licence-and-pin-its-provenance) · small
- [ ] [Add security headers and a favicon to the served page](#add-security-headers-and-a-favicon-to-the-served-page) · small
- [ ] [Write the operator runbook for the dashboard](#write-the-operator-runbook-for-the-dashboard) · small
- [ ] [Make the page usable on a phone](#make-the-page-usable-on-a-phone) · medium
- [ ] [Broadcast quotes from one shared task instead of per connection](#broadcast-quotes-from-one-shared-task-instead-of-per-connection) · medium
- [ ] [Add a browser smoke test for the dashboard](#add-a-browser-smoke-test-for-the-dashboard) · medium

*The research programme*

- [ ] [Ablate model tier and effort](#ablate-model-tier-and-effort) · small
- [ ] [Ablate conversation continuity](#ablate-conversation-continuity) · small
- [ ] [Ablate session length](#ablate-session-length) · small
- [ ] [Keep a family-wise register of every comparison run](#keep-a-family-wise-register-of-every-comparison-run) · small
- [ ] [Test whether headline text steers the agent](#test-whether-headline-text-steers-the-agent) · small
- [ ] [Export the cohort for outside analysis](#export-the-cohort-for-outside-analysis) · small
- [ ] [Ablate the load-bearing prompt paragraphs, one at a time](#ablate-the-load-bearing-prompt-paragraphs-one-at-a-time) · medium
- [ ] [Ablate the two clocks: policy tick and fast-loop interval](#ablate-the-two-clocks-policy-tick-and-fast-loop-interval) · medium

*Cross-cutting: security, budget, contracts, hygiene*

- [ ] [Add a type-checking gate](#add-a-type-checking-gate) · small
- [ ] [Keep a decision log for the choices that are only recorded in docstrings](#keep-a-decision-log-for-the-choices-that-are-only-recorded-in-docstrings) · small
- [ ] [Build the hash-chained audit_log the schema promises, or delete the promise](#build-the-hash-chained-audit-log-the-schema-promises-or-delete-the-promise) · medium

### SOMEDAY — 8


*Trading, risk and the session loop*

- [ ] [Support limit orders, or drop the columns that pretend to](#support-limit-orders-or-drop-the-columns-that-pretend-to) · medium
- [ ] [Model borrow cost and locate failure for shorts](#model-borrow-cost-and-locate-failure-for-shorts) · medium
- [ ] [Let a session resume after the process driving it restarts](#let-a-session-resume-after-the-process-driving-it-restarts) · large

*Measurement and the eval module*

- [ ] [Add a bootstrap interval on the arm means](#add-a-bootstrap-interval-on-the-arm-means) · medium
- [ ] [Make it safe to look at the number while sessions accrue](#make-it-safe-to-look-at-the-number-while-sessions-accrue) · medium

*Operations, packaging and the machine*

- [ ] [Let the Mac run eval against a copy of the database](#let-the-mac-run-eval-against-a-copy-of-the-database) · medium

*The dashboard*

- [ ] [Stage the rewrite behind API parity rather than a big-bang port](#stage-the-rewrite-behind-api-parity-rather-than-a-big-bang-port) · large

*The research programme*

- [ ] [Measure whether concurrent agents are actually independent](#measure-whether-concurrent-agents-are-actually-independent) · medium

---

## The backlog

Everything else, by priority. `NOW` means it makes other work possible or something
currently reports a number that is not true. `NEXT` is real work with a clear
owner-shaped hole. `LATER` is worth doing and can wait. `SOMEDAY` is a good idea
with no forcing function.

Entries merged into the critical path above have been removed from here, so this
list and that one do not overlap.

---

# NOW


## Trading, risk and the session loop

### Reject orders that flip a position's sign, instead of silently opening a naked short
*small* · [back to checklist](#the-checklist)

**Why.** A sell larger than the long you hold is classified as "reducing", which bypasses every opening check. Shorting is disabled and it still creates a short; no stop is required; the old long exit plan stays active with long=1, so the fast loop then tries to close the short by SELLING more, gets rejected every 5s as 'past the point where new positions may be opened', and writes a rejection row each interval. The position is unwatched until the end-of-session flatten. On a $20 account every quantity is fractional, so a sell of 0.018 against a held 0.0178042 hits this by rounding, not by model error.

**What.** 1) In `trading/book.py::check`, before the `if not reducing:` block (currently line 328), add a sign-flip guard: `if current_qty != 0 and projected != 0 and (projected > 0) != (current_qty > 0): return Verdict(False, Reject.REVERSAL)`. Add `REVERSAL = "an order may not flip a position's direction; close it first"` to the `Reject` StrEnum. 2) In `session/runner.py::_opens_exposure` (line 570), return True when the sign flips, so `_place` demands a stop for the flipped leg rather than routing it to `_submit(can_open=False)`. 3) In `session/fastloop.py::step` (line 326), skip and close any plan whose `long` disagrees with the sign of the held qty, and log it via `_say('error', ...)` — a plan enforcing the wrong direction must never submit an order. 4) Tests in `tests/test_book.py` (`test_an_oversized_sell_cannot_reverse_a_long`) and `tests/test_fastloop.py` (assert the position stays +5 and one rejection carries the reversal reason).

**Evidence.** src/thepit/trading/book.py:326 `reducing = abs(projected) < abs(current_qty)` skips the `projected < 0 and not limits.allow_short` check at book.py:338. Reproduced against the real runner: buy 5 AAPL then sell 8 leaves `positions.qty = -3.0` with zero rejections, `exit_plans` still `long=True`, and `fast.step()` returning `['stop-blocked:AAPL']` on every subsequent pass.

**Risk.** Over-broad guard breaks legitimate de-risking. It must reject only sign flips, never an exact close (`projected == 0`) and never a partial reduction; `tests/test_book.py::test_reducing_is_allowed_while_halted` and `test_reducing_is_allowed_past_the_clock_and_the_loss_limit` must still pass unchanged.

### Make the fast loop sweep positions that have no active exit plan
*small* · [back to checklist](#the-checklist)

**Why.** `FastLoop.step` only iterates `self.plans()` and `self.armed()`. A position with no active plan row is invisible forever — nothing re-checks it, nothing closes it. `protect()` already documents the state it can produce: levels fail to resolve, the emergency unwind is also rejected, and the code merely says "close it by hand". Nothing after that ever looks again.

**What.** In `session/fastloop.py::step`, after the plans loop, diff `self._positions()` against `{p.symbol for p in self.plans()}`. For every symbol with `abs(qty) > 1e-9` and no active plan: emit an `unwatched:<symbol>` entry in the returned list, `_say('error', ...)` once per symbol (track in a `self._unwatched: set[str]` the same way `self._stale` is tracked), and re-submit the closing order via `self._submit({...}, can_open=False, origin='unprotected')`. Record an `exit_plan_events` row with a new kind `'unwatched'` (extend the CHECK constraint in a new `007_*.sql` migration — SQLite cannot ALTER a CHECK, so add the kind by recreating `exit_plan_events` or drop the CHECK there). Test in `tests/test_fastloop.py`: delete the plan row out from under a live position and assert the next `step()` closes it.

**Evidence.** src/thepit/session/fastloop.py:326 `for plan in self.plans():` is the only position iteration in `step()`; src/thepit/session/fastloop.py:470-472 `"{symbol} could not be closed either. It is open with no exit plan — close it by hand."`

**Risk.** An aggressive sweep could fight the model: a position the model intends to hold but whose plan was just closed by `_close_plan` on float dust would be liquidated. Gate on `abs(qty) > 1e-9` using the same DUST constant `eval/pnl.py:DUST` uses, and only sweep after one full interval has passed since the position last had a plan.

### Stop printing fractional quantities as integers in every prompt and summary
*small* · [back to checklist](#the-checklist)

**Why.** Three format strings render position and fill quantities with `:.0f`. At $20 capital in a $200 stock the position is ~0.02 shares, so the tick prompt tells the model it holds "+0" of a symbol it is actually long, the rejection feedback block says it tried to buy "0", and the end-of-session summary the review reasons over lists every fill as qty 0. The whole fractional-shares fix (commit 10545bd) is invisible to the agent.

**What.** In `session/runner.py`: line 799 `f"- {sym}: {pos.qty:+.0f} @ ..."` → `{pos.qty:+.6g}`; line 850 `f"- {r['side']} {r['qty']:.0f} {r['symbol']}..."` → `{r['qty']:.6g}`; line 961 in `_summary` → `{f['qty']:.6g}`. Also add the notional next to the quantity on line 799 (`≈$X at cost`), since a bare 0.0178 is hard to reason about. Add a test in `tests/test_session.py` or `tests/test_fastloop.py` that buys a fractional quantity and asserts the substring `"0"` alone does not appear as the position size — assert the actual decimal string is present.

**Evidence.** src/thepit/session/runner.py:799, src/thepit/session/runner.py:850, src/thepit/session/runner.py:961 — all use `:.0f`/`:+.0f` on REAL quantity columns. CLAUDE-READ-THIS.md: "Whole-share sizing does not work. At $20 with a $4 cap, `int(4 / 194)` is 0."

**Risk.** `%g` drops trailing zeros and can switch to scientific notation on very small numbers; pick the format so 0.0000178 renders readably rather than as `1.78e-05`, which a model will misread by orders of magnitude.

### Put position sizing arithmetic in the prompt instead of making the model do it
*small* · [back to checklist](#the-checklist)

**Why.** The prompt states the cap as a percentage and a dollar figure and then leaves the model to divide by a price it was given elsewhere. The most common recorded failure modes on this account are both sizing failures: whole-share orders that round to zero, and a correct call that earned one cent because two thirds of the capital sat idle. The model is never shown the maximum quantity it may buy of each symbol, and never shown what a stated stop distance costs in dollars.

**What.** In `session/prompt.py`, add a `## Sizing` block to both `build_plan_prompt` and the tick prompt: per symbol, `max_qty = (equity * max_position_pct/100) / last` rendered to 6 significant figures, plus `cash_available`. Add the risk identity in one line: `dollars at risk = qty x (entry - stop)`, worked through for the max quantity at a 30bp stop, so the number is concrete rather than a formula. Add `risk_budget_pct` to `SessionConfig` (default 100% under `risk_it`, i.e. non-binding) and state it. Move `TICK_SCHEMA`'s `"qty" may be fractional` note next to this block. Test in `tests/test_session.py` that a $20 capital / $194 stock prompt contains a max qty with a leading `0.`, never `0` alone.

**Evidence.** src/thepit/session/prompt.py:203-212 renders `Maximum position: 20% ... = $4.00 per symbol` and "Fractional shares are allowed" but no per-symbol quantity; src/thepit/session/config.py:44-49 records the agent's own review: "Reserve capital on an 8-minute session is just money left on the table."

**Risk.** A stated max qty computed off a stale quote will be rejected by the position cap when the price moves, teaching the model that the prompt lies. Quote it with the age of the price it was derived from, and leave a stated safety margin (the risk layer already applies a 1.01 cash buffer at book.py:357).

### Add an order rate limit and a re-entry cooldown after a stop fires
*medium* · [back to checklist](#the-checklist)

**Why.** README lists an order rate limit under "Not built". Nothing bounds order flow: a model that returns the same order every tick, or a fast loop retrying a blocked close every 5 seconds, writes unbounded `orders` rows. Worse, nothing stops the model re-entering a symbol in the same minute the fast loop just stopped it out — that is the exact churn the cost line in the prompt exists to prevent, and it is currently unenforced.

**What.** 1) Add `max_orders_per_minute: int = 6` and `reentry_cooldown_s: float = 60.0` to `session/config.SessionConfig`, validated in `validate()`. 2) Add `RATE_LIMIT = "too many orders in this window"` and `COOLDOWN = "stopped out of this symbol too recently"` to `trading/book.Reject`, and two new fields on `Limits`. 3) Extend `trading/book.check` with two counted arguments (`recent_order_count: int`, `seconds_since_stop: float | None`) so it stays pure — the caller counts. 4) In `session/runner.py::_submit`, count `SELECT COUNT(*) FROM orders WHERE session_id=? AND ts_ms > ?` and read the last `fast_loop_stop` fill time for the symbol from `exit_plan_events`. 5) Exempt `origin IN ('flatten','fast_loop_*','unprotected')` from the rate limit — a brake must never be rate limited. 6) Tests in `tests/test_book.py` for both rejections and for the brake exemption.

**Evidence.** README.md:101 "Not built: an order rate limit, a gross exposure cap, a flow-adjusted drawdown index across sessions, and a permanent halt that survives a restart."; src/thepit/session/runner.py:699-706 already notes that hammering writes a rejection row per attempt and that rejections are supposed to mean something.

**Risk.** Rate-limiting the wrong origin makes a position unclosable, which is strictly worse than churn. The exemption list must be tested explicitly, and the cooldown must apply only to opening orders on that symbol.


## Measurement and the eval module

### Stop pooling fill tiers after MixedTierError is caught
*small* · [back to checklist](#the-checklist)

**Why.** `cohort.meta` excludes a session whose own fills mix tiers, but two sessions each internally consistent at *different* tiers both land in `usable`. `cohort_report` catches the raise, sets `tier = "MIXED"`, appends a note, and then computes `by_arm`, `difference_bp`, `permutation_p`, `_level_slippage` and `_lateness` over all of them anyway. NOTES.md says a bar-derived and a quote-derived run "can never be averaged into one number"; the code averages them and prints a sentence about it.

**What.** In `report.cohort_report` (report.py:181-187), on `MixedTierError` partition `usable` by `SessionMeta.tiers`, compute the whole report against the largest tier only, and return the rest as `CohortReport.other_tiers: dict[str, int]` mapping tier to sessions set aside. Change `tier = "MIXED"` to the tier actually used. Update the header at tradectl.py:229-231 to print which tier produced the numbers and how many sessions were held back. Test: build two single-tier sessions at `bars` and `quotes`, assert `arms[Arm.LLM].n` counts one, not two.

**Evidence.** src/thepit/eval/report.py:181-187 catches and continues; report.py:189-193 then iterates all of `usable`; src/thepit/eval/cohort.py:183-195 `require_single_tier`; docs/NOTES.md:41-42.

**Risk.** Silently dropping the minority tier is a new way to shrink a denominator without saying so — the count must be printed, and if the two tiers are near-equal in size the honest output is two reports, not one.

### Mark a reaped session at its last heartbeat, not at the reap time
*small* · [back to checklist](#the-checklist)

**Why.** `_reap_orphans` sets `finished_ms = now_ms()` on every orphan, and `pnl.mark_instant` prefers `finished_ms` over `heartbeat_ms`. A session whose process died on Friday and is reaped when the API next starts on Monday has its open positions marked at Monday's tape. That is precisely the "scoring last Tuesday's session against this morning's tape invents P&L out of the weekend" failure the module docstring says it prevents, reintroduced by the reaper.

**What.** Change the orphan UPDATE at api/main.py:104-108 to set `finished_ms=COALESCE(heartbeat_ms, created_ms)` rather than `now`, keeping `halt_reason` as it is. Independently harden `pnl.mark_instant` (pnl.py:76-89): when `sessions.halt_reason` starts with 'interrupted', prefer `heartbeat_ms` over `finished_ms`. Both, because the API fix does not repair rows already written. Add a test: a session with `heartbeat_ms = NOW`, `finished_ms = NOW + 86_400_000`, one tick at each instant, asserting `marks[symbol]` is the NOW price — the mirror of `test_a_finished_session_is_marked_at_its_own_clock` at tests/test_eval.py:108.

**Evidence.** src/thepit/api/main.py:104-108 `"...finished_ms=? WHERE id IN (...)", (now, *dead)`; src/thepit/eval/pnl.py:83 `for key in ("finished_ms", "heartbeat_ms", "ends_ms")`; src/thepit/eval/pnl.py:19-22.

**Risk.** `heartbeat_ms` can be NULL for a session reaped from 'planned' (api/main.py:97-98), so the COALESCE chain must terminate at `created_ms` or the row loses its mark instant entirely and every held symbol becomes unmarkable.

### Refuse a mark from a tick that is hours older than the mark instant
*small* · [back to checklist](#the-checklist)

**Why.** `mark_at` takes the newest tick at or before `at_ms` with no lower bound. A held symbol whose feed died two days before the session ended is still marked, and marked at a two-day-old price; only a symbol with no tick anywhere in history reaches `unmarkable`. The module's docstring says it "refuses to guess" — here it guesses silently, and the guess feeds `pnl`, `equity`, `scorable` and every cohort mean downstream.

**What.** Add `MAX_MARK_AGE_S = 900.0` beside `CASH_TOLERANCE` (pnl.py:41). Give `mark_at` a `max_age_s` parameter and return `None` when `at_ms - row_ts_ms` exceeds it. In `session_pnl` (pnl.py:151-157) route those symbols into a new `SessionPnL.stale_marks: tuple[str, ...]` field, distinct from `unmarkable`, and include it in the `scorable` property. Add a `STALE_MARK` exclusion constant to cohort.py:46-53 and append it in `cohort.meta` (cohort.py:127-134), so the exclusion table distinguishes "no price at all" from "a price too old to use". The threshold should be consistent with `enforcement.MAX_QUOTE_AGE_S` reasoning but is deliberately looser, since a mark is not an execution.

**Evidence.** src/thepit/eval/pnl.py:92-102 `mark_at`; src/thepit/eval/pnl.py:24-28: "It refuses to guess. A held symbol with no tick at or before the mark instant is returned in `unmarkable`."

**Risk.** Set too tight and every session that ran into a feed gap becomes unscorable, which shrinks an already small n. Print the count of newly-excluded sessions when the threshold is introduced and pick the number from the observed tick-gap distribution (`FetchLogRepo.gaps`), not from taste.


## Feeds, storage and the engine

### Reject quotes by the provider's timestamp, not just by when we polled
*small* · [back to checklist](#the-checklist)

**Why.** The staleness guard measures how recently the engine polled, not how old the price is. A feed that keeps answering in 200ms with a frozen price passes the 120s check forever, and the first 120s of any session started after an overnight or outage gap can price orders off a hours-old close while the risk layer reports a fresh quote.

**What.** Add `max_quote_lag_s: float = 30.0` to `Limits` in src/thepit/trading/book.py:69-76. In `check()` at src/thepit/trading/book.py:315, keep the existing `received_ms` test and add a second test on `now_ms - quote.ts_ms` with its own reject reason (`Reject.LAGGED_QUOTE`), so "we stopped polling" and "the venue stopped printing" are distinct rejections. Mirror the same test in `session/fastloop.py:383` where `_stale` is maintained, and in `session/runner.py:741`. Exempt the check when `calendar.state_at()` is CLOSED so overnight sessions do not spam rejections — or, better, refuse to open the session at all. Add tests in tests/test_book.py covering: fresh receipt of an old print (must reject), fresh receipt of a fresh print (must pass), and a symbol whose provider timestamp is exactly at the boundary.

**Evidence.** src/thepit/trading/book.py:315 `age_s = (now_ms - quote.received_ms) / 1000` is the only staleness test; `Quote.age_ms` in src/thepit/core/types.py:91 exists for exactly this and is called nowhere. Live DB row: AAPL ts_ms=1785355201000 (2026-07-29 20:00:01Z), received_ms=1785381970539 (2026-07-30 03:26:10Z) — a 7.4-hour-old price that scores age_s≈0.

**Risk.** Setting max_quote_lag_s too tight will reject every order on thinly traded symbols, whose last print is legitimately minutes old — the threshold must be per-symbol-liquidity-tolerant or generous (30-60s), and the rejection reason must name the lag so a session that sits out is diagnosable rather than mysterious.

### Drop or flag Yahoo's synthetic zero-volume closing candle
*small* · [back to checklist](#the-checklist)

**Why.** Yahoo appends a 16:00 ET bar with open=high=low=close and volume 0. The parser only skips nulls, so this fabricated flat candle is stored as a real one at the end of every trading day — precisely the "fake structure into every indicator downstream" the parser's own comment says it exists to prevent. The 5-minute momentum baseline reads the last six 1m bars, so on any session near the close its newest input is a fiction.

**What.** In `YahooChartFeed._parse_bars` (src/thepit/feeds/yahoo.py:135-155), after the null check, skip bars where `v == 0 and o == h == l == c`. Record the count of skipped rows on the `FetchRecord` (or emit an `events` row via the poller) so silent suppression is itself visible. Add tests/test_yahoo.py with a captured chart payload fixture (tests/fixtures/yahoo_chart_1m.json) asserting the closing candle is dropped and that a genuine low-volume-but-not-flat bar is kept.

**Evidence.** src/thepit/feeds/yahoo.py:148 "Those are absences, not zero-volume bars, and inventing a flat candle would put fake structure into every indicator downstream". Live DB: AAPL ts_ms=1785355200000, o=h=l=c=338.19, v=0.0, alongside 19:59 with v=2,284,490.

**Risk.** A genuine one-tick minute is also o=h=l=c but has non-zero volume; keying the filter on volume alone would delete real data, so both conditions must hold. Existing rows already in `bars` are not cleaned by this change — either accept the contamination in historical data or write a one-off pass and say which.

### Make uptime gaps per-source; today one live feed masks another's outage
*small* · [back to checklist](#the-checklist)

**Why.** `FetchLogRepo.gaps` selects every successful fetch regardless of source, so EDGAR succeeding every ten minutes hides a completely dead price feed. That query is what `tradectl uptime` and the dashboard both present as the answer to "did it stay up", and its own docstring claims exactly that.

**What.** Add a `source: str | None = None` parameter to `FetchLogRepo.gaps` (src/thepit/store/repos.py:244) and push it into the WHERE clause. Change the two call sites — src/thepit/cli/tradectl.py:409 and src/thepit/api/main.py:220 — to loop over the distinct sources in the window and report gaps per source, keyed by source in the JSON payload. Optionally extend to `kind` so a dead quote loop is distinguishable from a dead bars loop on the same source. Add tests in tests/test_repos.py: a window where yahoo is silent but edgar succeeds must report a yahoo gap.

**Evidence.** src/thepit/store/repos.py:252 `SELECT ts_ms FROM fetch_log WHERE ok=1 AND ts_ms BETWEEN ? AND ?` — no source predicate, under the docstring at line 246 "This is the number that actually answers 'did it stay up for 24 hours'".

**Risk.** The dashboard and CLI both render this; changing the return shape from a flat list of tuples to a per-source mapping breaks both call sites at once. Change the signature and all three consumers in one commit.

### Stop counting a cycle where most symbols failed as a healthy cycle
*small* · [back to checklist](#the-checklist)

**Why.** `_note_result` marks the price feed healthy if *any* fetch in the cycle succeeded. Yahoo issues one request per symbol, so eleven of twelve symbols 429ing while one succeeds resets `consecutive_failures` to zero, clears the degraded flag, emits a spurious `feed_recovered`, and leaves the watchlist mostly blind with the dashboard green.

**What.** In `Poller._ingest_quotes` (src/thepit/engine/poller.py:282-293), compute a success ratio over `records` and treat the cycle as a failure below a configurable `min_cycle_success_ratio` (add to `PollerConfig`, default 0.5). Emit a distinct `feed_partial` event carrying the failing symbols so a single sick ticker is visible without degrading the whole feed. Separately, track per-symbol consecutive failures in `FeedHealth` (a `dict[str, int]`) and expose it on `/api/status` so a permanently unresolvable symbol surfaces. Tests in tests/test_poller.py alongside `test_feed_degrades_only_after_repeated_failure`.

**Evidence.** src/thepit/engine/poller.py:291-293 `self._note_result(self._price.name, ok=bool(records) and any(r.ok for r in records))`, against `YahooChartFeed.quotes` at src/thepit/feeds/yahoo.py:97-133 which issues one request per symbol.

**Risk.** Alpaca batches into one request, so the ratio is trivially 0.0 or 1.0 there and the threshold does nothing — correct, but do not let that make the code look broken when the feed is switched.

### Separate storage failures from feed failures in the poller loops
*small* · [back to checklist](#the-checklist)

**Why.** Each loop wraps the network call and the database write in one `try`, so a `sqlite3.OperationalError` (lock timeout, disk full, schema drift) is recorded as a feed outage. Worse, the `fetch_log` rows for that cycle are inside the transaction that just rolled back — so a database problem erases the evidence that the fetch happened at all, which is precisely the ambiguity `fetch_log` exists to remove.

**What.** Split the bodies of `_quote_loop` (src/thepit/engine/poller.py:191-209), `_bar_loop` (211-225) and `_news_loop` (251-278) into a fetch phase and a persist phase with separate `except` blocks. On a persist failure emit an `events` row with kind `store_failed` and do not touch `FeedHealth`. Add a `store_failures` counter to the poller exposed on `/api/status` and printed by `tradectl status`. Consider retrying the persist once before giving up, since `BUSY_TIMEOUT_MS` is only 5s. Test with a fixture connection whose `executemany` raises.

**Evidence.** src/thepit/engine/poller.py:198-202 wraps `await self._price.quotes(...)` and `self._ingest_quotes(...)` in one try that ends in `self._note_failure(self._price.name, ...)`; the `fetch_log.record` calls live inside `db.immediate` at src/thepit/engine/poller.py:285-289.

**Risk.** Writing fetch_log outside the tick transaction to save it from a rollback introduces the opposite failure — a logged fetch whose ticks were never stored. Prefer keeping one transaction and reporting the storage failure distinctly rather than splitting the write.

### Stop storing the in-progress bar as if it were final
*medium* · [back to checklist](#the-checklist)

**Why.** Bars are fetched every 300s with `interval=1m`, so the last candle in every response is the minute currently forming. `ON CONFLICT DO NOTHING` keeps that truncated version forever and discards the completed one, so roughly one bar in five in the primary price series has a clipped high, low, close and volume. Every indicator, the momentum baseline and the bar table in the prompt are built on that series.

**What.** Add migration `src/thepit/store/schema/007_bar_finality.sql` adding `final INTEGER NOT NULL DEFAULT 1` and `revised_ms INTEGER` to `bars`. In `BarsRepo.upsert_many` (src/thepit/store/repos.py:42-63) take a `now_ms` and mark any bar whose `ts_ms + timeframe_ms > now_ms` as `final=0`; change the conflict clause to `ON CONFLICT DO UPDATE ... WHERE bars.final = 0` so a non-final bar is replaced by its completed version and a final bar is still never rewritten. Set `revised_ms` on each update so the revision is measurable. Filter `final=0` out of `BarsRepo.latest()` by default with an explicit `include_forming: bool = False` argument. Tests in tests/test_repos.py: a forming bar is overwritten by its final version; a final bar is not; `latest()` omits the forming bar unless asked.

**Evidence.** src/thepit/store/repos.py:50-51 "Providers do revise recent bars, but silently rewriting history under a running strategy is worse than a stale final candle" — the docstring argues for the current behaviour without distinguishing a revision from an incomplete bar. src/thepit/engine/poller.py:48 `bar_interval_s: float = 300.0` against `bar_timeframe: str = "1m"` at line 49.

**Risk.** Making bars mutable weakens the append-only guarantee the eval module leans on. The `WHERE bars.final = 0` predicate is what keeps that bounded — without it this becomes exactly the silent history rewrite the current docstring warns against. Any replay harness must read `final=1` only.

### Give the news feed its own poll watermark instead of reusing the last stored filing
*medium* · [back to checklist](#the-checklist)

**Why.** The EDGAR watermark is `MAX(published_ms)` over stored watchlist filings. For a 12-symbol watchlist that is usually days old, while the firehose page only spans ~54 minutes — so `oldest_ms > since_ms` is true on nearly every poll and the saturation warning fires permanently. A warning that is always on is a warning nobody reads, and the real saturation event it was built to catch becomes invisible.

**What.** Add a `feed_state` table in a new migration (`src/thepit/store/schema/007_feed_state.sql`): `source TEXT PRIMARY KEY, last_poll_ms INTEGER, oldest_seen_ms INTEGER, newest_seen_ms INTEGER`. Write it in `Poller._news_loop` (src/thepit/engine/poller.py:251-278) inside the existing `db.immediate` block, from the values `EdgarNewsFeed.poll` already computes. Pass `since_ms = feed_state.newest_seen_ms` rather than `NewsRepo.latest_published_ms`, and move the saturation test in src/thepit/feeds/edgar.py:192 to compare the new page's `oldest_ms` against the previous poll's `newest_seen_ms`. Keep `NewsRepo.latest_published_ms` — it is still the right thing for a cold start. Test in a new tests/test_poller_news.py: two consecutive polls of the same fixture emit no warning; a poll whose page is entirely newer than the previous page's newest entry does.

**Evidence.** src/thepit/engine/poller.py:259 `since = self._news_repo.latest_published_ms(self._news.name)` feeding src/thepit/feeds/edgar.py:192 `if oldest_ms is not None and oldest_ms > since_ms > 0:`. Measured page spans documented at src/thepit/feeds/edgar.py:56-63: form 4 ≈ 54 minutes.

**Risk.** Moving off the stored-filing watermark means a crash between the page fetch and the state write can advance the watermark past filings that were never stored. Write `feed_state` in the same transaction as the `news` inserts (the code already opens one) so the two cannot diverge.

### Derive the fill tier from the quote that priced the fill, not from a session constant
*medium* · [back to checklist](#the-checklist)

**Why.** `FeedTier.BARS` is hardcoded where the session runner is constructed, so every fill is stamped `bars` no matter which feed is live. The moment Alpaca is connected, quote-derived fills get labelled bars-derived, `round_trip_cost_bp` keeps quoting the assumed spread instead of the real one, and the mixed-tier exclusion in the eval module — the guard that exists to stop bars-tier and quotes-tier runs being averaged — can never fire because the value is constant.

**What.** Delete `tier=FeedTier.BARS` at src/thepit/api/main.py:472 and instead resolve the tier per fill inside `simulate_fill` (src/thepit/trading/book.py:232-263): tier is QUOTES when `quote.has_book`, BARS otherwise, and the `Fill.tier` written to `fills.sim_tier` follows that. Add a `source` column to `fills` in a migration so the feed is recorded alongside the tier. Replace `runner._round_trip_cost` (src/thepit/session/runner.py:747-752) with a per-symbol cost so a watchlist where only some symbols have a book does not claim quote-tier costs across the board. Assert in tests/test_book.py that a bookless quote and a booked quote in the same session produce two distinct `sim_tier` values, and in tests/test_eval.py that `cohort.meta` then reports MIXED_TIER.

**Evidence.** src/thepit/api/main.py:472 `quotes=quotes, tier=FeedTier.BARS,`; src/thepit/session/runner.py:747 `has_book = any(q.has_book for q in self._quotes.values())`; src/thepit/eval/cohort.py:137 `if len(tiers) > 1: excluded.append(MIXED_TIER)`.

**Risk.** This makes mixed-tier sessions common rather than impossible, which will start excluding sessions from the eval cohort that used to be counted. That is the correct outcome and it will look like a regression in the scoreboard — say so in the commit message, and consider whether a session should refuse to start when its symbols span both tiers.

### Pick one price source per symbol when two feeds are recording at once
*medium* · [back to checklist](#the-checklist)

**Why.** Every read of the latest price takes `MAX(ts_ms)` across all sources with no tie-break or source preference. Once Alpaca and Yahoo are both writing to `ticks` — which is what a mid-day restart to add credentials produces — the quote handed to the fill engine can flip between a booked Alpaca quote and a bookless Yahoo quote depending on whose provider timestamp happens to be newer. That is the half-bars-tier, half-quotes-tier failure, and it is silent: the two rows have different `bid` nullability and produce different fill prices from the same instant.

**What.** Add a `preferred_source` to `Config` (src/thepit/config.py) resolved at startup from which feed the engine actually selected in src/thepit/engine/main.py:83-92, and persist it to `meta` so readers in other processes can see it. Change `_quotes_for` (src/thepit/api/main.py:513-524), `_latest_quotes` (src/thepit/api/main.py:597-604) and `SessionsRepo.marks` (src/thepit/store/repos.py:280-287) to filter on that source, falling back only when the preferred source has no row in the window. Note that the `MAX(ts_ms)` JOIN in both API queries also returns duplicate rows per symbol when two sources share a timestamp — the source filter fixes that too. Record the chosen source on `sessions` in a migration so a session's tape is reconstructable. Tests: seed `ticks` with both sources at interleaved timestamps and assert a single deterministic source is used throughout.

**Evidence.** src/thepit/store/repos.py:284-286 `SELECT t.symbol, t.last FROM ticks t JOIN (SELECT symbol s, MAX(ts_ms) m FROM ticks GROUP BY symbol) x ON x.s = t.symbol AND x.m = t.ts_ms` — no source predicate; src/thepit/api/main.py:517 `SELECT * FROM ticks WHERE symbol=? ORDER BY ts_ms DESC LIMIT 1`. `ticks` PK includes `source` (src/thepit/store/schema/001_init.sql:77), so both rows coexist by design.

**Risk.** Hard-preferring one source means a gap in that source now reads as no data at all rather than silently falling back. That is the honest behaviour, but the fallback path must exist and must be logged, or a brief Alpaca outage will look like a dead market.


## Operations, packaging and the machine

### Fix setup.ps1 reporting success when uv sync or pytest actually failed
*small* · [back to checklist](#the-checklist)

**Why.** `setup.ps1` sets `$ErrorActionPreference = "Stop"`, but in Windows PowerShell that has no effect on native executables -- a non-zero exit from `uv` sets `$LASTEXITCODE` and does not throw. So a failed dependency sync prints `OK dependencies ready` and a failing test suite prints `OK tests pass`, and the script proceeds to tell the operator everything is ready. This is the first thing a fresh machine or a fresh session runs, and it currently cannot fail.

**What.** In `setup.ps1`, after each native command (`uv sync` at line 50, `uv run pytest -q` at line 55) check `if ($LASTEXITCODE -ne 0) { Bad "..."; exit 1 }`. Add a small `function Run($label, $cmd, $argsArray)` helper that invokes, checks `$LASTEXITCODE`, and calls `Bad`/`exit 1` on failure, then route every native call through it. Also capture and print the failing command's output rather than swallowing it. Add a test-free fast path (`-SkipTests` switch) so re-running setup on a machine with a known-red suite is a deliberate choice rather than an accident.

**Evidence.** setup.ps1:12 `$ErrorActionPreference = "Stop"`; setup.ps1:50-51 `uv sync` then unconditional `Ok "dependencies ready"`; setup.ps1:55-56 `uv run pytest -q` then unconditional `Ok "tests pass"`

**Risk.** None material. Take care that `$LASTEXITCODE` is read immediately after the native call -- any intervening cmdlet clobbers it.

### Add a LICENSE file -- the repo is public with no license at all
*small* · [back to checklist](#the-checklist)

**Why.** `README.md` ends with "## License" / "TBD." and there is no `LICENSE` file in the tree. The repo is public (`CLAUDE-READ-THIS.md` states this explicitly and history was rewritten to keep it safe). With no license, the default is all-rights-reserved: nobody can legally copy, run or contribute, and the vendored `web/vendor/uPlot.*` files sit under a third-party license with nothing stating how they coexist with the rest.

**What.** Add a top-level `LICENSE` file. Update `README.md:215-217` to name it instead of "TBD". Add `license = { file = "LICENSE" }` and a `classifiers` entry to `[project]` in `pyproject.toml`. Add `web/vendor/README.md` (already referenced by `.gitignore:37`) noting uPlot's own license and that the vendored files retain it. Baron picks the license; this entry is the plumbing, not the choice.

**Evidence.** README.md:215-217 "## License\n\nTBD."; `ls LICENSE*` returns no such file; CLAUDE-READ-THIS.md:153 "The repo is PUBLIC"; .gitignore:37 references `web/vendor/README.md`, which does not exist

**Blocked by.** Baron choosing a license (MIT, Apache-2.0, or explicitly proprietary)

**Risk.** Picking a license without asking. Do not guess -- state the tradeoff and let him choose.

### Add ruff to the dev dependency group so `uv run ruff check` works
*small* · [back to checklist](#the-checklist)

**Why.** `pyproject.toml` configures ruff (`line-length = 100`, `target-version = "py312"`) and source files carry `# noqa: BLE001` / `# noqa: S104` suppressions written for it, but ruff is not in `[dependency-groups] dev`, which holds only pytest and pytest-asyncio. `uv run ruff check` fails on a clean checkout. A `.ruff_cache/` exists locally, so someone ran it through `uvx` -- meaning the version used is whatever was latest that day, unpinned and unrecorded in `uv.lock`.

**What.** Add `"ruff>=0.8"` to `[dependency-groups] dev` in `pyproject.toml:29-32`. Run `uv lock` and commit the updated `uv.lock`. Add `[tool.ruff.lint] select = [...]` making explicit which rule families are on -- the existing `noqa: BLE001` (blind-except) and `noqa: S104` (bind-all-interfaces) codes come from `flake8-bandit` and `flake8-blind-except`, which are NOT in ruff's default `E,F` selection, so those suppressions currently suppress nothing. Add `uv run ruff check .` to the CI workflow and to `setup.ps1`.

**Evidence.** pyproject.toml:53-55 `[tool.ruff]` block with no ruff dependency; pyproject.toml:28-32 dev group is `pytest` + `pytest-asyncio` only; src/thepit/engine/poller.py:201 `# noqa: BLE001`; src/thepit/api/main.py:648 `# noqa: S104`; .ruff_cache/ exists in the working tree

**Risk.** Turning on the rule families those noqa comments target will surface real findings across ~11k lines. Land the dependency and the explicit `select` in one commit and the resulting fixes in a second, so the diff that changes behaviour is separate from the diff that changes config.

### Create the CI workflow the README already claims exists
*medium* · [back to checklist](#the-checklist)

**Why.** `README.md:123-126` lists as "Not built" a CI that fails the build on an `if live:` branch, but there is no CI at all: no `.github/` directory, no workflow, no status badge. `setup.ps1` runs the tests once at install time and (see the exit-code entry) does not even fail when they fail. There are ~236 test functions across 11 files and nothing runs them except a human remembering to. The project's whole value is measurement correctness -- `eval/pnl.py:session_pnl` is described as the one true P&L implementation guarding against a bug that once reported -$3,060 for a -$1.97 session -- and nothing verifies that on push.

**What.** Add `.github/workflows/ci.yml`: triggers on `push` and `pull_request`. Matrix on `windows-latest` (the target deployment) and `ubuntu-latest`. Steps: `astral-sh/setup-uv` with `enable-cache: true`, `uv sync --locked` (fails if `uv.lock` is stale -- it is committed), `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`. Add a second job `schema` that runs `uv run python -c "from thepit.store import db; db._migration_files()"` so a gapless-migration violation is caught before it reaches a database. Add the badge to `README.md` under the title. Note in the workflow comment that no network-dependent tests may be added without a recorded fixture -- `tests/fixtures/edgar_form4.atom.xml` is the existing pattern.

**Evidence.** README.md:123-126 "*Not built:* ... CI that fails the build on an `if live:` branch"; `ls .github` returns no such directory; tests/ contains 11 test modules; uv.lock is tracked (git ls-files)

**Risk.** CI on `windows-latest` is slow and the Windows runner's clock/timezone differs -- `core/calendar.py` is America/New_York and `tzdata` is only installed by platform marker, so a Windows CI failure there is a real signal, not flake. Do not add `continue-on-error` to silence it.


## The dashboard

### Fix the Chg column: it measures from the oldest bar ever recorded
*small* · [back to checklist](#the-checklist)

**Why.** The Prices table's "Chg" reads as a day-change percentage and is not one. `_latest_quotes` takes the *first* 1m bar in the whole table for that symbol as the reference, and nothing prunes `bars` (retention only prunes raw HTTP payloads). After a month of recording, the dashboard's headline change figure is the move since a month ago, shown in an intraday view, next to a 1m chart. The function's own docstring says "with the previous close for a change figure", so the code contradicts its stated contract.

**What.** In `src/thepit/api/main.py::_latest_quotes` (lines 610-618), replace `SELECT c FROM bars WHERE symbol=? AND tf='1m' ORDER BY ts_ms LIMIT 1` with a reference tied to the current trading day: use `thepit.core.calendar` to get the session open instant for `now_ms()`, then take the last 1d bar close strictly before it, falling back to the first 1m bar at or after the open. Return both `ref_price` and `ref_kind` ('prev_close' | 'today_open' | 'first_bar') in the quote dict so the UI can label what it is comparing against. Update the docstring. Rename the JSON field `open` to `ref_price` and update `web/index.html::renderQuotes` (line 306) which currently prints `q.change_pct` raw with no `toFixed`.

**Evidence.** src/thepit/api/main.py:610-618 — `first = c.execute("SELECT c FROM bars WHERE symbol=? AND tf='1m' ORDER BY ts_ms LIMIT 1"...)`; docstring at src/thepit/api/main.py:595 claims "with the previous close"; src/thepit/engine/main.py:162-180 `_retention` prunes only `recorder.prune_older_than`, never the `bars` table

**Risk.** Picking the reference with a naive `now - 24h` window reintroduces the same class of bug across weekends and holidays. The calendar module already knows session boundaries — use it. Also do not silently fall back to the first bar without setting `ref_kind`, or the fix hides itself the same way the bug did.

### Stop the quote Age column freezing when the feed dies
*small* · [back to checklist](#the-checklist)

**Why.** The file header of web/index.html names quote age as one of three things that are "not placeholder and should survive the rewrite", with the reason: "A feed that stopped updating looks exactly like a quiet market otherwise." It is broken in exactly that case. The WebSocket only sends a quote when its `ts_ms` changes. A dead feed produces no new `ts_ms`, so no delta is sent, the client's cached `age_s` is never updated, and the Age column freezes on its last value forever. The `.stale` red styling at >60s is therefore unreachable by the failure it was written for.

**What.** Compute age client-side instead of trusting the server-stamped value. `_latest_quotes` already returns `received_ms` (src/thepit/api/main.py:599). In `web/index.html::renderQuotes` (lines 303-307), derive age as `(serverNow - q.received_ms)/1000` where `serverNow` is tracked from the socket's `heartbeat` frame (`{type:"heartbeat", ts_ms}`, src/thepit/api/main.py:268-269) — the client currently discards those frames entirely. Set `dirty = true` on every heartbeat so the table repaints on the existing 1s timer. Add a test in tests/test_api.py that opens `/ws` with a TestClient, advances no ticks, and asserts a heartbeat frame arrives carrying `ts_ms`.

**Evidence.** src/thepit/api/main.py:258-262 — `changed = [q for q in current if last.get(...).get("ts_ms") != q["ts_ms"]]`; web/index.html:348-358 — `ws.onmessage` handles only `snapshot` and `delta`; web/index.html:303 — `const stale = q.age_s > 60 ...`

**Risk.** Naively using `Date.now()` makes the age wrong by the client/server clock skew, which is invisible on localhost and arbitrarily large for a LAN phone. Anchor to the heartbeat's `ts_ms` and store the offset.

### Disable Start on the read-only LAN listener instead of dead-ending it
*small* · [back to checklist](#the-checklist)

**Why.** README.md advertises `--lan` as a read-only viewer. The viewer still gets the MFT Session dialog, still gets a *live-enabled* Start button, and clicking it POSTs to an unmounted route. FastAPI returns a 404 HTML body, `await r.json()` throws, the exception propagates out of the click handler, and the button is left permanently disabled reading "Start session" with no message. The one signal the LAN viewer gets is the word "read-only view" in the footer — which is itself overwritten (see the kill-switch footer entry).

**What.** Two changes. (1) In `src/thepit/api/main.py::_readiness` (lines 559-589) accept `allow_control: bool` and append to `missing` when false: "this is the read-only LAN listener; control endpoints are not mounted here". Pass `allow_control` from the `/api/session/preview` handler (line 403). (2) In `web/index.html`, read `control_enabled` from `/api/status` in `poll()` (line 386) into a module-level flag, and when false hide `#mft-open` entirely rather than letting the dialog open. Wrap the `mft-start` handler body (lines 506-526) in try/catch that restores `disabled=false` and writes the error into `#mft-banner`.

**Evidence.** src/thepit/api/main.py:559-589 `_readiness` checks only `claude_mod.available()`, `has_book` and bar depth — never `allow_control`; web/index.html:479 `$("mft-start").disabled = !d.readiness.can_execute;`; web/index.html:506-526 no try/catch around `await r.json()`; README.md:156-164 documents `--lan` as the read-only viewer

**Risk.** Hiding the button in JS is a UI nicety, not a boundary — the server-side 404 remains the actual control. Do not let the UI change get described as the security fix; main.py's module docstring is explicit that "A UI check is not a security boundary."

### Fix the footer: read-only overwrites KILL SWITCH ENGAGED, and neither resets
*small* · [back to checklist](#the-checklist)

**Why.** Two lines run unconditionally in sequence in `poll()`. On the LAN listener with the kill engaged, the KILL SWITCH message is written and then immediately replaced by "read-only view", while the red colour it set stays behind — so a viewer sees red text saying "read-only view" and no indication that everything is halted. On the loopback listener, once the kill is released the footer never reverts: the text stays "KILL SWITCH ENGAGED" and `style.color` stays red for the life of the page.

**What.** In `web/index.html::poll` (lines 382-386) replace the two `if` statements with a single precedence chain writing to a dedicated status element rather than mutating `#foot`'s text and inline style: kill engaged wins over read-only wins over the default "paper trading · levels enforced in Python between model ticks". Clear `style.color` on every pass instead of only setting it. Give the kill state its own element with `role="status"` so it is announced, and keep the descriptive footer text intact underneath.

**Evidence.** web/index.html:382-386 — `if (s.kill_engaged) { ...textContent = "KILL SWITCH ENGAGED"; ...style.color = "var(--down)"; } if (!s.control_enabled) $("foot").textContent = "read-only view";`

**Risk.** Easy to fix the ordering and still leave the stuck colour, because the colour is set with an inline style and never cleared. Reset both, every poll.

### Render fractional quantities to a fixed precision
*small* · [back to checklist](#the-checklist)

**Why.** CLAUDE-READ-THIS.md: "Whole-share sizing does not work. At $20 with a $4 cap, `int(4 / 194)` is 0." Fractional quantities are therefore the normal case, and the dashboard interpolates them raw. A position of 0.178042452830188 renders every one of those digits into the open-positions list and the armed-entries list, wrecking the layout of the panel that matters most.

**What.** In `web/index.html`, format quantity everywhere it is printed: line 606 `${p.qty > 0 ? "+" : ""}${p.qty}` and line 620 `${e.side} ${e.qty} ${e.symbol}`. Add a helper next to `fmt` (line 270) — `const qty = n => n == null ? "-" : (Math.abs(n) >= 1 ? n.toFixed(3) : n.toPrecision(3))` — and use it for both, plus for order rows at line 634 `${o.side} ${o.qty} ${o.symbol}`. `tradectl` already solved this with `%g` (src/thepit/cli/tradectl.py:178) — match its output so the two views agree.

**Evidence.** web/index.html:606, 620, 634 — bare `${p.qty}` / `${e.qty}` / `${o.qty}`; src/thepit/cli/tradectl.py:178 uses `{p['qty']:g}`; CLAUDE-READ-THIS.md:105-107 on fractional sizing

**Risk.** Rounding display quantity to 3 places on a $20 account can round a real position to 0.000 for a very high-priced symbol. Use `toPrecision` for sub-1 values, not `toFixed`.

### Escape model-authored and feed-authored text before it reaches innerHTML
*small* · [back to checklist](#the-checklist)

**Why.** The page has an `esc()` helper and uses it for four of the roughly ten places untrusted text lands in `innerHTML`. Order rejection reasons and the agent's stated reasons come straight from a language model's output; filing headlines and URLs come from EDGAR. All four go in unescaped. A headline containing an angle bracket breaks the list; a model that emits markup gets it executed in the operator's browser, in the same origin that holds the control endpoints.

**What.** In `web/index.html`, wrap with the existing `esc()` (line 657): the order list's `${o.reject_reason}` and `${o.reason}` (lines 635-636) and `${o.symbol}`/`${o.side}` (line 634); `${s.halt_reason}` (line 595); the armed-entry `${e.symbol}`/`${e.side}` (line 620); the positions `${p.symbol}` (line 606). In `loadNews` (lines 392-398) escape `n.headline` and the joined `n.symbols`, and build the anchor with `document.createElement` so `n.url` goes through the `href` property (which will not execute a `javascript:` scheme when set on an element that is then checked) — or validate the scheme is http/https before rendering a link at all. Add a test fixture row in tests/test_api.py with a `<script>` in `news.headline` so a regression is visible.

**Evidence.** web/index.html:657 — `const esc = t => (t || "").replace(/[<>&]/g, ...)`; web/index.html:635-636 — `(o.reject_reason ? `: ${o.reject_reason}` : "")` and `${o.reason}` unescaped; web/index.html:395 — `n.url ? `<a href="${n.url}" ...>${n.headline}</a>``; web/index.html:595 — `halted: ${s.halt_reason}`

**Risk.** `esc()` handles `<`, `>` and `&` but not `"` — it is unsafe for anything interpolated into an attribute value. Do not reach for it for `href` or `data-` attributes; use DOM construction there.

### Ship web/ in the wheel, or make its absence loud
*small* · [back to checklist](#the-checklist)

**Why.** `[tool.hatch.build.targets.wheel] packages = ["src/thepit"]` excludes `web/`. `WEB_DIR` is resolved as `parents[3]` of `src/thepit/api/main.py`, which is the repo root in a source checkout and site-packages' parent in an installed wheel. The mount is guarded by `if WEB_DIR.exists()`, so an installed copy starts cleanly, prints nothing, and serves a bare 404 at `/`. CLAUDE-READ-THIS.md: "Ship working artifacts. Do not hand him something to compile."

**What.** Move `web/` under `src/thepit/web/` (or add `[tool.hatch.build.targets.wheel.force-include]` mapping `web` to `thepit/web`), resolve `WEB_DIR` with `importlib.resources.files("thepit") / "web"` in src/thepit/api/main.py:57, and turn the silent `if WEB_DIR.exists()` at line 503 into an explicit branch that registers a `/` handler returning a plain-text explanation of where it looked. Add a test asserting `GET /` returns 200 and `GET /vendor/uPlot.iife.min.js` returns 200.

**Evidence.** pyproject.toml — `[tool.hatch.build.targets.wheel]` / `packages = ["src/thepit"]`; src/thepit/api/main.py:57 — `WEB_DIR = Path(__file__).resolve().parents[3] / "web"`; src/thepit/api/main.py:503 — `if WEB_DIR.exists():`

**Risk.** Moving the directory breaks the relative paths in `.claude/launch.json` and any muscle memory. Do it in one commit with the README's `web/` reference (README.md architecture block) updated in the same change.

### Show whether a session is scorable, not just its P&L
*small* · [back to checklist](#the-checklist)

**Why.** `SessionPnL` carries `scorable`, `unmarkable`, `discrepancy`, `pnl_bp`, `costs`, `gross`, `notional` and `n_fills`. The API returns three of them. So the dashboard can display a confident "P&L +$0.14" for a session that `tradectl eval` refuses to score because its cash rebuilt from fills disagrees with the cached balance, or because a held symbol has no tick at the mark instant. The two views disagree and the dashboard is the one that looks certain. On $20 of capital, `pnl.toFixed(2)` also rounds most real outcomes to $0.00 — `pnl_bp` is the readable figure and it is computed and discarded.

**What.** In `src/thepit/api/main.py::session_detail` (lines 341-370) return the whole `SessionPnL` shape: add `pnl_bp`, `costs`, `gross`, `notional`, `n_fills`, `scorable`, `unmarkable`, `discrepancy`, `mark_ms`. In web/index.html's `.kv` row (lines 586-593) show P&L as dollars *and* bp, and when `scorable` is false render a warning strip naming the reason — "not scorable: cash rebuilt from fills differs by $X" or "not scorable: no tick for NVDA at the mark instant". Show `mark_ms` for a finished session so it is obvious the numbers are at the session's own clock, not now.

**Evidence.** src/thepit/eval/pnl.py:44-73 — `SessionPnL` with `pnl_bp`, `discrepancy`, `unmarkable` and the `scorable` property; src/thepit/api/main.py:344-347 returns only `equity`, `pnl`, `pnl_realised`; docs/NOTES.md:70-74 lists the exclusion conditions

**Risk.** Do not compute `scorable` a second time in JavaScript. The point of eval/pnl.py is that there is exactly one implementation — CLAUDE-READ-THIS.md: "Do not write a fourth."

### Test that control endpoints 404 on the LAN listener
*small* · [back to checklist](#the-checklist)

**Why.** The central security claim of the API is stated twice in prose — main.py's module docstring ("not permission-checked -- *not mounted*, so they 404 rather than 403") and README.md ("They 404 rather than 403 — the router does not exist, so there is no check to get wrong"). Nothing tests it. tests/test_api.py has ten tests and every one is about the reaper or session-start validation. A refactor that moves a route from the control router to the read router — which is exactly what happened deliberately for `/api/session/preview` — would ship silently.

**What.** Add to tests/test_api.py: build `create_app(home, allow_control=False)` and assert 404 for POST `/api/control/kill`, `/api/control/release`, `/api/control/session/start`; assert the same routes are reachable (non-404) with `allow_control=True`. Add a route-inventory test that walks `app.routes` and asserts every path starting `/api/control` is absent from the LAN app — so a new control endpoint added to the wrong router fails the build rather than passing three specific assertions. Assert `/api/status` reports `control_enabled: false` on that app.

**Evidence.** src/thepit/api/main.py:9-18 — the access-model docstring; README.md:163-164 — "Control endpoints are not mounted on that listener. They 404 rather than 403"; tests/test_api.py — ten tests, all reaper or session-start; the only read-only test, `test_the_read_only_listener_never_reaps` (line 124), asserts about the database, not the routes

**Risk.** Asserting 404 on three hardcoded paths passes while a fourth control route is added to the read router. The route-inventory assertion is the one that actually holds.

### Stop rebuilding the session panel from an HTML string every 4 seconds
*medium* · [back to checklist](#the-checklist)

**Why.** CLAUDE-READ-THIS.md says the session console is "the point" — two sessions were lost because a working session and a dead one looked identical. Today `loadSession` sets `$("sess").innerHTML = html` on a 4s self-scheduling timer while a session is live. Every rebuild: collapses an open `<details>Plan</details>` (rendered without `open`), destroys any text the operator had selected, throws away the console DOM and restores it by copying an HTML string, and then `loadActivity` slams `scrollTop` to the bottom. Reading a line while a session runs is not possible for longer than four seconds.

**What.** Split `loadSession` (web/index.html:568-655) into build-once and patch-in-place. Render the panel skeleton on first load (kv row, `#console`, `#positions`, `#armed`, `#orders`, `<details id="plan">`, `<details id="review">`) and thereafter update only `textContent`/`className` of the leaf nodes. Never touch `#console`'s parent — `loadActivity` already appends incrementally and is the only writer it needs. Preserve `<details>.open` by not re-creating the element. Make the console auto-scroll conditional: only scroll to bottom if the user was already within ~40px of it before the append (`box.scrollHeight - box.scrollTop - box.clientHeight < 40`).

**Evidence.** web/index.html:648-654 — `const prev = $("console"); const carry = prev ? prev.innerHTML : ""; $("sess").innerHTML = html; ... if (carry) $("console").innerHTML = carry;`; web/index.html:642-643 — `<details><summary>Plan</summary>` with no `open`; web/index.html:565 — `box.scrollTop = box.scrollHeight;` unconditional; web/index.html:654 — `if (live) setTimeout(loadSession, 4000);`

**Risk.** Also fix the timer stacking while you are here: `loadSession` self-schedules and is separately called from boot (line 660) and from the Start handler (line 525), so two chains can run at once and double the request rate. Guard with a single stored timer id.

### Serve the equity curve and draw it
*medium* · [back to checklist](#the-checklist)

**Why.** The `equity` table exists specifically "for the curve" and now gets one row per fast-loop pass — every 5 seconds by default — including for a session that never traded, because that was fixed deliberately. A 30-minute session therefore holds ~360 points describing exactly how the money moved. Nothing serves it, nothing draws it, and the operator's only view of P&L is one scalar that updates every 4 seconds. This is the single largest gap between data recorded and data shown.

**What.** Add `GET /api/sessions/{sid}/equity` to the read router in src/thepit/api/main.py next to `session_activity` (line 305), returning `SELECT ts_ms, cash, positions_value, equity FROM equity WHERE session_id=? ORDER BY ts_ms` with an optional `after` cursor mirroring the activity endpoint. In web/index.html add a second uPlot instance below the price chart plotting `equity` against `ts_ms`, with a horizontal reference line at `sessions.capital` so above/below the line is the P&L sign. Mark the session's `ends_ms` and the flatten window on the x-axis. Reuse `loadChart`'s pause-when-hidden discipline (web/index.html:288-293).

**Evidence.** src/thepit/store/schema/002_trading.sql — `-- Equity snapshots, for the curve.` / `CREATE TABLE equity (session_id, ts_ms, cash, positions_value, equity)`; src/thepit/session/runner.py:923-930 `_snapshot_equity`; src/thepit/session/fastloop.py:96-98 — "Called once per pass so the equity curve exists even for a session that never traded"; grep for `equity` in src/thepit/api/main.py returns only line 344, the scalar

**Risk.** A 60-minute session at a 2s fast loop is 1,800 points; do not re-fetch the whole series every poll. Use the `after` cursor and append. Also do not scale the y-axis from zero — on $20 capital the entire interesting range is a few cents and a zero-based axis renders a flat line.

### Add a session picker so past sessions are reachable at all
*medium* · [back to checklist](#the-checklist)

**Why.** `/api/sessions` returns up to 25 sessions with liveness computed, and the dashboard uses exactly one field of one row: `list[0].id`. Every finished session — the whole record the eval module scores — is unreachable from the browser. The only way to look at yesterday's session is `uv run tradectl eval 7` in a terminal. NOTES.md is explicit that "one session is a sample, not a result", which makes a UI that can only show the newest sample the wrong shape for the project.

**What.** Render the `/api/sessions` payload as a left-rail list or a header `<select>`: id, status, started time, P&L, and the `alive` flag already computed at src/thepit/api/main.py:295-299. Clicking sets `activeSession`, resets `lastActivityId = 0`, and re-runs `loadSession`/`loadActivity` — both already take a session id. Put the id in the URL hash (`#/session/7`) and read it on boot so a view is linkable. Stop the 4s self-scheduling poll when the selected session is not live (the `live` check already exists at web/index.html:581). Extend `/api/sessions` to return `pnl` and `pnl_bp` per row so the list does not need N detail fetches.

**Evidence.** src/thepit/api/main.py:276-303 — `sessions()` returns 25 rows with `alive`; web/index.html:570-572 — `const list = await fetch("/api/sessions")...; activeSession = list[0].id;`; docs/NOTES.md:16 — "one session is a sample, not a result"

**Risk.** Computing P&L for 25 sessions per list request runs `eval_pnl.session_pnl` 25 times, each of which does several queries. Cache per (session_id, status) — a finished session's P&L cannot change.


## The research programme

### Refuse to substitute the baseline when the model is unavailable
*small* · [back to checklist](#the-checklist)

**Why.** `runner.use_stub = bool(payload.get("use_stub")) or not claude_mod.available()`. In a scheduled programme a logged-out CLI or an exhausted 5-hour rate window turns a planned LLM session into a baseline session, which then classifies as baseline. The arm counts drift silently and the operator only finds out when `tradectl eval` prints an n nobody can explain — after the market slot is gone.

**What.** Write `arm_intended` before any fallback (migration 007). In `session/launcher.py` and at `api/main.py:478`, when the configured arm is an LLM arm and `claude_mod.available()` is false, abort with a 409/nonzero rather than flipping the arm; record the reason on the session row so a missing slot is visible in the schedule. Add `tradectl doctor`: `claude.find_binary()`, one cheap `claude.ask` probe, engine heartbeat age, kill-switch state, and `FetchLogRepo.gaps` over the last hour — run it once at the head of a scheduled block, not per session.

**Evidence.** src/thepit/api/main.py:478

**Blocked by.** Migration 007 (arm_intended)

**Risk.** The probe itself costs a call against the same shared rate window `SessionConfig.model_calls` exists to protect. One probe per block, and cache the result for the block's duration.

### Correlate conviction against return, not dollars
*small* · [back to checklist](#the-checklist)

**Why.** `report._conviction` correlates `Episode.conviction` against `Episode.net`, which is dollars. Size is chosen by the same model that states the conviction, so a high-conviction trade is mechanically a larger trade and the coefficient partly measures sizing. The conviction experiment's primary metric would be confounded from its first session, and the number has never been reported at n>=20 so nothing published moves when it is fixed.

**What.** Add `net_bp` to `trades.Episode` = `net / (entry_price * peak_qty) * 10_000` (both fields already exist on the dataclass). Use it as the primary series in `report._conviction`; keep tau against `net` as a secondary, labelled size-inclusive. Add a per-bucket win rate with `stats.wilson`. Keep the `MIN_N_FOR_CORRELATION = 20` floor and add a second guard: report the number of distinct conviction values, because `kendall_tau_b` already returns None when one side is entirely tied and "the scale was unused" is a different finding from "no relationship".

**Evidence.** src/thepit/eval/report.py:337-348 (`nets.append(e.net)`); src/thepit/eval/trades.py:65-97; src/thepit/eval/stats.py:110-114

**Risk.** None material to existing results. The one trap is computing net_bp on an open episode, where `net` is deliberately zeroed — keep the `open_at_end` filter that is already there.

### Scope the cohort report to one experiment
*small* · [back to checklist](#the-checklist)

**Why.** `cohort.all_meta(conn, limit=200)` takes the most recent 200 sessions regardless of what they were, and `cohort_report` folds every LLM session into one `ArmSummary` and every baseline into another. The moment two experiments run in the same week, the headline difference mixes blinded sessions, prompt variants, different universes and different tick rates into a single mean — and prints a permutation p beside it.

**What.** Add `experiment_id: str | None` and `arm: str | None` parameters to `cohort.all_meta` and `report.cohort_report`, defaulting to None but with `tradectl eval` requiring `--experiment` once more than one experiment id exists in the database. Fold `ArmSummary` by the recorded arm string rather than the three-valued `Arm` enum. Add a guard next to `require_single_tier` that raises when a cohort mixes experiment ids, universe ids or prompt variants — the same shape as the mixed-tier raise, for the same reason.

**Evidence.** src/thepit/eval/cohort.py:163-166; src/thepit/eval/report.py:177-201; src/thepit/eval/cohort.py:183-195 (require_single_tier as the precedent)

**Blocked by.** Migration 007 (experiment_id, arm)

**Risk.** Raising on a mixed cohort will make `tradectl eval` with no arguments fail on a database that used to print something. Keep the bare command working by reporting per-experiment blocks rather than refusing outright.

### Shake the programme down on closed-market tape before spending market hours
*small* · [back to checklist](#the-checklist)

**Why.** There are roughly thirteen 30-minute regular-hours slots per week per arm and no way to make more. Discovering that the scheduler double-starts, that the launcher's lock file deadlocks, or that a twin pair leaves a position open costs a slot each time. Every one of those failures is reproducible against a closed market where the price never moves.

**What.** Run the full launcher plus scheduler for a week with deterministic arms only, outside regular hours, and assert at the end: no session row in `halted ... still holding` state, no `unknown_arm` or `cash_mismatch` exclusions in `tradectl eval`, no orphan reaping in the API log, `exit_plans` all closed, and `pending_entries` all resolved. Add the assertions as `tests/test_shakedown.py` run against the burn-in database so the check is repeatable. Note that `calendar.is_open` is false out of hours so the poller drops to a 300s quote cadence — set `quote_interval_closed_s` low for the burn-in only, and record that the shakedown proves plumbing, not enforcement timing.

**Evidence.** src/thepit/eval/cohort.py:46-53 (exclusion reasons); src/thepit/session/runner.py:311-318 (still-holding path); src/thepit/config.py:52-55

**Blocked by.** Headless launcher and the twin spawner

**Risk.** A closed-market tape never breaches a level, so the fast loop is exercised only in its no-op path. The shakedown must not be read as evidence that enforcement works; that is what `eval/enforcement.py` measures on live sessions.

### Pre-register every experiment as a file the eval reads
*medium* · [back to checklist](#the-checklist)

**Why.** NOTES states the central risk plainly: one session is a sample, and running twenty guarantees one looks brilliant on noise. Nothing currently records what an experiment intended to measure before it ran, and `tradectl eval` prints every metric it has at any n. Without pre-registration the primary metric is whichever one came out well.

**What.** One `docs/experiments/EXP-XXX-*.md` per experiment, fixed front matter: hypothesis, arms, what is held fixed, ONE primary metric named as an `eval` function, secondary metrics, target n per arm (from `stats.sessions_needed`), stopping rule, falsification condition. New `experiments` table in migration 007 (`id, title, primary_metric, target_n, effect_bp, registered_ms, spec_sha`) and `src/thepit/eval/registry.py` to load and hash them. `SessionRunner.create` stamps `experiment_id` and the spec sha it ran under. `tradectl eval --experiment EXP-001` reports the primary metric and labels everything else exploratory.

**Evidence.** docs/NOTES.md:16-18 ("one session is a sample, not a result"); src/thepit/cli/tradectl.py:209-288 (cmd_eval prints everything, always)

**Blocked by.** Migration 007 (experiment_id)

**Risk.** Pre-registration is theatre if the spec can be edited after the data exists. Store the sha on the session at create time and have eval refuse to fold sessions whose recorded sha differs from the current file, printing the mismatch rather than dropping them quietly.

### Build decision-level behavioural metrics so the questions are not all gated on P&L
*large* · [back to checklist](#the-checklist)

**Why.** The Honest scope table promises answers in 2-12 weeks, but session P&L is the only outcome measured and one session yields exactly one data point. `stats.sessions_needed` at the default 125bp effect will demand a session count that does not fit in weeks. Each session produces `tick_count + 2` model calls and several episodes; metrics at that grain carry 7-15x the n and are what actually make blinding, news and prompt ablations answerable on the stated timeline.

**What.** New `src/thepit/eval/behaviour.py` computing from `decisions.parsed`, `orders`, `fills` and `bars`: symbol-choice rank agreement (did it open the top-|5m-move| name — `prompt._return_bp` already computes that ranking), entry-level distance (`orders.trigger_price` against the tick at `orders.ts_ms`), plan adherence (planned entry parsed from `sessions.plan` against realised `fills.price` — the recorded TSLA 303.50 -> 304.82 failure), stop distance in bp against `SymbolSnapshot.realized_vol_bp`, orders per tick, revision rate (`exits` and `cancel_pending` counts per tick), and flat-tick rate. Surface on `SessionReport` and in `_print_session_eval`. Add a clustered bootstrap to `eval/stats.py` for their intervals.

**Evidence.** README.md:56-62 (Honest scope timelines); src/thepit/session/config.py:170-180 (model_calls per session); src/thepit/session/prompt.py:98-102

**Risk.** Decisions inside one session are not independent — `runner._ask` resumes the same Claude conversation across plan, ticks and review. Any interval on a decision-level metric must bootstrap by resampling SESSIONS, not decisions, or it will claim significance it has not earned. Build the clustered bootstrap first and make the naive one unavailable.


## Cross-cutting: security, budget, contracts, hygiene

### Add an Origin/Host guard to the loopback control API
*small* · [back to checklist](#the-checklist)

**Why.** `/api/control/*` is mounted with no authentication and no CSRF protection, on the theory that binding to 127.0.0.1 is the boundary. It is not: FastAPI json-decodes the request body regardless of Content-Type, so any web page Baron visits can issue a simple cross-origin POST to http://127.0.0.1:8000/api/control/release (no body at all) and clear the kill switch, or POST /api/control/session/start and burn a rate window. DNS rebinding gets past the loopback bind the same way. The one control surface that is meant to be the brake is the easiest to trip.

**What.** In `src/thepit/api/main.py::create_app`, add middleware applied to the control router only: reject any request whose `Origin` header is present and is not `http://127.0.0.1:<port>` / `http://localhost:<port>`, and reject any request whose `Host` header is not a loopback literal. Apply the same Origin check to the `/ws` WebSocket handler at main.py:239 (WebSockets are not covered by CORS at all). Add tests in tests/test_api.py asserting: POST /api/control/release with `Origin: https://evil.example` returns 403; the same request with no Origin and Host `127.0.0.1:8000` succeeds; `/ws` with a foreign Origin is refused.

**Evidence.** src/thepit/api/main.py:429-501 (control router, no auth/middleware), main.py:438 `release()` takes no arguments, main.py:239 `@read.websocket("/ws")`, README.md:163 claims the boundary is "not mounted" — true for LAN, silent about the browser.

**Risk.** Over-tight matching breaks the dashboard when it is opened as `localhost` rather than `127.0.0.1`, or on a non-default port. Allow both spellings and derive the port from config rather than hardcoding 8000.

### Refuse to start a session that cannot finish before the close
*small* · [back to checklist](#the-checklist)

**Why.** `SessionConfig.validate()` checks tick arithmetic and risk percentages but never asks whether the session fits inside a trading day. Start a 60-minute session at 15:30 ET and the flatten window lands after the close, against a feed that has stopped printing — the risk layer then correctly rejects every closing order as stale and the session ends `halted` still holding stock. `calendar.minutes_to_close()` already computes the number and only `/api/status` reads it.

**What.** Add a `SessionReadiness` check in `src/thepit/api/main.py::_readiness` (and in the same place a future `tradectl session start` calls) that reads `calendar.state_at(now_ms)` and `calendar.minutes_to_close(now_ms)`: block with `missing` when the market is closed and the config is not explicitly marked as a closed-market shakedown; block when `duration_minutes > minutes_to_close`; warn when `duration_minutes > minutes_to_close - flatten_before_end_minutes`. Add a `--closed-ok` style flag on the payload so the deliberate closed-market rehearsal is still possible. Tests: a config at 15:50 ET with duration 30 is refused; the same config at 10:00 is accepted.

**Evidence.** src/thepit/session/config.py:117-165 (`validate()` — no calendar check); src/thepit/core/calendar.py exposes `minutes_to_close`; src/thepit/api/main.py:559-589 (`_readiness` checks only CLI presence, bid/ask and bar depth); README.md:118 the flatten "retries through its window" and gives up if the feed is dead.

**Risk.** Being too strict blocks the closed-market rehearsals the research plan explicitly wants. Make it a `missing` entry with an explicit override, not a hard exception.

### Record the resolved model identity, not the string "sonnet"
*small* · [back to checklist](#the-checklist)

**Why.** `SessionConfig.model` defaults to the alias `"sonnet"`, which resolves to whatever snapshot the CLI points at that week. The research programme runs for months and compares arms across that span. If the alias silently moves to a new model mid-cohort, every arm comparison is confounded by a variable nobody recorded, and there is no way to detect it after the fact — the schema stores only the alias inside `sessions.config` JSON.

**What.** Capture the concrete model id from the CLI's JSON response (it reports the model used) in `agent/claude.py::_run`, add it to `ClaudeResult`, and persist it per call as `decisions.model_id` (migration 007+, alongside the arm/experiment columns the research area already wants — one migration, not three). In `eval/cohort.py::meta`, surface the distinct set of model ids per session and add a `MIXED_MODEL` exclusion constant next to `MIXED_TIER`. Report the model id in `tradectl eval`'s per-arm block.

**Evidence.** src/thepit/session/config.py:98 `model: str = "sonnet"`; src/thepit/session/runner.py:876-879 passes the alias straight through; src/thepit/eval/cohort.py:44-52 (exclusion constants — nothing about model identity); src/thepit/store/schema/002_trading.sql decisions table has no model column.

**Blocked by.** the same migration 007 that carries arm/experiment/twin_of columns — write them together

**Risk.** If the CLI does not report a resolved id, fall back to recording the alias plus the CLI version rather than inventing one.

### Count parsed items per fetch, so a 200 that yields nothing is visible
*small* · [back to checklist](#the-checklist)

**Why.** `fetch_log` records HTTP success and latency but not what came out of the parse, so the uptime report says a feed is healthy when its parser has silently stopped producing rows. This is not hypothetical on this machine: the live database has eight successful EDGAR fetches (`ok=1, http_status=200`) and zero rows in `news`. That is indistinguishable from a quiet filing window, which is exactly the ambiguity the fetch_log docstring says it exists to remove.

**What.** Migration 007: `ALTER TABLE fetch_log ADD COLUMN items INTEGER`. Populate it in `feeds/edgar.py` and `feeds/yahoo.py` where the `FetchRecord` is built (add the field to `core/types.py::FetchRecord`), and in `FetchLogRepo.record`. Add a `zero_yield` check to `FetchLogRepo.uptime` / the `/api/health` payload: N consecutive successful fetches from one source with `items = 0` emits a `feed_silent` event through `EventsRepo`. Show it in `tradectl uptime`. Test with a fake feed that returns 200 and an empty item list ten times.

**Evidence.** src/thepit/store/schema/001_init.sql fetch_log columns (no item count) and its docstring "a gap in this table is ambiguous and the 24h uptime claim is unfalsifiable"; live DB C:\Users\baron\.thepit\paper\thepit.db: `SELECT source,kind,ok,COUNT(*) FROM fetch_log GROUP BY 1,2,3` → edgar/news/ok=1: 8 rows, `SELECT COUNT(*) FROM news` → 0.

**Risk.** A legitimately empty window will trip the alert overnight and on weekends. Gate the event on `calendar.is_open` for price feeds and on a generous consecutive-count for filings.

### Take a single-instance lock in the engine
*small* · [back to checklist](#the-checklist)

**Why.** The engine calls itself "the only DB writer" and the whole WAL safety argument rests on that. Nothing stops a second `uv run python -m thepit.engine.main` — which is exactly what happens when a Scheduled Task starts one at boot and Baron starts another in a terminal. Two pollers double every request against Yahoo and SEC (the rate-limit failure mode the http layer paces to avoid), double-write ticks, and both claim the heartbeat file, so `tradectl status` cannot tell that it happened.

**What.** In `src/thepit/engine/main.py::run`, before opening the database, acquire an exclusive lock on `state_dir/engine.lock` (on Windows, open with `O_EXCL` and write the pid; verify a stale pid with `os.kill(pid, 0)` equivalent before overriding). Refuse to start with a clear message naming the pid that holds it. Release in the shutdown path next to `db.checkpoint(conn)`. Write the pid into the file so `tradectl status` can report which process owns the engine. Add a test that a second acquisition fails and that a stale lock from a dead pid is reclaimed.

**Evidence.** src/thepit/store/db.py:1-18 ("One writer" module contract); src/thepit/engine/main.py:44-150 (no lock anywhere in the startup sequence); src/thepit/feeds/http.py:88-98 (pacing exists precisely because bursts get throttled); ops inventory already wants a Scheduled Task pair, which makes double-start the default failure.

**Risk.** A crash leaves a stale lock and the engine will not restart unattended. The pid check is the mitigation and must be tested, not assumed.

### Fix the doc claims that are already false, and add a test that keeps them true
*small* · [back to checklist](#the-checklist)

**Why.** CLAUDE-READ-THIS.md is the first thing every future session reads, and it is already wrong in checkable ways: it says 184 tests when there are 236, and the README says ten sessions are logged when this machine has zero. A brief that is wrong about the things easiest to check is a brief nobody trusts on the things that are hard to check — and it is the primary defence against a fresh session breaking something.

**What.** Correct CLAUDE-READ-THIS.md:29 and README.md:18. Then remove the class of error: replace the hardcoded test count with no number at all, and add `tests/test_docs.py` asserting the claims that can be machine-checked — that every file path named in CLAUDE-READ-THIS.md's `Shape` block exists, that every `uv run` command listed is a real `tradectl` subcommand or module, and that files referenced by README (`config.toml.example`, `.env.example`, `web/vendor/README.md`) exist or are not referenced. Wire it into the CI workflow the README claims exists.

**Evidence.** CLAUDE-READ-THIS.md:29 "# 184 tests" vs `grep -h 'def test' tests/*.py | wc -l` → 236; README.md:18 "Ten sessions logged so far" vs sessions=0; .gitignore:33 references `web/vendor/README.md`, which does not exist (only uPlot.iife.min.js and uPlot.min.css are present).

**Risk.** A doc test that is too clever fails on every prose edit and gets deleted. Assert only paths and command names.

### Make .claude/launch.json start the engine as well as the API
*small* · [back to checklist](#the-checklist)

**Why.** The launch config a fresh AI session will use starts only `thepit.api.main`. That brings up a dashboard reading a database nothing is writing: no ticks, no bars, session start refused with "no prices available yet". The next session then debugs a data problem that is really a missing process — and the two-process shape is the first thing CLAUDE-READ-THIS explains.

**What.** Add a second configuration to `.claude/launch.json` named `thepit-engine` running `uv run python -m thepit.engine.main` with no port, and note in CLAUDE-READ-THIS.md's Run it block that both must be up. Confirm the api entry's `autoPort: true` does not break the loopback control mount, which derives its allow_control decision from `--lan`, not from the port.

**Evidence.** .claude/launch.json contains exactly one configuration (`thepit-api`); src/thepit/api/main.py:466-468 refuses session start with "no prices available yet; let the engine run first".

**Risk.** None beyond a stale port assumption.

### Verify the Windows deployment end to end and close issue #16
*small* · [back to checklist](#the-checklist)

**Why.** The README names Windows as "the target deployment" and then says it is "Not yet verified on a real Windows machine". The repo is now sitting on one, with a live database. Every ops entry (Scheduled Tasks, rotating logs, service accounts) builds on a path nobody has walked once, and the Windows-specific handling — no `add_signal_handler`, `taskkill /F /T` for the CLI process tree, tzdata, `%USERPROFILE%\.thepit` — is asserted in comments rather than exercised.

**What.** Walk the documented path from a clean clone: `setup.ps1`, both processes, dashboard, a stub session, `tradectl status/sessions/eval/uptime`, kill switch engage and release via `New-Item`/`del`, and a `taskkill` of the CLI mid-call to prove `_kill_tree` works. Record what actually happened in docs/NOTES.md, fix what broke, and correct README.md:192-202's platform table where it is wrong. Close issue #16 with the evidence.

**Evidence.** README.md:168-202 ("The target deployment" … "Not yet verified on a real Windows machine — see issue #16"); src/thepit/agent/claude.py:238-248 (`taskkill /F /T` path, untested); src/thepit/engine/main.py:110-120 (KeyboardInterrupt path that skips the WAL checkpoint).

**Risk.** Doing this after the Scheduled Task work means debugging two unknowns at once. Walk it by hand first.

### Define a working agreement for ROADMAP.md itself
*small* · [back to checklist](#the-checklist)

**Why.** Several AI sessions will work from this file over months with no shared memory. Without stable identifiers and a status convention, the second session cannot tell what the first one finished, entries get re-implemented, and the GitHub issue list — which CLAUDE-READ-THIS says must stay actionable and mirror a realistic history — drifts out of sync with the roadmap.

**What.** Put a header block in ROADMAP.md defining: a stable id per entry (`PIT-001`, never renumbered, never reused); the status vocabulary (`open / in-progress / done / dropped`, with the commit sha on done); the rule that an entry is only closed with a commit reference and a one-line result; that methodology arguments belong in docs/NOTES.md and never as roadmap entries; and that a GitHub issue is opened only for work actually in flight, with `fixes #n` in the trailer per CLAUDE-READ-THIS. Add a line to CLAUDE-READ-THIS.md pointing at ROADMAP.md as the backlog of record.

**Evidence.** CLAUDE-READ-THIS.md:147-151 ("Close GitHub issues as they are fixed… Keep the issue list to actionable work"); docs/ contains only NOTES.md; no ROADMAP.md exists yet.

**Risk.** A heavy process nobody follows is worse than none. Keep it to eight lines.

### Budget the model rate window, and fail loudly when it runs out
*medium* · [back to checklist](#the-checklist)

**Why.** The whole reason this project shells out to the CLI is that the metered API would cost ~$1,400/month, and NOTES.md says the constraint therefore "becomes rate windows, which is a scheduling problem". Nobody has scheduled it. `SessionConfig.model_calls` warns the operator up front and nothing tracks actual consumption. When the 5-hour window is exhausted mid-session the CLI returns a normal JSON result with `is_error`, `runner._ask` logs "Model returned an error" and returns None, and the session keeps ticking — spending its remaining ticks producing nothing while the eval module records it as an LLM session that chose not to trade. It can also lock Baron out of his own Claude Code, which the config docstring already calls a worse outcome than a bad trade.

**What.** 1) In `src/thepit/agent/claude.py::_run`, classify the error text: add `RateLimited` as a distinct `ClaudeResult` flag when the reply matches the CLI's limit wording or exit status, alongside the existing `not logged in` special case at claude.py:216. 2) Persist consumption: a `model_calls` table (migration 007+) with ts_ms, session_id, model, effort, latency_ms, tokens_in, tokens_out, cost_usd, outcome — written by `runner._ask` for every call including failures. 3) A `budget.py` helper that answers "calls and tokens in the last 5 hours" and is checked in `_readiness` before a session starts and before each tick. 4) On rate-limit mid-session: stop asking, say so in the activity log, and take the documented path (halt and flatten) rather than ticking into a wall. Test with a fake `ask` that returns the rate-limited result on call 3.

**Evidence.** docs/NOTES.md:109-117 ("the constraint becomes rate windows"); src/thepit/session/config.py:180-190 (`model_calls` docstring: "the same 5-hour subscription window the operator uses for everything else"); src/thepit/agent/claude.py:194-235 (all non-zero exits and `is_error` results collapse to one opaque failure); src/thepit/session/runner.py:899-902 (error → `return None`, tick continues).

**Risk.** Matching on the CLI's human-readable limit message is fragile — pair it with the exit code and treat an unrecognised error as "unknown", not "fine". Do not auto-substitute the stub when the window is exhausted: that silently relabels the arm (see the research entry refusing baseline substitution).

### Preflight the claude CLI contract and record its version
*medium* · [back to checklist](#the-checklist)

**Why.** Every session depends on an undocumented subprocess contract: the flags `-p --output-format json --model --effort --resume --append-system-prompt --disallowed-tools`, and the response keys `result`, `session_id`, `is_error`, `usage.input_tokens`, `usage.output_tokens`, `total_cost_usd`. There is no test for any of it and no version is recorded. If a CLI update renames `--effort`, drops a key, or changes the JSON envelope, every call returns `is_error` or `unparseable CLI output`, every tick does nothing, and the cohort quietly fills with zero-trade "LLM" sessions that look like prudence. This is the single most likely way months of measurement get silently invalidated.

**What.** 1) Add `agent/claude.py::preflight()` that runs `claude --version` and one trivial `-p --output-format json` echo call, asserts the presence of `result`, `session_id`, `usage`, and returns the version string. Call it from `_readiness` and from `tradectl doctor`. 2) Store the version string on the session row (`sessions.cli_version`, migration 007+) and refuse to pool sessions across CLI versions in `eval/cohort.py` the way fill tiers are already refused. 3) Add tests/test_agent.py — there is no test file for the agent module at all — covering `extract_json` (raw, fenced, brace-matched, non-dict, empty) and `_run`'s parsing against a recorded JSON fixture in tests/fixtures/claude_result.json.

**Evidence.** src/thepit/agent/claude.py:148-161 (flag construction), claude.py:210-235 (key reads), claude.py:251-294 (`extract_json`); tests/ contains no test_agent.py or test_prompt.py; docs/NOTES.md:117 "The CLI must be logged in as a CLI."

**Risk.** A preflight call costs a rate-window call per session. Cache the version per process and only re-probe when the binary's mtime changes.

### Neutralise feed-authored text before it enters a prompt
*medium* · [back to checklist](#the-checklist)

**Why.** `build_plan_prompt` interpolates SEC and vendor headlines straight into the markdown the agent acts on. Those strings are third-party text arriving over HTTP into a component whose output places orders. A headline containing markdown table syntax corrupts the market table; one containing instruction-shaped text ("ignore prior constraints", a fake operator note, a fabricated position) is being handed to the model with the same authority as the harness's own words. The operator note is already delimited and labelled as data — the untrusted source is not.

**What.** In `src/thepit/session/prompt.py`, route every externally-sourced string (`n.headline`, `n.summary`, symbols) through a `_as_data()` helper that strips control characters, collapses newlines and pipes, truncates to a fixed length, and emits inside a `<news_item>` block the way the operator note uses `<operator_note>` at prompt.py:274. Add the standing line that nothing inside those blocks is an instruction. Do the same for the rejection feedback block in `runner._tick_prompt` (reject reasons contain model-authored `reason` text echoed back). Add tests asserting a headline containing `</operator_note>`, a pipe table row, and an imperative sentence cannot break out of its block.

**Evidence.** src/thepit/session/prompt.py:260-269 (`add(f"- [{mins}m ago] {', '.join(n.symbols)}: {n.headline}")`); prompt.py:271-279 (the operator note IS delimited, proving the pattern exists); src/thepit/session/runner.py:839-851 (rejected-order feedback loop); docs/NOTES.md lists per-trade news attribution as unbuilt but says nothing about trust.

**Risk.** Over-aggressive stripping mangles legitimate filing titles and weakens the news arm of EXP-002. Sanitise structure, not content: keep the words, kill the framing.


---

# NEXT


## Trading, risk and the session loop

### Add a gross exposure cap to the risk layer
*small* · [back to checklist](#the-checklist)

**Why.** Every size check in `check()` is per symbol. With `preserve` (20% per position, 3 concurrent) a session can be 60% gross long, and with shorts enabled longs and shorts would both count as "under the cap" while the book carries twice the stated risk. The cash check bounds long gross incidentally, not deliberately, and would not bound it at all once short proceeds add to cash. README lists this under "Not built".

**What.** 1) Add `max_gross_pct: float = 100.0` to `trading/book.Limits` and `session/config.SessionConfig` (with `PROFILES` entries — 60/100/100 for preserve/balanced/risk_it). 2) Add `GROSS_CAP = "exceeds gross exposure cap"` to `Reject`. 3) In `check()`, inside the `if not reducing:` block after the per-symbol cap, compute `gross = sum(abs(p.qty) * mark for p in book.positions)` using the projected quantity for this symbol, and reject when it exceeds `equity * max_gross_pct/100`. The mark must come from the caller's quote dict — pass `marks: dict[str, float]` so `check()` stays pure and does not reach into the book for prices it cannot verify. 4) State the cap in the prompt's hard-limits block. 5) Tests in `tests/test_book.py`: three positions each under the per-symbol cap that together breach the gross cap.

**Evidence.** src/thepit/trading/book.py:341-348 caps `notional` for the single symbol only; README.md:101 "Not built: ... a gross exposure cap"; src/thepit/session/config.py:58-65 `PROFILES` is where the three risk numbers already move together.

**Risk.** Marking gross exposure needs a price for every held symbol, and `Book.equity` deliberately values an unpriced position at cost. Using cost inside a risk cap understates exposure when the position has moved; reject rather than approximate when `book.unpriced()` is non-empty, matching how `runner._stopped` already refuses to evaluate the loss limit on a price nobody can stand behind.

### Stop the orphan reaper from abandoning open positions silently
*small* · [back to checklist](#the-checklist)

**Why.** `_reap_orphans` marks a dead session halted, closes its `exit_plans` and cancels its `pending_entries` — correctly, since nothing is enforcing them any more — but leaves the `positions` rows open. The result is a position with no process, no plan, no armed entry, and a session row that reads as terminal. That is the exact state the whole "a session refuses to call itself done while it still holds stock" rule exists to prevent, reached by a different door.

**What.** In `api/main.py::_reap_orphans`, after the two UPDATEs, query open positions for the reaped ids and (a) append the held symbols to `halt_reason` in the same `still holding X` wording `runner._finish` uses, so `cohort.SessionMeta.ended_holding` classifies it correctly, (b) write an `events` row at level warn naming the symbols, and (c) print the exact `tradectl flatten --session N` command to the console. Add a test in `tests/test_api.py` that a reaped session holding stock gets the `still holding` marker.

**Evidence.** src/thepit/api/main.py:112-117 updates `exit_plans` and `pending_entries` only; src/thepit/session/runner.py:311-316 is the wording `_finish` writes; src/thepit/eval/cohort.py:82-86 `ended_holding` matches on the substring "still holding".

**Blocked by.** Build the standalone flatten.py brake and honour the FLATTEN file

**Risk.** Auto-flattening from the reaper would mean the API process trades on behalf of a session it is not driving, on a quote it has not validated. The reaper should report and hand off, never trade.

### Close the gaps in TICK_SCHEMA's vocabulary
*small* · [back to checklist](#the-checklist)

**Why.** The schema the model writes against cannot express several things the engine supports or needs. `cancel_pending` takes symbols only, and `pending_entries` has no uniqueness constraint, so two armed entries on one symbol can only be cancelled together. There is no way to say "flatten everything now". The numeric floors the risk layer enforces — a stop must be at least 2x the round trip away, a target at least 1x — appear nowhere, so the model learns them only by being rejected and reading the feedback block on the next tick.

**What.** In the tick prompt (after it moves to prompt.py): 1) state the computed floors as numbers, e.g. "a stop closer than 6.0bp or a target closer than 3.0bp will be rejected", derived from `round_trip_cost_bp * levels.MIN_STOP_COST_MULTIPLE`. 2) Accept `cancel_pending` entries as either a symbol string or an integer `pending_entries.id`, and show the id in the armed-entries block. 3) Add a top-level `"flatten": true` verb that calls `runner._flatten()` immediately — the model currently has to emit a sell order per symbol with the right quantity. 4) Add `"exits": [{"symbol":"X","clear_target":true}]` so a target can be removed, which `FastLoop.amend` cannot express today (it carries over anything unmentioned). 5) Extend `tests/test_fastloop.py::test_the_tick_schema_documents...` for each.

**Evidence.** src/thepit/session/runner.py:55 `"cancel_pending": ["TSLA"]`; src/thepit/session/fastloop.py:250-263 `cancel_pending` filters by symbol only; src/thepit/trading/levels.py:31 `MIN_STOP_COST_MULTIPLE = 2.0` and levels.py:199-206 produce the rejection text the model never sees in advance; src/thepit/session/fastloop.py:174-182 amend carries over anything unmentioned.

**Blocked by.** Move the whole agent-facing text into prompt.py and stamp it with a version

**Risk.** Every added verb is another parse path that can fail mid-tick and another thing for the model to get wrong. Each must be rejected explicitly with a reason rather than ignored, following the rule in `levels.parse` that a malformed level is an error and not something to shrug at.

### Move the whole agent-facing text into prompt.py and stamp it with a version
*medium* · [back to checklist](#the-checklist)

**Why.** `session/prompt.py` is documented as the highest-leverage file in the repo, but half of what the agent reads lives in `session/runner.py`: `TICK_SCHEMA` is a module constant there and `_tick_prompt` builds the rest inline, mixed with database queries. Nothing records which revision of that text produced a decision, so no measurement can ever attribute a change in behaviour to a change in the prompt. Every prompt edit today is unfalsifiable.

**What.** 1) Move `TICK_SCHEMA` (runner.py:43) and `_tick_prompt` (runner.py:754) into `session/prompt.py` as `build_tick_prompt(...)` taking plain values (book state, plans, armed entries, quotes, minutes left) rather than a runner, so it is testable without a session. `runner._tick_prompt` becomes a two-line adapter. 2) Add `PROMPT_VERSION = "YYYY-MM-DD.N"` and `PROMPT_SHA = hashlib.sha256(...)` over the static template text. 3) Migration `007_prompt_version.sql`: `ALTER TABLE decisions ADD COLUMN prompt_version TEXT;` — write it in `runner._ask`. 4) In `eval/cohort.py::SessionMeta`, add a `prompt_versions: tuple[str, ...]` field and an exclusion constant `MIXED_PROMPT` for a cohort spanning versions, mirroring the existing `MIXED_TIER` treatment. 5) Move the prompt tests out of `tests/test_session.py` into `tests/test_prompt.py`.

**Evidence.** src/thepit/session/runner.py:43 `TICK_SCHEMA = """Return ONLY a JSON object...`; src/thepit/session/prompt.py:1-8 "This is the most important text in the project"; src/thepit/eval/cohort.py:50 `MIXED_TIER` is the existing precedent for refusing to average across a changed measurement instrument.

**Risk.** Migrations only run in the engine process (CLAUDE-READ-THIS.md), so the API cannot see column 007 until the engine restarts; write the column defensively or gate on `schema_version`. Sessions recorded before this land have a NULL version and must be reported as `unknown`, never bucketed into the current one.

### Count armed entries against the concurrency cap and reserve their capital
*medium* · [back to checklist](#the-checklist)

**Why.** An armed entry is not an order, so it consumes neither a concurrency slot nor cash until it triggers. Arm three entries at 100% of equity under `risk_it` (max_concurrent=1) and all three sit waiting; whichever prints first fills, and the other two are rejected at trigger time and silently marked `cancelled` in `pending_entries`. The model is never told this — the tick prompt lists all three as if they will fill — and `eval/enforcement.armed_outcomes` already documents that `cancelled` conflates the model withdrawing an entry with a level that triggered into a rejection.

**What.** 1) In `session/fastloop.py::arm`, before inserting, count `waiting` entries plus open positions and refuse when that exceeds `max_concurrent`, returning an error the caller turns into a `_reject` row rather than an `Armed`. 2) Add committed-cash accounting: sum `qty * trigger_price` over waiting buy entries and subtract it from the cash figure `book.check` sees, or expose it as `Book.committed_cash(pending)`. 3) In `_fill_armed` (fastloop.py:423), distinguish the outcomes: add status `'rejected'` to the `pending_entries` CHECK via migration and use it when `_submit` returns None, so `armed_outcomes` can put a triggered-but-rejected entry in the right bucket. 4) Show committed cash and remaining slots in the armed-entries block of the tick prompt. 5) Tests in `tests/test_fastloop.py` for both the arm-time refusal and the new status.

**Evidence.** src/thepit/trading/book.py:350 counts only `book.open_count()`, never `pending_entries`; src/thepit/session/fastloop.py:436 `self._resolve_entry(entry.id, "cancelled", now)` on a rejected fill; src/thepit/eval/enforcement.py:182-186 "one status covers the model withdrawing an entry, the flatten clearing them all, and a level that DID trigger into a rejected order. Only the third belongs in the denominator."

**Risk.** Reserving cash for armed entries makes the account look smaller than it is and can block a better opportunity at market. Reserve only for entries within some distance of the market, or make the reservation a configurable flag so the effect on P&L is measurable rather than assumed.

### Track drawdown across sessions and add a halt that survives a restart
*medium* · [back to checklist](#the-checklist)

**Why.** A session is the only unit with a loss limit. Ten consecutive sessions can each halt at −60% under `risk_it` and nothing notices the account is gone; the eleventh starts with a clean slate. The only persistent brake is the KILL file, which is manual. README lists both the cross-session index and a persistent halt under "Not built".

**What.** 1) Add `eval/account.py` (read-only, reusing `eval/pnl.session_pnl` — do not write a second P&L implementation): `rolling_drawdown(conn, since_ms)` returning cumulative realised P&L, peak, and drawdown from peak across sessions, plus a flow adjustment for capital added between sessions (`sessions.capital` per row). 2) Add `[risk] max_account_drawdown_pct` to `config.Config` (loaded from config.toml like the feed intervals). 3) In `api/main.py::session_start`, before constructing the runner, call it and return HTTP 409 with the number when the limit is breached — the same shape as the existing kill-switch refusal at main.py:455-459. 4) On breach, write `~/.thepit/state/HALT` and have `SessionRunner._stopped` and `session_start` treat its presence like the kill switch, so it survives a process restart; releasing it is manual, mirroring `KillSwitch.release`. 5) `tradectl account` printing the rolling figure. 6) Tests in `tests/test_eval.py` with three losing sessions.

**Evidence.** README.md:101-103 "Not built: ... a flow-adjusted drawdown index across sessions, and a permanent halt that survives a restart. A session is the only unit with a loss limit today."; src/thepit/engine/killswitch.py:103-107 `release()` is the precedent for a manual-only clear; CLAUDE-READ-THIS.md: "There is now exactly one implementation — `eval/pnl.py:session_pnl` ... Do not write a fourth."

**Risk.** Sessions holding open positions have unrealised P&L that is not a result; including them makes the index swing on marks. Count only sessions with `pnl_realised` true and report the excluded ones, the way `eval/cohort.py` prints exclusions before means.

### Extract an Agent protocol so the decision source is injectable
*medium* · [back to checklist](#the-checklist)

**Why.** `SessionRunner` branches on `self.use_stub` in three separate places (`_plan`, `_tick`, `_review`), each with its own duplicated `INSERT INTO decisions` and its own idea of what the stub writes. `eval/cohort.classify` then reverse-engineers the arm by string-matching the literal `'(deterministic baseline)'` prompt back out of that table. There is no seam to plug a replay agent, a prompt-variant agent, or a second model into — every one of those needs a fourth `if`.

**What.** Define `agent/base.py`: `class Agent(Protocol)` with `name: str`, `arm: str`, and `async def ask(prompt: str, *, phase: str) -> ClaudeResult | None`. Wrap the existing code as `ClaudeAgent` (agent/claude.py) and `StubAgent` (agent/stub.py, which returns its canned JSON without a prompt). Change `SessionRunner.__init__` to take `agent: Agent` and delete `use_stub` and the three branches (runner.py:358, 392, 450); `_plan`/`_tick`/`_review` then have exactly one path each and one `decisions` insert. Add migration `007`: `ALTER TABLE sessions ADD COLUMN arm TEXT;` written at `create()` from `agent.arm`, and rewrite `eval/cohort.classify` to read the column, falling back to the current string match for pre-migration rows.

**Evidence.** src/thepit/session/runner.py:358, 392, 450 — three `if self.use_stub:` branches; src/thepit/eval/cohort.py:28-31 `STUB_PROMPT = "(deterministic baseline)"` with the comment "Written verbatim by runner._plan and runner._tick when use_stub is set."

**Risk.** `api/main.py:478` sets `use_stub` from the request payload and from CLI availability; that fallback ("run the baseline rather than failing the session") must survive the refactor, and `tests/test_fastloop.py:583` and `tests/test_eval.py:377` both poke `runner.use_stub` directly and will need updating in the same commit.

### Implement the fills-versus-positions boot check the schema says exists
*medium* · [back to checklist](#the-checklist)

**Why.** `002_trading.sql` states that `positions` is a cache and "a boot check reconstructs it from the fill stream and refuses to start if they disagree". No such check exists. `Book.load()` is written and never called by anything. `eval/pnl.py` does rebuild cash from fills and reports a `discrepancy`, but only after the fact and only in the read-only path — nothing refuses to trade on a divergent ledger, and any future resume or flatten path would trust the cache.

**What.** Add `Book.rebuild_from_fills()` to `trading/book.py`: replay `SELECT side,qty,price FROM fills WHERE session_id=? ORDER BY ts_ms,id` through the same arithmetic `apply()` uses, returning positions and cash. Add `Book.assert_consistent(tolerance=0.01)` comparing it to the cached `positions` rows and `sessions.cash`, raising a new `LedgerMismatch`. Call it from `Book.load()`, and call `load()` from any path that adopts an existing session (the flatten tool, a future resume). Reuse the constants in `eval/pnl.py` (`DUST`, `CASH_TOLERANCE`) rather than defining new ones. Tests in `tests/test_book.py`: corrupt a `positions` row and assert the raise; assert a clean session round-trips exactly.

**Evidence.** src/thepit/store/schema/002_trading.sql:31-33 "Derived from fills and treated as a cache: a boot check reconstructs it from the fill stream and refuses to start if they disagree. Fills are the truth."; `Book.load` at src/thepit/trading/book.py:116 has no callers anywhere in src/ or tests/; src/thepit/eval/pnl.py:CASH_TOLERANCE is the existing tolerance.

**Risk.** Float replay of thousands of fractional fills will not reproduce the cached cash to the cent; the tolerance has to be chosen against real data or the check becomes a startup failure on every healthy session. Start by logging the divergence for a week before making it fatal.

### Bound concurrent sessions and make the writer story true
*medium* · [back to checklist](#the-checklist)

**Why.** `db.py` opens with "One writer. The engine process is the only thing that writes" and that is already false: session runners write from the API process. `session_start` has no ceiling, so N sessions can run as N asyncio tasks with N separate write connections to one SQLite file, plus the engine. Each fast loop writes an equity snapshot every 5 seconds and an `exit_plan_events` row per trail step, all contending for the same write lock with a 5s busy timeout, and a `BEGIN IMMEDIATE` that times out inside `Book.apply` raises straight out of a fill.

**What.** 1) Add `max_concurrent_sessions: int = 1` to `config.Config` and enforce it in `api/main.py::session_start` by counting rows with status in ('planned','running','flattening') and a fresh heartbeat, returning 409 with the running ids (the baseline twin must be exempt or the ceiling must be 2). 2) Share one write connection across sessions in the process behind an `asyncio.Lock`, or give each session its own connection and document the contention explicitly — pick one and correct the `db.py` module docstring either way. 3) Wrap `Book.apply`'s `db.immediate` in a bounded retry on `sqlite3.OperationalError: database is locked`, since a lost fill is worse than a slow one, and log the retry. 4) Test in `tests/test_db.py` that two concurrent writers on one file complete without raising.

**Evidence.** src/thepit/store/db.py:5-8 "**One writer.** The engine process is the only thing that writes."; src/thepit/api/main.py:461 `wconn = db.connect(config.db_path)` inside `session_start`, once per session, with no ceiling on how many sessions may be started; src/thepit/store/db.py:33 `BUSY_TIMEOUT_MS = 5_000`; src/thepit/trading/book.py:213 `with db.immediate(self._conn):`.

**Risk.** A shared connection behind a lock serialises the fast loops of all sessions; at a 5s cadence that is fine, but it puts a session's stop enforcement behind another session's write. Measure the held-lock duration before choosing, and never hold the lock across an await.


## Measurement and the eval module

### Make level_fills see pre-005 orders and stop silently dropping unpaired fires
*small* · [back to checklist](#the-checklist)

**Why.** Two silent zeros in the flagship enforcement measurement. `level_fills` selects closes with `o.origin LIKE 'fast_loop_%'`, but `orders.origin` only exists from migration 005 and is NULL for every order written before it — `trades.origin_of` carries a legacy prefix table for exactly this case and enforcement does not use it, so an older session reports zero fired levels and looks identical to a session where no level ever fired. Separately, `zip(events, closes, strict=False)` discards a fire with no matching close without counting it.

**What.** In `enforcement.level_fills` (enforcement.py:95-103), select all filled orders for the session and filter in Python through `trades.origin_of(row)` rather than matching on the column in SQL — the query must also select `o.reason` for the legacy fallback to work. Count leftovers from the pairing loop into a new `unpaired_fires: int` returned alongside the list (or as a module function `enforcement.unpaired(conn, session_id)`), and surface it in `CohortReport.lateness_ms` beside `n` so a truncated pairing is visible. Test: insert an order with `origin=NULL` and `reason='fast loop stop: ...'` plus a matching `exit_plan_events` fired row and assert one `LevelFill` comes back.

**Evidence.** src/thepit/eval/enforcement.py:100 `"...AND o.origin LIKE 'fast_loop_%' "`; src/thepit/eval/trades.py:41-49 `_LEGACY_PREFIXES` and trades.py:100-116 `origin_of`; src/thepit/eval/enforcement.py:107 `zip(events, closes.get(symbol, []), strict=False)`.

**Risk.** Pre-006 sessions have no `exit_plan_events` at all, so widening the close side alone produces closes with no fire to pair against. Those sessions should report `None` for lateness rather than a partial figure — reviving them halfway is how the 112-second lateness bug happened in the first place.

### Detect unprotected fills against the plan-event history, not the upserted plans table
*small* · [back to checklist](#the-checklist)

**Why.** `unprotected_fills` asks whether *any* `exit_plans` row exists for the (session, symbol) pair, with no status filter and no time bound. That table is keyed by symbol and its rows persist as `fired`/`closed`, so a symbol that was protected once reads as protected for every later fill — including a fill whose levels failed to resolve and which the fast loop had to unwind. The docstring says the list "should always be empty" and the query is built so it usually will be regardless of the truth.

**What.** Rewrite `trades.unprotected_fills` (trades.py:249-255) against `exit_plan_events`: for each opening fill (origin `model` or `armed`, decided via `origin_of`, not via the SQL `COALESCE(o.origin,'model') IN (...)` which mislabels every legacy row), require an `attached` or `amended` event for that symbol with `ts_ms` between the fill instant and the fill instant plus one `SessionMeta.fast_loop_seconds`. Return `list[tuple[str, int, int]]` of (symbol, fill_id, ts_ms) rather than bare symbols, and update the printer at tradectl.py:326-327.

**Evidence.** src/thepit/eval/trades.py:249-255; src/thepit/store/schema/004_levels.sql:36 `PRIMARY KEY (session_id, symbol)` with statuses active/fired/closed at line 37; src/thepit/store/schema/006_plan_events.sql:1-16 explains why the plans table cannot answer any history question.

**Risk.** The one-interval window is a guess about how promptly `FastLoop.protect` runs after a fill. Derive it from the session's configured `fast_loop_seconds` rather than hardcoding, and allow a small grace, or a legitimate slow attach reads as an unprotected position and the check cries wolf.

### Stop counting model-issued closing fills as at-market entries
*small* · [back to checklist](#the-checklist)

**Why.** `entry_discipline` skips flatten, the three fast-loop origins and unprotected, then counts everything remaining as an opening fill and buckets it `armed` or `at_market`. A sell the model itself issued to close a position has origin `model`, so it is counted as an at-market entry. The headline "N armed at a level, M at market" is the direct measurement of whether arming fixed the recorded TSLA-chasing failure, and it is inflated by every discretionary exit the model takes.

**What.** Derive discipline from the episode fold instead of from raw fills: `trades.episodes` (trades.py:119-172) already tracks which legs are opening via its `opening` test at line 142, and `Episode.entry_origin` records how the position was entered. Rewrite `entry_discipline(conn, session_id)` to fold once and count `Origin.ARMED` versus everything else across episodes, plus a separate `unprotected` count from origin. If per-leg granularity is wanted, have `episodes` expose an `opening_legs: int` and `opening_origins: tuple[Origin, ...]` on `Episode`. Test: a model buy followed by a model sell must yield `opening_fills == 1`.

**Evidence.** src/thepit/eval/trades.py:225-246 `entry_discipline`; src/thepit/session/runner.py:570-579 `_opens_exposure` is the correct test and is not used here; src/thepit/eval/trades.py:142 `opening = (current["qty"] > 0) == (signed > 0)`.

**Risk.** The episode fold is per-symbol and resets on a symbol change (trades.py:133-139); a discipline count derived from it inherits that boundary. Assert the total legs across episodes equals the fill count so a fold bug cannot quietly shrink the discipline denominator.

### Compute thinking share against the session's real wall clock
*small* · [back to checklist](#the-checklist)

**Why.** `ModelUse.thinking_share` divides total model latency by `meta.duration_minutes * 60_000` — the *configured* length. A session that halted on its loss limit after four minutes of a thirty-minute config reports 13% when it spent effectively its whole life waiting on the model. The one number that says "this session was mostly latency" is wrong in exactly the sessions where it matters, and nothing clamps it in the other direction either.

**What.** In `report._model_use` (report.py:254-274) compute `wall_ms = max(1, meta.mark_ms - (meta.started_ms or meta.created_ms))` and keep the configured value as a second field `configured_wall_ms`. Print both in `_print_session_eval` (tradectl.py:356-358) when they differ by more than 10%, since a large gap is itself the signal that the session ended early. `SessionMeta` already carries `mark_ms`, `started_ms` and `created_ms` (cohort.py:60-63), so no schema change is needed.

**Evidence.** src/thepit/eval/report.py:261 `wall_ms = meta.duration_minutes * 60_000`; report.py:272 `thinking_share=sum(latencies) / wall_ms if wall_ms else None`.

**Risk.** `mark_ms` comes from `mark_instant`, which is wrong for reaped sessions until that entry lands — fixing this one first makes thinking share correct for clean sessions and still wrong for interrupted ones.

### Say when the cohort was truncated at the row limit
*small* · [back to checklist](#the-checklist)

**Why.** `all_meta` takes the newest 200 sessions and `cohort_report` defaults to that limit. At session 201 the report silently starts answering a different question — "the last 200" — with no note, no total, and a `flat_rate` denominator that quietly stops growing. Months of running is exactly the condition where this bites, and it is the one failure mode that arrives on its own without anyone changing code.

**What.** Have `cohort.all_meta` also return the total from `SELECT COUNT(*) FROM sessions`, or add `cohort.total_sessions(conn)`. Add `CohortReport.total_sessions: int` and `CohortReport.truncated: bool`, append a note naming both numbers when truncated, and print the total in the header at tradectl.py:229-231. Expose `--limit` on the eval subparser (tradectl.py:447-452), mirroring `sessions --limit` at tradectl.py:440.

**Evidence.** src/thepit/eval/cohort.py:163-166 `"SELECT id FROM sessions ORDER BY id DESC LIMIT ?", (limit,)`; src/thepit/eval/report.py:174 `limit: int = 200`.

**Risk.** None, beyond the report getting slower once the limit is raised — see the recompute entry.

### Distinguish the three causes of a cancelled armed entry
*small* · [back to checklist](#the-checklist)

**Why.** `enforcement.armed_outcomes` documents the problem itself: one `pending_entries.status='cancelled'` covers the model withdrawing an entry, the flatten clearing all of them, and a level that *did* trigger into a rejected order. Only the third belongs in the hit-rate denominator, so the rate is computed over `triggered + expired` and every genuinely-triggered-but-rejected entry disappears from both numerator and denominator. On a session where sizing was wrong, that is the whole failure, invisible.

**What.** Migration 007: `ALTER TABLE pending_entries ADD COLUMN cancel_reason TEXT;`. Write it at each of the three sites: `FastLoop.cancel_pending` (fastloop.py:250-263) sets `'withdrawn'` when `symbols` is given and `'flatten'` when it is not; `FastLoop._fill_armed` (fastloop.py:436) sets `'rejected'` when `_submit` returns None; the `not can_open` branch in `FastLoop.step` (fastloop.py:367-369) sets `'window_closed'`. Thread it through `_resolve_entry` (fastloop.py:498-504) as a keyword. Then `enforcement.armed_outcomes` (enforcement.py:175-209) puts `'rejected'` into the hit-rate denominator and reports `withdrawn`, `flattened` and `window_closed` as separate fields on `ArmedOutcome`, printed at tradectl.py:330-335.

**Evidence.** src/thepit/eval/enforcement.py:180-186: "one status covers the model withdrawing an entry, the flatten clearing them all, and a level that DID trigger into a rejected order. Only the third belongs in the denominator, and they are not currently distinguishable."; src/thepit/session/fastloop.py:250-263, 367-369, 436.

**Risk.** Legacy rows have a NULL `cancel_reason` and must stay out of the denominator rather than defaulting into it — a default of 'rejected' would retroactively invent triggered entries, which is the same class of error as bucketing an unknown origin as 'model'.

### Pre-register a primary metric before a cohort runs
*medium* · [back to checklist](#the-checklist)

**Why.** There is no record anywhere of what a session was run to find out. `DEFAULT_EFFECT_BP = 125.0` is a module constant chosen after the fact, the arm comparison and the conviction correlation and the flat rate all get printed together, and nothing distinguishes the number the run was designed around from the ones that happened to be computable. Over months this is how a noise result becomes a finding.

**What.** Migration 007: `CREATE TABLE experiments (id INTEGER PRIMARY KEY, created_ms INTEGER NOT NULL, name TEXT NOT NULL UNIQUE, hypothesis TEXT NOT NULL, primary_metric TEXT NOT NULL, direction TEXT NOT NULL, effect_bp REAL NOT NULL, planned_n INTEGER NOT NULL, arms TEXT NOT NULL, frozen_ms INTEGER, CHECK (direction IN ('greater','less','two_sided')));` and `ALTER TABLE sessions ADD COLUMN experiment_id INTEGER REFERENCES experiments(id);`. Add a `tradectl prereg` subcommand in `main()` (tradectl.py:428-460) that creates a row and refuses to modify one whose `frozen_ms` is non-NULL. Give `report.cohort_report` an `experiment` parameter: when supplied it filters sessions to that experiment_id, uses `experiment.effect_bp` in place of `DEFAULT_EFFECT_BP` for the `sessions_needed` line, and tags every metric other than `primary_metric` as exploratory in `CohortReport.notes`. When absent, add the note "no pre-registration: every number in this report is exploratory".

**Evidence.** src/thepit/eval/report.py:35 `DEFAULT_EFFECT_BP = 125.0`; src/thepit/store/schema/002_trading.sql:8-27 — `sessions` records config but no hypothesis; docs/NOTES.md:16-17: "Run twenty and one will look brilliant on noise alone. Aggregate before concluding."

**Risk.** A pre-registration nobody can amend becomes a pre-registration nobody writes. Allow superseding by creating a new row that references the old one, never by editing a frozen row, and make the report print the lineage.

### Carry the risk limits on SessionMeta and refuse to pool arms across them
*medium* · [back to checklist](#the-checklist)

**Why.** `ArmSummary.mean_bp` averages `pnl_bp` across every scorable session in an arm. `max_position_pct` moves between 20 and 100 across the three risk profiles, so one `risk_it` session can move a mean built from `preserve` sessions by five times what a `preserve` session could — these are not one population and the mean is not a mean of anything. `SessionMeta` records duration, policy tick and fast-loop interval but none of the three risk fields, so the report cannot even detect the mixture, let alone report it.

**What.** Add `max_position_pct`, `max_concurrent_positions`, `session_loss_limit_pct` and a derived `risk_profile: str` to `SessionMeta` (cohort.py:56-76), read from the config JSON `runner._config_json` already writes (runner.py:994-996). Add `cohort.strata(metas) -> dict[str, list[SessionMeta]]` keyed on the three-tuple. Have `cohort_report` compute `arms` per stratum and set `CohortReport.strata_mixed: bool` plus a note naming the strata present when more than one appears. `tradectl eval` prints the stratum in the arm table header (tradectl.py:236-241).

**Evidence.** src/thepit/eval/cohort.py:56-76 `SessionMeta` fields; src/thepit/session/config.py:58-65 `PROFILES` maps preserve/balanced/risk_it to 20/50/100 percent position; src/thepit/eval/report.py:194-201 builds `ArmSummary` from a flat list.

**Risk.** Stratifying at n=4 produces strata of one, which is worse than a wrong pooled mean because it looks precise. The stratum report must inherit the same refusals — no sd under five, no correlation under twenty — and the honest output at small n is the exclusion table plus a statement that the sessions are not comparable.

### Give every session a stable uid that survives a rebuilt database
*medium* · [back to checklist](#the-checklist)

**Why.** `sessions.id` is a SQLite rowid, local to one file. Paper and live are separate databases by design, so session 7 exists twice with different meanings; a database rebuilt from raw recordings renumbers everything; and a result written down as "session 7 made 43bp" stops being checkable. A fixture corpus, an exported CSV and a months-long record all need an identifier the schema cannot renumber.

**What.** Migration 007: `ALTER TABLE sessions ADD COLUMN uid TEXT;` plus `CREATE UNIQUE INDEX ix_sessions_uid ON sessions(uid) WHERE uid IS NOT NULL;`. Backfill existing rows in the same migration: `UPDATE sessions SET uid = 'paper-' || created_ms || '-' || lower(substr(hex(randomblob(4)),1,8)) WHERE uid IS NULL;`. Generate new ones in `SessionRunner.create()` (runner.py:173-199) as `f"{mode}-{created_ms}-{uuid4().hex[:8]}"` so it sorts by time and names its mode — mode is a constructor argument read from argv per the README, so it must be threaded to the runner. Add `uid` to `SessionMeta`, print it at tradectl.py:294, accept either an int id or a uid string at tradectl.py:448-449, and key every export on it.

**Evidence.** src/thepit/store/schema/002_trading.sql:8-9 `id INTEGER PRIMARY KEY`; src/thepit/cli/tradectl.py:448-449 `e.add_argument("session", nargs="?", type=int, ...)`; README.md:120-122 "Paper and live are different types, not a boolean. Separate databases, separate directories."

**Risk.** Two identifiers for one thing invites code that joins on the wrong one. Keep `id` as the only foreign key target — `positions`, `orders`, `fills`, `decisions`, `exit_plans`, `pending_entries` and `exit_plan_events` all reference it — and treat `uid` strictly as an external name for reports and fixtures.

### Freeze a corpus of recorded sessions and assert the report against it
*medium* · [back to checklist](#the-checklist)

**Why.** Every test in tests/test_eval.py builds its database from scratch through `a_runner`, which exercises the current code against the current schema. Nothing tests a session recorded under an older schema — pre-005 orders with NULL `origin`, pre-006 sessions with no `exit_plan_events` — and nothing catches a metric silently changing value when a formula is edited. Over months of AI sessions editing this module, a frozen corpus is the only thing that makes a changed number visible.

**What.** Add `tests/fixtures/sessions/` holding three committed SQL dumps produced by `sqlite3 thepit.db .dump` filtered to one session and its dependent rows: a clean LLM session, a baseline session, and one halted while still holding. Add a fourth dump captured at `schema_version = 4` to exercise the legacy paths in `trades.origin_of` and the enforcement fix. Alongside each, `tests/fixtures/sessions/expected/<uid>.json` holding the serialized `SessionReport`. New `tests/test_eval_corpus.py` loads each dump into a tmp database, runs `db.migrate` then `report.session_report`, and asserts against the frozen JSON with a float tolerance. Add a determinism case there too: `stats.permutation_p` must return the same value across two processes, which its `seed=20260729` intends and nothing currently asserts. `.gitignore` excludes the database — add an explicit `!tests/fixtures/**` rule or the dumps will not be committed.

**Evidence.** tests/test_eval.py:27-33 — the `conn` fixture creates an empty database per test; `tests/fixtures/` contains only `edgar_form4.atom.xml`; CLAUDE-READ-THIS.md: "The database, raw recordings, logs, journals and `.env` are gitignored and must stay that way."

**Blocked by.** Serialize the eval reports to JSON and CSV

**Risk.** The repo is public. A dump carries `decisions.prompt`, `decisions.response`, `sessions.plan`, `sessions.review` and `news.headline` — scrub or synthesize all free text before committing, and check the dump for the operator note field, which is user-supplied. A fixture that has to be regenerated on every legitimate formula change also becomes a rubber stamp; require the expected-JSON diff to appear in the same commit as the formula change, with the reason.


## Feeds, storage and the engine

### Checkpoint the WAL on a timer, not only on a clean shutdown
*small* · [back to checklist](#the-checklist)

**Why.** The WAL is truncated only in the engine's shutdown path, and on Windows Ctrl-C arrives as KeyboardInterrupt and skips it — the README says so. `wal_autocheckpoint` cannot help while any reader holds a snapshot, and the dashboard polls continuously. So the -wal file grows through the session and survives the process, which on a disk-tight machine is the failure that shows up at 3am.

**What.** Add a `_wal_maintenance` task to src/thepit/engine/main.py alongside `_retention` (line 162), running `db.checkpoint(conn)` every 5 minutes and logging the -wal size before and after. Expose the -wal byte size on `/api/status` next to the existing table counts (src/thepit/api/main.py:156-159) and in `tradectl status`, so growth is observable rather than discovered. Log a warning when a checkpoint returns a non-zero busy count, which is the signal that a reader is pinning the log.

**Evidence.** src/thepit/engine/main.py:147 `db.checkpoint(conn)` is the only call, inside the post-`stopping.wait()` shutdown path; README.md:196 "Windows has no `add_signal_handler`, so it arrives as `KeyboardInterrupt` and skips the WAL checkpoint". Live install: thepit.db-wal is 98,912 bytes with an mtime of Jul 29 23:16 against a thepit.db mtime of 21:37 — a WAL left uncheckpointed by the last exit.

**Risk.** A TRUNCATE checkpoint blocks writers briefly. At this write volume that is microseconds, but if the checkpoint interval ever lands inside a session's order path it will show up as latency — use PASSIVE on the timer and TRUNCATE only at shutdown if that proves true.

### Detect schema drift by hashing applied migrations
*small* · [back to checklist](#the-checklist)

**Why.** The migration runner records a version number and nothing else, so editing an already-applied .sql file leaves the database silently different from what the code believes it created. The runner already fails hard on gapped numbering and on a database newer than the code — this is the remaining way the two diverge without anyone noticing.

**What.** Add a `migrations` table in a new .sql file: `number INTEGER PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL, applied_ms INTEGER NOT NULL`. Populate it in `db.migrate` (src/thepit/store/db.py:151-197) as each script runs. In `db.assert_healthy` (line 210), re-hash every migration file on disk and raise `SchemaError` naming the file when a hash disagrees with the recorded one. Backfill the table for existing databases by recording the current hashes on first run at the current version, with a logged warning that they were assumed rather than verified. Tests in tests/test_db.py: an edited migration file fails `assert_healthy`; an unedited one passes.

**Evidence.** src/thepit/store/db.py:180-186 writes only `INSERT INTO meta (k, v) VALUES ('schema_version', ...)`; `_migration_files` at line 122 validates filenames and numbering but never content.

**Risk.** Backfilling hashes for an existing database records whatever is on disk as correct, so drift that already happened is blessed rather than caught. Say that in the warning text — a false clean bill of health is worse than no check.

### Check the schema version on the read side before touching tables
*small* · [back to checklist](#the-checklist)

**Why.** Migrations run only in the engine. The API opens the database and immediately queries `sessions` with no version check, so starting the API first on a fresh machine — the natural order for someone following the README's two-terminal instructions out of order — crashes with `no such table: sessions` instead of "start the engine first". The same applies to `tradectl` against a database written by newer code.

**What.** Add `db.assert_readable(conn)` in src/thepit/store/db.py that checks `meta.schema_version` exists and equals `len(_migration_files())`, raising `SchemaError` with the remedy in the message. Call it in `create_app` (src/thepit/api/main.py:123) before `_reap_orphans` and in each `tradectl` command that opens a connection (src/thepit/cli/tradectl.py:95, 156, 221, 381), converting the exception into a one-line stderr message and a non-zero exit rather than a traceback. Tests in tests/test_api.py and tests/test_db.py covering the unmigrated and newer-than-code cases.

**Evidence.** src/thepit/api/main.py:86-98 `_reap_orphans` opens the database and runs `SELECT id FROM sessions ...` with no version check; CLAUDE-READ-THIS.md:129 "**Migrations only run in the engine.** A new `.sql` file needs an engine restart before the API can see the tables."

**Risk.** None material. Just be careful that the LAN read-only listener gets the same check — a remote viewer silently serving an empty dashboard is the same failure with less visibility.

### Assert the market calendar covers the date being traded
*small* · [back to checklist](#the-checklist)

**Why.** The holiday and early-close tables are hand-transcribed and end on 2027-12-31. After that every holiday reads as a normal trading day and every early close reads as a 4pm close, with no error — the poller polls at open cadence on Christmas and, more seriously, a session's flatten window is computed against a close that already happened. The module docstring already flags this as the classic under-budgeted item and it has no guard.

**What.** Add `COVERAGE_UNTIL: date = date(2027, 12, 31)` to src/thepit/core/calendar.py and a `assert_covers(ts_ms)` that raises when the instant is past it. Call it from `db.assert_healthy`'s caller in src/thepit/engine/main.py:60 at boot and from `SessionConfig.validate` (src/thepit/session/config.py) so a session cannot be scheduled into uncovered time. Extend the tables through 2028-2029 in the same commit and add the missing half-day pattern (the 1pm close before Independence Day when July 3 is a weekday) with a comment naming the NYSE page each entry came from. Add a test that `assert_covers` rejects a date past the table and that every listed holiday is a weekday.

**Evidence.** src/thepit/core/calendar.py:38-68 — HOLIDAYS and EARLY_CLOSES contain only 2026 and 2027 entries; src/thepit/core/calendar.py:12-14 "**Holiday data below is transcribed from the NYSE schedule and should be verified against the exchange before any live trading.**"

**Risk.** Hard-failing at boot on an uncovered date turns a stale table into an outage. That is the right trade for a trading clock, but it means the table must be extended before the deadline, so the boot check should warn for the last 60 days of coverage rather than only failing at the cliff.

### Wake the poller at the session boundary instead of sleeping through the open
*small* · [back to checklist](#the-checklist)

**Why.** The quote loop picks its interval from `is_open` at the top of each cycle and then sleeps. Overnight that interval is 300s, so a cycle starting at 09:28 does not take its first regular-hours quote until 09:33 — three minutes of the highest-information period of the day missing from the tape, every day. `calendar.next_open_ms` was written for exactly this and is called by nothing.

**What.** In `Poller._quote_loop` (src/thepit/engine/poller.py:191-209), clamp the sleep so it never crosses a state change: compute the next boundary (next open, or the close, via `calendar.next_open_ms` and `calendar.minutes_to_close`) and sleep `min(interval, seconds_to_boundary)`. Do the same in `_bar_loop`, which currently runs at a flat 300s around the clock. While there, decide the pre/post cadence explicitly — `SessionState.PRE` and `POST` currently fall into the 300s closed branch even though sessions can be configured to run in them. Add tests driving a `FixedClock` across 09:29→09:30 and asserting the computed sleep lands on the boundary.

**Evidence.** src/thepit/engine/poller.py:196 `open_now = calendar.is_open(now)` then lines 204-209 sleep the whole interval; src/thepit/core/calendar.py:129-130 "Used to schedule the poller's sleep rather than spinning through a closed weekend at full cadence" — a grep for `next_open_ms` across src/ returns only the definition.

**Risk.** Boundary maths across a DST transition is where this kind of code breaks. The calendar already has a DST test (tests/test_poller.py:84); extend it to the sleep computation rather than trusting the boundary function alone.

### Handle 429 and Retry-After as a distinct condition from a transport failure
*small* · [back to checklist](#the-checklist)

**Why.** A rate limit is the one failure that gets worse when you retry it, and nothing distinguishes it. `backoff_s` also contradicts its own docstring — it claims to apply only after a feed is degraded but returns a delay from the first failure — so the actual retry behaviour is not what the comment says, on the feed whose module docstring opens with a rate-limiter warning.

**What.** In `FeedHttp.get` (src/thepit/feeds/http.py:121-184), capture `Retry-After` into a new `retry_after_s: float | None` field on `FetchRecord` (src/thepit/core/types.py:118) and a matching nullable column on `fetch_log` via a migration. In `Poller._note_failure` (src/thepit/engine/poller.py:315-322), treat an HTTP 429 as an immediate degrade regardless of `degrade_after`, and have the loop sleep `max(interval, retry_after_s)`. Fix `FeedHealth.backoff_s` (src/thepit/engine/poller.py:67-75) so the code and the docstring agree — pick one and make the other match. Tests in tests/test_poller.py: a 429 degrades on the first occurrence and the next sleep honours Retry-After.

**Evidence.** src/thepit/engine/poller.py:69-70 "Applied only after a feed is degraded" against lines 73-75 which return a delay whenever `consecutive_failures` is non-zero; src/thepit/feeds/yahoo.py:3-8 "**Rate limiter warning.** A burst of diagnostic requests ... earned an immediate 429 on every API host"; 429 is special-cased only in `probe` (src/thepit/feeds/yahoo.py:63).

**Risk.** Honouring a large Retry-After blindly can park the price feed for an hour. Cap it at `max_backoff_s` and emit an event when the cap is applied, so a provider asking for a long sleep is visible rather than silently obeyed or silently ignored.

### Refresh the EDGAR ticker map and surface unresolved symbols
*small* · [back to checklist](#the-checklist)

**Why.** The ticker map is loaded once, lazily, and never refreshed — the field recording when it loaded is written and never read, and the method that reports watchlist symbols with no CIK is called by nothing outside its own test. A newly listed ticker produces zero filings until the engine restarts, and a typo'd one produces zero filings forever, silently. The code says it does both of these things.

**What.** In `EdgarNewsFeed.poll` (src/thepit/feeds/edgar.py:146-199), reload the map when `self._clock.now_ms() - self._map_loaded_ms > 86_400_000`. In `Poller._news_loop` (src/thepit/engine/poller.py:251-278), call `self._news.unresolved(self._cfg.symbols)` after the map loads and emit a `warn`/`symbols_unresolved` event listing them; surface it on `/api/status` next to the feed health so it is visible rather than buried in the events stream. Add the count to `tradectl status`. Tests: a clock advanced past a day triggers a reload; unresolved symbols produce exactly one event, not one per poll.

**Evidence.** src/thepit/feeds/edgar.py:111-112 "it changes only when companies list or delist, so this is refreshed daily rather than per poll" — `_map_loaded_ms` is assigned at line 133 and read nowhere; `unresolved` at src/thepit/feeds/edgar.py:139-142 says "a typo'd ticker silently produces zero filings forever" and has no caller in src/.

**Risk.** The map is an 800KB download; reloading it on a timer that resets on restart means a crash loop re-fetches it repeatedly. Gate the reload on the persisted `feed_state.last_poll_ms` rather than in-memory state if that becomes a problem.

### Make the raw archive readable, including after a hard kill
*small* · [back to checklist](#the-checklist)

**Why.** The recorder appends gzip members with no atomicity, and the watchdog terminates the process with `os._exit`, which skips buffer flushes by design. A kill mid-append leaves a truncated final member, and `gzip.open(...)` raises `EOFError` at the end of the file — so one hard kill can make an hour's archive unreadable by the obvious method. There is also no code anywhere that reads the archive back, so nobody would find out until they needed it.

**What.** Add `RawRecorder.read(source, kind, day)` in src/thepit/feeds/recorder.py yielding decoded records and tolerating a truncated tail (catch `EOFError`/`BadGzipFile` on the last member, yield what parsed, and report the byte offset where it stopped). Add `tradectl raw --source yahoo --kind bars --day YYYY-MM-DD` to print or export it. Add a `verify` mode that walks every file and reports unreadable tails. Tests in tests/test_recorder.py: a file truncated mid-member reads back every complete record plus a reported truncation; a clean file reads back whole.

**Evidence.** src/thepit/feeds/recorder.py:110-111 `with gzip.open(path, "at", ...) as fh: fh.write(line + "\n")`; src/thepit/engine/killswitch.py:131-133 "it calls ``os._exit``, which skips atexit handlers, finalizers, and buffer flushes -- deliberately". No reader for the archive exists in src/.

**Risk.** None. Read-only tooling. Resist the temptation to auto-repair files — reporting a truncation is enough, and rewriting the archive undermines the point of keeping verbatim bytes.

### Test the poller loops, not just the functions they call
*medium* · [back to checklist](#the-checklist)

**Why.** Every poller test either calls `_ingest_quotes` directly or calls the fake feed directly. The loop bodies — interval selection, backoff application, the stop-event sleep, the `bars_many` batching fallback, cancellation on shutdown — have no coverage at all. The test named `test_transport_exception_does_not_kill_the_loop` never runs the loop: it asserts the feed raises and then hand-calls `_note_failure`. The one property the poller exists to have is the one nothing tests.

**What.** Add tests in tests/test_poller.py driving `Poller.run()` under `asyncio` with a `FixedClock`, a `FakeFeed` whose mode changes mid-run, and a monkeypatched `_sleep` that records requested durations instead of sleeping. Cover: a feed that raises on cycle 3 and recovers on cycle 5 leaves the loop running; `stop()` cancels all three loops within one tick; `_fetch_bars` uses `bars_many` when present and per-symbol `bars` when not, with the per-symbol path actually writing rows (the regression called out in the docstring); a closed-market cycle requests the closed interval. Rename or rewrite the misleading test at line 250.

**Evidence.** tests/test_poller.py:250-261 — `test_transport_exception_does_not_kill_the_loop` calls `await feed.quotes(["AAPL"])` inside `pytest.raises` and then `p._note_failure(...)` by hand; src/thepit/engine/poller.py:227-235 documents a bug where the batching gate "silently recorded zero bars for the entire first engine run" and there is no test for it.

**Risk.** Loop tests that use real sleeps make the suite slow and flaky. Inject the sleep (or the interval source) rather than patching `asyncio.sleep` globally, or the fast-loop tests will start interacting with these.

### Write unit tests for the two price feeds — there are none
*medium* · [back to checklist](#the-checklist)

**Why.** `feeds/yahoo.py` and `feeds/alpaca.py` have no test file. Between them they contain all the parsing: null-padded OHLCV arrays, the `regularMarketTime` fallback, Alpaca's zero-means-no-quote handling, and a hand-rolled RFC-3339 truncation with a latent bug. Alpaca is the feed the project is about to switch to and not one line of it is exercised.

**What.** Add tests/fixtures/yahoo_chart_1m.json and tests/fixtures/alpaca_quotes_latest.json / alpaca_bars.json from real captured responses (the raw recorder at ~/.thepit/paper/raw already holds Yahoo samples). Add tests/test_yahoo.py and tests/test_alpaca.py exercising the parsers directly, as tests/test_edgar.py does with `_parse_atom`. Cover specifically: `_rfc3339_to_ms` (src/thepit/feeds/alpaca.py:205-223) against `...T13:45:01.123456789Z`, a bare `Z` with no fraction, and a numeric offset such as `-04:00` — the last one is currently broken because `"".join(c for c in tail if c.isdigit())[:6]` sweeps the offset's digits into the fractional part, producing an unparseable string and a silently dropped bar; Alpaca's `bp`/`ap` of 0 becoming None rather than a zero price; a bar row missing a key being dropped without failing the batch; Yahoo's null-padded minutes being skipped.

**Evidence.** tests/ contains no test_yahoo.py or test_alpaca.py. src/thepit/feeds/alpaca.py:218 `digits = "".join(c for c in tail if c.isdigit())[:6]` — for `2026-07-29T13:45:01.12-04:00` this yields `120400` and the reassembled string fails `fromisoformat`, so `quotes` falls back to `now` and `bars` drops the row at src/thepit/feeds/alpaca.py:189-190.

**Risk.** Captured Alpaca fixtures may contain account identifiers; scrub them before committing, since the repo is public (CLAUDE-READ-THIS.md:153).

### Detect and backfill holes in the bar series
*medium* · [back to checklist](#the-checklist)

**Why.** Bars are always requested as "the most recent 100" with no start or end, and nothing ever asks whether the stored series has holes. An engine restart more than 100 minutes after an outage permanently loses that window, and no code path can recover it. The dashboard's readiness check only counts the most recent 30 bars, so a hole in the middle of the day is invisible.

**What.** Add `BarsRepo.gaps(symbol, tf, since_ms, until_ms)` in src/thepit/store/repos.py, returning missing intervals against the market calendar rather than against wall-clock time (so an overnight is not a gap). Add a startup backfill in the engine: on boot, ask for gaps over the last N trading days and fetch them explicitly. That requires extending the feed protocol — add `bars_range(symbol, tf, start_ms, end_ms)` to `PriceFeed` in src/thepit/core/types.py:150-178, implemented with Alpaca's `start`/`end` parameters and Yahoo's `period1`/`period2`. Surface gap counts in `tradectl uptime`. Tests: a seeded series with a punched-out hour reports exactly that hour, and an overnight reports nothing.

**Evidence.** src/thepit/engine/poller.py:50 `bar_limit: int = 100` with `bar_interval_s: float = 300.0` at line 48; `_fetch_bars` (src/thepit/engine/poller.py:227-249) passes only `(symbol, tf, limit)` — no time range exists anywhere in src/thepit/feeds/. src/thepit/api/main.py:581 `thin = [s for s in symbols if len(bars.latest(s, "1m", limit=30)) < 30]` is the only completeness check and only looks at the newest 30.

**Risk.** A backfill on boot competes with live polling for the same rate limit, on a feed documented as throttling bursts hard. Pace it behind the same `FeedHttp(min_interval_s=...)` and run it after the first live cycle, not before.


## Operations, packaging and the machine

### Add the CI check that fails the build on a live-trading branch
*small* · [back to checklist](#the-checklist)

**Why.** `README.md:123-126` explicitly claims, as a safety property, "CI that fails the build on an `if live:` branch". There is no CI and no such check. This is the one README safety claim that is purely a build-time guard -- it costs nothing and is currently vapour. `engine/main.py:216-223` handles `--live` by printing a refusal and returning 2, and `config.py:82-83` exposes `is_live`, so the codebase has live-shaped surface that a future edit could accidentally make reachable.

**What.** Add `tools/check_no_live.py` (stdlib only) that walks `src/` with `ast`, and fails with a non-zero exit if it finds: a branch whose test is `config.is_live` / `mode is Mode.LIVE` / a bare `if live:` that leads to an order-placing call; any string literal matching `ALPACA_LIVE_` outside `README.md`; or any call to a broker order endpoint not guarded by the paper base URL. Keep the rule list short, explicit, and documented in the file's docstring so a future session can tell an intentional addition from an accident. Wire it as a required step in `.github/workflows/ci.yml` and as a test in `tests/test_safety.py` so it also fails locally under `uv run pytest`. This is a static guard on the repo; it does not touch credentials, arming, or any live path.

**Evidence.** README.md:123-126 lists this exact check as not built; src/thepit/engine/main.py:204-223 `--live` flag exists and returns 2; src/thepit/config.py:82-83 `is_live` property; src/thepit/feeds/alpaca.py:67-68 reads `ALPACA_PAPER_KEY_ID` / `ALPACA_PAPER_SECRET`

**Blocked by.** the CI workflow entry

**Risk.** An over-broad AST rule will fire on `config.is_live` used for a harmless display string and train people to bypass the check. Keep it narrow enough that a hit is always worth reading.

### Add `tradectl doctor`: one command that says why nothing is working
*small* · [back to checklist](#the-checklist)

**Why.** Diagnosing a broken install today means reading `setup.ps1` output that lies (see that entry), then `tradectl status`, then guessing. The failure modes are known and enumerable, and they are scattered: `THEPIT_CONTACT_EMAIL` unset means the SEC feed 403s; `claude` not on PATH means every session silently becomes the deterministic baseline (`api/main.py:478` falls back without failing); a stale heartbeat means the engine is dead; a schema/migration mismatch means the API cannot see new tables (`CLAUDE-READ-THIS.md:129-130`); low disk means the recorder is about to start losing writes silently (`recorder.py:82` swallows its own failures).

**What.** Add `cmd_doctor` to `cli/tradectl.py` checking, each with a one-line PASS/FAIL and the exact fix: (1) `feed_http.has_contact()` -- already exists at `feeds/http.py:47`; (2) `claude_mod.available()` -- already exists at `agent/claude.py:115`; (3) heartbeat age via `KillSwitch.heartbeat_age_s()`; (4) kill switch state; (5) `db.schema_version(conn)` vs `len(db._migration_files())` -- promote `_migration_files` to a public `migration_count()`; (6) free bytes on the drive holding `config.home` via `shutil.disk_usage`, warn under 5GB; (7) database + WAL + raw + logs bytes; (8) last successful fetch per source from `fetch_log`; (9) whether `~/.thepit/config.toml` exists and parses. Exit non-zero if any hard check fails so a scheduled task can use it as a precondition. Print it at the end of `setup.ps1` instead of the current static text.

**Evidence.** src/thepit/feeds/http.py:47 `has_contact()` exists and is unused by any diagnostic; src/thepit/agent/claude.py:114-119 `available()`; src/thepit/api/main.py:478 silently falls back to the stub when no model is reachable; CLAUDE-READ-THIS.md:129-130 "Migrations only run in the engine. A new `.sql` file needs an engine restart before the API can see the tables"; src/thepit/feeds/recorder.py:76-82 recording failures are swallowed and return None

**Risk.** A doctor that checks nine things and passes all of them creates false confidence when the tenth is what broke. Keep every check tied to a failure that has actually happened here, and say plainly what it does not check.

### Pick one version source, tag releases, and stop hardcoding the version in the User-Agent
*small* · [back to checklist](#the-checklist)

**Why.** The version appears in two places that can drift: `pyproject.toml:3` says `0.1.0`, and `feeds/http.py:39-41` sends `ThePit/0.1` to SEC and Yahoo as the outbound identity. There are zero git tags, no CHANGELOG, and no `tradectl --version`, so a database, a raw recording, or an eval report cannot be attributed to the code that produced it. `CLAUDE-READ-THIS.md:155-156` states the intent ("Semantic versioning on releases ... version and git tag in sync") and mentions a csproj, which is leftover from a different .NET project -- there is no csproj here.

**What.** Add `__version__` to `src/thepit/__init__.py` sourced from `importlib.metadata.version("thepit")` with a hardcoded fallback for an editable checkout. Import it in `feeds/http.py` to build `USER_AGENT` instead of the literal `"ThePit/0.1"`. Add `--version` to the `tradectl` argparser at `cli/tradectl.py:429` and to the `/api/status` payload at `api/main.py:145`. Write the version into the `meta` table at migration time (`k='app_version'`) so a database records which code wrote it. Add `CHANGELOG.md`. Add `ops/release.ps1` that checks the working tree is clean, bumps `pyproject.toml`, commits, tags `vX.Y.Z`, and runs `gh release create` -- note that this repo's git identity is repo-local. Correct the csproj line in `CLAUDE-READ-THIS.md:155-156`.

**Evidence.** pyproject.toml:3 `version = "0.1.0"`; src/thepit/feeds/http.py:39-41 `f"ThePit/0.1 ({_CONTACT})"`; `git tag -l` returns empty; CLAUDE-READ-THIS.md:155-156 mentions "csproj version and git tag in sync" in a repo with no csproj

**Blocked by.** the LICENSE entry (a public release with no license is worse than no release)

**Risk.** `importlib.metadata.version` raises `PackageNotFoundError` if the package was never installed into the venv; the fallback must be unconditional, because the SEC User-Agent is built at import time and an exception there takes down every feed.

### Pin the Python interpreter with .python-version -- the README's uv claim is not what happened here
*small* · [back to checklist](#the-checklist)

**Why.** `README.md:135-137` says "Python 3.12 is managed by uv; your system Python is untouched." On this machine `.venv/pyvenv.cfg` reports `home = C:\Users\baron\AppData\Local\Programs\Python\Python312` -- uv found and used the *system* Python 3.12.10 rather than downloading a managed one, because nothing pins it. `requires-python = ">=3.12"` permits 3.13 or 3.14, so the next machine (or the next `uv sync` after a system Python upgrade) can silently get a different interpreter, and CI would test a third one.

**What.** Add a `.python-version` file containing `3.12` at the repo root (uv reads it automatically). Optionally add `[tool.uv] python-preference = "only-managed"` to `pyproject.toml` so uv downloads its own interpreter rather than adopting whatever is on PATH -- that is what the README already claims happens. Have `setup.ps1` print the resolved interpreter path after `uv sync` so the operator can see which Python they actually got. Pin the same version in `.github/workflows/ci.yml`. Correct or keep `README.md:135-137` depending on which behaviour is chosen.

**Evidence.** README.md:135-137 "Python 3.12 is managed by uv; your system Python is untouched"; `.venv/pyvenv.cfg` -> `home = C:\Users\baron\AppData\Local\Programs\Python\Python312`, `version_info = 3.12.10`; `ls .python-version` -> no such file; pyproject.toml:5 `requires-python = ">=3.12"`

**Risk.** Switching to `only-managed` forces a fresh interpreter download and rebuilds the venv, which is a few hundred MB on a disk at 89%. Do it deliberately, not in the middle of a session.

### Make the Mac a read-only viewer: fix the dead lan_host config and document the network path
*small* · [back to checklist](#the-checklist)

**Why.** `README.md:156-164` tells the operator to run `uv run python -m thepit.api.main --lan` to view from another machine, and the access model is genuinely sound -- control endpoints are *not mounted* on that listener, so they 404 rather than 403. But `Config.lan_host` (config.py:49, parsed from `api.lan_host` at config.py:108) is read by nothing: `api/main.py:648` hardcodes `host = "0.0.0.0"` whenever `--lan` is passed. The documented safe default ("Empty disables remote viewing entirely") does not exist -- passing `--lan` always binds every interface. And nothing tells the operator that Windows Defender Firewall will block the inbound connection on a fresh machine, which is the actual first failure.

**What.** In `api/main.py:647-651`, use `config.lan_host` as the bind address and refuse to start with a clear message when it is empty, so the config field means what `config.py:46-49` says it means. Add `--lan-host` to override from argv. Add a `README.md` subsection under Windows: the exact `New-NetFirewallRule -DisplayName "The Pit LAN view" -Direction Inbound -LocalPort 8001 -Protocol TCP -Action Allow -Profile Private` command, the warning that `-Profile Any` would expose it on untrusted networks, and the note that the dashboard builds its WebSocket URL from `location.host` (`web/index.html:342`) so it works unmodified from another machine. Add a test in `tests/test_api.py` asserting the control router is absent when `allow_control=False` (there is a reaper test for this shape already at `test_the_read_only_listener_never_reaps`) and that `/api/control/kill` returns 404.

**Evidence.** src/thepit/config.py:46-50 `lan_host: str = ""` with the comment "Empty disables remote viewing entirely, which is the safe default"; src/thepit/config.py:108 parses it; grep shows `lan_host` is never read outside config.py; src/thepit/api/main.py:648 `host = "0.0.0.0"` hardcoded; README.md:156-164; web/index.html:342 `new WebSocket(\`ws://${location.host}/ws\`)`

**Risk.** This is the one place in the project that binds a non-loopback socket. Do not add any control capability to the LAN router while making it easier to reach, and do not weaken the not-mounted (vs permission-checked) design -- `api/main.py:11-17` explains why that distinction is the whole boundary.

### Make THEPIT_CONTACT_EMAIL survive a service account, and assert it at startup
*small* · [back to checklist](#the-checklist)

**Why.** `feeds/http.py:37` reads the env var at *module import time* into a module constant, and `USER_AGENT` is built from it once. `setup.ps1:67` persists it with `SetEnvironmentVariable(..., "User")`, which only new processes launched by that user inherit. A Scheduled Task or Windows service running as SYSTEM or a different account gets `"ThePit/0.1 (set THEPIT_CONTACT_EMAIL)"` and every SEC request returns a bare 403 -- a failure mode `CLAUDE-READ-THIS.md:134` names as already painful. The engine does not refuse to boot or warn about this; `has_contact()` exists at `feeds/http.py:47` and is called by nothing.

**What.** Add `contact_email` to `Config` in `src/thepit/config.py`, read from the TOML `[feed]` table with the env var as an override (env wins), and make `feeds/http.py` build the User-Agent from a value passed in rather than from a module-level constant -- `FeedHttp.__init__` already takes `user_agent` at line 85, so plumb `config.contact_email` through `engine/main.py:76-81`. Log a WARNING at engine startup when it is empty, listing the SEC feed as degraded ahead of time rather than after three 403s. Have `setup.ps1` also write it into `~/.thepit/config.toml` so it is inherited by any account. Add it to the `tradectl doctor` checks.

**Evidence.** src/thepit/feeds/http.py:37 `_CONTACT = os.environ.get("THEPIT_CONTACT_EMAIL", "").strip()` at module scope; src/thepit/feeds/http.py:39-41 `USER_AGENT` built once at import; src/thepit/feeds/http.py:47 `has_contact()` has no callers; setup.ps1:67 `SetEnvironmentVariable(..., "User")`; CLAUDE-READ-THIS.md:134 "SEC needs a contact email in the User-Agent or it returns a bare 403"

**Risk.** Putting an email address in `~/.thepit/config.toml` is fine (that path is outside the repo and the repo ignores `.env`), but do not let it drift into anything committed. The `.gitignore` already covers the repo side.

### Drain the `commands` table, or delete it and correct the README
*medium* · [back to checklist](#the-checklist)

**Why.** `README.md:38` and `001_init.sql:156-161` both describe `commands` as "the API's only write path" -- the API appends, the engine drains, and the single-writer invariant that makes WAL safe is preserved. The table exists with a partial index `ix_commands_pending`, a status CHECK constraint, and a test that inserts into it. Nothing drains it. In reality `api/main.py` opens a *second read-write connection* (`db.connect(config.db_path)` at line 461, no `readonly=True`) and runs sessions against it, so there are two writers to a database whose entire design rationale is that there is one. The documented architecture and the running architecture disagree, and the running one is the unsafe one.

**What.** Either (a) build it: add `src/thepit/engine/commands.py` with `async def drain(conn, handlers, stopping)` polling `SELECT * FROM commands WHERE status='pending' ORDER BY id` every second, dispatching by `kind`, and writing `status`/`result`/`handled_ms` inside `db.immediate()`; register it as a task in `engine/main.py:121-128` next to the poller; change `api/main.py` control endpoints to append rows through a small write-only helper instead of holding an rw connection. Or (b) delete the table in a new `007_drop_commands.sql`, delete the `test_db.py:156` insert, and rewrite `README.md:36-38` and the `001_init.sql` comment to describe the loopback control router that actually exists. Do (b) unless the sessions-in-the-engine move (separate entry) is happening, in which case (a) becomes the natural shape.

**Evidence.** src/thepit/store/schema/001_init.sql:156-177 `commands` table + `ix_commands_pending`; README.md:36-38 "Designed and not built yet: the `commands` table the engine would drain (mutations go straight through the loopback control router today)"; grep for `commands` across src/ finds only the schema file -- no drain loop; src/thepit/api/main.py:461 `wconn = db.connect(config.db_path)` opens a read-write handle in the API process; tests/test_db.py:156 inserts a pending command that nothing consumes

**Blocked by.** a decision on whether sessions move into the engine process

**Risk.** Option (a) without moving sessions gains nothing -- the API would still hold an rw connection for the session runner. Option (b) is a schema deletion; write the migration as a `DROP TABLE`, not a data-preserving rename, and confirm no eval query references it first.


## The dashboard

### Stop shipping every model response on the 4-second session poll
*small* · [back to checklist](#the-checklist)

**Why.** `/api/sessions/{sid}` returns the full `decisions` list including `response` text, up to 50 orders and up to 50 fills, plus `sessions.config` and `sessions.plan` and `sessions.review`, on every poll. The dashboard polls it every 4 seconds while a session is live, uses `decisions` only to sum one float, and never touches `fills` or `config` at all. A 60-minute session at a 1-minute tick with 1-2KB responses is on the order of 100KB re-serialised from SQLite and re-parsed in the browser every 4 seconds, to compute a number the server could have computed in the same query.

**What.** In `src/thepit/api/main.py::session_detail` replace the decisions select (line 367) with an aggregate: `SELECT COUNT(*) n, SUM(cost_usd) spend, SUM(tokens_in) tin, SUM(tokens_out) tout, MAX(ts_ms) last_ms FROM decisions WHERE session_id=?`, returned as a `model` object. Move the full list behind the new `/api/sessions/{sid}/decisions` endpoint. Drop `plan`/`review`/`config` from the polled payload and serve them from a `GET /api/sessions/{sid}/text` fetched once per session id. Update web/index.html line 584 to read `d.model.spend`, and lines 642-647 to fetch plan/review once.

**Evidence.** src/thepit/api/main.py:361-369 — `orders ... LIMIT 50`, `fills ... LIMIT 50`, `decisions ... ORDER BY id` with no limit; src/thepit/api/main.py:342 — `"session": dict(s_row)` includes `config`, `plan`, `review`; web/index.html:654 — `if (live) setTimeout(loadSession, 4000);`

**Risk.** Removing fields from a polled payload will break anything else reading it. Grep first — today only web/index.html consumes this endpoint, and tests/test_api.py:170-175 asserts on `exit_plans` and `pending_entries`, which must stay.

### Show the rejection histogram instead of a bare count
*small* · [back to checklist](#the-checklist)

**Why.** 002_trading.sql: "Keeping the REJECTED orders is the whole point: 'what did it want to do that it was not allowed to do' is a more interesting question than 'what did it do'." The dashboard prints one sentence — "N order(s) rejected by the risk layer" — with no breakdown, while `eval/report.py::_rejections` already computes reason→count and `tradectl eval <id>` prints it. Worse, the order list is sliced to the newest 8, so on a session where the risk layer rejected twenty orders the operator sees a count and eight rows.

**What.** Add `rejections` (reason → count) to the `/api/sessions/{sid}` payload by calling `thepit.eval.report._rejections` — promote it to a public `rejections()` while you are there. In web/index.html replace line 640 with a horizontal bar list sorted by count, each row linking to a filtered order list. Also record intent: migration 005 added `orders.stop_price`, `orders.target_price` and `orders.trigger_price` "recorded even when the order is rejected" — render those on rejected rows so "what would that trade have been" is visible. Raise or paginate the `slice(0, 8)` at line 632.

**Evidence.** src/thepit/eval/report.py:277 `_rejections(conn, session_id) -> dict[str, int]`; web/index.html:639-641 — `html += `<div class="stat">${rejects.length} order(s) rejected by the risk layer</div>``; web/index.html:632 — `d.orders.slice(0, 8)`; src/thepit/store/schema/005_provenance.sql — "The intent, recorded even when the order is rejected"

**Risk.** Reject reasons are f-strings from trading/book.py containing numbers ("exceeds 100% of $20"), so a naive group-by produces one bucket per order. Bucket on the reason's stable prefix, and 005_provenance.sql already warns that string-matching prose reattributes history when the wording changes — normalise in Python where it is testable, not in SQL.

### Put the kill switch on the dashboard
*small* · [back to checklist](#the-checklist)

**Why.** `/api/control/kill` and `/api/control/release` are mounted, tested-adjacent and reachable — and the dashboard has no button for either. The single most safety-critical control in the project is available from `tradectl`, from `touch ~/.thepit/state/KILL`, and from an HTTP POST, but not from the screen the operator is actually looking at while a session runs. The page only *reports* `kill_engaged`, and reports it in a footer that another line overwrites.

**What.** Add a KILL button to the header in web/index.html next to `#mft-open` (line 164), rendered only when `control_enabled` is true. POST to `/api/control/kill` with a `reason` from a one-line prompt, then force an immediate `poll()`. Release is a separate, deliberately awkward affordance — a confirm step — matching the endpoint's own docstring: "Recovery is a human decision, never automatic." Style it distinctly from the accent-orange primary button so it cannot be hit by muscle memory aiming at MFT Session.

**Evidence.** src/thepit/api/main.py:432-442 — `@control.post("/kill")` and `@control.post("/release")`; web/index.html has no reference to `/api/control/kill`; README.md:110-116 documents the kill switch as the primary brake

**Risk.** A kill button one click from a Start button is a way to halt a session by accident. Separate them physically, require a confirm, and never make Release a single click. The file-based switch remains the real brake — this is convenience, and the README should keep saying so.

### Render the health events and the fills the page already downloads
*small* · [back to checklist](#the-checklist)

**Why.** Two payloads are fetched and silently discarded. `/api/health` returns the last 50 `events` rows (level/kind/subject/detail) — the engine's warn and error stream, the thing that says a feed degraded — and `loadHealth` renders only `by_source` and `gaps`. `/api/sessions/{sid}` returns up to 50 `fills` and web/index.html never mentions `d.fills`. So the operator sees orders but not what actually filled, at what price, against which reference, at which `sim_tier`.

**What.** In `web/index.html::loadHealth` (lines 401-417) append a list of `h.events` filtered to `level != 'info'`, newest first, with a relative timestamp. In `loadSession`, add a fills list showing `ts_ms`, `side`, `qty`, `symbol`, `price`, `ref_price`, `cost` and `sim_tier` — NOTES.md says "Every fill records its `sim_tier` so a bar-derived and a quote-derived run can never be averaged into one number", which means the tier belongs on screen next to the fill, not only inside the eval module. Show `quote_ts_ms` (migration 005) alongside `ts_ms` so the gap between the quote a fill was priced from and the fill itself is visible.

**Evidence.** src/thepit/api/main.py:221-226 — `events` selected into the `/api/health` response; web/index.html:401-417 — `loadHealth` uses `h.by_source`, `h.gaps`, `h.partial_window` only; src/thepit/api/main.py:364-366 — `fills` in the detail payload; docs/NOTES.md:41-42 on `sim_tier`

**Risk.** Fifty engine events with JSON `detail` blobs will dominate the right column. Collapse to one line each with the detail behind a disclosure, and keep the gap display first — it is the number that proves uptime.

### Give every form control an accessible name
*small* · [back to checklist](#the-checklist)

**Why.** The MFT dialog has fifteen inputs and selects. Every one is preceded by a `<label>` that is neither wrapping the control nor carrying a `for` attribute, so none of them is programmatically associated. A screen reader announces "combo box" fifteen times with no indication of which is duration and which is the session loss limit. Clicking a label also does not focus its control, which is a plain usability loss for everyone.

**What.** In web/index.html add `for="f-duration"` etc. to each `<label>` in the `.grid` (lines 200-241) and to the two below it (lines 243-250), matching the existing ids `f-duration`, `f-capital`, `f-tick`, `f-fast`, `f-risk`, `f-maxpos`, `f-maxcon`, `f-loss`, `f-flat`, `f-model`, `f-effort`, `f-research`, `f-blind`, `f-baseline`, `f-symbols`, `f-notes`. Give the `<dialog>` an `aria-labelledby` pointing at the MFT SESSION heading. Move the `title=` tooltip on `#f-fast` (line 209) into a visible `<span id="f-fast-help">` referenced by `aria-describedby` — a title attribute is invisible to touch and to keyboard.

**Evidence.** web/index.html:134-135 — `label { display: block; ... }`; web/index.html:201-202 — `<div><label>Duration (min)</label><select id="f-duration">`; the pattern repeats for all fifteen controls through line 250

**Risk.** None. This is mechanical. The only way to get it wrong is to add `for` values that do not match the ids, which is silent — check with a quick script that every `for` resolves.

### Make the quote table keyboard-operable
*small* · [back to checklist](#the-checklist)

**Why.** Selecting a symbol is the only navigation on the page and it is a bare `tr.onclick`. The rows are not focusable, carry no role, and expose no selected state to assistive technology — `tr.sel` is a background-colour change and nothing else. A keyboard user cannot change which symbol the chart shows, at all.

**What.** In `web/index.html::renderQuotes` (lines 304-313) render each row's symbol cell as a `<button>` (or give the row `tabindex="0"` plus `role="button"` and a `keydown` handler for Enter/Space), add `aria-pressed` / `aria-current` reflecting `selected`, and add a `:focus-visible` outline rule — the stylesheet has none anywhere. Add `scope="col"` to the `<th>`s (line 171) and a visually-hidden `<caption>`. Replace the row `onclick` reassignment on every 1s repaint with one delegated listener on `#quotes`, which also stops the repaint from dropping focus.

**Evidence.** web/index.html:311-313 — `for (const tr of tb.querySelectorAll("tr[data-s]")) { tr.onclick = ... }`; web/index.html:67-68 — `tr.sel td { background: #1a1d23; } tr:hover td { ...cursor: pointer; }`; no `:focus` or `:focus-visible` rule exists in the stylesheet (lines 26-152)

**Risk.** Repainting the whole tbody every second destroys focus regardless of role. The delegated-listener change is what actually fixes it; the ARIA is what makes it announceable. Do both or neither helps.

### Fix the two contrast failures: console timestamps and the LIVE badge
*small* · [back to checklist](#the-checklist)

**Why.** `.console .t` is #4b5262 on the console's #08090b background — a contrast ratio of about 2.5:1 against a 4.5:1 requirement, at 12px, for the timestamp column of the panel the project calls "the point". And `.badge.live` is #fff on #ff5c5c, about 3.0:1, at 11px bold — the least legible element on the page is the one that would say real money is at risk. Issue #12 wants a PAPER/LIVE badge that gets "louder, never quieter"; it is currently quieter than the body text.

**What.** In web/index.html raise `.console .t` (line 93) to at least #7d8595 (the existing `--dim`, which measures ~5.2:1 on the page background). For `.badge.live` (line 49) invert the treatment: dark text on a saturated red field, or white on a darker red (#b3261e reaches ~5.9:1 with white), plus a border and non-colour redundancy — the word LIVE is already there, so add a shape or an icon so it does not depend on hue. Audit the rest with a scripted contrast check rather than by eye and record the results in a comment block next to the `:root` variables.

**Evidence.** web/index.html:93 — `.console .t { color: #4b5262; ... }` against web/index.html:88 `background: #08090b`; web/index.html:49 — `.badge.live { background: var(--down); color: #fff; }` with `--down: #ff5c5c` at line 29; web/index.html:16-18 — "The PAPER badge is loud. It gets louder in Stage 9, never quieter."

**Risk.** Issue #12 will re-pick the palette in Claude Design and this work could be discarded. It should not be: the ratios are a constraint on whatever palette lands, so write them down as the constraint ("body text ≥4.5:1, badges ≥4.5:1 at 11px") and hand that to #12 rather than only changing hex values.

### Honour prefers-reduced-motion and announce state changes
*small* · [back to checklist](#the-checklist)

**Why.** The console's pending indicator blinks on a 1.1s infinite loop. A model call takes 9-40 seconds and CLAUDE-READ-THIS.md notes the timeout is 180s — so blinking content runs well past the five seconds at which it becomes a WCAG failure, with no way to stop it. Separately, nothing on the page is announced: a session halting, the engine dying, the kill switch engaging and the WebSocket dropping are all silent DOM mutations. An operator not staring at the tab learns nothing.

**What.** In web/index.html wrap the `@keyframes blink` usage (lines 103-106) in `@media (prefers-reduced-motion: no-preference)`, and under `reduce` substitute a static marker. Add `role="status" aria-live="polite"` to the engine/WS/session status spans in the header (lines 159-163) and `role="alert"` to the kill-switch element from the footer entry. Give `#console` `role="log" aria-live="polite"` — but only after the panel stops being rebuilt every 4 seconds, or every rebuild re-announces the entire log. Add `aria-busy` while a session poll is in flight.

**Evidence.** web/index.html:103-106 — `.console .live .m::after { content: " ▊"; animation: blink 1.1s steps(2) infinite; }`; CLAUDE-READ-THIS.md:118 — "A model call takes 9-40 seconds"; src/thepit/api/main.py:72 — `SESSION_STALE_S = int(claude_mod.TIMEOUT_S + claude_mod.KILL_DRAIN_S + 60)`

**Blocked by.** the session-panel rebuild fix — adding aria-live to a container that is replaced every 4s makes it worse

**Risk.** `aria-live` on a high-frequency log is a well-known way to make a page unusable with a screen reader. Scope it to the newest line, or gate announcements to `kind === 'error'` and phase changes.

### Implement the elapsed-time counter the schema promises
*small* · [back to checklist](#the-checklist)

**Why.** Migration 003 justifies the `pending` column with a specific behaviour: "That is what lets the UI show 'asking the model (23s)' with a live counter instead of a stale line that might be from a minute ago." The counter was never built. `pendingSince` is declared and assigned and never read. So during a 40-second model call the console shows a blinking cursor and no number — which is better than nothing, but is not the thing the column exists for, and the difference between a 12-second call and a call that is about to hit the 180-second timeout is invisible.

**What.** In web/index.html, drop the unused `pendingSince` (lines 539, 556) and instead stamp `el.dataset.since = r.ts_ms` on pending rows in `loadActivity` (lines 548-557). Add a 1s ticker that, for every `.live` element, appends `(Ns)` computed from `Date.now() - since`, and turns the text amber past 60s and red past `claude_mod.TIMEOUT_S`. Expose `TIMEOUT_S` from `/api/status` so the thresholds are not duplicated as JavaScript literals.

**Evidence.** src/thepit/store/schema/003_activity.sql — "lets the UI show 'asking the model (23s)' with a live counter"; web/index.html:539 — `let pendingSince = null;` and web/index.html:556 — `pendingSince = r.pending ? Date.now() : null;` with no other reference in the file; src/thepit/session/runner.py:868 — `act = self.say("model", label + "…", pending=True)`

**Risk.** `Date.now()` against a server `ts_ms` drifts on a LAN client. Anchor to the same heartbeat-derived clock offset as the quote-age fix, and build that offset helper once.

### Colour the console by the kinds actually emitted
*small* · [back to checklist](#the-checklist)

**Why.** The stylesheet defines `k-phase`, `k-fill`, `k-error`, `k-order`, `k-wait` and `k-levels`. The runner emits `phase`, `error`, `order`, `fill`, `wait`, `model` and `levels`. So `model` — the most frequent kind, and the only one that blinks — has no rule and renders in default grey. And `k-order` is painted `var(--down)` red for every order line, including the successes: an armed entry firing and a stop executing both write kind `order` and both render in the same red as a risk-layer rejection. The one colour distinction that matters, worked-vs-refused, is inverted into no distinction at all.

**What.** In web/index.html add `.console .k-model .m { color: <accent or a distinct hue>; }` (lines 92-102). Split the order kind rather than colouring it uniformly: either add a `k-reject` kind at the two `REJECTED` call sites in src/thepit/session/runner.py (lines 602, 659) and colour that red while `order` goes neutral, or add an `outcome` column to the `activity` table. The first is a smaller change and keeps `kind` a closed vocabulary — update the comment in 003_activity.sql, which lists six kinds and omits `levels` entirely.

**Evidence.** web/index.html:92-102 — CSS for `k-phase`, `k-fill`, `k-error`, `k-order`, `k-wait`, `k-levels`; src/thepit/session/runner.py:868 emits kind `model`; src/thepit/session/runner.py:602,659 emit `order` for REJECTED; src/thepit/session/fastloop.py:397,424 emit `order` for a fired stop and a triggered entry; src/thepit/store/schema/003_activity.sql — `kind TEXT NOT NULL, -- 'phase' | 'model' | 'order' | 'fill' | 'wait' | 'error'`

**Risk.** Adding a kind means old rows keep the old value, so the CSS must degrade to something readable rather than invisible. Add a catch-all `.console div .m` colour first.

### Give the WebSocket client a heartbeat watchdog
*small* · [back to checklist](#the-checklist)

**Why.** The server sends `{type:"heartbeat", ts_ms}` every second when nothing changed, and its comment says the purpose is to let "the client distinguish 'nothing changed' from 'server gone'." The client parses the frame and does nothing with it. So when the connection wedges without a clean close — laptop sleep, wifi handoff, a proxy dropping the stream — `onclose` may not fire for minutes and the header keeps showing a green dot and the word "live" while the data behind it is frozen. The feature was built on the server and never wired up on the client.

**What.** In `web/index.html::connect` (lines 341-368) track `lastFrameMs` on every `onmessage` regardless of type. Run a 5s watchdog: if `Date.now() - lastFrameMs > 10000`, set the dot to bad, set the text to "stalled", and call `ws.close()` to force the existing reconnect path. Also fix two smaller things in the same function: build the URL as `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws` (line 342 hardcodes `ws://`, which fails outright behind any TLS terminator on the LAN listener), and replace the fixed 2000ms retry (line 364) with capped exponential backoff — the comment justifying the fixed delay says "this is a localhost socket", which stops being true under `--lan`.

**Evidence.** src/thepit/api/main.py:266-269 — `# Keeps proxies from idling the socket out, and lets the client distinguish "nothing changed" from "server gone".`; web/index.html:348-358 — `onmessage` branches only on `snapshot` and `delta`; web/index.html:342 — `new WebSocket(`ws://${location.host}/ws`)`; web/index.html:361-364 — `// Fixed backoff: this is a localhost socket`

**Risk.** A watchdog that is too aggressive will churn the socket on a busy laptop. Ten seconds against a one-second push interval is ten missed frames — do not tighten it below five.

### Extract the design tokens into a documented token block
*small* · [back to checklist](#the-checklist)

**Why.** Issue #12 is blocked on Baron for accent, dark/light and the PAPER-LIVE badge. The part that is not blocked is making the current page's decisions explicit so #12 inherits a specification instead of a spelunking exercise. Right now the palette is nine `:root` variables plus about a dozen hardcoded hexes scattered through the rules — #123a2a, #1d5c42, #1a1d23, #171a20, #08090b, #4b5262, #b9c0cc, #d7b45a, #cfd4dd, #2a1d12, #5c3d1d, #ffcb8a, #ffa94d, #11130f — and none of them is named or reasoned about.

**What.** In web/index.html promote every hardcoded colour to a named `:root` variable with a comment stating its role and its measured contrast against its background (`--console-bg`, `--console-ts`, `--banner-bg`, `--banner-border`, `--banner-text`, `--row-hover`, `--row-selected`, `--levels`, and so on). Add a `[data-theme="light"]` block defining the same names — even if it is never switched on yet — so the page proves the token set is complete rather than assuming it. Write the constraints #12 must satisfy into a short `docs/UI.md`: minimum contrast ratios, the PAPER/LIVE badge rule from the file header, which behaviours survive the rewrite, and the token names. This is the deliverable that unblocks #12 rather than waiting on it.

**Evidence.** web/index.html:27-32 — the nine-variable `:root`; hardcoded hexes at lines 48, 49, 67, 68, 88, 93, 94, 102, 107, 142-145; web/index.html:10-18 — "Three things are not placeholder and should survive the rewrite"

**Blocked by.** nothing — #12 is blocked on Baron, this is the part that is not

**Risk.** Inventing a light palette without Baron will produce something he discards. Define the token *names* and the contrast constraints; leave the light-mode values as a stub with a comment saying they are placeholders pending #12.

### Add a prompt and response viewer over the decisions table
*medium* · [back to checklist](#the-checklist)

**Why.** CLAUDE-READ-THIS.md: "`session/prompt.py` is the highest-leverage file in the repo." The `decisions` table stores `prompt`, `response`, `parsed`, `error`, `tokens_in`, `tokens_out` and `latency_ms` for every model call. The API's select drops `prompt`, `parsed`, `tokens_in` and `tokens_out`. The dashboard fetches what remains and uses it for one thing: summing `cost_usd`. The exact text sent to the model during a live tick — the artefact the whole project turns on — cannot be seen anywhere except by querying SQLite by hand. The pre-session Preview shows the *plan* prompt only, and only for a config that has not run.

**What.** Add `GET /api/sessions/{sid}/decisions` (id, ts_ms, phase, latency_ms, cost_usd, tokens_in, tokens_out, error, `length(prompt)`, `length(response)`) and `GET /api/sessions/{sid}/decisions/{did}` returning the full `prompt`, `response` and `parsed`. In web/index.html render a per-tick list under the console; clicking a row opens the existing `<dialog>` pattern with three tabs — Prompt / Response / Parsed — reusing the `pre.prompt` style already defined at line 147. Diff consecutive tick prompts so the operator can see what changed between ticks without reading 4KB twice.

**Evidence.** src/thepit/store/schema/002_trading.sql — `CREATE TABLE decisions (... prompt TEXT NOT NULL, response, parsed, error, latency_ms, cost_usd, tokens_in, tokens_out)`; src/thepit/api/main.py:367-369 — `SELECT id,ts_ms,phase,response,error,latency_ms,cost_usd FROM decisions`; web/index.html:584 — `const spend = d.decisions.reduce((a, x) => a + (x.cost_usd || 0), 0);` is the only use

**Risk.** Prompts contain the operator note and the full universe. That is fine on loopback and is already true of `/api/session/preview` on the LAN listener — but decide deliberately whether the decisions detail endpoint belongs on the read router or the control router, and write the decision down rather than defaulting.

### Expose the exit-plan event history as a timeline
*medium* · [back to checklist](#the-checklist)

**Why.** Migration 006 was written because `exit_plans` is keyed by symbol and upserted, so it holds only the last state — and measuring against it produced "112 seconds of lateness for a loop that acted inside one second". The append-only history now exists and the migration names two questions it makes answerable: "what level was in force at 14:32:07" and "did the agent tighten its stop as it learned". No endpoint reads `exit_plan_events`. The dashboard still renders only the upserted current row, which is the exact artefact the migration exists to stop people reasoning from.

**What.** Add `GET /api/sessions/{sid}/levels` returning `SELECT id, symbol, ts_ms, kind, long, entry_price, stop_price, target_price, trail_bp, high_water, detail FROM exit_plan_events WHERE session_id=? ORDER BY ts_ms`. Render per symbol as a vertical timeline: attached → amended → trailed(×N) → fired/closed, with the stop price at each step and the delta from the previous step. In web/index.html the current `plans` map (lines 600-601) keeps only `status === 'active'` rows — leave that for the at-a-glance position line and put the history behind a per-symbol expander.

**Evidence.** src/thepit/store/schema/006_plan_events.sql — the whole file, including "which produced a lateness of 112 seconds for a loop that acted within one second"; src/thepit/api/main.py:353-355 selects `exit_plans` only; grep for `exit_plan_events` in src/thepit/api/main.py returns nothing

**Risk.** A trailing stop on a 5s loop writes a `trailed` row on every raise — a 30-minute trend produces hundreds. Collapse consecutive `trailed` events into a single range with a min/max, or the timeline is unreadable for exactly the sessions worth reading.

### Build the LLM-versus-baseline comparison view, honest about pairing
*medium* · [back to checklist](#the-checklist)

**Why.** CLAUDE-READ-THIS.md names this as the biggest gap: "nothing spawns the baseline twin, so `run_baseline` is recorded and never acted on and the LLM-versus-baseline comparison is unpaired." The dashboard has a "Baseline alongside" dropdown defaulting to yes, which implies a comparison the system does not produce. A comparison view is the natural home for saying so — and for showing the substitute (`cohort.pair` by overlapping clock and universe) labelled as a substitute.

**What.** Add a two-column comparison to the eval view driven by `/api/eval`: `rep.arms[Arm.LLM]` against `rep.arms[Arm.BASELINE]`, with `rep.pairs`, `rep.paired_p` and `rep.difference_bp`. When `rep.pairs == 0`, render the CLI's exact framing — "unpaired: nothing links a session to its control" — as a persistent banner, not a footnote. Add a side-by-side session diff (pick two session ids, show both equity curves on one axis, both order lists, both prompts). Change the `#f-baseline` label in web/index.html:239-240 from "Baseline alongside" to something that does not promise a twin until one is actually spawned.

**Evidence.** CLAUDE-READ-THIS.md:168-171; docs/NOTES.md:78-81 — "Pairing is by overlapping clock and universe, which is a substitute, not a control"; src/thepit/eval/cohort.py:198 `pair(...)` and :226 `_overlap_fraction`; web/index.html:239-240 — `<label>Baseline alongside</label>`

**Blocked by.** the /api/eval endpoint

**Risk.** A side-by-side view is the most persuasive thing on the site and the least warranted by the sample. If it ships without the unpaired banner and the n, it will be read as a result. Build the banner first and the columns second.

### Cover the read endpoints the dashboard depends on
*medium* · [back to checklist](#the-checklist)

**Why.** tests/test_api.py's own docstring says "The API had no tests, which is how the reaper came to mark a session 'halted' while leaving its exit plans 'active'." That gap was closed for the reaper and left open everywhere else. `/api/status`, `/api/quotes`, `/api/bars`, `/api/news`, `/api/health`, `/api/session/preview`, `/api/sessions/{sid}/activity` and `/ws` have zero coverage — and `/api/session/preview` is the endpoint that renders "the highest-leverage text in the project".

**What.** Extend tests/test_api.py using the existing `home`/`conn` fixtures. Assert: `/api/status` returns the counts dict and `control_enabled`; `/api/quotes` orders by the configured symbol order and populates `age_s`; `/api/bars` clamps `limit` to 1000 and returns ascending `ts_ms`; `/api/news` respects the 200 cap; `/api/health` sets `partial_window` true when the first fetch is inside the window and reports a gap when one exists; `/api/sessions/{sid}/activity?after=` returns only rows with a greater id; `/api/session/preview` returns 400 with `errors` for an invalid config and a non-empty `prompt` for a valid one. For `/ws`, use `TestClient.websocket_connect` and assert the first frame is a snapshot and a heartbeat follows when no tick changes.

**Evidence.** tests/test_api.py:1-9 — the docstring; tests/test_api.py contains no reference to `/api/status`, `/api/quotes`, `/api/bars`, `/api/news`, `/api/health`, `/api/session/preview` or `websocket_connect`; src/thepit/api/main.py:374-382 — the preview docstring on prompt review

**Risk.** `/api/health` and `/api/status` call `now_ms()` directly rather than taking a clock, so time-dependent assertions will be flaky. Either inject the clock (the codebase already has `SystemClock` in core/clock.py) or assert on shape and ordering rather than exact values.

### Overlay fills, enforced levels and armed triggers on the price chart
*large* · [back to checklist](#the-checklist)

**Why.** The chart plots close prices and nothing else. "Why did that close by itself" — the question the exit_plans/pending_entries design exists to answer — is currently answerable only by reading text lines. The entry price, the stop, the target, the trailing stop's path and the armed trigger are all recorded with timestamps, and the one place they would be legible at a glance is the one place they are absent.

**What.** Extend `GET /api/bars/{symbol}` or add `GET /api/sessions/{sid}/chart/{symbol}` returning bars plus: fills (`ts_ms`, `side`, `price`, `qty` from the `fills` table, already in the detail payload), exit-plan level history (`exit_plan_events` — `ts_ms`, `kind`, `stop_price`, `target_price`, `high_water`), and pending entries (`trigger_price`, `created_ms`, `expires_ms`, `status`). In web/index.html's `loadChart` (lines 316-339) add uPlot series for stop and target as step lines from `exit_plan_events`, plus a draw hook plotting buy/sell markers at fill points and a dashed horizontal segment for each armed trigger between `created_ms` and `expires_ms`.

**Evidence.** web/index.html:322 — `const data = [bars.map(b => b.ts_ms / 1000), bars.map(b => b.c)];` is the entire dataset; src/thepit/store/schema/006_plan_events.sql — "Append-only. Nothing here is ever updated, so 'what level was in force at 14:32:07' is answerable"; src/thepit/api/main.py:364-366 fetches `fills` which web/index.html never reads

**Blocked by.** the equity-curve endpoint lands first and establishes the per-session series pattern

**Risk.** uPlot draw hooks are the fiddly part of this library and the chart is destroyed and rebuilt every 60 seconds (web/index.html:324). Fix the rebuild to an incremental `setData` before adding hooks, or every overlay gets recomputed a minute at a time.


## The research programme

### EXP-003: is stated conviction calibrated
*small* · [back to checklist](#the-checklist)

**Why.** Third row of the Honest scope table at 8-12 weeks. The eval already refuses to print a coefficient under 20 closed episodes, and the metric it would print is size-confounded. This is the one question that needs no arms at all — it accumulates for free while the other experiments run — so it should be registered early and left to fill.

**What.** Spec at `docs/experiments/EXP-003-conviction.md`. Observational, pooled across every scorable LLM session in the programme regardless of which experiment produced it (the one legitimate cross-experiment fold, and it must be stated as such). Primary metric: Kendall tau-b between the episode's opening conviction and `Episode.net_bp`, target n=60 closed episodes — three times the existing floor — pre-registered with a clustered bootstrap CI. Secondary: per-bucket win rate with Wilson intervals and a bucket->mean net_bp calibration table, both of which `report._conviction` already assembles. Note that the tick schema asks for a conviction per ORDER while the plan asks for one per SESSION: record and analyse them separately, they are different claims. Falsification: a bootstrap CI on tau spanning zero at n=60 means conviction is decoration.

**Evidence.** src/thepit/eval/report.py:32 (MIN_N_FOR_CORRELATION = 20) and report.py:316-349; src/thepit/session/prompt.py:298-299 versus src/thepit/session/runner.py:49-50

**Blocked by.** Conviction metric fix

**Risk.** Conviction is likely to pile up at 7-8, and `kendall_tau_b` returns None when one side is entirely tied. If the distribution collapses, the answer is "the scale is unused" and the fix is a forced-choice prompt (rank your two best ideas) — which is a new registered prompt variant, never an edit made to a running experiment.

### Freeze and version the trading universe
*small* · [back to checklist](#the-checklist)

**Why.** `DEFAULT_SYMBOLS` is a plain list in config.py, and editing it silently changes what later sessions were comparing. Worse, `cohort.pair` skips any candidate where `a.universe != b.universe`, so a single-symbol edit stops every future session from pairing with the history. A months-long programme needs the universe to be a versioned object with a name.

**What.** New `src/thepit/core/universe.py` with named frozen tuples (`UNIVERSES = {"mega12_v1": (...)}`) and `sessions.universe_id` (migration 007) alongside the existing `sessions.universe` JSON from 005. Experiment specs name a universe id; `cohort.pair` and every cross-experiment fold compare the id rather than the tuple. Add a test asserting the hash of each published id's tuple, so a future edit fails the build instead of silently forking the record.

**Evidence.** src/thepit/config.py:29-35 (DEFAULT_SYMBOLS as a mutable module list); src/thepit/eval/cohort.py:214-216; src/thepit/store/schema/005_provenance.sql (sessions.universe)

**Blocked by.** Migration 007

**Risk.** A frozen mega-cap universe means every finding is about mega-caps and nothing else — costs are unrepresentatively tiny and news is unrepresentatively abundant. State that scope limit in every spec, and register a second, wider-spread universe as its own later experiment rather than editing the first.

### Report the minimum detectable effect before committing months of sessions
*small* · [back to checklist](#the-checklist)

**Why.** `stats.sessions_needed` already prints what an experiment would require, but nothing runs it before the sessions are burnt. Several rows of the Honest scope table are almost certainly not answerable on P&L at any feasible n, and knowing which ones in advance is what decides the order of this entire programme.

**What.** New `tradectl power --sd-from EXP-004 --sessions-per-day 6 --days 60` printing (a) the minimum detectable effect at the achievable n by inverting `sessions_needed`, and (b) the calendar time implied by the pre-registered effect. Take sd from the observed `ArmSummary.sd_bp` once an arm has `MIN_N_FOR_SD` sessions, and from a stated prior before that, printing which. Have `eval/registry.py` refuse to register an experiment whose target n exceeds what the schedule can produce in its own stated window unless the primary metric is behavioural rather than P&L.

**Evidence.** src/thepit/eval/stats.py:162-172; src/thepit/eval/report.py:220-227; src/thepit/eval/report.py:36 (DEFAULT_EFFECT_BP = 125.0)

**Blocked by.** Experiment registry and behavioural metrics

**Risk.** An MDE computed from four sessions of sd is itself noise. Print the sd's own n beside it and refuse below `MIN_N_FOR_SD`, which is the rule the report already follows everywhere else.

### Record the regime each session ran in
*small* · [back to checklist](#the-checklist)

**Why.** Paired arms control for the regime; the pooled `ArmSummary` statistics do not. Two sessions on the same universe can be a 4bp/min tape and a 15bp/min tape, and pooling them is most of the variance the sample-size arithmetic is fighting. It also cannot be reconstructed later — `build_market_block` reads a 200-bar window that will have rolled past.

**What.** Migration 007 adds `sessions.regime_vol_bp`, `sessions.regime_range_pct`, `sessions.minutes_to_close_at_start`. Compute in `SessionRunner.create` from the same helpers the prompt uses — `prompt._realized_vol_bp` and `SymbolSnapshot.range_pct` — averaged across the universe. Add a regime column to `tradectl eval`'s session table, and once n allows, split `ArmSummary` on the median vol. This is also what makes the momentum baseline interpretable: it should win in one regime and lose in the other, and a suite that never shows that is telling you the regimes are all the same.

**Evidence.** src/thepit/session/prompt.py:105-123 (_realized_vol_bp); src/thepit/eval/report.py:189-201 (by_arm pooling)

**Blocked by.** Migration 007

**Risk.** A median split at n=20 is two groups of ten and every interval will be useless. Record the covariate now anyway and do not report the split until the register says n supports it.

### EXP-001: reasoning versus recall, three blinding arms on one tape
*medium* · [back to checklist](#the-checklist)

**Why.** First row of the Honest scope table, quoted at 2-6 weeks, and the `Blinding` enum already states the design: if behaviour tracks the label rather than the tape, that is recall demonstrated directly rather than inferred. No spec for it exists and it has never been run.

**What.** Spec at `docs/experiments/EXP-001-blinding.md`. Three arms per slot — REAL, ANONYMIZED, MISLABELED — run simultaneously as a triplet. Critically, ALL THREE use `research=OFF`, including REAL: `SessionConfig.validate` forbids research on a blinded arm, so a REAL+AMBIENT control would confound blinding with news access. Hold fixed: universe id, capital, `risk_it` profile, 30m/5m clock, model and effort, prompt variant. Primary metric (pre-registered, decision grain): under MISLABELED, the fraction of opening orders whose SERVED LABEL leads the universe on familiarity rank versus whose UNDERLYING TAPE leads on 5m momentum. Secondary: paired session P&L bp; rate of plan text asserting a company fact absent from the market table. Falsification: if MISLABELED orders track the served label at a rate indistinguishable from REAL tracking its own, the behaviour is recall.

**Evidence.** src/thepit/session/config.py:18-31 (Blinding docstring, MISLABELED as "the sharpest test"); src/thepit/session/config.py:159-163 (blinding requires research=off)

**Blocked by.** Blinded order path, twin spawner, behavioural metrics

**Risk.** `_label_for` currently rotates the universe by a fixed offset, so a model that notices a $890 price under the AAPL label has de-blinded itself. Randomise the permutation per session, record it in `label_map`, and add a de-blinding probe to the review prompt asking whether it believed the labels — a session that says yes is evidence, not a spoiled sample.

### Record which news items each decision actually saw
*medium* · [back to checklist](#the-checklist)

**Why.** NOTES lists per-trade news attribution as structurally unanswerable: headlines are interpolated into the prompt and no `news.id` is stored against a decision. Without it the news experiment can only say "the arm with news did X", never "the decision that saw this filing did X" — which is the whole point of the question.

**What.** Migration 007 adds `decision_news (decision_id INTEGER REFERENCES decisions(id), news_id TEXT REFERENCES news(id), position INTEGER, age_min INTEGER, PRIMARY KEY (decision_id, news_id))`. `build_plan_prompt` calls `NewsRepo.as_of` inline today (prompt.py:261); change it to return the items alongside the text so `runner._ask` can insert the rows against the decision row it just created, and add the same block to `_tick_prompt`. New `src/thepit/eval/news.py`: headline-to-order latency, fraction of opening orders on a symbol with a filing inside the last N minutes, and P&L split by whether the symbol had a filing in context.

**Evidence.** docs/NOTES.md:85-86; src/thepit/session/prompt.py:260-269; src/thepit/store/repos.py:144-173 (as_of takes the cutoff as a required argument)

**Blocked by.** Migration 007

**Risk.** EDGAR is the only wired news feed and Form 4 insider filings are a thin, odd signal for a 30-minute window. Say so in the experiment spec — "news" here means SEC filings — so the result is not overclaimed as being about headlines.

### EXP-002: does research access change decisions, paired OFF against AMBIENT
*medium* · [back to checklist](#the-checklist)

**Why.** Second row of the Honest scope table at 4-8 weeks. Run unpaired it is a comparison of market regimes, not of research access, and on P&L alone it is hopeless at any feasible n.

**What.** Spec at `docs/experiments/EXP-002-news.md`. Two arms per slot, `research=OFF` and `research=AMBIENT`, everything else fixed, run simultaneously by the twin runner. Primary metric (pre-registered, decision grain): the rate at which the two arms' tick decisions choose different symbols on ticks where the universe had at least one item in context, MINUS the same rate on ticks where it had none — a difference-in-differences that controls for the arms simply disagreeing with each other. Secondary: paired session P&L bp by sign test; whether AMBIENT's orders concentrate on symbols with a filing (the metric from the news-attribution entry); plan length and prose. Falsification: no difference between the news-present and news-absent disagreement rates means news changes the prose only. Do not add `ResearchAccess.REQUESTED` as a third arm — it is a config value with no implementation behind it.

**Evidence.** README.md:59 ("Does news access improve decisions, or just the prose? 4-8 weeks"); src/thepit/session/config.py:33-36

**Blocked by.** News attribution, twin spawner, behavioural metrics

**Risk.** On most 30-minute windows EDGAR has nothing at all for a 12-name mega-cap universe, so the majority of pairs carry no signal and the experiment burns months on empty tape. Compute a news-density figure per candidate slot from the recorded `news` table first and schedule EXP-002 into windows that actually have filings.

### EXP-004: does the LLM beat the harness it is steering
*medium* · [back to checklist](#the-checklist)

**Why.** The headline question, the fourth row of the table, and the one the project's own report currently refuses to answer — it prints a note saying nothing links a session to its control, so the comparison is unpaired and confounded by whatever the market did each day.

**What.** Spec at `docs/experiments/EXP-004-baseline.md`. Every scheduled slot runs the LLM and `momentum_5m` as a twin pair. Primary metric: paired difference in session P&L bp, exact sign test via `stats.sign_test_p`, target n taken from `stats.sessions_needed(sd, effect_bp=125)` recomputed once after the first ten pairs and then frozen. Secondary, and the part likely to pay off far sooner: the `trades.by_exit` split for both arms — if every winner exits on a Python target and every loser on a Python stop, the levels did the work and the reasoning did not. Also decompose the LLM's edge into symbol selection (did it pick a better name than the rule chose that slot) versus level setting (did its stop/target do better on the same name), which is computable from `episodes` plus the baseline twin's chosen symbol.

**Evidence.** docs/NOTES.md:19-25 ("If the LLM cannot beat a five-minute momentum rule, the Python did the work"); src/thepit/eval/report.py:214-219; src/thepit/eval/trades.py:208-213

**Blocked by.** Twin spawner and the experiment registry

**Risk.** The honest expected outcome is "not distinguishable at the n we can run", and the spec should say so before the first session. The by_exit split and the selection/level decomposition are what make the months worth having when the headline number stays inside its interval — write them into the spec as co-primary, not as consolation.

### Fix the session schedule so time of day is not confounded with arm
*medium* · [back to checklist](#the-checklist)

**Why.** Sessions are started by hand from the dashboard whenever someone is watching. The open, midday and the last half hour behave nothing alike, so an arm disproportionately run at 09:45 is measuring the clock. The model can even see the clock — `calendar.minutes_to_close` is interpolated into the plan prompt.

**What.** New `src/thepit/session/schedule.py` defining fixed ET slots (e.g. 10:00, 11:30, 13:30, 15:00), each running the full arm set simultaneously so time of day is held constant WITHIN a comparison, and rotating which slots run when the daily model budget cannot cover all four. Record `sessions.slot` and `sessions.minutes_to_close_at_start` (migration 007). Drive it via `tradectl run --schedule` from Windows Task Scheduler, invoking the headless launcher. Refuse to start a slot when `FetchLogRepo.gaps` shows a gap in the previous hour — a session run on a feed that was dead is not evidence about anything.

**Evidence.** src/thepit/api/main.py:444-499 (the only start path is a dashboard button); src/thepit/core/calendar.py:119-126; src/thepit/store/repos.py:244-255 (gaps)

**Blocked by.** Headless launcher

**Risk.** An unattended scheduler needs the kill switch and the orphan reaper to be trustworthy before an LLM arm runs with nobody watching. Gate it behind the shakedown week and a clean `tradectl sessions` — no `halted ... still holding` rows.

### Add a stopping rule and stop reading results early
*medium* · [back to checklist](#the-checklist)

**Why.** `tradectl eval` prints means, an arm difference and a permutation p on demand at any n. Running it daily and stopping when it looks good is the cheapest possible way to manufacture the exact result NOTES warns about, and it costs almost nothing to prevent.

**What.** Extend `eval/registry.py` with `target_n` and a stopping rule per experiment. `tradectl eval --experiment X` prints progress (`n=13/40`) and WITHHOLDS the primary metric until target n — the same discipline `stats.stdev` already applies below `MIN_N_FOR_SD`. A `--peek` flag prints it with a penalty (a Bonferroni split across a fixed number of pre-declared interim looks is defensible and is thirty lines; an O'Brien-Fleming boundary if it ever matters) and records the look in an `eval_peeks` table so the number of looks is itself auditable. Secondary and behavioural metrics stay visible throughout — those are what you steer the harness with.

**Evidence.** docs/NOTES.md:16-18; src/thepit/eval/stats.py:24-26 and 162-172; src/thepit/cli/tradectl.py:226-247

**Blocked by.** Experiment registry

**Risk.** The operator is not blindable — `tradectl sessions` shows per-session P&L and always will. The stopping rule binds the REPORTED comparison only, and the spec should say that plainly rather than claiming a blinding it cannot enforce.

### Replace the single momentum stub with a baseline suite
*large* · [back to checklist](#the-checklist)

**Why.** `stub.py` is one rule and its own docstring calls it a floor whose purpose is to be beaten. Beating a floor tells you almost nothing: momentum wins in a trending regime and loses in a chopping one, so a single-baseline result is a regime report wearing a comparison's clothes. A handful of cheap deterministic arms bracket the regime and separate which part of the LLM's job actually matters.

**What.** New `src/thepit/agent/baselines.py` with a `BASELINES: dict[str, Baseline]` registry, each member exposing `plan(symbols)` and `decide(conn, symbols, quotes, positions, budget)` matching the existing stub signature. Members: `momentum_5m` (the current rule, moved verbatim), `reversion_5m` (same parameters, opposite sign — running both bounds the regime), `random_levels` (seeded random symbol with the stop/target distribution the LLM actually used that day, isolating symbol selection from level structure), `buy_hold_equal` (one entry per name at the first tick, flatten at the end, reported as a regime marker and explicitly not as the benchmark), `flat` (never trades — the zero floor and the check that the cost model reports what it claims), and `plan_replay` (executes the LLM's own recorded phase-1 plan levels mechanically with no ticks, isolating whether re-thinking every five minutes adds anything at all). `sessions.arm` records which. Change `cohort.Arm` from a three-valued enum to a string arm with `is_llm`/`is_baseline` helpers.

**Evidence.** src/thepit/agent/stub.py:1-18 ("It is meant to be a floor") and stub.py:30-33 (MIN_MOVE_BP/STOP_BP/TARGET_BP); docs/NOTES.md:19-25

**Blocked by.** Migration 007 (arm column)

**Risk.** `random_levels` is not reproducible unless its seed is recorded — put it in the `sessions.config` JSON, not in memory. And every extra arm multiplies sessions per slot against a fixed rate window and a fixed number of market hours; cost the schedule before adding a fifth arm.


## Cross-cutting: security, budget, contracts, hygiene

### Exclude sessions whose model output never parsed from the arm means
*small* · [back to checklist](#the-checklist)

**Why.** `eval/report.py` already detects the failure — `unparsed_ticks` and the `broken_model` flat reason exist — but `cohort.py` does not exclude on it. A session where the CLI errored or the JSON never parsed placed no orders, and it is currently pooled into the LLM arm as a flat session. That is a harness failure being scored as a trading decision, and it biases the LLM arm toward zero in exactly the way the module's own docstring warns about for sessions with no decisions.

**What.** Add `BROKEN_MODEL = "broken_model"` to the exclusion constants in `src/thepit/eval/cohort.py` (alongside NO_FILLS, UNKNOWN_ARM, MIXED_TIER at cohort.py:44-52), set it in `meta()` when the session's tick decisions are all unparsed or errored, and report the count in the exclusions block ahead of the means. Keep the session visible in the per-session view — it is diagnostic — but out of the denominator. Test with a session whose three tick decisions all have `parsed IS NULL`.

**Evidence.** src/thepit/eval/report.py:267-268 (`unparsed_ticks`), report.py:305-306 (`return "broken_model"`); src/thepit/eval/cohort.py:44-52 (exclusion constants, no equivalent); cohort.py:8-12 ("A session with no decisions is not an LLM session… the misclassification pushes the comparison in the flattering direction").

**Risk.** Excluding too eagerly hides a real finding — that this model configuration cannot produce parseable JSON reliably. Report the exclusion count prominently rather than silently dropping.

### Report model spend and token use per session and per cohort
*small* · [back to checklist](#the-checklist)

**Why.** `decisions.tokens_in` and `tokens_out` are written on every call and read by nothing. `cost_usd` is summed into `ModelUse.usd` per session but never aggregated across a cohort. The claude.py comment says the cost is captured specifically so "P&L net of token spend stays measurable as a notional cost" — the measurement was never built. At the frequencies NOTES.md describes, notional model cost is the same order as the P&L being measured, and a result that ignores it is not a result.

**What.** Add tokens to `ModelUse` in `src/thepit/eval/report.py:254-274` (in, out, and calls-per-session). In the cohort block, print total notional spend per arm and P&L net of it, clearly labelled notional because the CLI bills against a subscription. Add a `dollars per basis point of P&L` line so an expensive arm cannot look free. Surface the same two numbers in `tradectl eval` and in the session detail panel next to the existing spend figure.

**Evidence.** src/thepit/agent/claude.py:228-233 ("Reported even on the subscription, so 'P&L net of token spend' stays measurable"); grep for `tokens_in` shows writes at runner.py:892-895 and a schema column, no reads; src/thepit/eval/report.py:273 sums only `cost_usd`; web/index.html:584 computes a per-session spend and nothing else does.

**Risk.** Presenting notional subscription cost as realised cost would be dishonest in the other direction. Label it every time it is printed.

### Define, document and test what the kill switch does to an open position
*small* · [back to checklist](#the-checklist)

**Why.** KILL is the brake and its semantics are only half-specified. `check()` treats it as absolute, so a killed session cannot close what it holds; `_flatten_until_flat` notices this and gives up after one attempt with a message. The FLATTEN file — the mechanism that would mean "close everything, then stop" — is defined in `killswitch.py` and read by nothing at all. So the honest current behaviour is "engaging the kill leaves your position open until you close it by hand", and that is written down nowhere Baron would read it.

**What.** Write `docs/RISK.md` stating the three states plainly: KILL = stop placing orders, positions stay as they are, close by hand; FLATTEN = close everything then stop; loss limit = halt but closing orders still allowed (the bypass at book.py:326-332). Then make FLATTEN real: `KillSwitch.flatten_requested()` gets a caller — checked in `session/fastloop.py`'s pass and in `runner._stopped()` — which drives the flatten path without engaging the kill. Add tests: a killed session with an open position ends `halted` naming the symbol; a FLATTEN'd session ends flat and then stops.

**Evidence.** src/thepit/engine/killswitch.py:72-77 (`flatten_requested` has no callers anywhere in src/); src/thepit/trading/book.py:308-309 (kill is absolute) and book.py:326-332 (the halted bypass that exists for exactly this reason); src/thepit/session/runner.py:688-694 ("Kill switch is engaged, so X cannot be closed by this session"); README.md:123 lists the standalone flatten brake as not built.

**Risk.** Making FLATTEN close positions automatically is a mechanism that trades without being asked. It must be a separate file from KILL, never implied by it, and it must not clear itself.

### Write docs/DATA-SOURCES.md: what each endpoint is, and on what terms
*small* · [back to checklist](#the-checklist)

**Why.** This is a public repository whose setup instructions point a stranger's machine at two third-party endpoints and archive the responses to disk. Yahoo's chart endpoint is an undocumented internal API with no published terms for this use; SEC EDGAR has an explicit fair-access policy that the code already honours with a contact email and request pacing. None of that is written down anywhere a user or a future session would find it, and the raw archive is a redistribution question nobody has stated an answer to.

**What.** Create docs/DATA-SOURCES.md with one section per source: the exact endpoint, what is fetched and how often (`config.quote_interval_open_s` / `bar_interval_s` / `news_interval_s`), the rate policy honoured and where it lives in code (`feeds/http.py::_pace`, `edgar.MIN_REQUEST_INTERVAL_S`), the identification sent (`USER_AGENT`), what is stored raw and for how long (`raw_retention_days`), and the explicit statement that raw recordings are local-only and never committed or redistributed. Link it from README's Setup section next to the THEPIT_CONTACT_EMAIL instruction. Add the same statement to the LICENSE entry's outcome so the license covers the code, not the data.

**Evidence.** src/thepit/feeds/http.py:26-47 (SEC contact-email requirement, verified 2026-07-29); src/thepit/feeds/http.py:88-98 (SEC pacing); CLAUDE-READ-THIS.md:132-142 (Yahoo quirks, undocumented endpoint behaviour); .gitignore:20-27 ("Recorded feed responses. This is the dataset"); README.md:215-217 ("License: TBD").

**Blocked by.** the LICENSE entry — do them in the same pass

**Risk.** Writing terms of use inaccurately is worse than not writing them. Quote the SEC policy and describe Yahoo factually as undocumented rather than asserting permission.

### Assert the system clock at startup and record skew
*small* · [back to checklist](#the-checklist)

**Why.** Every measurement in the project is a difference between timestamps taken from two clocks: the provider's `ts_ms` and this machine's `received_ms`. Feed latency, quote staleness rejections, stop lateness, and the enforcement slippage numbers are all wall-clock differences. A Windows box whose time service has drifted by a minute produces plausible, wrong numbers everywhere and nothing would notice — the fill would just look late, or a fresh quote would look stale enough to reject every order.

**What.** On engine startup, after the first successful fetch, compare the response's HTTP `Date` header against the local clock in `feeds/http.py` (it is already parsing responses) and record the delta as an `events` row of kind `clock_skew`. Refuse to start, with the delta named, above a threshold (say 30s, which is a quarter of `Limits.max_quote_age_s`). Surface the latest skew in `tradectl status` and `/api/status`. Add the same check to `eval` so a cohort recorded during a skew episode is flagged.

**Evidence.** src/thepit/store/schema/001_init.sql ticks table: "`ts_ms` -> `received_ms` is feed latency"; src/thepit/trading/book.py:315-319 (staleness rejection computed from `now_ms - quote.received_ms`); src/thepit/store/schema/005_provenance.sql fills.quote_ts_ms ("so the gap between 'the tape breached the level' and 'we acted' becomes a measurement"); nothing anywhere reads a `Date` header.

**Risk.** HTTP Date has one-second resolution and network delay, so the threshold must be generous — this catches minutes of drift, not milliseconds.

### Give the model call a retry and a stated policy for a mid-session outage
*small* · [back to checklist](#the-checklist)

**Why.** One transient CLI failure costs a whole policy tick: `_ask` returns None, `_tick` returns, and the session does nothing until the next interval — which on a 30-minute session is a sixth of its decisions. A `ClaudeUnavailable` mid-session halts the session outright and, because the flatten runs against whatever the risk layer allows, that can end holding. Neither behaviour is written down, and the eval module cannot distinguish "the agent chose not to act" from "the harness lost a turn".

**What.** In `src/thepit/session/runner.py::_ask`: retry once on a transport-shaped failure (non-zero exit, unparseable output) with a short backoff, never on a rate-limit result, and never for the review phase. Record the attempt count on the decision row (`decisions.attempts`, migration 007+) so lost turns are countable. Define the mid-session unavailability policy in one place: stop asking, do not silently substitute the stub (that would relabel the arm), flatten and finish. Add a `lost_ticks` figure to `ModelUse` in eval/report.py.

**Evidence.** src/thepit/session/runner.py:856-906 (`_ask`: no retry; `is_error` → None; `ClaudeUnavailable` → `self._halted`); src/thepit/session/runner.py:406-413 (a None result silently skips the tick); src/thepit/api/main.py:475-478 (stub substitution at start time, which the research area separately wants refused).

**Blocked by.** the rate-window classification entry — a retry against an exhausted window makes it worse

**Risk.** Retrying a call that timed out at 180s doubles the tick's wall time and can overrun the interval. Cap total time in `_ask`, not just per attempt.

### Decide what the LAN listener is, and secure or document it accordingly
*small* · [back to checklist](#the-checklist)

**Why.** `--lan` binds 0.0.0.0 with no authentication of any kind. It is read-only, but what it reads out is every prompt the agent was given, every response, the operator's notes, positions and capital. "Nothing here goes on the public internet" is a comment, not a control — on a coffee-shop network or a home network with a guest SSID, this is an open endpoint. The config also carries a `lan_host` setting that the argv path ignores entirely, so the one knob that could restrict the bind does nothing.

**What.** Pick one of two answers and implement it fully. Either (a) bind to `config.lan_host` when set rather than unconditional `0.0.0.0`, require a shared token in a header for every route on that listener, and print the token at startup; or (b) keep it open and make the log line and the README say plainly that anyone on the network can read the sessions. Also decide whether `/api/session/preview` belongs on the read router at all — it renders a full prompt and runs bar queries per request, which is both an information leak and an unauthenticated compute path.

**Evidence.** src/thepit/api/main.py:647-651 (`host = "0.0.0.0"  # noqa: S104`, `config.lan_host` unused in `main()`); src/thepit/config.py:47-50 (`lan_host` documented as the safe default that disables remote viewing — nothing reads it); src/thepit/api/main.py:374-425 (`session_preview` on the read router).

**Risk.** A token scheme nobody uses will be worked around with a hardcoded value. If (a) is chosen, generate the token into the state dir and print it, do not invent a login.

### Give session defaults one source of truth
*small* · [back to checklist](#the-checklist)

**Why.** Three entry points disagree about what a session is. `SessionConfig` defaults to $10,000 / 20% / 3 concurrent / −2%. The dashboard form defaults to $20 / risk_it / 100% / 1 / −60%. The API's `_parse_session` re-specifies every default inline, so a payload that omits a field silently gets the $10k profile. The README's Limits table documents the first set as the defaults while CLAUDE-READ-THIS says risk_it is the default "for exactly that reason". When `tradectl session start` is added it will pick a fourth answer, and sessions run from different surfaces will not be comparable.

**What.** Delete the literal defaults from `api/main.py::_parse_session` (main.py:527-556) and let `SessionConfig` supply them; change `SessionConfig`'s own defaults to the stated intent ($20, `risk_it` applied) or add a `SessionConfig.default()` factory that applies the profile. Have web/index.html render its initial values from `/api/session/preview` of an empty payload instead of hardcoding them at index.html:205-222. Correct README.md:86-99 so the table shows the actual default profile. Test that an empty POST body and the dashboard's default form produce identical `SessionConfig` objects.

**Evidence.** src/thepit/session/config.py:70,89-92 (capital 10_000, 20/3/2); web/index.html:205-221 (value="20", risk_it selected, 100/1/60); src/thepit/api/main.py:531-547 (defaults restated inline); CLAUDE-READ-THIS.md:17-19 vs README.md:86-99.

**Risk.** Changing SessionConfig's defaults changes what old test fixtures construct. Update tests in the same commit rather than adding a compatibility shim.

### Turn on a real ruff rule set so the noqa codes mean something
*small* · [back to checklist](#the-checklist)

**Why.** The source carries `# noqa: BLE001`, `# noqa: S104` and similar suppressions in the places where the author deliberately swallowed an exception or bound to all interfaces. Those are the most safety-relevant lines in the codebase and the rules they suppress are not enabled — `[tool.ruff]` sets only line-length and target-version, and ruff is not even a dependency. So the suppressions document intent to a linter that never runs, and a new blind `except Exception` added tomorrow is invisible.

**What.** Add `ruff` to the dev dependency group in pyproject.toml (merging with the ops entry that only asks for the dependency). Add `[tool.ruff.lint] select = ["E","F","W","B","S","BLE","ASYNC","RUF"]` with `RUF100` so unused suppressions fail, and `[tool.ruff.lint.per-file-ignores]` relaxing `S` for tests. Fix or explicitly suppress what it finds — expect hits on the blind excepts in poller.py, runner.py and engine/main.py, all of which are deliberate and should keep a noqa that now means something. Run it in CI.

**Evidence.** pyproject.toml [tool.ruff] has only line-length and target-version, and [dependency-groups].dev has pytest and pytest-asyncio only; suppressions at src/thepit/api/main.py:648 (S104), engine/poller.py:162,201,223,276 (BLE001), session/runner.py:258 (BLE001).

**Blocked by.** the CI workflow entry — a linter that only runs when someone remembers is a linter that does not run

**Risk.** Selecting too many rules produces hundreds of findings and the whole thing gets reverted. Start with the listed set, land the fixes, widen later.

### Alert when the engine dies, a feed goes dark, or a session halts holding
*medium* · [back to checklist](#the-checklist)

**Why.** Everything is observable and nothing is observed. The engine writes a HEARTBEAT file, the poller emits `feed_degraded` events, the API computes `engine_alive`, and all of it only exists if a human happens to be looking at the dashboard or runs `tradectl status`. The failure mode this project is most exposed to — recording silently stops overnight, or a session ends `halted` still holding stock — is precisely the one nobody is in front of a screen for.

**What.** Add `tradectl watch` (or a small `engine/alerts.py` task): on a 60-second timer, check heartbeat age, per-source freshness from `fetch_log`, and any session in `halted` with "still holding" in `halt_reason`; on transition into a bad state, fire a notification once and a recovery notification once. Ship one delivery mechanism that works unattended on this machine — a Windows toast, or a webhook the operator configures in config.toml (Atlas already runs on this PC and takes messages). Define the error budget explicitly in docs/RISK.md: what fraction of open-market minutes without a tick counts as an outage worth waking up for.

**Evidence.** src/thepit/engine/killswitch.py:113-122 (`beat`/`heartbeat_age_s`, "An external supervisor watches its mtime" — there is no external supervisor); src/thepit/api/main.py:150-154 (`engine_alive`, dashboard-only); src/thepit/engine/poller.py:315-322 (`feed_degraded` events written to the DB and read by the dashboard only).

**Blocked by.** the Scheduled Task pair (an alerter that only runs while a terminal is open solves nothing)

**Risk.** An alerter that cries wolf overnight when the market is closed gets muted permanently. Gate price-feed alerts on `calendar.is_open`.


---

# LATER


## Trading, risk and the session loop

### Cover multi-symbol and multi-position behaviour in the fast loop tests
*small* · [back to checklist](#the-checklist)

**Why.** Every test in `tests/test_fastloop.py` holds at most one position at a time. The whole file exercises AAPL alone or TSLA alone. Nothing tests two plans breaching in the same pass, whether the order of firing is deterministic, whether one symbol's dead feed stops enforcement on the other, or whether the concurrency cap and the cash check interact correctly when two positions are open — which is the default configuration under `preserve` and `balanced`.

**What.** Add to `tests/test_fastloop.py`: (a) two positions, both through their stops in one `step()`, asserting the returned list is ordered by symbol (`plans()` sorts, so this pins it) and both close; (b) AAPL's quote goes stale while TSLA's is fresh — assert TSLA is still enforced and only AAPL logs the dead-feed error; (c) with `max_concurrent_positions=2` and two positions open, a third opening order is rejected but a reducing order on either existing symbol still passes; (d) two positions where closing the first frees the cash the second needs; (e) an armed entry on a symbol already held, asserting `attach` carries the ratchet forward rather than resetting it.

**Evidence.** tests/test_fastloop.py:59 the fixture provides AAPL and TSLA quotes, but every test trades exactly one of them; tests/test_fastloop.py:447 `test_adding_to_a_position_keeps_the_ratcheted_stop_and_the_blended_entry` is the only multi-fill test and is single-symbol; src/thepit/session/fastloop.py:275-276 `ORDER BY symbol` is the ordering nothing asserts.

**Risk.** None beyond test maintenance. Note that (c) and (d) will likely surface the armed-capital problem described in the reserve-capital entry; if so, split the finding out rather than weakening the assertion.

### Property-test Book.apply against a replayed ledger
*small* · [back to checklist](#the-checklist)

**Why.** `Book.apply` is the arithmetic every number in the project rests on, and it is covered by four hand-picked example tests. Its hardest branch — reduce-or-reverse, with the average price rewritten only on a true reversal — is exercised once, by a partial close that never reverses. Fractional quantities make float dust a real concern (`eval/pnl.py` had to define a DUST constant because of it), and a cash-versus-fills divergence is what makes a session unscorable.

**What.** Add `tests/test_book_properties.py` driving randomised sequences of buy/sell at random fractional quantities and prices through `Book.apply`, asserting after each step: (1) cash equals the starting capital minus the signed sum of `qty * price` over all fills; (2) `sum(realized) + unrealized` at the last price equals `equity - capital`; (3) `avg_price` is unchanged by any reduction that does not cross zero; (4) a full close leaves `abs(qty) < DUST`. Use `random.Random(seed)` with a fixed seed rather than adding a hypothesis dependency — the project takes new dependencies seriously (pyproject.toml documents four deliberate exclusions). Reuse `Book.rebuild_from_fills` as the oracle once it exists.

**Evidence.** tests/test_book.py:91-119 — four position-accounting tests, none of which reverses a position; src/thepit/trading/book.py:194-203 the reduce-or-reverse branch; src/thepit/eval/pnl.py:DUST "Fractional shares make an exact-zero test unsafe: a buy of 0.178042 and a sell of the same leaves float dust."

**Blocked by.** Implement the fills-versus-positions boot check the schema says exists

**Risk.** Randomised tests that fail intermittently get muted. Fix the seed, print the failing sequence on assertion, and keep the generated sequences short enough to read.

### Write docs/RISK.md and keep the README's "Not built" list honest
*small* · [back to checklist](#the-checklist)

**Why.** The rejection vocabulary is spread across `Reject` in book.py, ad-hoc strings in `runner._reject`, error strings returned by `levels.resolve`, and the `origin` vocabulary in `005_provenance.sql`. There is no single page saying what can reject an order and why, which is the first thing a new session needs before touching this area — and the README's "Not built" list is the load-bearing statement of scope that several of these entries will falsify one at a time.

**What.** Add `docs/RISK.md`: one table of every rejection — constant name, exact user-visible string, which layer raises it, whether it applies to reducing orders, and the incident that motivated it. Add a second table for `origin` values, cross-referenced to `eval/trades.py`'s vocabulary check. Add a short "where a position can exist unwatched" section listing the paths and the mitigation for each. Then add a test in `tests/test_docs.py` that every member of `book.Reject` appears in RISK.md, so the doc cannot silently rot. Finally, add a checklist item to the README's Limits and Safety sections requiring the "Not built" lists to be edited in the same commit that builds one.

**Evidence.** src/thepit/trading/book.py:41-52 `class Reject(StrEnum)`; src/thepit/store/schema/005_provenance.sql:22-28 the origin vocabulary as a comment; README.md:86-103 the Limits table and its "Not built" paragraph; src/thepit/eval/trades.py:35-56 the second, independent copy of the origin vocabulary.

**Risk.** A doc that duplicates code drifts. The test that asserts coverage is what makes it worth writing; without it this entry produces a file that is wrong within two commits.

### Make prompt changes measurable with a variant registry
*medium* · [back to checklist](#the-checklist)

**Why.** The prompt is described as the highest-leverage file in the repo, and there is no way to test that claim. Two documented prompt failures ("an empty order list is valid and often correct" producing a zero-trade session, and a 5bp/side cost hurdle producing rational inaction) were each found by reading a review, not by measurement. Every future prompt edit has the same status: an opinion.

**What.** 1) Add `session/variants.py`: a dict of named prompt variants, each a small set of overrides (cost framing, the flat-session paragraph, whether the sizing block appears, whether market context is in the tick prompt). 2) Add `prompt_variant: str = 'default'` to `SessionConfig`, recorded in `_config_json` and in the `prompt_version` column. 3) In `eval/report.py`, add a variant arm to the existing arm comparison — it already computes Wilson intervals and prints the sessions-per-arm the observed spread would need, so the honest-n machinery is reusable as-is. 4) `tradectl eval --by variant`. 5) Assert in tests that every variant still contains the non-negotiable lines the existing tests pin: "earned nothing", "last resort", no "valid and often correct", and the cost number.

**Evidence.** CLAUDE-READ-THIS.md: "Never tell the agent that doing nothing is fine ... There is a test asserting that phrase never returns."; tests/test_session.py:147-160 `test_flat_session_is_framed_as_a_failure_not_prudence`; src/thepit/eval/report.py:14 and the arm-comparison machinery it already has.

**Blocked by.** Move the whole agent-facing text into prompt.py and stamp it with a version

**Risk.** Testing many prompt variants is exactly the multiple-comparisons problem NOTES.md warns about — "testing many configurations makes it worse". Fix the variant set before collecting, register the comparison in advance, and let the report print the required n rather than the winner.

### Model partial fills
*large* · [back to checklist](#the-checklist)

**Why.** `simulate_fill` always fills the entire quantity at one price. The schema already anticipates otherwise: `orders.status` permits 'partial' and `orders.filled_qty` exists, and both are unused — every order is written as 'filled' with `filled_qty = qty`. NOTES.md lists partial fills among the unmodeled negatives that all point the same way, and the downstream consequences are real: `FastLoop.protect` attaches a plan sized to a fill that may not have happened, and `_fire` closes `abs(qty)` from the book rather than from the order.

**What.** 1) Change `simulate_fill` to return a fill quantity capped by available liquidity: `min(qty, participation_pct * bar_volume_in_interval)` using the latest `bars.v` for the symbol, with `participation_pct` a new constant beside `ASSUMED_SLIPPAGE_BP` and documented as a guess at this data tier. 2) In `runner._submit`, write `status='partial'`, `filled_qty=<actual>` when the fill is short, and return the partial `Fill`. 3) Decide and document the residual policy — cancel the remainder (simplest, matches a market order) rather than resting it. 4) `FastLoop.protect` already reads the position's blended cost, so it is correct as-is; add a test that proves it. 5) `_flatten` must loop until flat rather than assuming one order closes the position — it already returns what is still held, so `_flatten_until_flat` covers it, but add an explicit test. 6) Tests in `tests/test_book.py` and `tests/test_fastloop.py`.

**Evidence.** src/thepit/trading/book.py:232-263 `simulate_fill` returns `qty=qty` unconditionally; src/thepit/store/schema/002_trading.sql:65 `CHECK (status IN ('proposed','rejected','filled','partial','cancelled'))` with 'partial' written nowhere in src/; src/thepit/session/runner.py:665-668 always writes `status='filled'`; docs/NOTES.md:36 lists "partial fills" among unmodeled negatives.

**Risk.** Partial fills change every historical comparison: a cohort spanning the change mixes two fill models, exactly what `sim_tier` exists to prevent. Introduce it as a new tier value rather than silently changing the 'bars' tier, so `cohort.require_single_tier` raises instead of averaging.


## Measurement and the eval module

### Compute the conviction p-value or delete the field
*small* · [back to checklist](#the-checklist)

**Why.** `Conviction.p_value` is a declared field and `_conviction` hardcodes it to `None` at the only place it is constructed, so the report advertises a number that can never appear. `tradectl eval` prints tau with no p at all. A permanently-dead field on a measurement report is worse than a missing one: it reads as "not enough data yet" rather than "not implemented".

**What.** Either implement it — a permutation test on tau, shuffling `nets` against `convictions` with the same trials-and-seed discipline as `stats.permutation_p`, exposed as `stats.permutation_tau_p(xs, ys, *, trials, seed)` and gated on the same `min_n_for_correlation` threshold that already gates tau — or remove `p_value` from the `Conviction` dataclass at report.py:63-69 and from the constructor. If implemented, it joins the family in the multiplicity entry. Add a test asserting it returns `None` below the threshold, matching the pattern of `test_no_correlation_from_three_episodes`.

**Evidence.** src/thepit/eval/report.py:63-69 declares `p_value: float | None`; src/thepit/eval/report.py:345-349 constructs `Conviction(... p_value=None, ...)`; src/thepit/cli/tradectl.py:277-279 prints tau only.

**Risk.** None.

### Version the metric definitions so a formula change is dated
*small* · [back to checklist](#the-checklist)

**Why.** Over months the formulas will change — the mark staleness bound, the thinking-share denominator, the discipline counter and the tier partition are all on this roadmap. A cohort report that mixes sessions scored under two definitions of the same metric is unfalsifiable, and today nothing records which definition produced any number. There is also no single document defining what each field means, so a future session reads the formula out of the code and reimplements a fourth P&L by accident.

**What.** Add `EVAL_SCHEMA_VERSION = 1` to `src/thepit/eval/__init__.py` beside the `__all__` block, bump it in the same commit as any formula change, and stamp it into `report.to_dict` and every exported CSV/JSON row. Write `docs/METRICS.md` with one section per field of `SessionPnL`, `Costs`, `ArmSummary`, `Conviction`, `LevelFill`, `ArmedOutcome`, `Blindness` and `ModelUse`, each giving the formula, the exclusions that apply, the n below which it is withheld, and the version in which it last changed. Link it from the "What the eval module refuses to answer" section of docs/NOTES.md, which currently states the refusals but defines none of the numbers that survive them.

**Evidence.** src/thepit/eval/__init__.py:16-26 exports names with no version; docs/NOTES.md:61-87 documents refusals only; CLAUDE-READ-THIS.md: "There is now exactly one implementation — `eval/pnl.py:session_pnl` ... Do not write a fourth."

**Risk.** A version number nobody bumps is worse than none, because it asserts stability that is not there. Tie the bump to the fixture corpus: any change to an expected-report JSON without a version bump fails the corpus test.

### Compute each session's numbers once per cohort run
*small* · [back to checklist](#the-checklist)

**Why.** `cohort_report` calls `pnl.session_pnl` for every session at least four separate times — inside `cohort.meta` via `all_meta`, again building `by_arm`, twice per pair building `paired_diffs`, and again building `flat` — and calls `enforcement.level_fills` twice, once for slippage and once for lateness, and `trades.episodes` again in `_conviction`. Each `session_pnl` runs several queries including one `ticks` lookup per held symbol. Beyond the wasted work, two call sites that later diverge on an `at_ms` argument would report two different P&Ls for one session inside one report.

**What.** Build `dict[int, SessionReport]` once at the top of `cohort_report` (report.py:173-248) and have `by_arm`, `paired_diffs`, `flat`, `_conviction`, `_level_slippage` and `_lateness` all read from it. Change those helpers to accept `list[SessionReport]` instead of `list[SessionMeta]` plus a connection, which also removes their ability to re-query with a different mark instant. `cohort.meta` already computes a `SessionPnL` internally (cohort.py:120) — hand that one out rather than recomputing.

**Evidence.** src/thepit/eval/report.py:192, 211, 229 (three `session_pnl` call sites in one function), report.py:355 and 371 (two `level_fills` passes), report.py:332 (`trades.episodes` again); src/thepit/eval/pnl.py:151-157 issues one `mark_at` query per held symbol per call.

**Risk.** Caching a report per session hides a real difference if any caller legitimately needs a different `at_ms` — the live dashboard does, at api/main.py:334. Keep `session_pnl` freely callable with `at_ms`; cache only inside the cohort path, which by definition marks at each session's own clock.

### Count the metric family and adjust alpha as the metric count grows
*medium* · [back to checklist](#the-checklist)

**Why.** One `cohort_report` call already produces a permutation p, a sign-test p, a Kendall tau, a Wilson interval on the flat rate, a Wilson interval on every session's win rate and a per-exit-bucket breakdown — and this roadmap adds more. Reading whichever one crosses 0.05 is the same failure NOTES.md describes at the session level ("run twenty and one will look brilliant on noise alone") applied to metrics instead of sessions, and nothing in the module counts how many tests were run.

**What.** Add `stats.holm_bonferroni(ps: dict[str, float]) -> dict[str, float]` returning step-down adjusted p-values. Add `CohortReport.family: dict[str, float]` collecting every p the run computed, and have `tradectl eval` print "k tests in this family; 0.05 adjusted to X" beside each. When a pre-registration exists, report only its `primary_metric` unadjusted and label the rest exploratory. Also derive `stats.permutation_p`'s seed per metric — `seed=20260729` at stats.py:119 is a single module constant, so two different metrics currently share one permutation ordering, which correlates their p-values in a way nothing accounts for.

**Evidence.** src/thepit/eval/report.py:240-241 (`permutation_p`, `sign_test_p`) and report.py:347 (`kendall_tau_b`) produce independent p-like quantities in one call; src/thepit/eval/stats.py:117-141.

**Blocked by.** Pre-register a primary metric before a cohort runs

**Risk.** Holm over a family this small at n=4 makes every result non-significant, which is arithmetically correct and will read as the tool being broken. Print the unadjusted and adjusted values together with the family size so the reason is on screen, the same way `sessions_needed` is printed beside the observed n.

### Score the intra-session equity curve that is already being recorded
*medium* · [back to checklist](#the-checklist)

**Why.** `FastLoop.step` snapshots equity every interval into the `equity` table and the eval module never reads it. So the report cannot say how close a session came to its halt limit, what its maximum intra-session drawdown was, how much of the window it actually held exposure, or whether a session that finished up was ever badly underwater — all answerable today from data already on disk, and all relevant to whether a positive P&L was survivable rather than lucky.

**What.** New `src/thepit/eval/curve.py`, read-only like the rest of the package: `curve(conn, session_id) -> list[tuple[int, float]]` from `SELECT ts_ms, equity FROM equity WHERE session_id=? ORDER BY ts_ms`, and `CurveStats(points, max_drawdown_bp, worst_equity_bp, time_in_market_pct, closest_to_halt_pct)` where `closest_to_halt_pct` is the deepest loss as a fraction of `SessionMeta.session_loss_limit_pct`. Attach as `SessionReport.curve` and add a cohort median. Return `None` for every statistic below ten snapshots, matching the module's habit of refusing what the sample cannot support. Document in the docstring that the curve is only as dense as `fast_loop_seconds` and that a stale-quote interval writes an unchanged equity rather than a gap, so a flat stretch can mean either.

**Evidence.** src/thepit/store/schema/002_trading.sql:111-119 defines `equity`; src/thepit/session/fastloop.py:95-99 and 322-324 write a snapshot every pass; `grep "FROM equity" src/thepit/eval/` returns nothing.

**Blocked by.** Carry the risk limits on SessionMeta and refuse to pool arms across them

**Risk.** The curve is marked at whatever quote the fast loop held, which can be up to `MAX_QUOTE_AGE_S` old, so drawdown measured from it is not the drawdown that occurred. It is a lower bound on severity, and reporting it as the drawdown repeats the enforced-stop-is-not-a-venue-stop error in a new place.

### Fuzz the episode fold against the book's own realised P&L
*medium* · [back to checklist](#the-checklist)

**Why.** The fold's rule — a new episode begins when quantity crosses back through zero — has to match `Book.apply` exactly, and one test covers one shape: buy, stop out. Nothing tests reduce-then-add-then-reduce, a stream that crosses zero to the opposite side, or the `DUST` boundary where a buy of 0.178042 and a sell of the same must close the episode rather than leave a residual. Every conviction correlation, win rate, hold time and exit attribution in the report is computed on top of this fold.

**What.** Add a seeded property test to tests/test_eval.py (no new dependency — a `random.Random(fixed_seed)` loop over ~500 generated cases): generate fill streams of buys and sells with fractional quantities across two symbols, apply them through `Book.apply`, fold with `trades.episodes`, and assert (a) the sum of `net` over closed episodes equals the sum of `positions.realized` per symbol to 1e-9, (b) `sum(e.legs)` equals the fill count, (c) at most one open episode per symbol, and (d) an episode whose residual is under `DUST` is reported closed with a `closed_ms`. Generate at least one case with a buy and a sell of identical fractional quantity to hit the dust path directly.

**Evidence.** tests/test_eval.py:175-190 is the only fold-versus-book test and covers one buy and one stop; src/thepit/eval/trades.py:8-12: "it has to match `Book.apply` exactly"; src/thepit/eval/trades.py:166 `if abs(current["qty"]) < DUST`.

**Risk.** Random streams will generate orders the risk layer would reject, so the test has to write fills directly rather than going through `_submit` — which means it tests the fold against `Book.apply` but not against the risk path. That is the right scope; state it in the test docstring so nobody later reads a passing fuzz test as evidence the whole order path is sound.


## Feeds, storage and the engine

### Record and report feed latency, which the schema was built to measure
*small* · [back to checklist](#the-checklist)

**Why.** `ticks` stores both the provider's timestamp and ours specifically so the gap between them is measurable, and the type exposes it as `age_ms`. Nothing computes it. "How late is my price feed" bounds what any strategy in this system could possibly have captured, and it is the number that distinguishes the Yahoo tier from the Alpaca tier in practice rather than in a docstring.

**What.** Add a `latency` section to `FetchLogRepo.uptime` or a new `TicksRepo.latency(since_ms, until_ms)` in src/thepit/store/repos.py computing p50/p95/max of `received_ms - ts_ms` per symbol and source. Print it in `tradectl uptime` (src/thepit/cli/tradectl.py:370-420) beside the existing HTTP latency percentiles, which measure something different (round-trip time, not data age) and are currently the only latency shown. Add it to `/api/uptime`. Note in the output that Yahoo's `regularMarketTime` is the last trade print, so its latency figure conflates feed delay with symbol illiquidity — say which, rather than reporting one number that means two things.

**Evidence.** src/thepit/store/schema/001_init.sql:76 `received_ms INTEGER NOT NULL,          -- ts_ms -> received_ms is feed latency`; src/thepit/core/types.py:90-93 `age_ms` — no caller in src/. `FetchLogRepo.uptime` (src/thepit/store/repos.py:208) reports `latency_ms`, which is the HTTP round trip.

**Risk.** Reporting a single latency number across a mixed-source ticks table averages two feeds with different semantics into a meaningless figure — group by source from the start.

### Wire MarketView into something or delete it
*small* · [back to checklist](#the-checklist)

**Why.** The poller's in-memory cache is populated on every cycle and read by nothing. The module docstring says agents read it, which is false — the session runner reads `ticks` from the database in the API process. Worse, `stale_symbols` cannot work as documented: `update()` stores quotes whose `received_ms` is the poll time, so a provider serving a frozen price refreshes the freshness stamp on every cycle and the method returns empty forever. It has a passing test only because the test constructs the quotes by hand.

**What.** Decide which. If it stays: change `MarketView.update` to keep the previously seen `received_ms` when `(symbol, ts_ms, source)` is unchanged, so `stale_symbols` measures what it claims, and give it a consumer — the natural one is a `stale_symbols` check in the poller emitting a `feed_frozen` event, which is a failure mode nothing currently detects. If it goes: delete the class, the `self.view` attribute (src/thepit/engine/poller.py:125) and the `view.update` call (line 290), and correct the module docstring at src/thepit/engine/poller.py:3-4. Either way fix the docstring, which describes an architecture the code does not have.

**Evidence.** src/thepit/engine/poller.py:3-4 "Agents never touch the network -- they read :class:`MarketView`" against src/thepit/api/main.py:488 `runner.update_quotes(_quotes_for(wconn, symbols))` reading the `ticks` table. A grep for `stale_symbols` outside src/thepit/engine/poller.py returns only tests/test_poller.py:102-103.

**Risk.** Deleting it removes the only in-process price cache, which a future in-engine session runner would want back. Keeping it costs almost nothing; the real defect is the docstring claiming it is load-bearing when it is not.

### Measure the disagreement between two price sources
*medium* · [back to checklist](#the-checklist)

**Why.** `source` is in the bars primary key specifically so two feeds' versions of the same candle coexist and the difference can be measured — the schema comment says so. Nothing measures it. Once Alpaca is connected this is the cheapest available check on data quality, and it is the only way to know whether the IEX-only free tier is materially different from Yahoo's consolidated print before any result is built on it.

**What.** Add `src/thepit/eval/feeds.py` with a `disagreement(conn, symbol, tf, since_ms, until_ms)` returning per-bar close differences in basis points between two sources, plus p50/p95/max and the count of bars present in one source and absent from the other. Add `tradectl feeds compare --since 1d`. Report the coverage asymmetry separately from the price difference — a bar missing from IEX and present in Yahoo is a different problem from the same bar priced differently.

**Evidence.** src/thepit/store/schema/001_init.sql:29-32 "`source` is part of the primary key on purpose ... keeping both lets us measure the disagreement, which is a real data-quality signal" — nothing in src/thepit reads more than one source. src/thepit/feeds/alpaca.py:17-21 "the free tier is IEX only -- roughly 2.5% of US equity volume, and not the NBBO."

**Blocked by.** Alpaca credentials existing so both feeds record simultaneously (issue #11) — Baron's action, not a code change

**Risk.** Running both feeds at once doubles rate-limit pressure on the source documented as throttling bursts hard. Run the comparison over a bounded window with both feeds explicitly enabled rather than leaving dual recording on permanently.

### Decide what happens to stored bars across a split or dividend
*large* · [back to checklist](#the-checklist)

**Why.** Alpaca bars are requested with split adjustment and Yahoo bars with none, and stored rows are never revised because the conflict clause does nothing. So a split re-prices every future fetch while the already-stored history keeps pre-split prices, leaving a 4:1 discontinuity in the middle of the series with no marker. The Alpaca adapter's own comment says an unadjusted split "read as a 75% loss" — the same failure now lives in the stored data instead of the wire.

**What.** Add an `adjustment TEXT NOT NULL DEFAULT 'none'` column to `bars` in a migration and set it from the adapter (`'split'` for Alpaca, `'none'` for Yahoo). Add a nightly reconciliation in the engine: refetch the last N trading days and compare against stored rows, emitting a `bar_discontinuity` event per symbol where the ratio between a stored close and a refetched close exceeds a threshold. Do not silently rewrite — surface it and let a human decide, consistent with the existing DO NOTHING policy. Longer term, a `corporate_actions` table (symbol, ex_date, kind, ratio) populated from EDGAR 8-K or the broker would let the series be adjusted on read. Add `tradectl doctor` to run the reconciliation on demand.

**Evidence.** src/thepit/feeds/alpaca.py:172 `"adjustment": "split",   # unadjusted splits read as a 75% loss` against src/thepit/feeds/yahoo.py:78 which passes only `range` and `interval`; src/thepit/store/repos.py:59-60 `INSERT INTO bars ... ON CONFLICT DO NOTHING`.

**Blocked by.** a decision on whether the stored series is a raw archive or an adjusted analysis series — these want opposite things and the schema currently implies the first

**Risk.** Adjusting stored history retroactively invalidates every fill price and P&L already computed against it, which would silently change past eval results. Whatever is built must leave `fills` alone and adjust only on read.


## Operations, packaging and the machine

### Ship config.toml.example and .env.example -- both are referenced and neither exists
*small* · [back to checklist](#the-checklist)

**Why.** `config.py:86-100` loads `~/.thepit/config.toml` if it exists, supporting `symbols`, `[api] host/port/lan_host/lan_port`, `[feed] quote_interval_open_s/quote_interval_closed_s/bar_interval_s/news_interval_s`, `raw_recording` and `raw_retention_days`. Nothing documents this file: it is not in the README, not created by `setup.ps1`, and there is no example. The only way to learn the key names is to read `load()`. Separately, `.gitignore:8-10` has an explicit negation `!.env.example` written to preserve a file that does not exist.

**What.** Add `docs/config.toml.example` with every key `config.load()` reads, its default, and a one-line comment on what changing it costs -- particularly `quote_interval_open_s` (5.0) and `bar_interval_s` (300.0), where `poller.py:42-47` documents that 300 vs 60 is the difference between ~150MB/day and ~40MB/day of recording. Add `.env.example` listing `THEPIT_CONTACT_EMAIL`, `THEPIT_HOME`, `ALPACA_PAPER_KEY_ID`, `ALPACA_PAPER_SECRET`, `PORT`, `NO_COLOR` with empty values and a header stating that real values never go in the repo. Have `setup.ps1` copy the config example to `~/.thepit/config.toml` if absent. Add a README "Configuration" section pointing at both.

**Evidence.** src/thepit/config.py:86-116 `load()` reads `~/.thepit/config.toml` with keys documented nowhere else; .gitignore:8-10 `.env`, `.env.*`, `!.env.example` -- and `ls .env.example` returns no such file; src/thepit/engine/poller.py:42-47 the bar-interval cost comment; README.md has no Configuration section

**Risk.** An example file listing Alpaca variable names next to paper/live naming could be misread as setup instructions for live trading. Label it clearly as paper-only and do not include a live variable name at all.

### Correct the README claims that no longer match the repo
*small* · [back to checklist](#the-checklist)

**Why.** Several statements are stale in ways that mislead a fresh session about what exists. `README.md:202` says "Not yet verified on a real Windows machine -- see issue #16" while the repo is checked out and running on Windows 10 with a populated database. `README.md:18` says "Ten sessions logged so far" against a database with zero rows in `sessions` (this is a fresh `~/.thepit`, but the README states it as fact). `CLAUDE-READ-THIS.md:29` says "184 tests" against 11 test files containing ~236 test functions. `README.md:33` says tradectl offers "status, kill, release, sessions, eval, uptime" -- correct today, but the list is duplicated in three files and will drift. The whole point of `CLAUDE-READ-THIS.md` is that a session can trust it.

**What.** Sweep `README.md`, `CLAUDE-READ-THIS.md` and `docs/NOTES.md` for counts and status claims. Replace hardcoded numbers with either a generated line or no number at all: have CI regenerate the test count, or drop it. Update the Windows verification line to state what has actually been run on Windows and what has not. Move the session count out of the README and into `tradectl sessions` output where it is computed. Add a short `docs/OPERATIONS.md` that is the single home for the run/supervise/backup/restore/kill procedure so `README.md`, `setup.ps1` and `CLAUDE-READ-THIS.md` can all point at one place instead of each carrying a copy of the command list.

**Evidence.** README.md:202 "Not yet verified on a real Windows machine -- see issue #16" (repo is on Windows 10, `.venv` built, `~/.thepit/paper/thepit.db` exists); README.md:18 "Ten sessions logged so far" vs `SELECT COUNT(*) FROM sessions` = 0; CLAUDE-READ-THIS.md:29 "184 tests" vs ~236 test functions across tests/*.py; command list duplicated at README.md:33, CLAUDE-READ-THIS.md:38-45, setup.ps1:97-100

**Risk.** Docs drift again the moment they carry a number a human has to update. Prefer deleting a number to maintaining it.

### Merge the fast-loop branch to main and set a branching convention
*small* · [back to checklist](#the-checklist)

**Why.** All the work described in `CLAUDE-READ-THIS.md` as current state -- the fast loop, the eval module, risk profiles, fractional shares -- is on a local branch `fast-loop` that is ahead of `origin/main`. The README points contributors at GitHub Issues and the clone instructions in `setup.ps1:7` fetch `main`, which does not contain any of it. Anyone (including a future session, or the Mac) following the documented setup gets a repo without the fast loop, which is the component the safety story depends on.

**What.** Merge or rebase `fast-loop` onto `main` and push. Then write the convention down in `docs/OPERATIONS.md` or `CLAUDE-READ-THIS.md`: whether `main` is expected to be the running code, whether branches are per-feature, and that `setup.ps1`'s clone line fetches whatever `main` is. Once CI exists, protect `main` so the suite must pass. Close the GitHub issues the merged work fixes -- `CLAUDE-READ-THIS.md:147-149` states Baron wants issues closed as they are fixed, via commit trailers.

**Evidence.** `git branch -a` shows `* fast-loop`, `main`, `remotes/origin/main`; `git log --oneline` most recent commits are the eval module and fast loop; setup.ps1:7 clones the default branch; CLAUDE-READ-THIS.md:147-149 "Close GitHub issues as they are fixed"

**Risk.** Pushing to a public repo. Confirm nothing gitignored slipped into a commit first -- `CLAUDE-READ-THIS.md:153-154` records that PLAN.md had to be purged from history once already.

### Add a reset/uninstall script for the runtime directory
*small* · [back to checklist](#the-checklist)

**Why.** There is no way to start clean. `~/.thepit` accumulates a database, a WAL, raw gzipped recordings, a HEARTBEAT file and (after other entries land) logs and backups. Testing a migration, reproducing a first-run bug, or recovering from `assert_healthy` refusing to boot all require deleting the right subset by hand, and deleting the wrong subset destroys the only copy of every session result. `config.py:61-65` deliberately separates data by mode so "delete all my paper data" is not a dangerous query -- but nobody wrote the safe command.

**What.** Add `ops/reset.ps1` with explicit switches, no destructive default: `-Recordings` (delete `~/.thepit/paper/raw` only), `-Logs`, `-State` (delete HEARTBEAT, refuse if KILL is present), `-Database` (requires `-Confirm` AND takes a backup first via the backup entry's `snapshot()`), `-All`. Print exactly what will be deleted with byte counts and require a typed confirmation for anything touching the database. Never touch `~/.thepit/live` (`config.py:61-65` keeps modes in separate directories precisely so this stays possible) and never touch the repo. Document it in `docs/OPERATIONS.md`.

**Evidence.** src/thepit/config.py:60-79 `data_dir` / `raw_dir` / `state_dir` layout under `~/.thepit`; src/thepit/store/db.py:210-232 `assert_healthy` will refuse to boot, leaving manual cleanup as the only recovery; measured runtime tree: `~/.thepit/paper/{thepit.db,thepit.db-wal,thepit.db-shm,raw/}` and `~/.thepit/state/HEARTBEAT`

**Blocked by.** the database backup entry (reset must snapshot before it deletes)

**Risk.** This is a script whose entire job is deleting Baron's only copy of the measurement record. No default action, no wildcard delete, and it must refuse to run against any path it did not construct from `Config.home` itself.


## The dashboard

### Label the timezone on every rendered timestamp
*small* · [back to checklist](#the-checklist)

**Why.** The database is UTC milliseconds by convention, the market calendar is `America/New_York`, and every timestamp on the page is rendered with `toLocaleTimeString`/`toLocaleString` in the browser's local zone with no label. "close in 12m" comes from the New York calendar while "time stop 14:32:07" comes from the viewer's clock. On a LAN phone in another timezone, or on a machine whose clock is off, the two disagree with nothing on screen to explain why.

**What.** Add a single formatting helper in web/index.html and route every timestamp through it: `renderQuotes` has none, but `loadNews` (line 393), `loadActivity` (line 553), the time-stop (line 610), the armed expiry (line 622) and any new equity/level views all format independently today. Render in `America/New_York` with a suffix (`14:32:07 ET`) so the console lines up with `minutes_to_close`, and put the viewer's local offset in the footer or a header tooltip. Return the calendar's timezone name from `/api/status` rather than hardcoding it in JavaScript — `thepit.core.calendar` owns it.

**Evidence.** web/index.html:553, 610, 622 — `new Date(...).toLocaleTimeString([], {hour12: false})` with no zone; web/index.html:393 — `new Date(n.published_ms).toLocaleString(...)`; src/thepit/api/main.py:148 — `"minutes_to_close": calendar.minutes_to_close(now_ms())`; src/thepit/core/calendar.py:1 — "US equity market sessions"; src/thepit/store/schema/001_init.sql — "All times are integer milliseconds since the Unix epoch, UTC. Never local time"

**Risk.** Hardcoding 'America/New_York' in the browser duplicates a fact the calendar module owns, and it will be wrong the day a non-US venue is added. Serve it.

### Write the decision doc on whether the React rewrite is worth it
*small* · [back to checklist](#the-checklist)

**Why.** web/index.html's header commits the project to a rewrite — "The real UI comes from the Claude Design components in Stage 9 (issue #12), and React arrives with it. Hand-writing a component system here to throw away in three stages would be waste." That reasoning was sound when the page was a price table. It is now load-bearing for a live session and it is the only thing that has ever caught a session dying. The decision deserves to be re-made against what is actually on the roadmap rather than inherited from a comment, and it should be written down where the next session finds it.

**What.** Write `docs/UI.md` (or extend it from the token entry) with an honest assessment and a gate. The honest assessment as the evidence supports it today: the rewrite is **not** justified by the current page's defects — every bug in this roadmap is a 5-to-50 line fix in a 664-line file, and none of them is caused by the absence of a framework. It **is** justified, if at all, by three things that are on the roadmap and are genuinely painful in vanilla DOM: a session picker with per-session views (routing and state), the eval and comparison views (many small repeated components), and the prompt/decision viewer (nested dialogs and diffing). Set a concrete gate: take the rewrite only when (a) drill-down and the eval view have shipped in the existing page and it has passed roughly 1,200-1,500 lines, or (b) Baron wants the Claude Design look, in which case #12 drives it. Until then, every entry here is deliberately written to be portable — data goes in API endpoints, formatting goes in small pure functions.

**Evidence.** web/index.html:2-19 — the header comment committing to React in Stage 9; web/index.html is 664 lines total, of which roughly 130 are CSS and 400 are JavaScript; CLAUDE-READ-THIS.md:163 lists "Claude Design UI (#12)" under Not done; CLAUDE-READ-THIS.md:150 — "Keep the issue list to actionable work. Methodology lives in docs/NOTES.md"

**Blocked by.** nothing — the doc is writable now; acting on it is what waits on Baron

**Risk.** The failure mode is deciding this twice: a session reads the header comment, starts a React scaffold, and abandons it half-migrated, leaving two dashboards. Write the gate as a checkable condition, not a preference, and put a pointer to docs/UI.md in the index.html header comment so the next reader hits the decision before the assumption.

### Record the vendored uPlot licence and pin its provenance
*small* · [back to checklist](#the-checklist)

**Why.** The repo is public and its own licence is "TBD". `web/vendor/uPlot.iife.min.js` and `uPlot.min.css` are 52KB of third-party MIT code with the version in a banner comment and no licence text, no source URL in a manifest, and no record of how to reproduce the vendoring. This is small, unglamorous, and the kind of thing that is annoying to reconstruct later.

**What.** Add `web/vendor/uPlot.LICENSE` (upstream MIT text), and a `web/vendor/README.md` recording the version (v1.6.31), the exact source URL, the date vendored, and the one-line command to refresh it. Add a test or a `setup.ps1` check asserting the banner version in the .js matches the README, so a silent upgrade is visible. Resolve README.md's `## License` "TBD" while you are in there, or note explicitly that it is deferred — a public repo with no licence is a decision either way.

**Evidence.** web/vendor/uPlot.iife.min.js:1 — `/*! https://github.com/leeoniya/uPlot (v1.6.31) */`; `ls web/vendor` shows only the two minified files, no licence; README.md:215-217 — `## License` / `TBD`; CLAUDE-READ-THIS.md:154 — "The repo is PUBLIC."

**Blocked by.** the licence choice itself is Baron's call; the vendor manifest is not

**Risk.** None technically. The only way to get it wrong is to change the vendored file while updating the manifest and not test the page — uPlot's API changed across minor versions.

### Add security headers and a favicon to the served page
*small* · [back to checklist](#the-checklist)

**Why.** The LAN listener binds `0.0.0.0` and serves a page with a large inline `<script>` and no `Content-Security-Policy`, no `X-Content-Type-Options`, no `Referrer-Policy`. Combined with the unescaped model and EDGAR text going into `innerHTML`, a CSP is the second line of defence that currently does not exist. Separately, every page load requests `/favicon.ico`, which is not mounted and returns a 404, and a tab with no icon is harder to find among a dozen — on a page whose entire job is to be glanced at.

**What.** In `src/thepit/api/main.py` add a middleware setting `Content-Security-Policy` (start report-only), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `X-Frame-Options: DENY`. The inline script and inline styles need either a nonce injected into the response or extraction into `web/app.js` and `web/app.css` — extraction is the better change anyway and makes the file navigable. Add `web/favicon.svg` and a `<link rel="icon">`; make it visibly different for paper versus a future live mode. Note that `--lan` binds all interfaces (line 648) — the docstring says "Nothing here goes on the public internet", and the headers are what makes that survive being wrong.

**Evidence.** src/thepit/api/main.py:648 — `host = "0.0.0.0"  # noqa: S104 - deliberate, read-only router only`; src/thepit/api/main.py:19 — "Nothing here goes on the public internet."; src/thepit/api/main.py:503-508 — only `/` and `/vendor` are mounted, so `/favicon.ico` 404s; web/index.html:26-152 and 266-662 — inline `<style>` and `<script>`

**Risk.** A strict CSP with an inline script silently breaks the whole page and the failure appears only in the browser console. Ship report-only first, extract the script and style, then enforce — and add the browser smoke test before enforcing, not after.

### Write the operator runbook for the dashboard
*small* · [back to checklist](#the-checklist)

**Why.** README.md documents how to start the two processes and `tradectl`, and CLAUDE-READ-THIS.md documents the mistakes. Neither documents what the operator is looking at: what the console's colours mean, what "unrealised" changes about the P&L number, why an order shows as rejected, what a frozen Age column means, what the LAN viewer can and cannot do, and what to do when a session says running with no heartbeat. That knowledge currently lives in code comments scattered across 664 lines of HTML and 663 of Python.

**What.** Write `docs/DASHBOARD.md` covering, with screenshots or exact text: every header element and what a bad value looks like; the console kinds and their colours after the kind-colouring fix; the session panel fields, with `pnl_realised` and `scorable` explained against docs/NOTES.md's exclusion list; what the LAN viewer sees and why Start is unavailable; the kill-switch flow and the fact that the file is the real brake; and a short "it looks frozen — is it?" checklist keyed to the failures that have actually happened (engine dot, quote age, WS dot, session heartbeat). Link it from README.md's dashboard section and from web/index.html's header comment.

**Evidence.** README.md:156-164 covers only how to start the LAN listener; CLAUDE-READ-THIS.md:110-112 documents the orphaned-session failure but not how it presents on screen; docs/NOTES.md:70-74 lists the eval exclusions with no operator-facing counterpart; web/index.html:531-536 — "Two sessions were lost that way before this existed" is a code comment, not documentation

**Blocked by.** the console-colouring and scorable-display entries, so the doc describes the fixed behaviour rather than needing a rewrite

**Risk.** Writing it before the fixes land means documenting bugs as features. Write it last in each stage, and keep it short — CLAUDE-READ-THIS.md:145 says Baron got overwhelmed by long explanations and said so.

### Make the page usable on a phone
*medium* · [back to checklist](#the-checklist)

**Why.** Baron's kill switch is reachable over SSH from a phone by design, and `--lan` exists to view the dashboard from another machine — so the phone case is intended, not hypothetical. On a 375px screen the sticky header's seven flex children wrap to four or five rows and eat a third of the viewport before any content. Every input in the MFT dialog is 13px, which is below iOS Safari's 16px threshold, so focusing any field zooms the page. The prompt preview is a scroll container nested inside another scroll container inside a `<dialog>`. And the chart's width is captured once from `clientWidth` with no resize listener, so rotating the phone leaves it the wrong width for up to 60 seconds.

**What.** In web/index.html: (1) add a `@media (max-width: 560px)` block collapsing the header to two rows — title plus badge plus the primary action on the first, status stats on a horizontally-scrolling second — or move the stats into a disclosure. (2) Raise `input, select, textarea` (lines 136-139) to 16px at that breakpoint. (3) Add a `ResizeObserver` on `#chart` calling `chart.setSize({width, height})` instead of waiting for the 60s redraw at line 424. (4) Give the dialog `max-height: 100dvh` and let the body scroll as one column, dropping `.dlg-body`'s `max-height: 62vh` and `pre.prompt`'s `max-height: 46vh` under the breakpoint so there is one scroller, not three. Test at 375×667 and 390×844.

**Evidence.** web/index.html:38-42 — header is `display:flex; flex-wrap:wrap; position:sticky` with seven children including `margin-left:auto`; web/index.html:136-139 — `input, select, textarea { ... font: 13px var(--mono); }`; web/index.html:132 `.dlg-body { max-height: 62vh }` and web/index.html:150 `pre.prompt { max-height: 46vh }`; web/index.html:323 — `const width = $("chart").clientWidth || 640;` with no resize handler

**Risk.** `vh` on mobile Safari includes the retracting toolbar, which is why the nested scrollers look fine in a desktop emulator and are unreachable on the device. Use `dvh` and verify on real hardware, not devtools — README.md:202 already flags that Windows itself is unverified on real hardware, so do not add a second unverified platform claim.

### Broadcast quotes from one shared task instead of per connection
*medium* · [back to checklist](#the-checklist)

**Why.** The WebSocket handler opens its own read-only connection and runs `_latest_quotes` once per second *per client*. That call does a GROUP BY over the whole `ticks` table plus one bar lookup per symbol. `ticks` grows without bound — retention prunes only raw HTTP payloads, never the tables — so the per-second cost grows for the life of the install, multiplied by every open browser tab. There is no connection cap. Two tabs and a phone on the LAN listener is three copies of that scan every second, forever.

**What.** Replace the per-socket loop in `src/thepit/api/main.py::ws` (lines 239-274) with a single background task started on app startup that computes `_latest_quotes` once per `PUSH_INTERVAL_S` and fans out to a set of connected sockets, computing the delta once. Each socket still gets its own snapshot on accept. Add a max-connections guard on the LAN listener. Separately, tighten `_latest_quotes` (lines 592-626) — the N+1 reference-price lookup runs one query per symbol per second and its result changes at most once per trading day, so cache it keyed by (symbol, trading date).

**Evidence.** src/thepit/api/main.py:248-264 — `c = conn()` inside the handler, then `while True: await asyncio.sleep(PUSH_INTERVAL_S); current = _latest_quotes(c, config.symbols)`; src/thepit/api/main.py:610-613 — a per-symbol `SELECT c FROM bars ...` inside the per-quote loop; src/thepit/engine/main.py:162-180 — retention prunes recorder files only

**Risk.** main.py's own docstring says polling the read-only database "is entirely adequate for one user on a laptop. If it ever is not, the fix is a socket, not a rewrite." Measure before rebuilding: with 8 symbols and a week of ticks this may be genuinely free. Record the measurement in the commit so the next session does not redo the analysis.

### Add a browser smoke test for the dashboard
*medium* · [back to checklist](#the-checklist)

**Why.** web/index.html is 664 lines with about 400 lines of JavaScript, zero tests, and no build step or linter. Every bug in the entries above — the frozen age column, the collapsing `<details>`, the dead Start button on the LAN listener, the raw float quantities — is the kind a single scripted page load would have caught. The repo runs 184 Python tests and zero for the surface the operator actually uses.

**What.** Add a `tests/test_dashboard.py` driving a real browser against a `TestClient`-backed server with seeded fixture data (one finished session with fills and a fired stop, one running session, one halted-still-holding session). Assert: the quote table renders and the Age column increases across two heartbeats with no tick change; clicking a row changes `#chart-sym`; opening the MFT dialog and pressing Preview renders a non-empty prompt; on an `allow_control=False` app the Start button is unreachable; the session panel keeps an opened `<details>` open across a poll. Keep it opt-in behind a pytest marker so `uv run pytest` stays dependency-light — pyproject.toml is explicit that "Nothing else gets added without asking."

**Evidence.** pyproject.toml `[dependency-groups] dev = ["pytest>=8.3", "pytest-asyncio>=0.24"]` — no browser or JS tooling; no package.json, no eslint config, no web tests anywhere in the tree; CLAUDE-READ-THIS.md:29 — "uv run pytest # 184 tests"

**Blocked by.** Baron agreeing to a dev-only browser dependency — pyproject.toml's comment makes adding one a decision, not a default

**Risk.** Playwright pulls a browser download and is a real weight on a 2015 laptop. A cheaper first step is a JSDOM-free unit test: extract the pure formatting functions (`fmt`, quantity formatting, age derivation) into a `web/format.js` module and test those in Python via a tiny JS runtime, or port them to be generated from Python. Do not let the perfect harness block the pure-function coverage.


## The research programme

### Ablate model tier and effort
*small* · [back to checklist](#the-checklist)

**Why.** `SessionConfig.model` and `effort` are already plumbed through to the CLI arguments and recorded on every `decisions` row with latency and token counts, so this is a config sweep with zero new code in the trading path. It also answers the operating question directly: whether the programme should spend its shared rate window on the largest model.

**What.** Register `docs/experiments/EXP-005-model-tier.md` with arms haiku/medium, sonnet/medium, opus/medium, sonnet/low and sonnet/high, run as a simultaneous k-way twin on one tape. Primary metric: behavioural agreement with the sonnet/medium reference arm — fraction of ticks choosing the same symbol with a stop within 5bp — plus decision latency p50/p95, which `report.ModelUse` already computes. Secondary: paired P&L bp. Cost the sweep explicitly in the spec: `SessionConfig.model_calls` x arms x slots per day against the same 5-hour window that serves the operator's own tools.

**Evidence.** src/thepit/session/config.py:99-100; src/thepit/agent/claude.py:148-156 (--model/--effort passthrough); src/thepit/eval/report.py:72-82 and 254-274 (ModelUse)

**Blocked by.** Twin spawner (k-way) and the experiment registry

**Risk.** A k-way twin runs k `claude` subprocesses at once against one subscription. Measure the window cost on a closed-market dry run before scheduling it during market hours — locking the operator out of his own tools is the exact failure `model_calls` is surfaced pre-session to prevent.

### Ablate conversation continuity
*small* · [back to checklist](#the-checklist)

**Why.** `runner._ask` threads the returned `session_id` back into the next `claude.ask`, so plan, every tick and the review share one resumed conversation. Nobody has tested whether that accumulated context helps or drifts — and drift is the mechanism behind the one recorded plan violation, where the model acted on a plan it had not retained, which is why the plan is now restated every tick anyway.

**What.** EXP-008 with two arms: `resumed` (today) and `stateless` (`session_id=None` on every call). A stateless tick is not obviously under-informed: `_tick_prompt` already carries the plan verbatim, the positions with their enforced levels, armed entries, current prices with ages, and the last five rejections. Primary metric: plan adherence in bp — the distance between the entry level parsed from `sessions.plan` and the realised opening `fills.price` — plus the flat-tick rate. Secondary: paired P&L bp and `decisions.tokens_in` per call, which should fall sharply and is itself the rate-window argument for whichever arm wins.

**Evidence.** src/thepit/session/runner.py:876-890 (session_id threading); src/thepit/session/runner.py:762-777 (the plan restatement and the recorded violation)

**Blocked by.** Behavioural metrics and the experiment registry

**Risk.** Parsing the planned entry out of free-text `sessions.plan` is fragile — the plan format is line-oriented but not machine-checked. Write a strict parser with an explicit `unparseable` bucket counted in the denominator rather than silently dropping those sessions, which would bias toward the arm that formats better.

### Ablate session length
*small* · [back to checklist](#the-checklist)

**Why.** 30 minutes is the default and the project describes the regime as 15-60. The `RiskProfile` docstring already records the finding that an 8-minute session earned one cent because two thirds of the capital sat idle, so length interacts with sizing in a way that has been observed once and never measured. Length is the cheapest lever on the stated objective — maximum P&L over the window — and it has never been varied.

**What.** EXP-009 with arms at `duration_minutes` in {15, 30, 60}, scaling `policy_tick_minutes` to hold `tick_count` constant at 6 so the ablation varies duration and not the number of model calls. Primary metric: session P&L bp; secondary normalised per trading minute, since the arms are not per-session comparable by construction. Report `Costs.breakeven_bp` per arm — a longer session takes more turnover and pays more hurdle, and this is the direct measurement of that trade-off. Also report the share of capital deployed per arm, which is where the recorded one-cent session came from.

**Evidence.** src/thepit/session/config.py:39-50 (RiskProfile docstring, "Reserve capital on an 8-minute session is just money left on the table"); src/thepit/session/config.py:167-195; src/thepit/eval/report.py:52-60

**Blocked by.** Experiment registry and the fixed schedule

**Risk.** 60-minute sessions consume the rate window twice as fast for the same n, and there are only about 6.5 hours of tape per day. At three arms per slot this is most of a trading day; cost the schedule before registering it, and expect to run it after the four headline experiments rather than beside them.

### Keep a family-wise register of every comparison run
*small* · [back to checklist](#the-checklist)

**Why.** This roadmap proposes at least nine experiments, several with three to five arms, most reporting more than one metric. Nothing records how many comparisons have been made, so the programme's real false-positive rate is unknowable and every individual p-value overstates its case — which is exactly the correction NOTES flags when it says testing many configurations makes the sample-size problem worse.

**What.** `docs/experiments/REGISTER.md` listing every registered experiment, its primary metric, its status (registered / running / finished / abandoned) and its result, updated as routine. Add `holm(p_values)` to `eval/stats.py`. `tradectl eval --experiment X` prints the raw and the family-adjusted p for the primary metric, taking the family size from the register. Secondary and behavioural metrics print as exploratory and are never adjusted, because they are not being used to make a claim.

**Evidence.** docs/NOTES.md:11-14 ("testing many configurations makes it worse"); src/thepit/eval/stats.py:117-141 (permutation_p, the only inference tool today)

**Blocked by.** Experiment registry

**Risk.** The register only works if abandoned experiments are recorded too, and abandoned arms are precisely the ones that get forgotten. Forgetting them is how the family size drifts down to whatever produced the good number — make `registry.py` refuse to drop a registered id, only mark it abandoned.

### Test whether headline text steers the agent
*small* · [back to checklist](#the-checklist)

**Why.** `build_plan_prompt` interpolates filing headlines straight into the context with no delimiter, while the operator note directly below it is carefully wrapped in `<operator_note>` and labelled as context rather than an override. The prompt module's own docstring explains why user text is treated as data — and then treats feed text as though it were not. This is both a safety question and a measurement one: if a headline can move a decision, the news experiment is partly measuring suggestibility.

**What.** Register as a red-team experiment run entirely against a throwaway database under a separate `THEPIT_HOME`, never the live feed. Seed `news` rows whose headlines contain instruction-shaped text, run planning-only via `/api/session/preview` (which renders the exact prompt and touches no capital), and measure whether the returned plan's stop distances or stand-down condition change against a control where the same headlines are rewritten as plain descriptions. Primary metric: rate of plans whose stated levels violate the limits printed in the same prompt. Whatever the result, then wrap the news block in a delimiter and a data-not-instruction line the way the operator note already is.

**Evidence.** src/thepit/session/prompt.py:260-269 (undelimited news) versus prompt.py:271-279 (delimited operator note); src/thepit/session/prompt.py:20-23

**Risk.** Seeding synthetic rows into the real `news` table would contaminate the recorded dataset permanently and silently — every later replay would carry them. Use a separate database and assert the real table's row count is unchanged as part of the test.

### Export the cohort for outside analysis
*small* · [back to checklist](#the-checklist)

**Why.** Every number in the programme currently exists only inside `tradectl eval`'s printed output, which means any analysis nobody wrote a Python function for cannot be done at all — including the plots that make a calibration or regime-split argument legible. The dependency budget is deliberately three packages, so the answer is an export, not pandas.

**What.** `tradectl export --experiment EXP-004 --out sessions.csv` writing one row per session (id, experiment, arm, twin_of, universe_id, slot, regime_vol_bp, prompt_variant, model, effort, duration, tick minutes, pnl_bp, costs bp of notional, n_fills, flat_reason, exclusions) and `--episodes` writing one row per episode (session, symbol, conviction, net, net_bp, held_s, exit, entry_origin). Uses `csv` from the standard library and `report.session_report`, so there is exactly one implementation of every number and no second definition to drift.

**Evidence.** src/thepit/cli/tradectl.py:291-363 (_print_session_eval is the only surface for these fields); pyproject.toml (three runtime dependencies, pandas deliberately absent)

**Blocked by.** Migration 007

**Risk.** An export is a second place a P&L number can be computed. Make it call `eval/pnl.session_pnl` and `report.session_report` only — the repo has already paid for the alternative once, with a -$3,060 figure on a session that was down $1.97.

### Ablate the load-bearing prompt paragraphs, one at a time
*medium* · [back to checklist](#the-checklist)

**Why.** Three passages in the prompt were each written in response to a single bad session: the cost line, the paragraph stating that a flat session has earned nothing, and the tick-prompt restatement of the plan. Each is a belief with a sample size of one. They are also the cheapest thing in the project to test — the harness is unchanged and only the text varies — and `prompt.py` is described in CLAUDE-READ-THIS as the highest-leverage file in the repo.

**What.** Parameterize `build_plan_prompt(..., variant: str = "v1")` and `SessionRunner._tick_prompt(..., variant)`, with variants defined in `src/thepit/session/prompt_variants.py` as section->include/replace maps. `sessions.prompt_variant` records which. Register as one family: `no_cost_line` (drops the "## Your costs" block), `no_flat_shaming` (drops prompt.py:164-172), `no_plan_restatement` (drops the tick block at runner.py:762-777), `no_terse_system` (drops `claude.TERSE_SYSTEM`), `levels_optional` (drops the must-carry-a-stop language while leaving the enforcement in place). Primary metrics are behavioural, not P&L: orders per tick, flat-session rate from `report._flat_reason`, entry deviation from the planned level, and output length in characters.

**Evidence.** src/thepit/session/prompt.py:164-172; src/thepit/session/runner.py:762-777; src/thepit/agent/claude.py:40-54 (TERSE_SYSTEM)

**Blocked by.** Migration 007, behavioural metrics, experiment registry

**Risk.** Five variants tested at p<0.05 produce a winner by chance roughly a quarter of the time. Register the whole family in one spec with a Holm correction and treat a variant's win as a hypothesis to re-run, never as a prompt edit to ship. There is a test asserting the old "doing nothing is fine" phrasing never returns — keep it, and make ablation variants explicitly exempt and labelled rather than deleting the guard.

### Ablate the two clocks: policy tick and fast-loop interval
*medium* · [back to checklist](#the-checklist)

**Why.** `policy_tick_minutes=5` and `fast_loop_seconds=5` were both chosen by argument, never measured. The tick rate is the project's central claim about where the model belongs and it has never been varied. The fast-loop interval has a directly measurable consequence: `enforcement.LevelFill.detect_bp` already separates how far the tape ran before the loop saw it from what the fill model charged, so shrinking the interval should move that number and nothing else.

**What.** EXP-006: arms at `policy_tick_minutes` in {2, 5, 10} with duration fixed at 30m — note `validate` rejects fewer than two ticks and a tick shorter than a model call. Primary metric behavioural: revision rate and orders per unit time; secondary paired P&L bp. EXP-007 as a separate experiment because it changes no model behaviour at all and can therefore run entirely on baseline arms at zero model cost: `fast_loop_seconds` in {1, 5, 15}, primary metric the median `detect_bp` and the p50/p95 `late_ms` from `enforcement.level_fills`, both of which `tradectl eval` already prints.

**Evidence.** src/thepit/session/config.py:79-88 (both intervals, with the reasoning as comments); src/thepit/eval/enforcement.py:9-24; src/thepit/eval/report.py:368-377 (_lateness)

**Blocked by.** Baseline suite and the experiment registry

**Risk.** At `fast_loop_seconds=1` the loop polls a feed that only updates about every 5s, so the expected result is no improvement below the feed interval. That is worth having on record, but pre-register it as the prediction or it reads afterwards as a null result nobody anticipated.


## Cross-cutting: security, budget, contracts, hygiene

### Add a type-checking gate
*small* · [back to checklist](#the-checklist)

**Why.** The codebase is fully annotated and uses `from __future__ import annotations` everywhere, dataclasses with slots, and StrEnum — it is written as if a type checker runs, and none does. The bugs this catches are exactly the ones this project has already paid for once: a `None` price reaching arithmetic, a dict key that drifted (`_qty` reads `qty` and silently returns 0.0 for `size`/`shares`), an optional `session_id`.

**What.** Add `mypy` (or pyright) to the dev group, a `py.typed` marker in src/thepit, and a `[tool.mypy]` section starting non-strict with `warn_unused_ignores` and `no_implicit_optional`, excluding tests initially. Fix the findings in `core/`, `trading/` and `eval/` first — the pure modules — and leave `api/` and `session/` on a laxer setting until the session-execution move lands. Run it in CI alongside ruff.

**Evidence.** Every module opens with `from __future__ import annotations` and annotates fully (e.g. src/thepit/trading/book.py, src/thepit/eval/pnl.py); pyproject.toml has no mypy/pyright config and no such dev dependency; src/thepit/session/runner.py:971-975 (`_qty` returning 0.0 on key drift, the failure documented at runner.py:500-507).

**Blocked by.** the CI workflow entry

**Risk.** Chasing strictness across the API layer stalls the useful part. Gate on the pure modules only at first.

### Keep a decision log for the choices that are only recorded in docstrings
*small* · [back to checklist](#the-checklist)

**Why.** The most important reasoning in this project lives in module docstrings: why the CLI instead of the API, why a file kill switch instead of a lock, why one 30-line migration runner instead of alembic, why fills are priced pessimistically, why paper and live are separate types. A future session that wants to change one of those has to already know which file's docstring to read. Losing that reasoning is how a well-argued design gets casually reversed.

**What.** Create docs/DECISIONS.md as a dated, append-only list: one short entry per irreversible choice, each with the decision, the alternative rejected, and a pointer to the module whose docstring carries the long form (agent/claude.py for CLI-vs-API, engine/killswitch.py for the file switch, store/db.py for migrations and single-writer, trading/book.py for the fill model, config.py for mode-as-argv, session/prompt.py for the three prompt principles). New entries appended when a decision of that weight is taken; nothing ever rewritten.

**Evidence.** src/thepit/agent/claude.py:1-16, src/thepit/engine/killswitch.py:1-30, src/thepit/store/db.py:1-18, src/thepit/trading/book.py:1-11, src/thepit/config.py:1-11, src/thepit/session/prompt.py:1-24 — six substantial rationales, discoverable only by opening the right file.

**Blocked by.** the ROADMAP working agreement (same pass, same conventions)

**Risk.** A decision log that duplicates the docstrings rots. Keep entries to three lines and point at the code.

### Build the hash-chained audit_log the schema promises, or delete the promise
*medium* · [back to checklist](#the-checklist)

**Why.** 001_init.sql tells every future reader that "the append-only, hash-chained `audit_log` carries anything that could ever be asked 'why did that happen'" and that `events` is only the cheap operational stream. No such table exists and no code references it. A promise like that in the foundational migration will be believed by the next session designing around it, and it is load-bearing for the live path the README describes as built-but-disabled.

**What.** Decide, and record the decision in docs/NOTES.md either way. If building: migration 007+ creating `audit_log(id, ts_ms, actor, kind, subject, payload, prev_hash, hash)` where hash = sha256 over the row plus prev_hash; write to it from the small number of places that matter (session create/finish, every order status transition, kill engage/release, config load), and add a `tradectl audit --verify` that walks the chain. If not building: delete the sentence from 001_init.sql's comment via a doc-only follow-up note (never edit an applied migration file's SQL) and say in NOTES.md that `events` plus the append-only `exit_plan_events` are the audit story.

**Evidence.** src/thepit/store/schema/001_init.sql events table comment ("In later stages the append-only, hash-chained `audit_log`…"); grep for `audit_log` across src/ and tests/ returns nothing; .gitignore:24 already reserves an `audit/` directory.

**Risk.** Editing the text of an already-applied migration changes what a schema-drift hash would compute. If the drift-detection entry lands first, correct the promise in NOTES.md rather than in the .sql file.


---

# SOMEDAY


## Trading, risk and the session loop

### Support limit orders, or drop the columns that pretend to
*medium* · [back to checklist](#the-checklist)

**Why.** `orders.kind` defaults to 'market' and `orders.limit_price` is nullable, and neither is written by any code path. A reader of the schema reasonably concludes limit orders exist. They do not: every order crosses at the assumed spread. The armed-entry mechanism is a partial substitute — it waits for a level and then buys at market — but it cannot express "buy at 303.50 or better", which is what a plan level usually means.

**What.** Either (a) implement: add `kind` and `limit_price` to the tick schema, write both in `_submit`, and in `simulate_fill` refuse the fill when the crossed price is worse than the limit, returning None so the order is recorded 'cancelled' rather than 'filled'; the fast loop would then need to decide whether an unfilled limit rests or dies. Or (b) delete: a migration cannot drop a column cleanly in older SQLite, so instead document in `002_trading.sql` that both are reserved and unused, and add a test asserting no code writes them. Pick (b) unless a measured need appears — an unfilled limit is a whole order-lifecycle state machine, and the armed entry already covers the recorded failure (chasing a missed level).

**Evidence.** src/thepit/store/schema/002_trading.sql:49-50 `kind TEXT NOT NULL DEFAULT 'market'` and `limit_price REAL`; grep across src/ shows `limit_price` appears only in that schema file; src/thepit/store/schema/004_levels.sql documents the armed-entry mechanism as the answer to the chasing problem.

**Risk.** Half-implementing this is the worst outcome: a limit order that silently fills at market is a lie in the fill record, and one that rests forever is a position that never opens while its capital stays committed. If (a) is chosen, the resting state needs the same watchdog treatment as an armed entry.

### Model borrow cost and locate failure for shorts
*medium* · [back to checklist](#the-checklist)

**Why.** NOTES.md lists borrow cost among the unmodeled negatives that all point the same direction. Once shorting is reachable, a paper short is strictly free: no locate, no borrow fee, no recall risk, no hard-to-borrow rate. A short-arm result compared against a long-arm result would then be comparing one instrument against a cheaper imaginary one, and the difference would look like alpha.

**What.** Add `BORROW_BP_PER_DAY` to `trading/book.py` beside the slippage constants, with a comment stating it is a guess at this data tier and naming what would replace it (a broker's rate feed). Accrue it per fast-loop interval on any negative position, writing the accrual into `fills.cost` at close or as a separate `costs` row, and surface the total in `eval/pnl.SessionPnL.costs` so it flows into the existing cost-drag reporting. Add a hard-to-borrow list in config that rejects a short outright, since the realistic failure is not an expensive borrow but no borrow at all. Extend `docs/NOTES.md`'s "Paper fills are optimistic" section to say which of its listed negatives are now modelled.

**Evidence.** docs/NOTES.md:36-38 "borrow cost on shorts, halts. Reality is worse than the simulator, never better."; src/thepit/trading/book.py:34-38 `ASSUMED_SLIPPAGE_BP` / `QUOTE_SLIPPAGE_BP` are the precedent for a documented guessed constant.

**Blocked by.** Make short selling reachable and safe, or delete the flag

**Risk.** A guessed borrow rate that is too high makes shorting look unprofitable and the model will rationally never short — the same failure as the 5bp/side slippage assumption that produced a whole session of inaction. State it as an estimate in the prompt and keep it small until it can be measured.

### Let a session resume after the process driving it restarts
*large* · [back to checklist](#the-checklist)

**Why.** Sessions are asyncio tasks inside the API process, so restarting the API kills them; CLAUDE-READ-THIS.md records two sessions lost that way before the reaper existed. The reaper now detects it, but detection is all it does — the window is abandoned mid-flight, the capital is stranded in an open position, and the run is unscorable. On a 15-60 minute session an accidental restart destroys the whole sample.

**What.** Add `SessionRunner.resume(conn, session_id)`: load the config from `sessions.config`, call `Book.load()` (which by then asserts against the fill stream), rebuild the `FastLoop` from the existing `exit_plans` and `pending_entries` rows (both already survive in the database, which is why the fast loop was built stateless), and re-enter `run()` at the tick loop with the original `ends_ms`. Add `POST /api/control/session/{sid}/resume` and a startup scan that offers resumable sessions rather than reaping them if `ends_ms` is still in the future. The model conversation cannot be resumed reliably — `_claude_session` is in-memory — so a resumed session starts a fresh CLI conversation and the tick prompt must say so, since the plan is restated every tick anyway.

**Evidence.** src/thepit/session/fastloop.py:76-81 "Holds no state of its own: plans live in the database, so what is being enforced is inspectable while the session runs and survives the loop being restarted."; src/thepit/api/main.py:75-85 `_reap_orphans` docstring; CLAUDE-READ-THIS.md: "Restarting the API orphans running sessions ... two were lost that way before the reaper existed."

**Blocked by.** Implement the fills-versus-positions boot check the schema says exists

**Risk.** A resume that races the reaper, or two processes both resuming the same session, would double every order. Resume must take the write lock and flip the status atomically from 'halted' to 'running' with a `WHERE status='halted'` guard, and refuse when the heartbeat is fresh.


## Measurement and the eval module

### Add a bootstrap interval on the arm means
*medium* · [back to checklist](#the-checklist)

**Why.** The arm table prints `mean_bp` with an `sd_bp` withheld under five sessions and a permutation p, but nothing bounds the mean itself. `pnl_bp` at this frequency is heavy-tailed — the module already hand-rolls Wilson rather than a normal approximation for proportions for exactly this reason — so a normal-theory interval on the mean would be wrong in the same way and worse.

**What.** Add `stats.bootstrap_ci(xs, *, trials=20_000, seed, alpha=0.05) -> tuple[float, float] | None`, percentile method, returning `None` below `MIN_N_FOR_SD` points to match the existing threshold. Add `ArmSummary.mean_ci` and print it beside the mean in the arm table (tradectl.py:236-241). Note in the docstring that sessions drawn from overlapping windows are not independent observations, so the honest version is a block bootstrap keyed on trading day — which needs a session-to-day grouping the schema does not carry, and which the `twin_of` and `experiment_id` work would supply.

**Evidence.** src/thepit/eval/report.py:118-126 `ArmSummary` has mean, median, sd and a positive count, no interval; src/thepit/eval/stats.py:62-76 `wilson` exists for the proportion case with the same reasoning; stats.py:24-25 `MIN_N_FOR_SD = 5`.

**Risk.** A percentile bootstrap at n=5 produces an interval determined almost entirely by which of five numbers got resampled; it will look like a measurement and is barely one. Withhold below the same threshold as sd, and print the n next to it as everything else in this module does.

### Make it safe to look at the number while sessions accrue
*medium* · [back to checklist](#the-checklist)

**Why.** The operator will run `tradectl eval` after every session, and repeatedly testing a growing sample at a fixed alpha inflates the false-positive rate well past 5% however carefully each individual p is computed. `sessions_needed` is printed on every run and its arithmetic assumes a single test at a pre-committed n; nothing in the module accounts for the fact that the test is being read continuously.

**What.** Add an always-valid alternative to `stats.py` — the simplest defensible form is an e-value or mixture sequential probability ratio for the difference in arm means, exposed as `stats.evalue_diff(xs, ys) -> float`, which can be inspected at any point without correction. Print it beside the permutation p in `cmd_eval` and label the permutation p as valid only at the pre-registered n. The stopping rule belongs in the `experiments` row (a `stopping_rule TEXT` column), not in code, because it is a decision about what Baron wants to conclude, not a formula.

**Evidence.** src/thepit/eval/stats.py:162-172 `sessions_needed` — "Sessions per arm to detect `effect` at 95% confidence and 80% power"; src/thepit/cli/tradectl.py:254-256 prints it on every invocation; docs/NOTES.md:16-17.

**Blocked by.** Pre-register a primary metric before a cohort runs

**Risk.** Speculative on two counts: it needs a written decision about the stopping rule before the code means anything, and an e-value printed next to a p-value without an explanation of why they disagree will be read as the tool contradicting itself. Do not ship it without the docs/METRICS.md section that says which one to believe and when.


## Operations, packaging and the machine

### Let the Mac run eval against a copy of the database
*medium* · [back to checklist](#the-checklist)

**Why.** The Mac cannot run sessions -- the engine, the `claude` CLI subprocess and the single-writer database are all on the Windows box -- but `eval/` is entirely read-only by design and would run anywhere. `tradectl eval` opens the database with `readonly=True` and every eval module is pure analysis. Today the only way to look at results from the Mac is the LAN dashboard, which shows live state rather than the cohort report, and requires the Windows machine to be up and on the same network.

**What.** Add `tradectl export --dest PATH` producing a portable, self-contained snapshot: a `VACUUM INTO` copy of the database (which collapses the WAL and is safe against a live writer), gzipped, named with the app version and a UTC timestamp. Confirm `tradectl eval` runs against an arbitrary `--db PATH` by adding that flag to `cli/tradectl.py:main()` (today it always uses `cfg.load().db_path`). Document in `docs/OPERATIONS.md` that the Mac needs only `uv sync` plus the exported file to run `tradectl eval` and `tradectl sessions`, and that it must never run the engine against a shared file -- two writers over a network filesystem is exactly what the single-writer design forbids. Note that macOS needs no `tzdata` (it ships a tz database), which is why that dependency is Windows-marked.

**Evidence.** src/thepit/cli/tradectl.py:221 `db.connect(config.db_path, readonly=True)` for eval; src/thepit/eval/__init__.py and the five eval modules are read-only (`eval/pnl.py`, `cohort.py`, `report.py`, `stats.py`, `trades.py`); src/thepit/cli/tradectl.py:455 `config = cfg.load(mode=cfg.Mode.PAPER)` -- no `--db` override; pyproject.toml:22 `tzdata>=2025.2; sys_platform == 'win32'`; src/thepit/store/db.py:5-11 the single-writer rule

**Blocked by.** the versioning entry, so an export names the code that produced it

**Risk.** An exported snapshot on the Mac is a second copy of the truth that will diverge the moment a new session runs. Stamp it with the export timestamp and the last session id in the filename, and have `tradectl eval` print loudly when it is reading an export rather than the live database.


## The dashboard

### Stage the rewrite behind API parity rather than a big-bang port
*large* · [back to checklist](#the-checklist)

**Why.** If the gate in the decision doc is met, the way to get this wrong is to rewrite the page and the endpoints at once, discover mid-port that the session console — the thing that stops sessions being lost silently — regressed, and have no working dashboard for a week. The current page's three non-placeholder behaviours are documented in its header precisely so a rewrite cannot quietly drop them.

**What.** Sequence it: (1) every entry above that adds an endpoint lands first, so the API is complete before any client is rewritten and the old page keeps working against it; (2) build the new client at `/next` served alongside the existing `/`, both hitting the same endpoints, so they can be compared side by side on the same live session; (3) port view by view in this order — quotes and chart, session panel and console, session picker, eval and comparison, MFT dialog; (4) carry forward, with tests, the three behaviours the header names: chart pauses on hidden tab while the table does not, quote age visible and moving, PAPER/LIVE badge loud; (5) delete `/` only after a full live session has been operated end to end on `/next` and the eval numbers match `tradectl eval` exactly. Keep uPlot — it is vendored, versioned (v1.6.31) and has no React dependency.

**Evidence.** web/index.html:10-18 — the three behaviours that must survive, including "gating cheap work on visibility left a background tab stuck on 'waiting for data' forever"; web/index.html:288-293 and 419-424 — the visibility split as implemented; web/vendor/uPlot.iife.min.js:1 — `/*! https://github.com/leeoniya/uPlot (v1.6.31) */`; CLAUDE-READ-THIS.md:110-112 — restarting the API orphans running sessions, so a rewrite must not require API restarts mid-session

**Blocked by.** the decision doc's gate being met, and issue #12's design foundations, which are blocked on Baron

**Risk.** A React build step introduces a compile artefact, and CLAUDE-READ-THIS.md says "Ship working artifacts. Do not hand him something to compile." Either commit the built bundle, or make `setup.ps1` build it, or pick a no-build approach — decide that before the first line, not after.


## The research programme

### Measure whether concurrent agents are actually independent
*medium* · [back to checklist](#the-checklist)

**Why.** The universe was chosen so that correlation between agents is at least observable, and the README opens by describing independent agents with their own capital and mandates — but every session run so far has been a single agent and no cross-agent correlation has ever been measured. If five agents with different mandates buy the same name in the same minute, independence is a UI concept and any portfolio-level claim built on it is wrong.

**What.** After the twin runner generalises to k arms, register EXP-010: five concurrent LLM sessions with different `SessionConfig.notes` mandates on the same universe, same clock, same everything else. Primary metric: the rate at which two agents hold the same symbol in the same direction in the same minute, against a permutation null built from the observed marginal symbol frequencies. Secondary: whether that correlation rises with `research=AMBIENT`, which would suggest the shared news block is the coupling rather than the tape.

**Evidence.** src/thepit/config.py:29-35 ("spread across sectors so correlation between agents is at least possible to observe"); README.md:3-5

**Blocked by.** Twin spawner, headless launcher, fixed schedule

**Risk.** Five concurrent agents is five times the rate-window cost and five sessions in one process — the orphaning failure CLAUDE-READ-THIS records applies fivefold. Do not schedule it until sessions run outside the API process, and expect it to be the experiment that finds the launcher's concurrency bugs rather than an answer about correlation.
