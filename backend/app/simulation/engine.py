"""Vectorized clearing-price simulation engine.

DESIGN NOTES — why this is shaped the way it is
-----------------------------------------------
The browser implementation looped in JavaScript: for every trial, for every
rival, draw a bid and bump a histogram bucket. Profiling the shipped app showed
that cost is entirely main-thread CPU and scales linearly with
courses x scenarios x trials x rivals; a 38-course run produced a single
11,888 ms task, which is what makes a browser tab unresponsive.

Moving that same loop into pure Python would be far SLOWER, not faster. So the
engine here is written to do the work in whole-array operations:

  * one batched RNG call produces every rival bid for a whole trial block
  * strategy assignment is a vectorized searchsorted over a cumulative mix
  * the per-trial histogram is built with a single bincount over a flattened
    (trial, rival) index, not a Python loop
  * win probability at every bid level comes from one reverse cumulative sum
    per trial block, giving all 76 bid levels at once
  * nothing allocates inside the hot path except the per-block buffers

Cancellation is cooperative and checked at block boundaries, so a running job
stops in bounded time instead of after burning the whole budget.
"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------- competition

@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    ratio: float          # expected bidders per seat
    cap_share: float      # fraction bidding exactly the cap
    mid_lo: float
    mid_hi: float
    low_share: float
    blurb: str
    comparison_only: bool = False


MODES: dict[str, Mode] = {
    "HIGH": Mode("HIGH", "High competition (default)", 1.35, 0.15, 0.65, 0.95, 0.12,
                 "Assumes every course is oversubscribed. Rival bids concentrate in the upper half "
                 "of the legal range."),
    "VERY_HIGH": Mode("VERY_HIGH", "Very high competition", 1.75, 0.22, 0.70, 0.97, 0.08,
                      "Popular, broadly accessible or graduation-critical courses at up to 2x capacity."),
    "EXTREME": Mode("EXTREME", "Extreme stress", 2.5, 0.30, 0.75, 1.00, 0.05,
                    "Adversarial. Large clusters of rivals bidding at the cap, correlated demand."),
    "OPTIMISTIC": Mode("OPTIMISTIC", "Optimistic comparison scenario", 0.70, 0.04, 0.25, 0.70, 0.45,
                       "Comparison only - not used for the conservative recommendation.",
                       comparison_only=True),
}
DEFAULT_MODE = "HIGH"
STRESS_GRID = ("HIGH", "VERY_HIGH", "EXTREME")

# strategy bands: (lo, hi) as a fraction of the cap. 'cap' bids exactly the cap.
_BASE_STRATEGIES = (
    ("defensive", 0.34, 0.80, 0.99),
    ("aggressive", 0.28, 0.65, 0.90),
    ("balanced", 0.26, 0.45, 0.75),
)


def strategy_mix(mode: Mode) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (weights, lo, hi) arrays. Index 0 is the cap strategy, last is 'low'."""
    rest = max(0.0, 1.0 - mode.cap_share - mode.low_share)
    bw = sum(s[1] for s in _BASE_STRATEGIES)
    w = [mode.cap_share]
    lo = [1.0]
    hi = [1.0]
    for _name, base_w, l, h in _BASE_STRATEGIES:
        w.append(rest * (base_w / bw))
        lo.append(l)
        hi.append(h)
    w.append(mode.low_share)
    lo.append(0.02)
    hi.append(0.35)
    return (np.asarray(w, dtype=np.float64),
            np.asarray(lo, dtype=np.float64),
            np.asarray(hi, dtype=np.float64))


# ---------------------------------------------------------------- popularity

_POP_FACTORS = {
    "open_to_all_majors": (1.30, "open as a University-Wide Elective, so the whole campus can bid",
                           "timetable-derived"),
    "satisfies_common": (1.25, "satisfies a Common Core requirement everyone must clear",
                         "timetable-derived"),
    "specialisation": (1.20, "counts toward a published specialisation bucket", "timetable-derived"),
    "sole_section": (1.20, "only one section exists, so demand cannot spread out", "timetable-derived"),
    "small_section": (1.15, "fewer than 40 seats", "timetable-derived"),
    "convenient_slot": (1.15, "sits in a mid-morning or early-afternoon weekday slot",
                        "timetable-derived"),
    "hot_topic": (1.25, "subject area with unusually high student demand (AI / data science)",
                  "model-inferred"),
    "broad_appeal": (1.20, "broad-appeal introductory subject", "model-inferred"),
}

_AI_DS = ("machine learning", "deep learning", "artificial intelligence", "data scien",
          "information retrieval", "neural", "vision", "nlp", "natural language", "generative")
_BROAD = ("python", "programming", "introduction", "foundations", "basics", "communication",
          "psychology", "economics", "entrepreneur", "design thinking", "film", "music", "sport")

