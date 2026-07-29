# Notes

Background reasoning that used to be spread across a dozen issues. Nothing here
blocks anything. Read it when the relevant part starts to matter.

## What this project can and cannot show

**Can, in weeks:** whether the model reasons about price action or recalls
tickers; whether news access changes decisions; whether stated conviction
predicts outcomes; whether the LLM beats the deterministic baseline.

**Cannot:** whether it has genuine alpha. Detecting a Sharpe of 1.0 at
conventional confidence needs years of live data, and testing many
configurations makes it worse. This is arithmetic, not pessimism.

The practical consequence: **one session is a sample, not a result.** Run twenty
and one will look brilliant on noise alone. Aggregate before concluding.

## The baseline is the benchmark

Not buy-and-hold, which flatters or damns arbitrarily depending on the regime.
The honest comparison is the identical engine with the model replaced by a rule
(`agent/stub.py`). If the LLM cannot beat a five-minute momentum rule, the
Python did the work.

## Costs decide everything at this frequency

Every round trip pays the spread. At ~20 round trips an hour that is roughly
50bp/hour of hurdle before any edge exists. This is why the prompt states the
cost as a number, and why an unknown cost is said out loud rather than omitted
-- a missing cost reads as "free".

## Paper fills are optimistic and always will be

Unmodeled, all in the same direction: adverse selection (resting orders fill
exactly when the market is about to move against you), queue position, market
impact, partial fills, borrow cost on shorts, halts. Reality is worse than the
simulator, never better. Bar-based fills validate *logic*, not *edge*.

Every fill records its `sim_tier` so a bar-derived and a quote-derived run can
never be averaged into one number.

## Backtests are contaminated

The model knows what happened before its training cutoff. No amount of care
fixes this. Live-forward is the only clean protocol.

## Replay cannot be bit-exact

Claude is not deterministic even at temperature 0. What is achievable: classical
strategies replay exactly, and LLM sessions replay by re-running *recorded*
decisions against the same tape.

## The PDT rule is gone

FINRA's pattern-day-trader rule and the $25,000 minimum were repealed effective
2026-06-04; Alpaca removed the API fields on 2026-07-06. No day-trade counter
will be built. For a small cash account the binding constraint is T+1 settlement
and good-faith violations, which were not repealed.

The durable lesson: encode *broker* constraints, not regulations, and assert at
startup that the fields you depend on still exist.

## Why the CLI instead of the API

On the metered API this costs roughly $1,400/month at five agents on a
five-minute loop -- a 17%/yr fee on $100k, which would dominate any result. The
`claude` CLI bills against the subscription, so marginal cost is zero and the
constraint becomes rate windows.

The CLI must be logged in as a CLI. Having the desktop app open does not
authenticate it for other processes.

## Data source quirks, all verified the hard way

- SEC requires a **contact email** in the User-Agent, or a bare 403.
- `browse-edgar?type=` is a **prefix** match: asking for Form 4 returns 424B2.
- EDGAR emits one entry per **party**, so one Form 4 appears twice under two CIKs.
- Yahoo's `interval=1m` is 20,753 bytes; `interval=1d` is 1,189 for the same price.
- Yahoo has **no bid/ask at all**, and rate-limits bursts hard.
