# Bidding algorithm audit and replacement design

Date: 2026-08-06
Scope: default bid recommendation path, official bidding PDFs, and the screenshots supplied with the defect report.

## Executive finding

The previous recommender was not merely conservative. Its market generator was
mathematically coupled to the current student's point balance: synthetic rivals'
bids were sampled as fractions of that student's category pool. If the student had
more points, every invented opponent was automatically made richer by the same
factor. This creates scale invariance and drives recommendations toward the full
pool by construction. The reported `297`, `0`, and `492` recommendations are a
direct consequence, not a surprising edge case.

The replacement default is therefore not a patched Monte Carlo model. It is a new,
deterministic resource-allocation planner in
`backend/app/services/bid_strategy.py`. It solves the observable planning problem
and refuses to claim the unobservable market result.

## What the official documents establish

The three supplied University documents consistently establish:

1. ME, UWE, and CCC have separate point balances.
2. Points on simultaneous active bids are held against the relevant live balance;
   the total commitment cannot exceed that balance.
3. The highest bids win. Winners pay the lowest winning bid, excess points are
   refunded, and unsuccessful bids are fully refunded.
4. A student may revise or withdraw a bid while the round is open.
5. The portal exposes seats and live bidder counts, but other students' bids remain
   private.
6. Unused points carry forward. Fourth-year students receive no fresh Semester 8
   allocation, making a Semester 7 reserve strategically meaningful.
7. The published documents define no per-course maximum bid other than the
   student's available category balance.

They do **not** publish historical clearing prices, a rival-bid distribution, or a
calibrated relationship from bidder count to price. Those missing data are the
identification boundary for any predictive model.

## Audit of the previous default

### 1. Opponent bids scaled with the user's own pool — critical

`backend/app/simulation/engine.py` generated each synthetic bid as a fraction of
`cap`; `backend/app/services/runner.py` supplied the current student's category
pool as that cap.

If a simulated rival fraction is `F` and the student's pool is `B`, the synthetic
bid is approximately:

```text
R = round(F × B)
```

For a user bid `b = xB`, the simulated event `b >= R` becomes `x >= F`. Changing
`B` changes the displayed number but not the modeled chance. More University points
therefore cannot improve modeled purchasing power; the synthetic market inflates
with the user. Any target-search optimizer will tend to return a constant fraction
of the pool—often the entire pool. This is the main cause of the screenshots.

### 2. Unidentified parameters were presented as probabilities — critical

Competition ratios, rival strategy bands, dispersion, topic popularity factors,
and oversubscription floors were assumptions without historical calibration.
Running 30,000 trials only reduces Monte Carlo sampling error around those assumed
numbers. It does not reduce parameter error or prove that synthetic students behave
like real students.

The previous UI nevertheless displayed exact-looking values such as `56%`,
`expected charge`, and `expected refund`. These outputs were precise conditional on
the invented distribution, but not empirically defensible estimates of the real
auction.

### 3. Every unobserved course was forced into oversubscription — high

In stress modes, `expected_rivals` enforced a minimum of `1.25 × seats`, before
additional timetable and subject-popularity multipliers. A missing observation was
therefore treated as evidence of excess demand. This reversed the burden of proof
and made the fallback systematically pessimistic.

### 4. Reliability targets were arbitrary — high

`backend/app/optimization/robust.py` mapped priorities to fixed probability targets
such as 95% or 99% across selected scenarios. Those targets are not University
rules and were not elicited from the student as risk preferences. Searching for the
minimum bid that clears an arbitrary target produces cap-hugging outputs whenever
the synthetic curve is pessimistic.

### 5. Infeasible allocations were repaired by greedy deletion — critical

The old optimizer first solved each course independently. When their sum exceeded a
category balance, it subtracted points from the lowest-priority rows until feasible.
For equal priorities, input order became an implicit preference. This is why one
strongly preferred course could receive the full pool while another received zero.
It was not joint utility maximization and had no diversification objective.

### 6. Carry-forward value was absent — high

The official mechanism makes unused points an intertemporal resource. The old
objective assigned no value to retaining points, including for Year IV students who
receive no new Semester 8 allocation. Exhausting a balance was therefore free in the
optimizer even when it was strategically expensive for the student.

### 7. A resolved rule was still displayed as uncertain — medium

The FAQ confirms shared live category balances, but the UI continued to compare
`SHARED_LIVE` against a hypothetical `INDEPENDENT` mode and described the rule as
unresolved. The alternative reallocation reconstructed probability curves from a
few retained values and linearly scaled expected charges—neither operation is valid.

### 8. Whole-plan stress added unsupported scenario probabilities — high

The plan stress test sampled High, Very High, and Extreme uniformly. No source says
those regimes are equally likely—or that they describe the real market at all.
Outputs such as `worst-case credits 0` were properties of that synthetic mixture,
not evidence about the student's schedule.