# Up to 5 provenance factors can legitimately co-occur on one course (small AND sole-section
# AND specialisation AND convenient-slot AND hot-topic), and they were multiplied together
# uncapped - 1.15 x 1.20 x 1.20 x 1.15 x 1.25 = ~2.38x. That pushes a single course's effective
# rivals-per-seat ratio under "High competition" (base 1.35x) to 3.2x, well past what "Extreme
# stress" (2.5x) itself represents - the tiers stop being distinct. Capping keeps a maximally
# popular course pinned near the top of ITS OWN tier instead of silently jumping into the next
# one. 1.85 is deliberate, not derived: at that ceiling, HIGH's worst case (1.35 x 1.85 = 2.5)
# lands exactly on EXTREME's own uncapped base ratio - "as bad as it gets for this one course
# under High" is allowed to feel like "Extreme," never worse.
POPULARITY_CAP = 1.85


def popularity(course: dict, opts: dict | None = None) -> dict:
    """Course-level popularity multiplier with per-factor provenance.

    Never derived from category capacity - that is what produced the old
    "55 rivals for a 120-seat course" failure. The combined provenance-based
    multiplier is capped (see POPULARITY_CAP) so stacking several real factors
    on one course cannot silently escape its competition tier; the user's own
    estimate is applied after the cap and is not capped, since it is a direct
    input, not a compounding assumption.
    """
    opts = opts or {}
    m = 1.0
    reasons: list[dict] = []

    def add(key: str) -> None:
        nonlocal m
        mult, why, src = _POP_FACTORS[key]
        m *= mult
        reasons.append({"key": key, "multiplier": mult, "why": why, "provenance": src})

    if course.get("category") == "UWE" or course.get("open_as_uwe"):
        add("open_to_all_majors")
    if course.get("category") == "CCC":
        add("satisfies_common")
    if opts.get("in_specialisation"):
        add("specialisation")
    seats = course.get("seats") or 30
    if seats < 40:
        add("small_section")
    if course.get("section_count", 1) <= 1:
        add("sole_section")
    if course.get("convenient_slot"):
        add("convenient_slot")

    title = f"{course.get('title', '')} {course.get('code', '')}".lower()
    if any(k in title for k in _AI_DS):
        add("hot_topic")
    elif any(k in title for k in _BROAD):
        add("broad_appeal")

    uncapped = m
    capped = uncapped > POPULARITY_CAP
    if capped:
        m = POPULARITY_CAP
        reasons.append({"key": "popularity_cap", "multiplier": round(POPULARITY_CAP / uncapped, 4),
                        "why": f"stacked factors would have reached {round(uncapped, 2)}x; capped at "
                               f"{POPULARITY_CAP}x so they cannot exceed the next competition tier",
                        "provenance": "model-inferred"})

    up = opts.get("user_popularity")
    if up is not None and abs(up - 1.0) > 1e-9:
        m *= up
        reasons.append({"key": "user_estimate", "multiplier": up,
                        "why": "your own popularity estimate", "provenance": "user-entered"})
    return {"multiplier": m, "reasons": reasons, "capped": capped}


def expected_rivals(course: dict, mode_id: str, opts: dict | None = None) -> dict:
    """Expected rival count. A live observed count REPLACES the model, never blends."""
    opts = opts or {}
    live = opts.get("live_bidders")
    if live is not None and live >= 0:
        return {
            "lambda": float(max(0, live - 1)),
            "source": "live",
            "observed_at": opts.get("live_observed_at"),
            "round": opts.get("live_round"),
            "note": "Observed on the University platform. Replaces the model estimate entirely.",
            "popularity": None,
        }
    mode = MODES.get(mode_id, MODES[DEFAULT_MODE])
    pop = popularity(course, opts)
    seats = course.get("seats") or 30
    lam = seats * mode.ratio * pop["multiplier"]
    if not mode.comparison_only:
        # in any stress mode demand must exceed supply - that is the whole point
        lam = max(lam, seats * 1.25)
    return {
        "lambda": float(max(0.0, lam - 1)),
        "source": "stress-default",
        "mode": mode.id,
        "note": f"No live data. Modelled under the {mode.label} assumption.",
        "popularity": pop,
    }


# ---------------------------------------------------------------- the engine

class Cancelled(Exception):
    """Raised when a cooperative cancellation check fires."""


@dataclass
class SimResult:
    win: np.ndarray            # P(win) at each integer bid 0..cap
    paid: np.ndarray           # E(price | win) at each bid
    ci: np.ndarray             # 95% half-width on win
    beat_q: dict               # quantiles of the price you must beat
    trials: int
    lam: float
    cap: int


