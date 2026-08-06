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
