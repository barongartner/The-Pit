# CLAUDE, READ THIS

Everything you need to know about this project before touching it.

Claude Code auto-loads `CLAUDE.md` by filename, so that file is a one-line
pointer here. This is the real document.

**Then read [ROADMAP.md](ROADMAP.md) before choosing what to work on.** This file
is the memory — what has been learned and paid for. That one is the forward list:
~200 entries with evidence, a sequenced critical path, the working agreement for
claiming work and migration numbers, and a register of the claims in these
documents that are not yet true.

## What this is

**The Pit** runs Claude-powered trading agents in bounded paper-trading sessions
and measures whether they make money.

Baron's goal, stated plainly and not to be softened into something else:
**maximum P&L over the session window, subject to the risk limits.** Not
risk-adjusted-something, not a research proxy. He wants it to make money.

He trades **$20** because losing all of it is fine. Do not treat that as an
error or scale it up. The `risk_it` profile (100% position, 1 concurrent, halt at
−60%) is the default for exactly that reason.

Sessions are 15–60 minutes, "MFT" — medium frequency. Higher turnover than
buy-and-hold, nowhere near real HFT, and nothing here can do real HFT. Say so
if it comes up rather than implying otherwise.

## Run it

```bash
uv sync
uv run pytest                              # 184 tests
export THEPIT_CONTACT_EMAIL=you@example.com   # SEC 403s without it

uv run python -m thepit.engine.main        # records prices + filings
uv run python -m thepit.api.main           # dashboard on :8000
```

Windows: `powershell -ExecutionPolicy Bypass -File setup.ps1`

```bash
uv run tradectl status      # is the engine alive
uv run tradectl sessions    # scoreboard with CORRECT P&L
uv run tradectl eval        # scored cohort: arms, exclusions, required n
uv run tradectl eval 7      # one session in detail
uv run tradectl uptime      # feed reliability
touch ~/.thepit/state/KILL  # emergency stop, works when everything else is wedged
```

## Shape

Two processes. The **engine** is the only DB writer. The **API** opens the
database read-only and serves the dashboard; a stray write raises immediately
instead of surfacing as intermittent `SQLITE_BUSY`.

```
core/       clock, calendar, value types, feed protocols
feeds/      yahoo (prices), edgar (filings), shared http, raw recorder
engine/     poller, kill switch + watchdog, entrypoint
trading/    book.py — ledger, fill model, risk checks (all three together)
            levels.py — exit levels: parse, resolve, decide if breached (pure)
agent/      claude.py (CLI subprocess), stub.py (deterministic baseline)
session/    config.py, prompt.py, runner.py — plan/tick/flatten/review
            fastloop.py — enforces those levels every 5s, no model in the path
api/        FastAPI, endpoints, WebSocket
eval/       measurement, read-only: pnl.py owns THE P&L, cohort.py owns what is
            scorable, enforcement.py puts a number on "a stop here is late"
web/        single-page dashboard, vendored uPlot
docs/NOTES.md   methodology and honest limits. Read before eval work.
```

Two loops run inside a session. The **slow loop** asks the model every few
minutes. The **fast loop** enforces the levels that model committed to every five
seconds, including while a 40-second model call is in flight. Levels live in
`exit_plans` and `pending_entries`, so what is being enforced is inspectable
while the session runs rather than buried in a reason field.

`session/prompt.py` is the highest-leverage file in the repo. Everything else
exists to put accurate numbers into it.

## Mistakes already made here. Do not repeat them.

**Never tell the agent that doing nothing is fine.** The prompt once said an
empty order list was "valid and often correct" and that the agent was "not
scored on activity". It made zero trades for a whole session and quoted that
line back as its justification. A flat session earns nothing; that is a failure
to find an opportunity, not prudence. There is a test asserting that phrase
never returns.

**Never set the cost hurdle above reality.** Assumed slippage was 5bp/side
against a real large-cap spread of ~0.3bp, and the prompt said to skip anything
under 10bp. Nothing clears that, so sitting out was the *correct* response to
the numbers. It is 1.5bp/side now. State both failure modes together — churning
loses to costs, skipping loses to inaction — so neither reads as safe.

