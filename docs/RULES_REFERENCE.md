# University rule reference (source of truth for all domain logic)

Extracted from `backend/app/domain/rules.py` (30 rules, machine-readable, each with
`status`, `source`, `resolution`/`note`). This document is the human-readable mirror —
if the two ever disagree, `rules.py` wins because tests run against it.

## Provenance breakdown
21 official · 2 prospectus · 2 inferred · 4 disputed · 1 unknown

## The mechanism, in one paragraph
Students get three separate, non-interchangeable point pools (Major Elective / UWE /
CCC) per bidding semester. For each course they bid any whole number of points, 0 up to
25×credits. When a round closes, seats go to the highest bidders; **everyone who wins
pays the same price — the lowest winning bid (the clearing price)** — and gets the
difference between their bid and that price refunded. Ties are broken by a random
6-digit number assigned when the course is added, not by timing.

## Verified pool formulas (all reproduce the Concept Note's own worked examples exactly)

```
Y4 (final semester, Sem 7 only):
  ME  = remaining_ME  x 15 + 162
  UWE = (remaining_UWE + floater/2) x 15 + 110
  CCC = (remaining_CCC + floater/2) x 30 + 72

Y3 (transition, Sem 5/6/7):
  category = (remaining x 10 x semester_share) + flat_constant
  semester_share: 40% / 30% / 30% for Sem 5/6/7
  flat_constant: +65 ME, +50 UWE, +30 CCC (added IN FULL each semester)

Y2 (steady state, Sem 2-7):
  10 points per remaining credit, released on a staggered schedule:
    UWE:     15/20/20/20/15/10% across Sem 2-7
    ME/CCC:  7.5/17.5/20/20/20/15% across Sem 2-7
  minus 10 points per already-completed credit
```

**This specific student's numbers** (4th year, Sem 7): 9 ME / 4 UWE / 11 CCC / 6 floater
remaining → **ME=297, UWE=215, CCC=492**. Verified against the Concept Note's own
worked example (12/4/6/6 → 342/215/342) — exact match, not approximate.

## The one unresolved rule that matters most: `BUDGET.SHARED_LIVE`

No University document states whether bids on different courses within the same round
draw from one shared live pool, or whether each bid is independently capped only by the
category total at settlement. This is not a detail — it decides whether the optimizer
has a real allocation problem to solve at all. **Both interpretations are implemented
and compared side-by-side on every simulation run** (see `run_plan_both_budget_modes`
in `backend/app/services/runner.py`).

## Disputed data points (confirmed by direct arithmetic, not assumed)

- The Concept Note's own "average 4th-year" summary row (297/125/239 for a 9/~0/5.5/6
  profile) does **not** reconcile with its own formula: UWE gives 155 (or 110 without
  floater), never 125; CCC gives 327 (or 237), never 239. Only ME (297) reconciles. The
  fully-worked example immediately below it in the same document DOES reconcile exactly.
  **Resolution: the formula is used everywhere; this one disputed row is excluded.**
- Only 33 of 326 offered courses have an officially published credit value; the rest are
  derived from L-T-P contact-hour patterns in the timetable. Since the bid cap is
  25×credits, a wrong credit value directly changes the legal maximum bid.
- Three major electives (CSD365, CSD436, CSD438) do not appear in the printed prospectus
  elective table at all; CSD358/361/457 do, at 3 credits each (confirmed match).
- Whether CSD336 (a 4-credit Major Core) counts toward the AI specialisation bucket —
  the bucket lists "Reinforcement Learning" by name, but the requirement text says
  "elective courses from the chosen bucket." Excluded by default, user-toggleable.

## Auction settlement — verified against the guide's own three worked examples

1. 3 seats, bids 900/800/650 → all 3 win, clearing price = **650** (the lowest winner),
   refunds 250/150/0.
2. 3 seats, only 2 bidders (900/800) → both win, clearing price = **0** (seats went
   unfilled), both refunded in full.
3. 2 seats, bids 800/700/700/600 with tie-breaks 253456/403456/653456/103456 → winners
   are exactly {A, B}; D loses despite having the numerically lowest tie-break value
   (tie-breaks only resolve *equal bids*, they never override bid size).

## Why the competition model assumes serious competition by default

This is the **first cohort** ever to go through bid-point enrolment at this
institution. There is no historical clearing-price data and no historical bidder-count
data anywhere. An earlier version of this project estimated demand for a 120-seat course
at 55.6 expected rivals (derived from total category capacity), which produced a
recommended bid of 0 and a displayed "100% — likely free." That was an invented,
unsupported number presented as if it were evidence, and it is the single most dangerous
thing this kind of tool can do, because the failure mode is silent: the student bids
nothing and simply loses the seat.

The current model (`backend/app/simulation/engine.py` + rule `COMP.STRESS_DEFAULT`)
inverts the default: every course is assumed oversubscribed (rivals > seats is a hard
floor in every stress mode except the explicitly-labelled "optimistic comparison"
scenario), rivals draw from realistic strategy bands (15-30% bid exactly at the cap),
and a live bidder count from the actual platform **replaces** the assumption entirely
the moment one is available.
