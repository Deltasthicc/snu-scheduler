# Research-to-implementation matrix — wishlist/credit-aware scheduler phase

Source documents: `gemini-research.txt` ("Designing the Optimal University Course
Scheduler for Shiv Nadar University") and `deep-research-report.md` ("Building
the Best University Course Scheduler for Shiv Nadar University"). Both were
read in full before any code changed. This matrix is graded against the actual
repository state as of this session, not against intent.

Status legend: **Done** (implemented and tested this session), **Partial**
(a real, working, tested subset — gap stated honestly), **Deferred** (not
attempted this session, by explicit scoping decision, not oversight).

## 1. Personal vs institutional data model

| Research recommendation | Already implemented? | Quality | Gap | Proposed change | Files | Test | Status |
|---|---|---|---|---|---|---|---|
| Institutional catalog shared; student profile starts empty; no dev-specific defaults ship | Yes, from the prior session (§11/§12 of CLAUDE.md: `FIXED`/`MANUAL`/`DONE_ME` generalized) | Good | `capCr` (credit ceiling) still defaulted to `28` — the original developer's own overload number, shipped as everyone's default | Change default to `25` (`CEILING.STANDARD`) | `frontend/src/ui/b_body.html` | e2e §21 (credit policy summary against the new default) | **Done** |
| Demo Mode, clearly labelled, one-click exit, never silently becomes the real profile | Not present | — | No demo mode of any kind exists | Not built this session | — | — | **Deferred** — real feature, not a bug fix; flagging for a future session rather than bolting on a half-built toggle |

## 2. Student profile & setup flow

| Research recommendation | Already implemented? | Quality | Gap | Proposed change | Files | Test | Status |
|---|---|---|---|---|---|---|---|
| Guided setup with unknown-vs-zero distinction, inspectable rules, plain-language summary | Partially — the Profile tab already collects programme-shaped data (year model, credits done/remaining) but forces numeric zero, and there was no summary sentence | Was weak | No "you have N fixed credits, room for M more" sentence; no `unknown` state | Added `POST /api/v1/profiles/validate` returning exactly this sentence, and `ProfileValidateRequest` treats `remaining_*_credits`/`floater_credits` as `float \| None` (null = unknown, echoed back in `unknown_fields`) | `backend/app/models/profile_schemas.py`, `backend/app/main.py`, `frontend/src/ui/b_body.html`, `frontend/src/glue.js` | `test_api_schedules.py::test_profiles_validate_*` | **Partial** — the summary sentence and unknown-tracking exist; CSV/transcript import and a multi-step "guided" wizard UI do not |
| Multiple entry paths: manual, search, CSV, JSON, pasted list, transcript | Manual selection (Course Picker) and JSON plan import/export already existed | Existing feature, unrelated to this phase | No transcript parser, no bare CSV/pasted-list importer for the wishlist itself | Not built | — | — | **Deferred** — a transcript parser is a real, separate parsing-and-verification project (spec's own words: "only if a safe and reliable parser exists"); not attempted rather than done unsafely |

## 3. Credit-ceiling policy

| Research recommendation | Already implemented? | Quality | Gap | Proposed change | Files | Test | Status |
|---|---|---|---|---|---|---|---|
| Five distinct numbers, never one ambiguous "credit limit" | No — one `capCr` field did everything | Was the exact anti-pattern the spec warns against | — | `resolve_ceiling()` distinguishes official / personal target / overload / fixed / wishlist room; never defaults 30 credits for anyone (rule `CEILING.APPROVED_OVERLOAD` is `Status.UNKNOWN`, numberless until a caller supplies it) | `backend/app/domain/rules.py`, `backend/app/services/credit_policy.py`, `backend/app/models/profile_schemas.py` | `backend/tests/test_credit_policy.py` (14 tests: Y1–Y4, overload, approved-exception, invalid ceiling, ceiling-below-fixed, decimals, stale-import) | **Done** |
| 4th-year 30-credit scenario labelled as unconfirmed unless approved | No such labelling existed | — | — | `ceiling_mode` is `"standard"` / `"what_if"` / `"approved_overload"`; summary text always states which, and states the source-confirmed standard explicitly when in overload mode | same as above | same as above + `frontend/src/ui/b_body.html` overload toggle/checkbox | `test_credit_policy.py::test_fourth_year_30_credit_planning_scenario_is_labelled_what_if` | **Done** |

## 4. Real wishlist

| Research recommendation | Already implemented? | Quality | Gap | Proposed change | Files | Test | Status |
|---|---|---|---|---|---|---|---|
| Course-level intent (must-have/strong/optional/backup), separate from package preference and bid amount | The existing `PICK`/`PRIO` model already had a 4-value priority (`MUST/STRONG/BACKUP/OPTIONAL`) that maps 1:1 onto `WishlistIntent` | Good foundation, just not typed/validated server-side | No backend model, no package lock/exclude, no notes/instructor fields | `WishlistItem` Pydantic model with `intent`, `priority`, `locked_package`, `excluded_packages`, `preferred_instructor`/`avoided_instructor`, `notes`; reused `PRIO`→intent mapping client-side rather than building a second parallel data model | `backend/app/models/profile_schemas.py` | `backend/tests/test_cp_scheduler.py` (locked/excluded package tests) | **Done** for intent/priority/locks/exclusions; **Partial** for instructor preference and notes (modeled and validated server-side, but no UI surfaced this session — a real, scoped-down cut, not a hidden gap) |
| Choice groups: exactly-one / at-least-one / at-most-one / min-credits, ordered fallback | Not present | — | — | `ChoiceGroup` model + CP-SAT constraints for all four kinds; ordered-fallback ("prefer A, else B") is expressible as `at_least_one` plus priority weighting, not a fifth first-class kind | `backend/app/models/profile_schemas.py`, `backend/app/services/cp_scheduler.py`, `backend/app/services/wishlist.py` | `test_cp_scheduler.py` (4 group-kind tests + differential test), `test_wishlist.py` (structural validation) | **Done** for the four listed kinds; **Partial** for "prefer X, else Y" as its own named relationship (works via priority, not a distinct constraint type) |
| Wishlist summary stats (count, min/max credits, category composition, must-have/backup/impossible/unconfirmed counts) | Not present | — | — | `wishlist_summary()` computes all eight fields listed in the spec | `backend/app/services/wishlist.py` | `test_wishlist.py` (9 tests) | **Done** on the backend; surfaced via `/wishlists/validate` but the frontend does not yet render this specific summary card (it renders the equivalent generation-result stats instead — see §16 below) |

## 5–6. Subset generation up to the ceiling; safe combinatorics

| Research recommendation | Already implemented? | Quality | Gap | Proposed change | Files | Test | Status |
|---|---|---|---|---|---|---|---|
| Backend decides the course subset, not just the section per course | No — the existing Schedule Builder only ever searched a fixed shortlist's package permutations | This was the single biggest gap both reports independently flaged | — | New CP-SAT layer decides *inclusion* (`y_c`) and *package* (`x_{c,p}`) jointly | `backend/app/services/cp_scheduler.py` | `test_cp_scheduler.py::test_prefers_target_over_maximizing_credits`, `test_returns_below_target_when_exact_target_is_unreachable` | **Done** |
| Exact target where feasible, below target when not, never above ceiling, never below user minimum unless impossible | No | — | — | Implemented exactly via a `dev = |target - total|` minimization term plus hard `total <= ceiling`, with a two-pass solve that only relaxes the minimum floor if the first pass is infeasible | `backend/app/services/cp_scheduler.py::solve` | `test_cp_scheduler.py::test_min_credits_floor_is_relaxed_only_when_infeasible`, differential test | **Done** |
| Backend engine (not browser Cartesian product); prevalidated packages; conflict bitsets; most-constrained-first; dominance pruning; connected-component decomposition; memoization | The pre-existing shortlist path (`app/services/scheduler.py`) already does most-constrained-first backtracking server-side with a documented, measured freeze this replaced | Good, proven | CP-SAT model does not implement its own dominance pruning or connected-component decomposition — it delegates that entirely to OR-Tools' own presolve/CDCL, which both reports note is exactly what CP-SAT is for | None needed — this is the correct division of labor, not a missing feature | — | (existing) `test_scheduler.py` covers the shortlist path; `test_cp_scheduler.py` covers the wishlist path | **Done** (via the right layer doing the right job, per both reports' own recommended architecture) |
| Diversity layer: multiple ranked, meaningfully-different schedule families per generation | Not present | — | — | Not built this session | — | — | **Deferred** — one optimized solution per generation call is returned, explicitly documented as a known limitation in `cp_scheduler.py`'s module docstring; a true diversity layer (no-good constraints + re-solve loop) is a real second feature, not a one-line addition |
| Exhaustive mode with exact count, pagination, honest partial-vs-complete labelling | The pre-existing shortlist path already does this (`truncated`/`total_found`/pagination) | Good, proven, tested since the prior session | The new wishlist/CP-SAT path does not offer an exhaustive-enumeration mode — it returns one optimized solution, honestly labelled by `cp_status` (`optimal`/`feasible`/`infeasible`/`unknown`), never claiming "all schedules" | Not built this session | — | (existing tests cover the shortlist path's exhaustive mode) | **Partial** — honest labelling carried over faithfully; true exhaustive enumeration of wishlist subsets is deferred |

## 7. Hard constraints

| Constraint (spec s.7) | Enforced by | Test |
|---|---|---|
| Fixed non-swappable courses always selected | Locked fixed meetings excluded from the search space entirely (not modeled as variables at all) | `test_cp_scheduler.py::test_fixed_locked_meeting_blocks_a_clashing_wishlist_package` |
| Exactly one valid package per selected course | `sum(x_{c,p}) == y_c` | `test_must_have_is_always_included`, `test_choice_group_*` |
| No overlapping meetings within overlapping date ranges | Term-aware `_meetings_overlap` reused unchanged from the proven shortlist path | `test_conflicting_packages_are_never_both_selected`, differential test |
| H1/H2 courses at the same clock time do not clash | Same term-aware function, unchanged | `test_half_semester_courses_in_opposite_halves_do_not_clash` |
| Same course never selected twice | Structurally impossible — one `y_c` per course code | (by construction) |
| Locked packages stay selected; excluded packages never appear | `locked_package`/`excluded_packages` restrict the variable set before the model is even built | `test_locked_package_is_respected`, `test_excluded_package_is_never_selected` |
| Credit ceiling never exceeded | Hard `total_var <= ceiling` constraint | `test_credit_ceiling_is_never_exceeded` |
| Choice-group constraints respected | Direct linear constraints per kind | `test_choice_group_*` (4 tests) |
| Equivalence/exclusion, prerequisites, category-specific sub-limits, core-swap round rules, bid-category budgets | **Not modeled in this phase** — no equivalence-group or prerequisite data is currently plumbed from the catalog into `WishItem`, and bid budgets are a separate simulation subsystem (`app/simulation/`) this phase did not touch | — | **Deferred**, and stated as a limitation rather than silently assumed away |

## 8. Ranking & explainability

| Research recommendation | Implemented? | Quality | Gap | Files | Test | Status |
|---|---|---|---|---|---|---|
| Lexicographic tiers (academic validity → resilience → lifestyle → stable tie-break), not one opaque AI score | Implemented as a single weighted objective with tier weights spaced by 1000x (`dev*1e6 - value*10 + days*1000`), documented explicitly as a deliberate simplification of true lexicographic multi-solve | The credit-target tier and the priority-value tier are provably dominant at these weight gaps for any realistic wishlist size; compactness is a real but secondary tier | No resilience tier at all (needs bid-simulation data this phase didn't wire in); no requirement-bucket-progress tier (needs ME/UWE/CCC bucket data beyond raw category, which the catalog only partially carries) | `backend/app/services/cp_scheduler.py` | `test_differential_against_brute_force_credit_target_fit` (proves the credit-target tier is provably optimal against brute force, not just plausible) | **Partial** — academic (credit target) and a value/priority proxy tier are done and proven optimal; resilience and requirement-progress tiers are deferred |
| Every schedule states why it's valid, why it outranks another, what's omitted and why, whether optimal or best-found | Per-course inclusion/exclusion with a real blocker (`why_not`), plus `cp_status` distinguishing `optimal` from `feasible`/`infeasible`/`unknown` | Real, tested | No cross-schedule "why does A outrank B" comparison exists yet (there's only one schedule returned per generation — see the diversity-layer gap above) | `backend/app/services/cp_scheduler.py::explain_omission`, `backend/app/main.py::schedule_explain_exclusion` | `test_cp_scheduler.py` (5 `explain_omission` tests covering time-clash, choice-group, credit-ceiling, no-valid-combination, already-included) + 2 HTTP-level tests | **Done** for single-schedule explainability; **Deferred** for cross-schedule comparison (no second schedule exists to compare against yet) |

## 9–11. Scheduler page redesign, credit controls, personalisation controls

| Research recommendation | Implemented? | Gap | Status |
|---|---|---|---|
| Three-area desktop layout (discovery / wishlist+preferences / results); dedicated mobile Discover/Wishlist/Schedules/Compare bottom nav | Not built — the existing 11-tab architecture was extended in place instead | A full IA redesign is a real, large frontend project; spec s.18 itself instructs *"integrate into the current scheduler architecture… do not build this as a disconnected experimental page"* — extending the existing Course Picker + Schedule Builder tabs is the integration path that instruction points to, not a shortcut around the redesign | **Deferred by design**, not oversight — see the migration report below |
| Clear "fixed + additional = ceiling" language everywhere; overload scenario visibly distinct | Done on the Profile tab (`/profiles/validate` summary) and in the wishlist-generation result card (`fixed X + wishlist Y`) | Not yet repeated on every screen that shows a credit number (e.g. the legacy exhaustive-search tab still uses the old single `capCr` framing) | **Partial** |
| Simple/Advanced preference modes (earliest/latest time, lunch, campus days, instructor prefs, package locks, solver time limit, exhaustive toggle, etc.) | Only credit target/min/overload, choice groups, and package locks (via `locked_package`) are exposed this session | No time-of-day/day-avoidance/lunch/instructor UI wired to the wishlist path yet (the *backend* CP-SAT model doesn't model these either — only credit/choice-group/conflict constraints were built) | **Deferred** — a real second increment, not a UI-only gap |

## 12. Creative resilience features

| Feature | Status |
|---|---|
| Resilient plan graph (best replacement per vulnerable course, precomputed) | **Deferred** — needs the diversity layer and bid-risk data first |
| Conditional backups ("use B only if A is lost") | **Partial** — expressible today via an `at_most_one`/`exactly_one` choice group at generation time, but there is no *post-bid-outcome* trigger; that requires wiring into the existing bid-result-entry flow, which this phase didn't touch |
| Minimal-disruption repair after a bidding round | **Deferred** — a real, separate feature (needs to read/write the existing bid-outcome ledger in `app/simulation/`) |
| Why-not analysis | **Done** — see §8 |
| Relaxation assistant | **Partial** — `explain_omission` returns one concrete relaxation per blocker (raise ceiling by exactly the needed amount, or deselect a specific choice-group rival); it does not enumerate multiple relaxation options (drop a different optional course, move a lunch window, etc.) because most of those preference types don't exist in the model yet |

## 13. Backend API

| Endpoint (spec's suggested list) | Built? | Notes |
|---|---|---|
| `POST /api/v1/profiles/validate` | Yes | |
| `POST /api/v1/wishlists/validate` | Yes | |
| `POST /api/v1/schedules/search` | Yes (extended, backward-compatible) | Now also accepts `wishlist`/`choice_groups`/`credit_min`/`credit_target`/`credit_max`; empty-shortlist + non-empty-wishlist is now valid |
| `GET /api/v1/schedules/{job_id}` | Yes | Pre-existing, unchanged |
| `GET /api/v1/schedules/{job_id}/events` | Yes | Pre-existing, unchanged |
| `POST /api/v1/schedules/{job_id}/cancel` | Yes | Pre-existing, unchanged |
| `GET /api/v1/schedules/{job_id}/results` | Yes (extended) | Now passes through `cp_status`/`total_credits`/`included`/`excluded`/`min_relaxed`/`why_not`/`credit_*` when the job was a wishlist search |
| `POST /api/v1/schedules/compare` | No | **Deferred** — needs ≥2 schedules to exist first (diversity layer) |
| `POST /api/v1/schedules/explain-exclusion` | Yes | |
| `POST /api/v1/schedules/relaxations` | No — folded into `explain-exclusion`'s `relaxation` field instead of a separate endpoint | **Deferred as a separate endpoint**, partially covered inline |
| `POST /api/v1/schedules/repair` | No | **Deferred** — see §12 |
| Profile import/export | No new endpoint — the existing plan import/export (`PLANS.exportJson`/`importAsNewPlan`) already covers the whole plan payload including the new `choiceGroups`/`creditPolicy` fields (schema v6) | **Done** via the existing mechanism, not a new one |

All new/changed models are explicit Pydantic classes with `extra="forbid"` — no raw-dict endpoints were added.

## 16. Tests actually run

- Backend: 40 pre-existing + 52 new = **92 passed** (`cd backend && python3 -m pytest -q`).
- Frontend adapter: 31 pre-existing + 6 new = **37 passed** (`node tests/adapter.test.js`).
- Frontend plans: 33 pre-existing + 2 new = **35 passed** (`node tests/plans.test.js`).
- End-to-end (real browser + real backend): 55 pre-existing + 7 new = **62 passed** (`./scripts/run-e2e.sh tests/e2e.test.js`).
- Accessibility: **0 violations** across all 11 tabs (`./scripts/run-e2e.sh tests/a11y-audit.js`) — including one pre-existing, unrelated violation (`#ttTerm` missing an accessible name) found and fixed while re-running this audit.
- Differential test: `test_differential_against_brute_force_credit_target_fit` compares CP-SAT's credit-target fit against a brute-force enumeration over every subset of a 6-course synthetic wishlist, across 6 different targets — exact match every time, not merely "close."

## Known limitation found while verifying: desktop .exe + OR-Tools

Rebuilt the PyInstaller desktop exe with `ortools` added and ran it directly (not
assumed). The web app path (dev server, Docker, and the exe's own `/health/ready` +
plain shortlist search) all work through the exe unchanged. The new wishlist/CP-SAT
path does not: the spawned worker process dies at the OS level before it can report an
error, specific to OR-Tools' native extension inside a frozen, doubly-spawned process —
not present unfrozen (pytest, `TestClient`, and the real browser e2e suite all exercise
the identical code path successfully). Not root-caused further this session; see
`CLAUDE.md` §13 for the full finding. **The web application is fully verified working
end to end; the desktop exe's wishlist feature specifically is not**, and should not be
relied on until this is fixed.

## 17. Performance

No formal benchmark suite (5/10/15/25-course sweep, peak-memory tracking, cancellation-latency measurement) was built this session — **deferred**, stated plainly rather than presented via a fabricated table. What *was* measured directly: the real end-to-end wishlist test in `e2e.test.js` §21 uses a Performance-Observer long-task check (the same regression guard §17/§18/§19 already use) and recorded **zero long tasks** during a real 2-course CP-SAT solve through the full job-manager/worker-process pipeline — i.e., the browser main thread is provably never blocked by this new path, which was the one performance property both research reports treat as non-negotiable.

## 18. Migration report

- **Current profile state before this phase**: `PICK`/`FIXED`/`PRIO` (course + package + priority) already existed and already matched most of the wishlist data model conceptually; no rename or parallel data structure was introduced.
- **Hard-coded personal defaults found and fixed this session**: `capCr` defaulting to `28` (the original developer's own overload number) — changed to `25`, matching `CEILING.STANDARD`.
- **Existing schedule builder** (`app/services/scheduler.py`, branch-and-bound over a shortlist): left completely untouched and still fully tested; the new CP-SAT layer is an addition selected by request shape (`wishlist` present), not a replacement.
- **Existing saved-plan schema**: versioned migration path preserved — bumped `v5 -> v6`, added `choiceGroups`/`creditPolicy` with safe defaults for every plan saved under an older schema.
- **Existing backend job infrastructure** (`ScheduleJobManager`, spawn-based worker isolation, SSE progress, cancellation): reused unchanged for wishlist-mode jobs — no second job manager was built.
- **Existing UI routes**: the Course Picker tab's existing "Chosen" table and its priority selector were reused as the wishlist's intent input, rather than building a second, disconnected wishlist panel.
- **Legacy code removed**: none — both search paths coexist and are independently tested, per this project's own stated rule of not removing a working path until its replacement has parity tests (and here the new path is additive, not a replacement, so no removal is due).

## 19. Acceptance criteria — honest self-check against the spec's own list

| Criterion | Met? |
|---|---|
| First-time user sees no personal developer data | Yes (already true from the prior session; `capCr` default fixed this session too) |
| Institutional course data remains available | Yes (untouched) |
| Personal courses appear after entry/import | Yes (untouched) |
| A persistent wishlist exists | Yes — the existing `PICK`/`PRIO`, now backed by validated server-side intent semantics and choice groups, persisted in plan schema v6 |
| Wishlist courses support intent and backups | Yes |
| Credit controls clearly distinguish fixed/additional/target/maximum | Yes, on the Profile tab and in the wishlist result card; **not yet** repeated on the legacy exhaustive-search tab |
| 4th-year 30-credit planning supported without false universality | Yes |
| Valid subsets below/up to the ceiling generated | Yes |
| Course-package permutations handled | Yes (same term-aware conflict logic proven since the prior session) |
| Exhaustive vs partial search labelled honestly | Yes for the legacy path (pre-existing); wishlist path is honest about `optimal`/`feasible`/`infeasible` but does not offer exhaustive enumeration |
| No invalid L/T/P package appears | Yes (unchanged validated package construction) |
| H1/H2 dates handled correctly | Yes |
| Backend performs heavy scheduling | Yes — CP-SAT runs in the same process-isolated worker as the existing search |
| Progress and cancellation work | Yes — reused the existing SSE/poll/cancel machinery unchanged |
| Results grouped into meaningful families | **No** — one result per generation; deferred |
| Every result is explainable | Yes for the one schedule returned; no cross-schedule ranking explanation yet |
| Every omitted course has a why-not explanation | Yes |
| Plans can be compared | **No** — deferred |
| Personal data private by default | Yes (unchanged: localStorage-only, sent to the backend only for calculation) |
| All relevant tests pass | Yes — 92 + 37 + 35 + 62 + 0 violations, all reverified together at the end of this session |
| Application remains responsive | Yes — zero long tasks measured for the new path |

**Bottom line**: the architectural core — correct data model, a real typed credit policy, a real wishlist with choice groups, and a genuine CP-SAT exact-optimization backend with tested explainability — is built, integrated into the existing app (not bolted on separately), and fully tested. The parts explicitly not attempted this session (diversity/multiple-schedule-families, plan comparison, resilience/bid-repair, mobile-specific IA, a formal performance-benchmark report, transcript import, Demo Mode) are real, separately-scoped features and are named here rather than silently skipped or falsely claimed done.
