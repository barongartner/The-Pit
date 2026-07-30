"""Statistics, hand-rolled, because the dependency budget is three packages.

Nothing here is clever. What matters is that every function returns ``None``
rather than a number when the sample cannot support one -- a standard deviation
from two sessions is not a standard deviation, and a correlation over three
episodes is a coincidence with a decimal point.

The project's central risk is not a wrong formula, it is a right formula applied
to four sessions and read as a result. NOTES.md: *one session is a sample, not a
result. Run twenty and one will look brilliant on noise alone.* So the tests that
matter here are the ones asserting that small samples come back empty-handed.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

# Two-sided 95% and 80% power, the constants behind `sessions_needed`.
Z_95 = 1.959964
Z_POWER_80 = 0.8416

# Below this, a spread is not a spread. Printed as an n rather than an sd.
MIN_N_FOR_SD = 5


def mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def median(xs: Sequence[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def stdev(xs: Sequence[float]) -> float | None:
    """Sample standard deviation. None below two points."""
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def percentile(xs: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, matching `FetchLogRepo.uptime`.

    Deliberately the same crude method used for feed latency: two percentile
    definitions in one codebase produce numbers that disagree by a rank and an
    afternoon of confusion.
    """
    if not xs:
        return None
    s = sorted(xs)
    idx = min(len(s) - 1, max(0, int(len(s) * q)))
    return s[idx]


def wilson(k: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Confidence interval for a proportion, Wilson score.

    Not the normal approximation: at these sample sizes it produces intervals
    that include negative rates, and a win rate of 2/3 deserves to be shown as
    [21%, 94%] rather than as 67%.
    """
    if n <= 0:
        return None
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - spread), min(1.0, centre + spread)


def kendall_tau_b(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation, tie-corrected.

    Tau-b rather than Pearson because conviction is an ordinal 1-10 scale and
    P&L is heavy-tailed: one 40-cent winner would dominate a Pearson coefficient
    computed over cents.

    None when either side is entirely tied, which is the honest answer -- a
    session where every order carried conviction 7 says nothing about conviction.
    """
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    concordant = discordant = tx = ty = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            if dx == 0 and dy == 0:
                tx += 1
                ty += 1
            elif dx == 0:
                tx += 1
            elif dy == 0:
                ty += 1
            elif (dx > 0) == (dy > 0):
                concordant += 1
            else:
                discordant += 1

    pairs = n * (n - 1) / 2
    denom = math.sqrt((pairs - tx) * (pairs - ty))
    if denom == 0:
        return None
    return (concordant - discordant) / denom


def permutation_p(
    xs: Sequence[float], ys: Sequence[float], *, trials: int = 20_000,
    seed: int = 20260729,
) -> float | None:
    """Two-sided p for a difference in means, by shuffling the labels.

    Seeded, so the same data always gives the same p. An unseeded p-value that
    drifts between runs of the same report is indistinguishable from a result
    that changed.
    """
    if len(xs) < 2 or len(ys) < 2:
        return None
    observed = abs(sum(xs) / len(xs) - sum(ys) / len(ys))
    pool = list(xs) + list(ys)
    cut = len(xs)
    rng = random.Random(seed)
    hits = 0
    for _ in range(trials):
        rng.shuffle(pool)
        a, b = pool[:cut], pool[cut:]
        if abs(sum(a) / cut - sum(b) / len(b)) >= observed:
            hits += 1
    # +1 in both parts: a p-value of exactly zero claims more than 20,000
    # shuffles can support.
    return (hits + 1) / (trials + 1)


def sign_test_p(diffs: Sequence[float]) -> float | None:
    """Exact two-sided sign test on paired differences.

    A sign test rather than a paired t-test: at four pairs, normality is an
    assumption with nothing behind it, and the sign test needs none. Zero
    differences are dropped, which is the standard treatment and is stated here
    because it changes n.
    """
    non_zero = [d for d in diffs if d != 0]
    n = len(non_zero)
    if n == 0:
        return None
    k = sum(1 for d in non_zero if d > 0)
    tail = min(k, n - k)
    cumulative = sum(math.comb(n, i) for i in range(tail + 1))
    return min(1.0, 2 * cumulative / (2 ** n))


def sessions_needed(sd: float | None, effect: float) -> int | None:
    """Sessions per arm to detect `effect` at 95% confidence and 80% power.

    Printed beside every arm comparison on purpose. NOTES.md calls the
    sample-size problem arithmetic rather than pessimism; this is the arithmetic,
    and having it on screen is what stops the fourth completed session from being
    read as an answer.
    """
    if sd is None or sd <= 0 or effect <= 0:
        return None
    return math.ceil(((Z_95 + Z_POWER_80) * sd / effect) ** 2)