**P&L is never `cash - capital`.** While a position is open that difference is
just money sitting in stock. An interrupted session showed "−$3,060" when its
real P&L was −$1.97. Two separate ad-hoc queries made that mistake. There is now
exactly one implementation — `eval/pnl.py:session_pnl` — and tradectl, the API and
the dashboard all call it. Do not write a fourth.

**A number that needs a level's history cannot come from `exit_plans`.** That
table is keyed by symbol and upserted, so a session that stopped out and
re-entered has one row describing only the last state. Measuring both fires
against it reported 112 seconds of lateness for a loop that acted inside one
second. `exit_plan_events` is the append-only history; use it.

**Whole-share sizing does not work.** At $20 with a $4 cap, `int(4 / 194)` is 0.
Every order is unaffordable and the session silently does nothing. Fractional
quantities are required and Alpaca supports them for real.

**Restarting the API orphans running sessions.** They are asyncio tasks inside
that process. Sessions heartbeat and get reaped as `interrupted` after 120s, but
avoid restarting the API mid-session — two were lost that way before the reaper
existed.

**The plan must be in every tick prompt.** It was absent once: the agent planned
entries at TSLA 303.50, then bought at 304.82 and wrote "Violated plan. Chasing
late entries lost the session."

**A model call takes 9–40 seconds.** Sub-minute policy ticks are not possible;
the call outlasts the interval. `validate()` rejects configs with fewer than two
ticks. The answer to a 10-second tick request is the fast loop, not a faster
model: levels are enforced every 5 seconds by Python and the model is asked every
few minutes.

**An enforced stop is not a venue stop.** The fast loop fires up to one interval
plus feed latency late, on a price that already printed, with no bid/ask to
cross. Say that plainly rather than reporting a level as if it filled at the
level.

**Migrations only run in the engine.** A new `.sql` file needs an engine restart
before the API can see the tables.

## Data source quirks, all verified painfully

- SEC needs a **contact email** in the User-Agent or it returns a bare 403.
- `browse-edgar?type=` is a **prefix** match: asking for Form 4 returns 424B2.
  In one sample 73 of 100 results were the wrong form.
- EDGAR emits one entry **per party**, so one Form 4 appears twice under two
  CIKs. Merge by accession number.
- Yahoo `interval=1m` is 20,753 bytes; `interval=1d` is 1,189 for the same
  price field. It has **no bid/ask at all** and rate-limits bursts hard — a
  diagnostic burst once produced 429s that looked exactly like an IP ban and
  was not.

## Working with Baron

- **Be brief.** He got overwhelmed by long explanations mid-project and said so.
  Answer the question, then stop.
- **Close GitHub issues as they are fixed.** He wants a realistic history.
  Commit trailers (`fixes #6`) do it automatically.
- **Keep the issue list to actionable work.** Methodology lives in
  `docs/NOTES.md`, not as a dozen open issues.
- **Ship working artifacts.** Do not hand him something to compile.
- **The repo is PUBLIC.** The database, raw recordings, logs, journals and `.env`
  are gitignored and must stay that way. `PLAN.md` was purged from history.
- Semantic versioning on releases (patch/minor/major), csproj version and git
  tag in sync.

## State

Paper trading works end to end, with levels enforced between ticks. Yahoo prices
and SEC filings recording continuously. `tradectl eval` scores what has run.

Not done: Alpaca for a real bid/ask (#11), Claude Design UI (#12), live-money
arming, a standalone `flatten.py`.

The eval module exists and its own report says what it still cannot measure. The
biggest gap is the one that matters most: **nothing spawns the baseline twin**, so
`run_baseline` is recorded and never acted on and the LLM-versus-baseline
comparison is unpaired. Second is blinding — `prompt._label_for` relabels symbols
for display only, the order path never inverts the mapping, so a blinded session's
orders can only be rejected. Both are listed in the report's own notes rather than
quietly producing a number.

**Never enable live trading, enter broker credentials, or run a live session.**
That is Baron's action alone. The code path exists and is exercised against
Alpaca's paper endpoint; the arming ceremony is not built and should not be
built without him asking explicitly.
