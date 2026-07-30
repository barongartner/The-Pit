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

## An enforced stop is still late

The fast loop (`session/fastloop.py`) checks levels every 5 seconds against
last-trade snapshots from a feed that itself updates about every 5 seconds. So a
stop here fires **late by up to one interval plus feed latency**, on a price that
already printed. It cannot see the path between two snapshots, and it has no
bid/ask to cross.

That is a large improvement on the previous behaviour -- a level was checked only
when the model was next asked, up to a full policy tick late -- and it is not the
same thing as a resting stop order at a venue. Measured slippage past a level
belongs in the eval module once it exists; do not report enforced stops as if
they filled at the level.

Two conservative choices inside it, both to keep the paper result from
flattering: a breach is checked before a trailing stop is raised, and a stop and
a target reachable in the same interval resolve as the stop.

## What the eval module refuses to answer

`tradectl eval` prints exclusions before it prints means, and withholds numbers
the sample cannot support: no standard deviation under five sessions, no rank
correlation under twenty closed episodes, no cost-drag ratio on a losing session
(where `costs / gross` is negative and reads as a benefit). Every rate carries a
Wilson interval, and the arm comparison carries the sessions-per-arm the observed
spread would actually need.

Sessions are excluded, not adjusted, when: the arm is unknown (a session that died
before its first model call has no decisions and must not be counted as an LLM
run), the cash rebuilt from fills disagrees with the cached balance, a held symbol
has no tick at or before the mark instant, or fill tiers are mixed. That last one
raises rather than averaging.

Structurally unanswerable today, and stated in the report rather than fudged:

- **LLM versus baseline on the same tape.** Nothing links a session to its
  control; the twin is never spawned. Pairing is by overlapping clock and
  universe, which is a substitute, not a control.
- **Reasoning versus recall.** The blinded arms cannot execute at all: the label
  mapping is display-only and never inverted in the order path.
- **Per-trade news attribution.** Headlines are interpolated into the prompt; no
  `news.id` is stored against a decision.
- **The counterfactual of a rejected order.** Intended levels are now recorded on
  rejections, but pricing what would have happened needs a replay harness.

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