### 9. The seed was not process-reproducible — medium

The simulation seeded NumPy with Python's built-in `hash((seed, key))`. Python salts
that hash across interpreter processes, so the same visible seed could produce a
different result after a worker restart. The legacy/advanced engine now derives a
stable BLAKE2 seed, although it no longer drives the default recommendation.

### 10. UI contract leakage produced `undefined` — medium

The renderer expected a `disclaimer` property that was not guaranteed in the raw
worker result. The default replacement uses an explicit Pydantic response contract;
the uncertainty text is required and cannot silently disappear.

## Replacement objective

The app can truthfully optimize this problem:

> Given separate category balances, course priorities, live counts, and a chosen
> carry-forward reserve, construct a joint set of personal ceilings whose total is
> feasible and whose concentration matches the student's strategy.

It cannot truthfully optimize this problem without new data:

> Find the bid that wins with probability `p`, or estimate the expected clearing
> price.

The second problem requires historical market data or live bid/price information
that the supplied documents explicitly do not expose.

## Proposed logic flow

For each category independently:

```text
pool     = official live category balance
reserve  = round(pool × student_reserve_percent)
envelope = pool - reserve

for each selected course:
    weight = ordinal priority weight
    cap    = envelope × posture-specific maximum share

ceilings = capped_weighted_water_fill(weights, caps, envelope)

for each course:
    pressure = band(live_bidders / seats), or unknown
    opening  = pressure_step × personal_ceiling
```

Priority weights are explicit heuristics: Must 8, Strong 5, Backup 2, Optional 1.
They are not mislabeled as University policy. Posture controls maximum concentration:

- diversified: protects more alternatives;
- balanced: spreads by priority while allowing meaningful concentration;
- focused: permits a larger allocation to top courses.

The allocator is capped weighted water-filling with deterministic largest-remainder
rounding. When the envelope permits, every selected course receives at least one
point before marginal allocation, preventing accidental all-or-nothing deletion.

Live bidder counts affect the opening step only:

- below 0.8 bidders per seat: 15% of ceiling;
- 0.8–1.0: 35%;
- 1.0–1.5: 65%;
- above 1.5: 85%;
- unavailable: provisional 50% and a prompt to update.

These bands are visible planning heuristics. They do not claim a price or win
probability. A student can revise the count and immediately regenerate the plan.

## Invariants

The implementation and regression tests enforce:

```text
sum(personal_ceiling[c] for c in category) <= current_round_envelope[category]
current_round_envelope + carry_forward_reserve == pool
0 <= opening_bid[c] <= personal_ceiling[c]
personal_ceiling[c] <= posture_concentration_cap[c]
```

It is also:

- deterministic across processes;
- invariant to input ordering, with course code used only as a stable tie-breaker;
- jointly budget feasible;
- diversified when the budget permits;
- explicit about every heuristic;
- free of synthetic opponents, expected charges, and win-probability fields.

## Strategic use across rounds and semesters

1. Assign priorities based on academic need and viable alternatives.
2. Choose a reserve based on future uncertainty. A fourth-year Semester 7 student
   should consciously consider Semester 8 because there is no fresh allocation.
3. Enter the suggested opening bids, not every personal ceiling.
4. Update live bidder counts from the portal before the round closes.
5. Raise a bid only while the course remains worth the opportunity cost; do not
   exceed the ceiling by inertia.
6. After settlement, re-plan using actual refunds and outcomes.
7. If future anonymized clearing-price data becomes available, fit and back-test a
   separate calibrated forecasting model. Do not silently merge it into these
   deterministic budget guarantees.

## Code structure

- `backend/app/models/schemas.py`: strict request/response contracts.
- `backend/app/services/bid_strategy.py`: pure deterministic allocation logic.
- `backend/app/main.py`: synchronous `/api/v1/bid-strategy` endpoint.
- `frontend/src/api.js`: the only browser-to-API adapter.
- `frontend/src/glue.js`: interactive renderer and live-count replanning.
- `backend/tests/test_bid_strategy.py`: budget, reserve, diversification,
  permutation, live-count, and no-probability properties.

The legacy simulation endpoint remains available only for research/backward API
compatibility. It is no longer the application's default recommender or displayed as
an authoritative student decision tool.

---

## Addendum — marginal-value-v2 corrections (2026-08-07)

Three defects were found in the v2 planner itself and fixed. Each was measured
before being changed, and each is now pinned by a regression test in
`backend/tests/test_bid_strategy.py`.

### 1. A committed point was charged as if it were spent — critical

