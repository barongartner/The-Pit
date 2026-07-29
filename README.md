# The Pit

A local testbed for running independent Claude-powered trading agents against live market
data. Each agent gets a plain-English mandate, its own capital, its own position book, and
its own journal. You can fund them, pause them, kill them, and talk to them.

**Paper execution. The real-money path is built and disabled.**

The question underneath the whole thing: *can an LLM actually trade?* The answer this project
is designed to reach is not "look at the P&L" — see [Honest scope](#honest-scope).

---

## Status

**Stage 1 of 10: data layer + recorder.** Nothing trades yet. The current milestone is a
price and news feed that stays up for 24 continuous hours and records everything it sees.

Progress lives in [Issues](https://github.com/barongartner/The-Pit/issues), organized by
milestone.

---

## Architecture in one screen

Two processes plus emergency scripts.

```
engine      the only DB writer. Poller, fast loop, policy loop, risk, broker adapter.
api         FastAPI. Reads the DB read-only, serves the dashboard, fans out WebSocket.
            Every mutation goes through a `commands` table that the engine drains.
flatten.py  standalone liquidation. Zero imports from the app package, on purpose.
tradectl    CLI: halt, resume, deposit, spawn, status. Works with the API dead.
```

Claude sets **policy** on a slow loop (strategy, params, risk posture, watchlist,
conviction). Deterministic Python **executes** on the fast loop. Claude is never in a
sub-minute decision path, which is what makes a 1-5 second inference latency irrelevant.

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

Two design consequences worth knowing up front:

- **Paper fills flatter every strategy.** No adverse selection, no queue position, no borrow
  cost. Bar-based simulation validates *logic*, not *edge*. Every simulated fill is tagged
  with the data tier that produced it so runs at different fidelities can never be averaged.
- **Backtests on data before the model's training cutoff are contaminated** and no amount of
  care fixes it. Live-forward is the only clean protocol.

## Limits

Nothing external limits paper trading. The caps below are ours, and exist to stop a runaway
agent rather than to satisfy a regulator. All config-editable.

| Limit | Default | On breach |
|---|---|---|
| Max position size | 20% of that agent's capital | Order rejected |
| Max daily loss | 3% | Agent halts for the session |
| Max drawdown | 15% | Agent halts permanently, manual resume only |
| Order rate | 10/min per agent | Order rejected |
| Gross exposure | 100% of agent equity | No leverage by default |
| Short size | 10% | Tighter than long: short losses are unbounded |

Drawdown is computed from a flow-adjusted NAV index, so **a deposit does not reset the
high-water mark** and a withdrawal does not fabricate a drawdown.

There is no day-trade count limit. The FINRA pattern-day-trader rule and its $25,000 minimum
were repealed effective 2026-06-04.

## Safety

- **Kill switch:** `touch ~/.thepit/KILL`. Checked before every order, and by a watchdog
  running in its own OS thread so it still fires when the asyncio loop is wedged. Presence
  is the signal; the file is never parsed, because a kill switch that fails to parse is a
  kill switch that fails open. If the state directory is unreadable, that counts as killed.
- **`flatten.py`** cancels all orders, then liquidates, then halts every agent. It imports
  nothing from this package, so an import error here is not an import error in the brake. It
  is a convergent idempotent loop: re-running it is always safe and always correct.
- **Paper and live are different types, not a boolean.** Separate databases, separate
  directories, separate credential variable names. There is no `if live:` branch anywhere in
  the codebase and CI fails the build if one appears.
- Live mode requires a typed confirmation containing the date and the account's actual
  equity, and the arming expires at session close. Restarting re-runs the ceremony.

## Setup

```bash
uv sync
uv run pytest
```

Requires nothing preinstalled but `uv`. Python 3.12 is managed by uv; your system Python is
untouched.

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
