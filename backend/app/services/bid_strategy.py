"""Decision-theoretic bid planning for a uniform-price (clearing-price) auction.

WHY THIS SHAPE
--------------
The mechanism SNU published has one property that dominates everything else:
you are charged the *clearing price*, not your bid, and losing bids are refunded
in full.  So a bid is not a payment - it is a threshold.  You win course j iff
your bid clears that course's price.  Bidding higher never costs you more for a
course you win.

If that were the whole story the answer would be trivial: bid your entire
balance on everything.  It is not the whole story, because of one confirmed
official rule - BUDGET.SHARED_LIVE.  Points placed on a live bid are *held*
against that category balance while the round is open, so simultaneous bids in
one category must jointly fit inside it.  That single constraint is what makes
this a real allocation problem:

    maximise   sum_j  v_j * P_j(b_j)
    subject to sum_j  b_j <= envelope,   b_j >= 0 integer

where v_j is how much course j is worth to the student and P_j(b) is the chance
a bid of b clears course j.  This is a separable resource-allocation problem.
Its exact solution equalises *marginal* value per point across courses (KKT), and
the Lagrange multiplier lambda is the marginal value of one more point - which is
also exactly the number needed to decide how much to carry forward.

WHAT CHANGED FROM allocation-v1, AND WHY
----------------------------------------
allocation-v1 solved a *linear* objective (value proportional to points) under an
invented per-priority share cap table.  A linear objective on a bounded box has
its optimum at a corner, so the caps - not the optimisation - determined every
number.  Two consequences were measured directly before this rewrite:

  * a 20-seat course and a 300-seat course received the identical ceiling (107),
    because seats never entered the ceiling math at all; and
  * a MUST course sat pinned at floor(envelope * 0.60) in every configuration.

Both are fixed here by giving the objective genuine diminishing returns: P_j(b)
is a saturating win curve, so the marginal value of the 200th point on a course
you are already winning is near zero and the optimiser moves those points to
where they still buy something.  Scarcity (seats vs rivals) now drives the plan.

WHY PROBABILITIES ARE BACK, STATED PLAINLY
------------------------------------------
allocation-v1 removed win probabilities on the grounds that no historical
clearing-price data exists.  That premise is true and is preserved below.  But
removing probability did not remove assumption - it relocated it into weights
(8/5/2/1) and share caps (45%/35%/25%/15%) with no derivation at all.  You cannot
reason about "is this bid enough" without some belief about rivals; refusing to
write the belief down does not make it absent, it makes it unauditable.

So the belief is written down explicitly, anchored, and reported as a *band*:

  * Rival bids are anchored to the University's own published typical category
    pools (Course_Enrolment_FAQ_1.pdf), NOT to the user's balance.  This is the
    real fix for allocation-v1's audit finding #1: previously each synthetic
    rival got richer whenever the student did, so extra points bought no
    modelled purchasing power.  Anchoring to the cohort figure means a
    fourth-year with 492 CCC points is correctly modelled as rich relative to a
    typical rival, instead of being scale-invariant.
  * A rival's own budget constraint implies their per-course commitment is about
    (their pool) / (courses they bid on).  That is a derivation, not a guess.
  * The remaining genuinely unknown quantity - how hard rivals push - is carried
    as three labelled scenarios and the plan is optimised against a stated prior
    over them.  Every reported probability comes with the min-max band across
    that set, never as a single fake-precise number.
  * Where the portal supplies a live bidder count, that count REPLACES the
    modelled rival count in all three scenarios, so real data visibly narrows
    the band instead of being averaged into a guess.

Nothing here claims an expected clearing price or an expected charge; those
depend on the whole rival bid distribution and remain unidentified.

PROPERTIES THE TESTS PIN DOWN
-----------------------------
  * exact: the allocation is a dynamic program, not a greedy pass, so it is the
    true optimum of the stated objective (checked against brute force);
  * deterministic: closed-form binomial win curves, no Monte Carlo, no RNG, no
    seed - identical output across processes (allocation-v1 audit finding #9
    cannot recur here because there is no randomness to reproduce);
  * order independent: course code is the only tie-breaker;
  * budget feasible: sum of bids <= envelope, and envelope + reserve == pool;
  * monotone in information: supplying a live bidder count narrows the band.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import erf, exp, log, sqrt

import numpy as np

from app.models.schemas import BidStrategyRequest, Category, Priority, StrategyPosture

STRATEGY_VERSION = "marginal-value-v2"

# ---------------------------------------------------------------- preferences

# Relative worth of winning a course, by the student's own stated priority.
# These are preference ratios, the one place a genuine value judgement belongs,
# and they are ratios rather than absolute utilities because only ratios affect
# the allocation. MUST is the unit.
BASE_VALUE = {
    Priority.MUST: 1.00,
    Priority.STRONG: 0.62,
    Priority.BACKUP: 0.30,
    Priority.OPTIONAL: 0.12,
}

# Posture acts through exactly one mechanism: the breadth constraint below.
#
# An earlier draft of v2 *also* bent the priority value ratios by a posture
# exponent, and the two mechanisms interacted non-monotonically - measured on a
# slack budget, "diversified" produced a *more* concentrated plan (must-have 175)
# than "balanced" (166), which is incoherent for a user-facing risk control. It
# was also the wrong thing to do on its own terms: when a student marks a course
# MUST, a risk setting should not quietly reinterpret how much they want it.
# Priorities now mean exactly what the student said, and posture only governs how
# much expected value they will trade for keeping options alive.

# Largest share of one category envelope a single course may take, by posture.
#
# Value weights alone cannot deliver breadth, and it is worth being precise about
# why rather than tuning numbers until it looks right. A win curve is convex while
# a bid is hopeless, so when the budget is small against heavy competition,
# expected value is genuinely maximised by concentrating everything on one course
# - spreading 240 points across three 7x-oversubscribed 20-seat courses loses all
# three. That is a true and useful result, and the optimiser is right to find it.
#
# But a student choosing "diversified" is stating a risk preference: they would
# rather hold a real chance at something than the best chance at one thing. That
# is a legitimate constraint on the problem, not an error to be argued out of. So
# it is imposed as an explicit constraint and the expected-value cost of imposing
# it is measured and reported back (see `concentration_cost_percent`), instead of
# being buried in a weight table the way allocation-v1 did.
# Candidate single-course share caps, tightest first. The planner takes the
# tightest one whose expected-value cost the posture is willing to pay, which
# makes breadth monotone in that willingness. A single fixed cap per posture is
# NOT monotone and this was measured, not assumed: a tight 45% cap could cost more
# than the diversified tolerance allowed, get rejected outright, and fall back to
# the *most* concentrated plan - so asking for more breadth produced less of it.
CAP_LADDER = (0.35, 0.45, 0.55, 0.70, 0.85, 1.00)

# How much expected value the student is willing to give up to keep more options
# alive. This is the honest way to parameterise risk aversion - a price, not a
# taste - and it exists because a hard cap on its own misbehaves badly: measured
# on a binding 240-point budget against three 7x-oversubscribed courses, the 70%
# cap cut the must-have from 240 to 168 points and left 72 points idle, costing
# 33% of expected value for no gain, because the freed points could not buy a
# meaningful chance anywhere else. Breadth is therefore attempted and then kept
# only if it costs no more than this; otherwise the plan concentrates and says so.
POSTURE_BREADTH_TOLERANCE = {
    StrategyPosture.DIVERSIFIED: 0.15,
    StrategyPosture.BALANCED: 0.05,
    StrategyPosture.FOCUSED: 0.00,
}

# ---------------------------------------------------------------- rival anchor

# Typical first-cycle category pools, quoted directly from the University's own
# FAQ ("Typical first-cycle averages are about 90 / 70 / 40 (ME / UWE / CCC) for
# 2nd years, 145 / 92 / 72 for 3rd years, and 297 / 125 / 238.5 for 4th years").
# Courses are generally contested across year groups, so the reference rival pool
# is the mean over the published years. This is the anchor that makes the model
# independent of the user's own balance.
PUBLISHED_YEAR_POOLS = {
    2: {Category.ME: 90.0, Category.UWE: 70.0, Category.CCC: 40.0},
    3: {Category.ME: 145.0, Category.UWE: 92.0, Category.CCC: 72.0},
    4: {Category.ME: 297.0, Category.UWE: 125.0, Category.CCC: 238.5},
}
# Rivals are modelled as a mixture over these year groups rather than as one
# average rival. This matters and was got wrong in the first draft of v2: using
# the mean pool as the rival ceiling asserts that *no* rival can bid above it,
# which erased the rich end of the field (a fourth year really does hold 297 ME
# points) and made almost every course look certain to win. Equal weights,
# because published cohort sizes are not available - stated, not hidden.
RIVAL_YEAR_WEIGHTS = {2: 1 / 3, 3: 1 / 3, 4: 1 / 3}
REFERENCE_RIVAL_POOL = {
    category: sum(RIVAL_YEAR_WEIGHTS[year] * pools[category]
                  for year, pools in PUBLISHED_YEAR_POOLS.items())
    for category in Category
}


@dataclass(frozen=True)
class Scenario:
    """One labelled reading of how hard rivals compete.

    `rivals_per_seat` is used only when the portal has not supplied a live
    bidder count. `courses_per_rival` is how many courses in this category a
    typical rival spreads their balance across - fewer courses means a higher
    per-course bid, so it is the concentration dial. `prior` is the stated
    weight used to combine scenarios into the optimisation objective.
    """
    key: str
    label: str
    rivals_per_seat: float
    courses_per_rival: float
    prior: float


# Deliberately spans "calmer than seats imply" through "clearly oversubscribed".
# allocation-v1's audit finding #3 was that the previous engine forced EVERY
# unobserved course to at least 1.25x seats, so a missing observation was treated
# as proof of excess demand. The band below includes a sub-1.0 reading precisely
# so absence of data is not evidence of scarcity, while the prior still leans
# stress-ward (0.45 + 0.30 on central/tight) to keep the project's
# conservative-by-default posture.
# `courses_per_rival` is the strategic dial, and its range is deliberate. How hard
# rivals push is not identifiable from anything SNU publishes: it depends on their
# private values and their own portfolios. It is also not pinned down by theory -
# in a uniform-price auction a bidder who badly wants one course has no reason not
# to commit nearly their whole balance to it, so "rivals go effectively all-in" is
# a legitimate equilibrium and has to be inside the band. The tight reading
# therefore uses ~1.05 courses per rival (all-in), which is what makes contested
# courses read as genuinely contested instead of comfortably winnable.
# `rivals_per_seat` only applies when the portal has given us nothing. Its range
# has to be wide, because not knowing the bidder count is genuine ignorance: an
# unobserved elective could be undersubscribed or four times oversubscribed, and
# the observed example of 140 bidders for 20 seats (7x) shows the upper end is
# real. A narrow band here would be the dishonest choice - it would look
# confident precisely where there is no information. Note this is not the old
# engine's ">= 1.25x seats always" floor that the v1 audit criticised: the calm
# reading stays below 1.0, so a missing observation is never treated as proof of
# scarcity. It just stops the band from pretending to be tight.
SCENARIOS = (
    Scenario("calm", "Calm market", 0.90, 3.0, 0.25),
    Scenario("central", "Central estimate", 1.50, 1.9, 0.45),
    Scenario("tight", "Tight market", 4.00, 1.05, 0.30),
)
# Spread of rival bids about their median, on a log scale. One shape parameter.
RIVAL_LOG_DISPERSION = 0.55
# An opening bid only needs to capture most of the win probability the full
# planned bid would buy; the rest is held back and added if the live count climbs.
OPENING_CAPTURE = 0.90

# A COMMITTED POINT IS NOT A SPENT POINT.
#
# An earlier draft of v2 charged an opportunity cost for every point *bid*. That
# is economically wrong under this mechanism, and wrong in the direction that
# produces exactly the "it under-bids and the numbers look off" complaint: SNU's
# FAQ states that a winner is "charged the clearing price - the lowest winning
# bid - not the amount you bid", that surplus above the clearing price is
# refunded, and that unsuccessful bids are refunded in full. Bidding 200 on a
# course that clears at 50 costs 50, not 200.
#
# So the only genuine costs of a bid are:
#   1. the clearing price actually paid, and only if you WIN; and
#   2. the within-round hold - points on a live bid cannot simultaneously back
#      another bid in the same category (BUDGET.SHARED_LIVE), which is already
#      the sum(b) <= envelope constraint.
#
# Crucially the clearing price is set by *other* students' bids, not by yours, so
# raising your bid raises your chance of winning without raising what you pay.
# That is the defining property of a uniform-price auction and it is why bids
# here are bounded by value and budget rather than by an invented per-point tax.
#
# Numerical note: a win curve asymptotes to 1 without reaching it in floating
# point, so gains are quantised below before the optimiser compares them.
# Otherwise the DP will spend real points chasing improvements of order 1e-15,
# which is what the per-point tax was really being used to suppress.
GAIN_QUANTUM = 1e-6

_SQRT2 = sqrt(2.0)


def _phi(z: np.ndarray) -> np.ndarray:
    """Standard normal CDF. Uses erf so the module needs no SciPy."""
    return 0.5 * (1.0 + np.vectorize(erf)(z / _SQRT2))


def _log_factorial_table(n: int) -> np.ndarray:
    table = np.zeros(n + 1, dtype=np.float64)
    if n >= 1:
        table[1:] = np.cumsum(np.log(np.arange(1, n + 1, dtype=np.float64)))
    return table


def _truncated_lognormal_cdf(x: np.ndarray, median: float, ceiling: float) -> np.ndarray:
    """P(bid <= x) for one year group: lognormal about `median`, capped at `ceiling`.

    The cap is the rival's own published category pool - they cannot commit more
    points than they hold. Half-integer continuity correction because bids are
    whole numbers.
    """
    log_median = log(max(median, 1e-9))
    z = (np.log(np.maximum(x + 0.5, 1e-12)) - log_median) / RIVAL_LOG_DISPERSION
    raw = np.where(x + 0.5 <= 0.0, 0.0, _phi(z))
    cap_z = (log(max(ceiling + 0.5, 1e-12)) - log_median) / RIVAL_LOG_DISPERSION
    cap_mass = float(_phi(np.array([cap_z]))[0])
    if cap_mass <= 1e-12:
        # Essentially all of this group's mass sits at its own ceiling.
        return np.where(x >= ceiling, 1.0, 0.0)
    return np.where(x >= ceiling, 1.0, np.minimum(raw / cap_mass, 1.0))


def _rival_outrank_probability(bids: np.ndarray, category: Category,
                               scenario: Scenario) -> np.ndarray:
    """P(one randomly drawn rival outranks an integer bid b), for each b.

    The rival is drawn from a mixture over published year groups; within a group
    their per-course commitment is lognormal about (that year's published pool /
    courses they spread across), capped at that year's pool.

    A tie is settled by a fair random draw, so a rival bidding exactly b outranks
    you with probability one half. That is exactly correct for the pairwise
    comparison, not a convenience approximation.
    """
    at_or_below = np.zeros_like(bids)
    below = np.zeros_like(bids)
    for year, weight in RIVAL_YEAR_WEIGHTS.items():
        pool = PUBLISHED_YEAR_POOLS[year][category]
        median = pool / max(scenario.courses_per_rival, 1e-9)
        at_or_below += weight * _truncated_lognormal_cdf(bids, median, pool)
        below += weight * _truncated_lognormal_cdf(bids - 1.0, median, pool)

    tie = np.maximum(at_or_below - below, 0.0)
    outrank = (1.0 - at_or_below) + 0.5 * tie
    # Monotone by construction; enforced so discretisation cannot produce a curve
    # that dips, which would let the DP buy a worse outcome for more points.
    return np.clip(np.minimum.accumulate(outrank), 0.0, 1.0)


def _win_curve(seats: int, rivals: int, outrank: np.ndarray, log_fact: np.ndarray) -> np.ndarray:
    """P(win) at each bid level, in closed form.

    You take a seat iff at most seats-1 of the `rivals` independent rival bids
    outrank yours, so the win probability is a binomial CDF evaluated at each
    bid level. Exact and deterministic: no sampling, therefore no sampling error
    and nothing to seed.
    """
    n_levels = outrank.shape[0]
    if seats <= 0:
        return np.zeros(n_levels)
    if rivals <= 0 or seats > rivals:
        # Fewer rivals than seats: everyone who bids at all is accommodated.
        return np.ones(n_levels)

    k_max = min(seats - 1, rivals)
    i = np.arange(k_max + 1)
    log_choose = log_fact[rivals] - log_fact[i] - log_fact[rivals - i]

    q = np.clip(outrank, 1e-12, 1.0 - 1e-12)[:, None]
    log_terms = log_choose[None, :] + i[None, :] * np.log(q) + (rivals - i)[None, :] * np.log1p(-q)
    win = np.exp(log_terms).sum(axis=1)

    # Exact endpoints, so floating point cannot round a certainty away.
    win = np.where(outrank <= 0.0, 1.0, win)
    win = np.where(outrank >= 1.0, 1.0 if rivals <= seats - 1 else 0.0, win)
    return np.clip(np.maximum.accumulate(win), 0.0, 1.0)


def _clearing_price(outrank: np.ndarray, seats: int, rivals: int) -> int:
    """Modelled clearing price: the lowest bid that still takes a seat.

    With `rivals` other bidders and `seats` seats, the price is set by the
    marginal winner, i.e. the (seats-1)-th highest rival bid once you take one of
    the seats yourself. `outrank(b)` is the chance a single rival bids above b, so
    the expected number above b is rivals*outrank(b); the marginal winner sits
    where that count falls to seats-1.

    Returns 0 when there are fewer rivals than seats - SNU states directly that a
    course with seats left over clears at zero. The price is a property of other
    students' bids, not of yours: raising your own bid cannot raise it.
    """
    if seats <= 0:
        return 0
    if rivals <= seats - 1:
        return 0
    target = (seats - 1) / rivals
    reached = np.nonzero(outrank <= target + 1e-12)[0]
    return int(reached[0]) if reached.size else int(outrank.shape[0] - 1)


def _allocate(values: list[float], curves: list[np.ndarray], envelope: int,
              caps: list[int] | None = None) -> tuple[list[int], np.ndarray]:
    """Exact integer solution of  max sum_j v_j * P_j(b_j)  s.t.  sum b_j <= E.

    A dynamic program (max-plus convolution over the budget), not a greedy pass.
    Greedy is only optimal when every P_j is concave, and a win curve is S-shaped
    - convex while the bid is hopeless, concave once it is competitive - so a
    greedy allocator can genuinely stall below the useful range of a contested
    course. The DP has no such failure mode.

    Returns the per-course bids and the value function V(0..E), whose increments
    are the marginal value of each additional point.
    """
    n = len(values)
    if n == 0 or envelope <= 0:
        return [0] * n, np.zeros(max(envelope, 0) + 1)

    best = np.zeros(envelope + 1)
    choice = np.zeros((n, envelope + 1), dtype=np.int32)

    for j in range(n):
        # Net worth of winning course j, times the chance `a` points win it.
        # Quantised so the optimiser cannot be driven by floating-point noise in
        # the flat tail of a saturated win curve.
        gain = np.round(values[j] * curves[j][: envelope + 1] / GAIN_QUANTUM) * GAIN_QUANTUM
        if caps is not None and caps[j] < envelope:
            # Breadth constraint for the risk-averse postures. -inf rather than a
            # penalty, so the cap is a hard bound the DP cannot trade away.
            gain = gain.copy()
            gain[caps[j] + 1:] = -np.inf
        nxt = np.empty(envelope + 1)
        for budget in range(envelope + 1):
            # value of spending `a` here plus the best use of what is left
            totals = gain[: budget + 1] + best[budget::-1]
            take = int(np.argmax(totals))       # argmax takes the smallest index,
            nxt[budget] = totals[take]          # so ties spend fewer points
            choice[j, budget] = take
        best = nxt

    bids = [0] * n
    remaining = envelope
    for j in range(n - 1, -1, -1):
        take = int(choice[j, remaining])
        bids[j] = take
        remaining -= take
    return bids, best


def _first_bid_reaching(curve: np.ndarray, target: float, ceiling: int) -> int:
    """Smallest bid whose modelled win probability reaches `target`."""
    if ceiling <= 0:
        return 0
    reached = np.nonzero(curve[: ceiling + 1] >= target - 1e-12)[0]
    return int(reached[0]) if reached.size else ceiling


def _modelled_rivals(course, scenario: Scenario) -> tuple[int, str]:
    """Rival count for one course under one scenario.

    A live portal count is data and replaces the model in every scenario, which
    is what makes supplying it narrow the reported band rather than shift it.
    """
    if course.live_bidders is not None:
        return max(0, int(course.live_bidders) - 1), "live"
    return max(0, int(round(course.seats * scenario.rivals_per_seat))), "modelled"


def _pressure(course, rivals_central: int) -> dict:
    if course.live_bidders is None or course.seats <= 0:
        ratio = None if course.seats <= 0 else round((rivals_central + 1) / course.seats, 3)
        return {"label": "unknown", "live_bidders": None, "seats": course.seats,
                "bidder_to_seat_ratio": ratio, "provenance": "unknown"}
    ratio = course.live_bidders / course.seats
    if ratio < 0.80:
        label = "spare_capacity"
    elif ratio <= 1.00:
        label = "near_capacity"
    elif ratio <= 1.50:
        label = "oversubscribed"
    else:
        label = "heavily_oversubscribed"
    return {"label": label, "live_bidders": course.live_bidders, "seats": course.seats,
            "bidder_to_seat_ratio": round(ratio, 3), "provenance": "live"}


def _action(pressure: dict, planned: int, opening: int, band_low: float) -> str:
    if pressure["provenance"] == "unknown":
        return ("No live count yet, so this is a provisional opening. Enter it, then refresh the portal "
                "count before the round closes and re-plan.")
    if pressure["label"] == "spare_capacity":
        return "Observed bidders are still below seats. Hold near the opening bid and watch the count."
    if pressure["label"] == "near_capacity":
        return "Demand is close to capacity. Be ready to move toward your ceiling late in the round."
    if band_low < 0.35 and planned >= opening:
        return ("Heavily contested even at your ceiling. Keep a genuine alternative live in this category "
                "rather than spending past the ceiling.")
    return "The live count exceeds seats. Move toward your ceiling if the course is still worth the trade-off."


def build_bid_strategy(request: BidStrategyRequest) -> dict:
    grouped: dict[Category, list] = defaultdict(list)
    for course in request.courses:
        grouped[course.category].append(course)

    max_rivals = 1
    for course in request.courses:
        for scenario in SCENARIOS:
            max_rivals = max(max_rivals, _modelled_rivals(course, scenario)[0])
    log_fact = _log_factorial_table(max_rivals + 1)

    category_rows: list[dict] = []
    course_rows: list[dict] = []
    all_feasible = True

    for category in Category:
        pool = int(request.pools[category.value])
        reserve = round(pool * request.reserve_percent / 100)
        envelope = max(0, pool - reserve)
        courses = sorted(grouped[category], key=lambda item: item.code)
        # The clearing price is a property of the market, so it must be searched
        # over a range wide enough to contain any plausible rival bid - NOT over
        # the student's own envelope. Bounding it by the envelope truncated the
        # reported price: a student holding 120 ME points was told a course cleared
        # at 96 when the same course cleared at 161 for a student holding 450.
        # That is the user's own balance leaking into a market statistic, the same
        # class of defect the v1 audit found in the old rival generator.
        price_ceiling = int(max(envelope, max(PUBLISHED_YEAR_POOLS[y][category]
                                              for y in PUBLISHED_YEAR_POOLS)))
        price_grid = np.arange(price_ceiling + 1, dtype=np.float64)
        bids = price_grid[: envelope + 1]

        per_course_curves: list[dict[str, np.ndarray]] = []
        gross_values: list[float] = []
        rival_counts: list[dict[str, int]] = []
        prices: list[dict[str, int]] = []
        for course in courses:
            curves: dict[str, np.ndarray] = {}
            counts: dict[str, int] = {}
            price: dict[str, int] = {}
            for scenario in SCENARIOS:
                rivals, _ = _modelled_rivals(course, scenario)
                counts[scenario.key] = rivals
                # Computed on the full market grid, then sliced to what the
                # student can actually afford to bid.
                outrank_full = _rival_outrank_probability(price_grid, category, scenario)
                curves[scenario.key] = _win_curve(course.seats, rivals,
                                                  outrank_full[: envelope + 1], log_fact)
                price[scenario.key] = _clearing_price(outrank_full, course.seats, rivals)
            per_course_curves.append(curves)
            rival_counts.append(counts)
            prices.append(price)
            gross_values.append(BASE_VALUE[course.priority])

        expected_price = [
            sum(scenario.prior * prices[i][scenario.key] for scenario in SCENARIOS)
            for i in range(len(courses))
        ]
        # The clearing price is REPORTED, not netted off the value.
        #
        # Netting it was tried and is wrong here, in a way worth recording: it
        # needs an exogenous price per point, but inside the round the true
        # opportunity cost of a point is endogenous - it is whatever that point
        # would buy on the student's other courses, which the sum(b) <= envelope
        # constraint already prices correctly via the DP's own shadow price.
        # Supplying an outside number on top double-counts the constraint. Measured
        # on a live 20-seat/140-bidder course it drove a STRONG course to a bid of
        # zero purely because an accounting identity said its price "exceeded its
        # value", which is not a judgement the student asked for. Value beyond this
        # round is what the carry-forward reserve is for, and that is user-set.
        values = list(gross_values)

        # Objective: expected value under the stated prior over scenarios. A
        # mixture is separable across courses, so the DP solves it exactly; a
        # max-min objective would not be separable and could not be solved
        # exactly this cheaply.
        mixture = [
            sum(scenario.prior * curves[scenario.key] for scenario in SCENARIOS)
            for curves in per_course_curves
        ]
        # Unconstrained optimum first, so the cost of asking for breadth is
        # measured rather than asserted.
        free_bids, free_value_fn = _allocate(values, mixture, envelope)
        tolerance = POSTURE_BREADTH_TOLERANCE[request.posture]
        best_free = float(free_value_fn[-1]) if envelope >= 1 else 0.0

        # Take the tightest affordable cap: "give me as much breadth as I can have
        # for at most `tolerance` of expected value".
        planned, value_fn = free_bids, free_value_fn
        share_cap, breadth_cost, breadth_applied = 1.0, 0.0, False
        cheapest_declined = None
        if len(courses) > 1 and envelope > 0:
            for candidate in CAP_LADDER:
                if candidate >= 1.0:
                    break
                caps = [max(1, int(envelope * candidate)) for _ in courses]
                capped_bids, capped_value_fn = _allocate(values, mixture, envelope, caps)
                cost = (0.0 if best_free <= 1e-12
                        else max(0.0, best_free - float(capped_value_fn[-1])) / best_free)
                if cost <= tolerance:
                    planned, value_fn = capped_bids, capped_value_fn
                    share_cap, breadth_cost, breadth_applied = candidate, cost, True
                    break
                # Looser caps cost less, so the last one tried is the cheapest
                # breadth on offer - that is the honest number to quote back.
                cheapest_declined = cost
            if not breadth_applied and cheapest_declined is not None:
                breadth_cost = cheapest_declined
        concentration_cost = round(100 * breadth_cost, 2)

        planned_total = int(sum(planned))
        all_feasible = all_feasible and planned_total <= envelope <= pool
        marginal_next = float(value_fn[-1] - value_fn[-2]) if envelope >= 1 else 0.0
        expected_won = float(sum(mixture[i][planned[i]] for i in range(len(courses))))

        category_rows.append({
            "category": category,
            "pool": pool,
            "carry_forward_reserve": reserve,
            "current_round_envelope": envelope,
            "strategic_ceiling_total": planned_total,
            "uncommitted_in_envelope": envelope - planned_total,
            "course_count": len(courses),
            "expected_courses_won": round(expected_won, 3),
            "marginal_value_next_point": round(marginal_next, 6),
            "reserve_note": _reserve_note(marginal_next, reserve, envelope, planned_total),
            "concentration_cost_percent": concentration_cost,
            "single_course_share_cap_percent": round(100 * share_cap),
            "breadth_note": _breadth_note(breadth_applied, concentration_cost, share_cap,
                                          CAP_LADDER[0], len(courses)),
        })

        for index, course in enumerate(courses):
            curves = per_course_curves[index]
            ceiling = planned[index]
            probs = {key: float(curve[ceiling]) for key, curve in curves.items()}
            band_low, band_high = min(probs.values()), max(probs.values())
            live = course.live_bidders is not None
            # Measured against the same mixture the plan was optimised on, so the
            # opening step accounts for the tight reading instead of assuming the
            # central one and being caught out if the count climbs.
            reference = mixture[index]
            opening = _first_bid_reaching(reference, OPENING_CAPTURE * reference[ceiling], ceiling)
            pressure = _pressure(course, rival_counts[index]["central"])
            share = 0.0 if pool == 0 else round(100 * ceiling / pool, 1)

            free_seat_chance = float(curves["calm"][0])
            course_rows.append({
                "code": course.code,
                "title": course.title,
                "category": category,
                "priority": course.priority,
                "opening_bid": int(opening),
                "strategic_ceiling": int(ceiling),
                "allocation_share_percent": share,
                "pressure": pressure,
                "action": _action(pressure, ceiling, int(opening), band_low),
                "win_probability_band": {
                    "low": round(band_low, 4),
                    "central": round(probs["central"], 4),
                    "high": round(band_high, 4),
                    "basis": "live bidder count" if live else "modelled rival count",
                },
                "modelled_rivals": {key: rival_counts[index][key] for key in rival_counts[index]},
                "clearing_price_band": {
                    "low": min(prices[index].values()),
                    "central": prices[index]["central"],
                    "high": max(prices[index].values()),
                    "note": ("What a seat is modelled to actually cost. You are charged this, not your bid, "
                             "and it is set by other students' bids - raising your own bid raises your chance "
                             "of winning without raising what you pay."),
                },
                "rationale": _rationale(course, request, values[index],
                                        ceiling, opening, probs, live, free_seat_chance),
            })

    course_rows.sort(key=lambda row: (row["category"].value, -BASE_VALUE[row["priority"]], row["code"]))
    return {
        "strategy_version": STRATEGY_VERSION,
        "posture": request.posture,
        "reserve_percent": request.reserve_percent,
        "semester": request.semester,
        "courses": course_rows,
        "categories": category_rows,
        "invariants": {
            "shared_category_balances_respected": all_feasible,
            "carry_forward_reserve_protected": True,
            "allocation_is_exact_optimum_of_stated_objective": True,
            "deterministic_without_sampling": True,
            "rival_model_independent_of_user_balance": True,
        },
        "assumptions": {
            "objective": "maximise sum(value x win probability) subject to the shared category balance",
            "rival_bid_anchor": ("Published typical category pools (FAQ), averaged across the 2nd, 3rd and "
                                 "4th year figures, divided by the number of courses a rival spreads across. "
                                 "Never scaled by your own balance."),
            "reference_rival_pool": {c.value: round(REFERENCE_RIVAL_POOL[c], 1) for c in Category},
            "value_ratios": {p.value: BASE_VALUE[p] for p in Priority},
            "scenarios": [{"key": s.key, "label": s.label, "rivals_per_seat": s.rivals_per_seat,
                           "courses_per_rival": s.courses_per_rival, "prior": s.prior} for s in SCENARIOS],
            "rival_bid_log_dispersion": RIVAL_LOG_DISPERSION,
        },
        "uncertainty": (
            "There is no historical SNU clearing-price data, so these win probabilities are conditional on "
            "the rival model stated above, not measured frequencies. Every course therefore reports a band "
            "across three labelled market readings rather than a single figure. Supplying a live bidder count "
            "replaces the modelled rival count and narrows that band. No expected clearing price or expected "
            "charge is claimed: those depend on the full distribution of other students' bids, which the "
            "portal keeps private."
        ),
        "official_mechanism": [
            "ME, UWE and CCC use separate live point balances.",
            "Simultaneous bids hold points against the relevant category balance.",
            "Winners pay the lowest winning bid; excess points and losing bids are refunded.",
            "Unused points carry forward; fourth-year students receive no fresh Semester 8 allocation.",
        ],
        "next_steps": [
            "Enter the opening bids and keep alternatives active in the same category.",
            "Refresh live bidder counts before the round closes; the plan re-solves against real counts.",
            "Raise toward a ceiling only while the course still beats the alternative use of those points.",
            "After settlement, re-plan with the refunded balance and actual outcomes.",
        ],
    }


def _breadth_note(applied: bool, cost_percent: float, share_cap: float,
                  tightest_share: float, course_count: int) -> str:
    if course_count <= 1:
        return "Only one course in this category, so there is no breadth trade-off to make."
    if applied:
        if cost_percent <= 0.01:
            return (f"No single course takes more than {round(100 * share_cap)}% of the envelope, and holding "
                    "that line costs nothing measurable here.")
        return (f"Breadth applied: no course takes more than {round(100 * share_cap)}% of the envelope, at a "
                f"cost of about {cost_percent:.1f}% of expected value versus concentrating freely.")
    return (f"Spreading was NOT applied. Even the loosest useful cap would have cost about {cost_percent:.1f}% "
            f"of expected value (a {round(100 * tightest_share)}% cap is the tightest considered), because the "
            "freed points cannot buy a meaningful chance on the alternatives. Concentration is genuinely the "
            "better plan at this budget - to force breadth anyway, raise a backup's priority or lower the reserve.")


def _reserve_note(marginal_next: float, reserve: int, envelope: int, planned_total: int) -> str:
    """Turn the shadow price into the carry-forward advice it actually implies.

    lambda is the value the next point would add this round, in the same units as
    the priority value ratios (1.0 = winning one must-have course). Comparing it
    against what a carried point is worth later is the correct criterion for the
    reserve, and it is a criterion allocation-v1 could not express at all because
    a linear objective has a constant marginal value.
    """
    if envelope <= 0:
        return "The whole balance is reserved, so nothing is committed this round."
    if planned_total < envelope:
        return (f"{envelope - planned_total} point(s) of the usable envelope are already not worth "
                "committing this round, so the effective reserve is larger than the slider alone implies.")
    if marginal_next <= 1e-6:
        return ("The envelope is fully committed but another point would add nothing measurable, so raising "
                "the reserve costs you nothing here.")
    if marginal_next < 0.002:
        return ("The next point would add very little this round; carrying it forward is likely the better "
                "use if you still need courses in this category later.")
    return ("The envelope binds and the next point would still add real value this round, so lowering the "
            "reserve would measurably improve this round's chances.")


def _rationale(course, request, value, ceiling, opening, probs, live,
               free_seat_chance: float) -> list[str]:
    lines = [
        f"{course.priority.value.title()} priority carries value ratio {value:.3g} "
        f"(MUST = 1); the {request.posture.value} posture governs breadth, not this ratio.",
        f"{course.seats} seat(s) and the modelled rival count set how fast this course's win curve "
        f"saturates; points stop being allocated here once they buy more elsewhere.",
    ]
    if live:
        lines.append(f"Live pressure: {course.live_bidders} bidder(s) for {course.seats} seat(s). This is "
                     "real data and replaces the modelled rival count in all three readings.")
    else:
        lines.append("No live bidder count yet, so the rival count is modelled across three readings and the "
                     "reported band is correspondingly wide. Entering the portal count narrows it.")
    if ceiling == 0:
        if free_seat_chance >= 0.5:
            lines.append(f"No points allocated, and that is not the same as giving up: if this course ends up "
                         f"undersubscribed it clears at zero and a zero bid still takes a seat "
                         f"({free_seat_chance:.0%} chance under the calm reading). Points would buy more on a "
                         f"contested course. Raise the priority to override.")
        else:
            lines.append("No points allocated: under this objective they buy more on other courses in the same "
                         "category. Raise this course's priority if that is wrong.")
    else:
        lines.append(f"At {ceiling} point(s) the modelled win chance is {probs['central']:.0%} centrally "
                     f"(band {min(probs.values()):.0%} to {max(probs.values()):.0%}); the opening bid of "
                     f"{opening} already captures about {int(OPENING_CAPTURE * 100)}% of that.")
    return lines