v2 initially applied a per-point opportunity cost (`MATERIAL_VALUE_PER_POINT`) to
every point *bid*. Under this mechanism that is simply wrong, and wrong in the
direction that produces visibly conservative, off-looking numbers.

The FAQ states that a winner is charged the clearing price — the lowest winning
bid — not the amount bid; that surplus above the clearing price is refunded; and
that unsuccessful bids are refunded in full. Bidding 200 on a course that clears
at 50 costs 50, not 200. The genuine costs of a bid are therefore only:

1. the clearing price, and only if you win; and
2. the within-round hold, since points on a live bid cannot simultaneously back
   another bid in the same category — already the `sum(b) <= envelope` constraint.

The per-point tax was removed. The numerical problem it had been masking (a win
curve asymptotes to 1 without reaching it in floating point, so the optimiser
would commit real points chasing gains of order 1e-15) is now handled where it
belongs, by quantising the objective (`GAIN_QUANTUM`).

### 2. The reported clearing price was truncated by the student's own balance — critical

The bid grid ran to the student's envelope, and the clearing-price search ran on
that same grid. Any price above the envelope was therefore clamped to it. Measured:
an identical 20-seat course with 140 live bidders was reported as clearing at 96
for a student holding 120 ME points and at 161 for a student holding 450.

This is the user's own balance leaking into a market statistic — the same class of
defect the original audit found in the v1 rival generator, reappearing in a new
place. The clearing price is now computed on a market-wide grid bounded by the
largest published category pool, independent of the student. A student who cannot
afford a course is now told so plainly instead of being shown a comfortable price.

### 3. The clearing price was briefly netted off course value — rejected, with reasoning

An intermediate version subtracted the modelled price from each course's value.
It was measured driving a STRONG course to a bid of zero on a live
20-seat/140-bidder course, and was reverted. The reason it is wrong is structural:
netting requires an exogenous value per point, but inside a round the true
opportunity cost of a point is endogenous — it is whatever that point would buy on
the student's other courses, which the budget constraint already prices through the
DP's own shadow price. Supplying an outside number on top double-counts the
constraint. Value beyond the current round is what the carry-forward reserve is
for, and that is user-set.

The clearing price is therefore reported, not netted: it is shown per course as a
band, alongside an explicit warning when a personal ceiling falls below it.

### What the research corpus changed, and what it did not

The supplied systematic reviews independently state the objective this planner
already maximises — `sum_c u_c * Pr(b_c >= p_c)` subject to `sum_c b_c <= B` — which
is a useful corroboration rather than a change. They also record the Sönmez–Ünver
dual-role-of-bids critique, which is a property of the mechanism SNU has adopted
and cannot be engineered away by a planning tool; it is a reason to report
uncertainty honestly, not a reason to claim more precision.

One recommendation in that corpus is worth repeating here because it is outside
this tool's control: the single highest-value change SNU could make is to publish
clearing prices after each round. Every price this planner has to model would then
be a number students could simply look up.

---

## Addendum — marginal-value-v3 (2026-08-07)

Reported by the user against the live UI: CSD358 (Information Retrieval) and
CSD361 (Introduction to Machine Learning) are both Major Electives, both 3
credits, both marked "strongly preferred", and the plan showed them at a
personal ceiling of 166 and 72 respectively. The same table showed both with an
identical modelled price (55) and an identical live-pressure figure (1.51x
seats). Same cost, same value, same displayed pressure, a 2.3x difference in
recommended bid.

### The finding

The two courses' win curves are nearly identical. Measured directly:

| bid | 72 | 119 | 140 | 166 | 190 |
|---|---|---|---|---|---|
| CSD358 (120 seats) | 0.700 | 0.700 | 0.708 | 0.951 | 1.000 |
| CSD361 (80 seats) | 0.700 | 0.700 | 0.717 | 0.935 | 0.999 |

The curve is flat for 47 points at a stretch, and 47-56% of the entire budget
range bought no additional win probability at all. Cause: v2 modelled market
tightness as exactly three states. Within a state the rival distribution was
taken as exactly known, so the only residual randomness was binomial sampling
of a known distribution, which is O(1/sqrt(n)). Each state's win curve was
therefore a near step function (5%-to-95% transition width: 7 points at 300
seats, 12 at 120, 20 at 40), and the three-atom mixture was a staircase.

Given a staircase, the exact optimum of the stated objective really is to buy
the big jump on one course and the cheap jump on the other. 166/72 was the true
optimum. The objective was the problem, not the optimiser.

Three consequences, all measured:

1. **Arbitrary asymmetry.** Which course got the large share was decided by a
   1.6-percentage-point difference, well inside the model's own uncertainty.
2. **Instability.** Sweeping one course's seat count flipped a 22-point
   allocation seventeen times, on objective gaps as small as 3.4e-05 (0.0038%).