def simulate_course(
    seats: int,
    lam: float,
    cap: int,
    mode: Mode,
    trials: int,
    seed: int,
    key: str = "",
    dispersion: float = 0.18,
    block: int = 2048,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> SimResult:
    """Simulate one course across `trials` independent markets.

    Everything is done in whole-array NumPy operations over blocks of trials.
    """
    K = cap + 1
    win_acc = np.zeros(K, dtype=np.float64)
    sq_acc = np.zeros(K, dtype=np.float64)
    paid_acc = np.zeros(K, dtype=np.float64)
    beat_all = np.empty(trials, dtype=np.int32)

    if lam <= 0:
        return SimResult(np.ones(K), np.zeros(K), np.zeros(K),
                         {"p05": 0, "p50": 0, "p95": 0}, trials, 0.0, cap)

    rng = np.random.default_rng(abs(hash((seed, key))) % (2 ** 63))
    w, lo, hi = strategy_mix(mode)
    cw = np.cumsum(w)
    cw = cw / cw[-1]

    done = 0
    while done < trials:
        if should_cancel is not None and should_cancel():
            raise Cancelled()
        n = min(block, trials - done)

        # rival headcount per trial - real uncertainty is in who turns up
        counts = rng.poisson(lam, size=n).astype(np.int64)
        total = int(counts.sum())

        if total == 0:
            beat_all[done:done + n] = 0
            win_acc += n
            sq_acc += n
            done += n
            if on_progress:
                on_progress(done)
            continue

        # one flat array of every rival across the whole block
        trial_idx = np.repeat(np.arange(n, dtype=np.int64), counts)

        u = rng.random(total)
        strat = np.searchsorted(cw, u, side="right")
        np.clip(strat, 0, len(w) - 1, out=strat)

        band_lo = lo[strat]
        band_hi = hi[strat]
        t = 1.0 - np.power(rng.random(total), 1.6)          # cluster toward band top
        frac = band_lo + (band_hi - band_lo) * t
        frac *= np.exp(dispersion * rng.standard_normal(total))   # idiosyncratic spread
        is_cap = strat == 0
        frac[is_cap] = 1.0

        bids = np.rint(frac * cap).astype(np.int32)
        np.clip(bids, 0, cap, out=bids)

        # per-trial histogram in a single bincount over a flattened index
        flat = trial_idx * K + bids
        hist = np.bincount(flat, minlength=n * K).reshape(n, K)

        # rivals bidding strictly ABOVE each level b  == reverse cumsum shifted
        rev = np.cumsum(hist[:, ::-1], axis=1)[:, ::-1]      # rivals at >= b
        above = np.empty_like(rev)
        above[:, :-1] = rev[:, 1:]
        above[:, -1] = 0

        eq = hist
        room = seats - above
        p = np.where(room <= 0, 0.0, np.minimum(1.0, room / (eq + 1.0)))

        # price to beat = seats-th highest rival bid  (0 if fewer rivals than seats)
        # first level from the top where cumulative >= seats
        ge = rev >= seats
        has = ge.any(axis=1)
        # rev is non-increasing in b, so the LAST True along b is the seats-th highest
        idx = K - 1 - np.argmax(ge[:, ::-1], axis=1)
        c1 = np.where(has, idx, 0).astype(np.int32)
        beat_all[done:done + n] = c1

        # price actually paid if you win: min(your bid, (seats-1)-th highest rival)
        ge2 = rev >= max(seats - 1, 1)
        has2 = ge2.any(axis=1)
        idx2 = K - 1 - np.argmax(ge2[:, ::-1], axis=1)
        c2 = np.where(has2, idx2, 0).astype(np.int32)
        if seats - 1 <= 0:
            c2 = np.full(n, cap, dtype=np.int32)
        unfilled = (counts + 1) <= seats

        levels = np.arange(K, dtype=np.int32)[None, :]
        price = np.minimum(levels, c2[:, None])
        price = np.where(unfilled[:, None], 0, price)

        win_acc += p.sum(axis=0)
        sq_acc += (p * p).sum(axis=0)
        paid_acc += (p * price).sum(axis=0)

        done += n
        if on_progress:
            on_progress(done)

    with np.errstate(invalid="ignore", divide="ignore"):
        paid = np.where(win_acc > 0, paid_acc / win_acc, 0.0)
    mean = win_acc / trials
    var = np.maximum(0.0, sq_acc / trials - mean * mean)
    ci = 1.96 * np.sqrt(var / trials)

    beat_sorted = np.sort(beat_all)
    q = {
        "p05": int(beat_sorted[int(0.05 * (trials - 1))]),
        "p50": int(beat_sorted[int(0.50 * (trials - 1))]),
        "p95": int(beat_sorted[int(0.95 * (trials - 1))]),
        "at_cap_share": float((beat_sorted >= cap).mean()),
        "zero_share": float((beat_sorted <= 0).mean()),
    }
    return SimResult(mean, paid, ci, q, trials, float(lam), cap)
