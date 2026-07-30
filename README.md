# The Pit

A local testbed for running independent Claude-powered trading agents against live market
data. Each agent gets a plain-English mandate, its own capital, its own position book, and
its own journal. You can fund them, pause them, kill them, and talk to them.

**Paper execution. The real-money path is built and disabled.**

The question underneath the whole thing: *can an LLM actually trade?* The answer this project
is designed to reach is not "look at the P&L" — see [Honest scope](#honest-scope).

---

## Status

**Paper trading runs end to end.** A session plans, trades, enforces its own stops between
model ticks, flattens on the clock, and reviews itself. The feed records prices and SEC
filings continuously. Ten sessions logged so far, which is a sample and not a result.

Progress lives in [Issues](https://github.com/barongartner/The-Pit/issues), organized by
milestone.

---

## Architecture in one screen

Two processes.

```
engine      the only DB writer. Poller, kill switch + watchdog, raw recorder.
api         FastAPI. Reads the DB read-only, serves the dashboard, fans out WebSocket.
            Sessions currently run as asyncio tasks inside this process.
tradectl    CLI: status, kill, release, sessions, eval, uptime. Works with the API dead.
```

Designed and not built yet: the `commands` table the engine would drain (mutations go
straight through the loopback control router today), a standalone `flatten.py`, and the
live-money arming ceremony. Anything below marked *not built* is in the same state.

Claude sets **policy** on a slow loop (strategy, params, risk posture, watchlist,
conviction). Deterministic Python **executes** on the fast loop. Claude is never in a
sub-minute decision path, which is what makes a 1-5 second inference latency irrelevant.

Concretely: the model returns entry, stop, target, time-stop and trailing levels as
fields. Python checks them against the tape every five seconds and acts on its own --
including while the next model call is in flight. An order that opens a position without
a stop is rejected.

Agents propose. The risk layer disposes. It rejects with a reason and never silently
resizes an order.

## Honest scope

Things this project can establish, and roughly when:

| Question | Timeline |
|---|---|
| Is it analyzing price action, or recalling tickers it saw in training? | 2-6 weeks |
| Does news access improve decisions, or just the prose? | 4-8 weeks |
| Does its stated conviction (1-10) actually predict outcomes? | 8-12 weeks |
| Does the LLM beat the deterministic harness it is steering? | 4-12 weeks |
| Does it have alpha? | **Not answerable.** Years of data, and that is before correcting for testing many mandates. |

That last row is not pessimism, it is arithmetic. A system built to chase it would spend a
year failing to answer it. This one is built around the four questions above, which are
tractable and more interesting anyway.

`uv run tradectl eval` is where those questions get answered, and it is deliberately
grudging about it: exclusions before means, an interval on every rate, no standard
deviation under five sessions, no correlation under twenty episodes, and the
sessions-per-arm the observed spread would actually need printed next to the n it has.

Two design consequences worth knowing up front:

- **Paper fills flatter every strategy.** No adverse selection, no queue position, no borrow
  cost. Bar-based simulation validates *logic*, not *edge*. Every simulated fill is tagged
  with the data tier that produced it so runs at different fidelities can never be averaged.
- **Backtests on data before the model's training cutoff are contaminated** and no amount of
  care fixes it. Live-forward is the only clean protocol.

## Limits

Nothing external limits paper trading. The caps below are ours, and exist to stop a runaway
agent rather than to satisfy a regulator. All config-editable.

Enforced today, in `trading/book.py`, against the database rather than in a prompt:

| Limit | Default | On breach |
|---|---|---|
| Max position size | 20% of session capital | Order rejected |
| Max concurrent positions | 3 | Order rejected |
| Session loss limit | 2%, marked to market | Session halts immediately |
| Quote staleness | 120s | Order rejected — trading on a dead feed is worse than not trading |
| Shorting | off | Order rejected |
| Stop on every opening order | required | Order rejected before it can fill |

The risk profile presets move the first three together: `preserve` (20% / 3 / −2%),
`balanced` (50% / 2 / −15%), `risk_it` (100% / 1 / −60%). The last is the default, because
the account it was built for is $20 that can be lost entirely.

Not built: an order rate limit, a gross exposure cap, a flow-adjusted drawdown index across
sessions, and a permanent halt that survives a restart. A session is the only unit with a
loss limit today.

There is no day-trade count limit. The FINRA pattern-day-trader rule and its $25,000 minimum
were repealed effective 2026-06-04.

## Safety

- **Kill switch:** `touch ~/.thepit/state/KILL`. Checked before every order, and by a
  watchdog running in its own OS thread so it still fires when the asyncio loop is wedged.
  Presence is the signal; the file is never parsed, because a kill switch that fails to parse
  is a kill switch that fails open. If the state directory is unreadable, that counts as
  killed. The state directory is shared across modes on purpose — the brake stops everything,
  not whichever mode happens to be running.
- **A session refuses to call itself done while it still holds stock.** The flatten retries
  through its window and, if the feed is dead and the risk layer keeps refusing, the session
  is recorded as `halted` with the open symbol named. A terminal status that reads as settled
  while a position is open is worse than an ugly one.
- **Paper and live are different types, not a boolean.** Separate databases, separate
  directories, separate credential variable names. Mode is a constructor argument read from
  argv, never from the config file and never mutable at runtime.
- *Not built:* a standalone `flatten.py` brake that imports nothing from this package, CI
  that fails the build on an `if live:` branch, and the live arming ceremony (typed
  confirmation with the date and the account's real equity, expiring at session close).
  **Live trading has never been run and the arming path does not exist.**

## Setup

```bash
uv sync
uv run pytest
```

Requires nothing preinstalled but `uv`. Python 3.12 is managed by uv; your system Python is
untouched. On Windows `uv sync` also pulls `tzdata` by platform marker: the market calendar is
`America/New_York`, Windows ships no tz database, and without it nothing imports at all.

Set a contact address before running. SEC EDGAR requires one in the User-Agent and returns a
bare 403 without it:

```bash
export THEPIT_CONTACT_EMAIL=you@example.com
```

Then two processes:

```bash
uv run python -m thepit.engine.main
```

```bash
uv run python -m thepit.api.main
```

The dashboard is at `http://localhost:8000`. To view it from another machine on your own
network, run a second, read-only listener:

```bash
uv run python -m thepit.api.main --lan
```

Control endpoints are not mounted on that listener. They 404 rather than 403 — the router
does not exist, so there is no check to get wrong.

### Windows

The target deployment.

```powershell
git clone https://github.com/barongartner/The-Pit.git
cd The-Pit
powershell -ExecutionPolicy Bypass -File setup.ps1
```

`setup.ps1` installs uv if missing, fetches Python 3.12, syncs dependencies, runs
the tests, saves your SEC contact email, and checks for the `claude` CLI. Safe to
re-run.

Then two terminals:

```powershell
uv run python -m thepit.engine.main
```

```powershell
uv run python -m thepit.api.main
```

Dashboard at `http://localhost:8000`.

Platform-specific pieces, all already handled in code:

| | |
|---|---|
| Ctrl-C | Windows has no `add_signal_handler`, so it arrives as `KeyboardInterrupt` and skips the WAL checkpoint. Prefer `tradectl kill` or `type nul > %USERPROFILE%\.thepit\state\KILL`, which take the same shutdown path as everything else. |
| Kill switch | A file, so it works identically. `del` the file to release. |
| Colour | `tradectl` disables ANSI unless it detects Windows Terminal. Set `TERM` to force it on. |
| `uvloop` | POSIX-only, and `uvicorn[standard]` already excludes it by platform marker. Slightly higher event-loop overhead on Windows, irrelevant at this scale. |
| Paths | `~/.thepit` resolves to `%USERPROFILE%\.thepit`. |

Not yet verified on a real Windows machine — see issue #16.

## Credentials

Env or Keychain. Never in this repo, never in the database. Separate keys per mode:
`ALPACA_PAPER_KEY_ID` / `ALPACA_LIVE_KEY_ID` — never one variable whose meaning depends on a
flag, so a mixup fails with a 401 instead of trading real money.

Agents run on the `claude` CLI against a Claude Code subscription rather than the metered
API. On the metered API this project would cost roughly $1,400/month at five agents on a
five-minute loop, which is a 17%/yr fee on $100k of capital and would dominate any result it
produced.

## License

TBD.