3. **Wrong direction.** The transition narrows as seats grow, so the model was
   *most* confident about the largest courses purely because binomial noise
   averages out faster there. Real uncertainty about a clearing price comes from
   not knowing how hard the cohort will bid, and that does not shrink because a
   lecture hall is bigger.

### The fix

Market tightness is integrated as a continuous variable (32-node midpoint rule
on the quantile axis) instead of three atoms. The three published readings are
kept exactly as they are and become quantiles of that distribution, positioned
by their own priors: a reading holding mass p occupies an interval of width p
and sits at its midpoint. Between anchors the two dials are interpolated
log-linearly; outside the outermost anchors the mapping is flat, so the model
never extrapolates to a market calmer than "calm" or tighter than "tight".

No new assumption is introduced. The anchors, their spacing and their weights
are unchanged. This is a better numerical representation of the same stated
belief. Result: dead zone falls from 47-56% to 7-25%, and the win curve becomes
smooth and strictly increasing over its useful range, so "a few more points buys
a little more chance" is true and the DP equalises marginal value rather than
solving a knapsack over cliffs.

### Three smaller defects found while verifying the fix

- **The breadth ladder could not express an even split.** Share caps were a fixed
  tuple (0.35, 0.45, 0.55, ...). For two courses the even split needs share
  exactly 0.50 and the ladder jumps 0.45 to 0.55, so two *identical* courses were
  planned at 130/108 while the even split cost 0.24% of expected value, far
  inside the balanced posture's 5% tolerance. It was never rejected on the
  merits; it was never offered. The even share 1/n is now always a candidate.
  Nothing tighter is considered, which is provable rather than a preference: a
  cap below 1/n forces idle budget, and since win curves are monotone
  non-decreasing, every allocation feasible under it is also feasible at 1/n.
- **The objective was quantised a thousand times finer than the model resolves.**
  GAIN_QUANTUM was 1e-6 against an objective built from a 32-node quadrature over
  an assumed lognormal with priors quoted to two decimals. Raised to 5e-4, about
  a quarter of one point's worth of gain, which leaves genuine signal untouched
  (the decisions this planner acts on are 50-100x larger).
- **Ties between indistinguishable courses were resolved by noise.** Where the
  budget cannot secure two equally-wanted courses, the objective genuinely
  prefers securing one: the curve is convex there, so (130, 108) really does beat
  (119, 119). But (130, 108) and (108, 130) are distant local optima separated by
  that same valley, so nothing local can see they are a tie. Swapping them costs
  0.056% of plan value, while a decision that must always be preserved (must-have
  beside optional) costs 8.264%. A separate 1% threshold sits two orders of
  magnitude clear of both. Where two courses fall inside it, the larger share
  goes to the one with fewer seats, on the stated reasoning that the scarcer seat
  is least likely to still be available in a later round, and the plan now says
  it did this rather than presenting a coin flip as a finding. Flips across the
  same seat sweep: 17 to 1, and the remaining one is the meaningful handover
  where the two seat counts cross.

### Reporting defects fixed alongside

- **A bare "100.0%" was reaching the screen.** `probLabel` guarded at 0.9999 but
  printed to one decimal place, so anything from 0.9995 up rendered "100.0%".
  Visible in the reported screenshot as a course reading "100.0%" beside its own
  band of ">99.9%". The backend's own rationale text had the same bug via `:.0%`
  formatting ("win chance is 100% centrally (band 0% to 100%)"). Both now route
  through formatters that never claim certainty.
- **The live-pressure ratio was a tautology.** With no observed count the central
  reading sets rivals to round(seats * 1.5), so the reported bidder-to-seat ratio
  was (round(1.5 * seats) + 1) / seats, i.e. about 1.51 for every course in the
  catalogue whatever its size. It was displayed per course under a "live
  pressure" heading, inviting the reader to take a constant for a measurement.
  No ratio is reported without an observation; the modelled rival count is shown
  instead, labelled as modelled.
- **The decisive variable was invisible.** Seat count is what separates two
  otherwise identical courses, and it was not a column. It is now, together with
  the modelled rival range, and the per-course rationale names it explicitly.
- **The category-level explanations were computed but never displayed.**
  `breadth_note` and `reserve_note` existed in the response and nothing rendered
  them, which is why an uneven split read as arbitrary rather than as a decision
  with a stated reason.

### Verification

`backend/tests/test_bid_strategy.py` gained nine property tests covering: two and
three identical courses receiving identical bids; the even split always being an
available breadth option and nothing tighter being offered; a seat sweep
producing zero noise-driven swings; priority and observed counts outranking the
scarcity tie-break; the win curve having under 25% dead zone and never falling;
and the named readings being genuine quantiles of the distribution actually
optimised. Full suite 191 passed, 1 skipped.
