# University rule reference (source of truth for all domain logic)

Extracted from `backend/app/domain/rules.py` (30 rules, machine-readable, each with
`status`, `source`, `resolution`/`note`). This document is the human-readable mirror —
if the two ever disagree, `rules.py` wins because tests run against it.

## Provenance breakdown
As of the 2026-08-05 rectification (Dean Academics email + three revised/new official
PDFs): the large majority of rules are official, two are disputed, one remains genuinely
unresolved by the University itself (`SET.CREDIT_CAP`'s exact >25-credit handling), and
`BUDGET.SHARED_LIVE` moved from unknown to officially confirmed. See `rules.py`'s own
`counts()` for the exact live breakdown; this file is a mirror, not the source of truth.

## The mechanism, in one paragraph
Students get three separate, non-interchangeable point pools (Major Elective / UWE /
CCC) per bidding semester. **There is no minimum or maximum bid** (rectified
2026-08-05): a student may bid any whole number of points, 0 up to their *entire
available pool for that category*, on a single course. Points placed on a bid are held
against that category's live balance while the round is open, and released back if the
bid loses (`BUDGET.SHARED_LIVE`, now confirmed). When a round closes, seats go to the
highest bidders; **everyone who wins pays the same price — the lowest winning bid (the
clearing price)** — and gets the difference between their bid and that price refunded.
Ties are broken by a random number assigned uniquely per course per round, not by
timing; withdrawing and rebidding for the same course in the same round keeps the same
number.

## Verified pool formulas (all reproduce the two Concept Notes' own worked examples exactly)

```
Y4 (final semester, Sem 7 only):
  ME  = remaining_ME  x 15 + 162
  UWE = (remaining_UWE + floater/2) x 15 + 110
  CCC = (remaining_CCC + floater/2) x 30 + 72

Y3 (transition, Sem 5/6/7):
  category = (remaining x 10 x semester_share) + flat_constant
  semester_share: 40% / 30% / 30% for Sem 5/6/7
  flat_constant: +65 ME, +50 UWE, +30 CCC (added IN FULL each semester)

Y2 (steady state, Sem 2-7) — rectified 2026-08-05:
  10 points per credit of the category's TOTAL requirement (not "remaining"),
  released on a staggered schedule and accumulated across semesters:
    UWE:     15/20/20/20/15/10% across Sem 2-7
    ME/CCC:  7.5/17.5/20/20/20/15% across Sem 2-7
  minus 5 points per already-completed credit (was 10 before the rectification)
```

**Y4 example** (4th year, Sem 7): 9 ME / 4 UWE / 11 CCC / 6 floater remaining →
**ME=297, UWE=215, CCC=492**. Verified against the revised Concept Note's own worked
example (12/4/6/6 → 342/215/342) — exact match, not approximate; unchanged from the
prior document version.

**Y2 example** (2nd year, Sem 3, first bidding cycle): 36 total ME / 16 total UWE /
12 total CCC (excl. EVS), 8 floater, 3 UWE credits already completed →
**ME=90, UWE=55, CCC=40**. Verified against the revised Concept Note's own arithmetic:
UWE = 30 (Sem2 carry-forward) + 40 (Sem3 release) - 15 (3 credits x 5) = 55 exactly.
Note: the source PDF's own prose sentence for this example says "40 UWE points"
immediately after showing this 30+40-15=55 arithmetic — an apparent typo in the
document itself. This implementation trusts the arithmetic. The zero-completed-credit
baseline (90/70/40) independently matches `Course_Enrolment_FAQ_1.pdf`'s own stated
"typical first-cycle average" for 2nd years.

## `BUDGET.SHARED_LIVE` — resolved 2026-08-05, previously the one unresolved rule that mattered most

`Course_Enrolment_FAQ_1.pdf` states directly: *"Each category (ME / UWE / CCC) has its
own balance. Points you place on a bid are held against that balance and released if
the bid is unsuccessful back to the same category. You cannot commit more than you
have."* This confirms SHARED_LIVE as the official rule: simultaneous bids within one
category are a real, shared constraint, not independent per-course budgets. The
optimizer keeps the INDEPENDENT reading available purely as a labelled hypothetical
comparison (see `run_plan_both_budget_modes` in `backend/app/services/runner.py`),
the same treatment as the "Optimistic" competition scenario — never presented as an
equally-plausible alternative going forward.

## Disputed data points (confirmed by direct arithmetic, not assumed)

- The Concept Note's own "average 4th-year" summary row (297/125/239 for a 9/~0/5.5/6
  profile) does **not** reconcile with its own formula: UWE gives 155 (or 110 without
  floater), never 125; CCC gives 327 (or 237), never 239. Only ME (297) reconciles. The
  fully-worked example immediately below it in the same document DOES reconcile exactly.
  **Resolution: the formula is used everywhere; this one disputed row is excluded.**
- Only 33 of 326 offered courses have an officially published credit value; the rest are
  derived from L-T-P contact-hour patterns in the timetable.
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
