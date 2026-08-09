# CLAUDE.md — SNU Bid Simulator: Backend/Frontend Split

**Read this whole file before touching any code.** It is the handoff from a long chat
session that built this project from scratch, found and fixed real bugs along the way,
and ran out of budget partway through the frontend. Everything stated as "verified" or
"measured" below was actually run, not assumed — the specific commands are given so you
can re-run them yourself before trusting anything.

---

## 1. What this project is

A planning tool for Shiv Nadar University's Monsoon 2026 bid-point course enrolment
system — the *first cohort* at this institution to register for courses via a points
auction instead of first-come-first-served. Students get separate point pools for three
course categories (Major Elective / UWE / CCC), bid points on courses, and pay a
uniform clearing price if they win. The tool simulates that auction under adversarial
competition assumptions (see §5) and recommends bids that survive a worst-case scenario,
not just an average one.

**This is explicitly labelled an unofficial student tool, not a University product.**
Every rule it applies traces to a source document; every assumption is labelled as an
assumption, never presented as fact. That framing must survive any future changes.

### Why it's being split into backend + frontend
The original was a single HTML file with the whole simulation running in the browser.
Profiling a real Chromium session found the browser tab froze because **main-thread
time was 81,433ms out of an 81,360ms wall-clock run** (not a memory leak — heap stayed
flat at 10MB throughout) — a 38-course plan produced a single 11,888ms unbroken task.
The fix is to move all computation server-side (FastAPI + NumPy) and make the browser a
thin client that only renders results and manages local state.

---

## 2. Current state — exactly what works, proven by commands you can re-run

### Backend: fully built and verified

```bash
cd backend && python3 -c "
from app.domain.pools import compute_pools, max_bid
from app.domain.auction import settle
p = compute_pools('y4', 7, 9, 4, 11, 6)
print(p['ME'], p['UWE'], p['CCC'])   # -> 297 215 492
"
```

- `app/domain/rules.py` — 30-rule registry with provenance (`official` / `prospectus` /
  `inferred` / `disputed` / `unknown`), each citing its source document.
- `app/domain/pools.py` + `app/domain/auction.py` — pool formulas and clearing-price
  settlement. **Every value verified against the source documents' own worked
  examples, exactly** (see `docs/RULES_REFERENCE.md` for the specific numbers).
- `app/simulation/engine.py` — vectorized NumPy Monte Carlo simulator. A Numba-JIT
  kernel (`kernel.py`) was built, found to have a real stale-histogram bug via an
  isolation test (fixed), then **benchmarked at only 0.96-1.47x speedup — actually
  slower at 30k trials — and rejected** in favour of the pure NumPy path. That decision
  is evidence-based; don't re-introduce Numba without re-benchmarking.
- `app/optimization/robust.py` — reliability-target bid selection across a 3-scenario
  stress grid (High / Very High / Extreme), plus a genuinely separate "Optimistic
  comparison" scenario excluded from real recommendations.
- `app/workers/jobs.py` — process-isolated job manager (Python `multiprocessing`,
  `spawn` context) with real cooperative cancellation. **Measured: API ack 4.5ms,
  worker actually stops in 46ms** (target was <1000ms). Cancelled jobs return HTTP 409,
  never a partial result pretending to be complete.
- `app/persistence/store.py` — SQLite with versioned migrations (schema v1->v2 tested).
- `app/main.py` — 20 REST endpoints, including a **dual-budget-mode endpoint** that runs
  the simulation once and returns both the `SHARED_LIVE` and `INDEPENDENT` readings of
  the one unresolved rule (§5 below), so the student sees the disagreement directly
  instead of it being hidden.

Known real bugs found and fixed during backend development (documented so they are not
silently reintroduced):

1. **Convergence bug**: the original simulator resampled from a fixed 20,000-value pool
   for speed; that pool's own sampling error never averaged out, so the confidence
   interval understated true uncertainty by up to **7.4x**. Fixed by drawing fresh
   random values every trial (measured 5x slower per-trial, but net faster per unit of
   *useful* accuracy).
2. **Numba kernel bug**: reset only the histogram buckets touched by the current trial,
   leaving stale counts from previous trials. Caught by feeding both implementations
   *identical* inputs and diffing — never trust a benchmark until you've isolated
   correctness from performance.
3. **Dead optimizer parameter**: the `method` argument (minimax / cvar / mean) was
   computed but then compared against a constant that reduced to `>= 0`, so all three
   methods silently returned identical bids. Caught by mutation testing, not by a normal
   unit test — the mutation harness deliberately broke the aggregation logic and no
   existing test noticed. **Mutation score went from 11/15 to 15/15 after fixing this
   and adding the missing test.**
4. **`spawn` re-import gotcha**: an early test script re-executed itself inside the
   spawned worker process because `multiprocessing.get_context("spawn")` re-imports
   `__main__`. Any new test file that submits a real job must guard with
   `if __name__ == "__main__":`.
5. **`/tmp/snu.db` hardcoded default (found 2026-08-04)**: `app/persistence/store.py`
   defaulted `DEFAULT_DB` to the literal string `/tmp/snu.db`. On Windows this resolves
   to `C:\tmp\snu.db`, and `C:\tmp` does not exist, so the backend failed at startup
   with `sqlite3.OperationalError: unable to open database file` the first time this
   project was run on a non-Unix machine. Fixed by using `tempfile.gettempdir()` instead
   of the hardcoded path; the `SNU_DB` env var override still works unchanged.

### Frontend: partially built, thin client, verified where noted

Files that exist and work:

- `frontend/src/api.js` — the **only** place the frontend calls `fetch()`. Handles
  offline/timeout/HTTP-error/malformed-JSON/version-mismatch as distinct, student-
  readable error kinds. Implements a monotonic run-token so a stale simulation result
  can never overwrite a newer one, with server-side cancellation of the superseded job.
  SSE progress with automatic polling fallback if `EventSource` is unavailable or drops.
  **31/31 tests pass** (`node frontend/tests/adapter.test.js`) as of the schedule-search
  work in §10; was 24/24 before that.
- `frontend/src/plans.js` — saved plans with a versioned schema (v1->v2->v3 migrations
  tested), corrupted-`localStorage` quarantine-and-recover (never silently destroys
  data), CSV/JSON export, and import validation that assumes the imported file is
  hostile. **29/30 tests pass**, one known flaky test (see §4).
- `frontend/src/clash.js` — the *only* computation permitted to run in the browser per
  the project's own rule: pure day/time-overlap arithmetic for the schedule preview.
  Nothing that touches a bid, a probability, a pool, or a price.
- `frontend/src/glue.js` — DOM binding layer: builds the plan payload, drives the run
  lifecycle (validate -> submit -> subscribe to progress -> render), renders results,
  wires the plan toolbar (save/duplicate/delete/export/import/print), and boots the
  page. This is where new frontend features should be added.
- `frontend/src/ui/*.html` — nine UI-layer fragments (tabs), concatenated at build time.
  These carry the visual design and markup; they should contain **no fetch calls and
  no simulation math** — that's `glue.js` and `api.js`'s job respectively.
- `frontend/build_frontend.py` — assembles everything into one `dist/index.html`. It
  contains a **leak detector** that fails the build if any removed compute engine
  (`SIMULATE.`, `OPTIMIZE.`, `ROBUST.`, `COMPETITION.`, `ENGINE.`) is even *referenced*
  in the bundle, not just defined — this caught a real dangling `ROBUST.PRIORITY` call
  that would have thrown at runtime on the Course-picker tab. Keep this check; extend
  it if you add new engine modules to the backend.
- `scripts/start-local.sh` — one-command local launch (backend + static frontend
  server + health-check wait loop).
- `scripts/run-e2e.sh` — starts both servers, waits for health, then runs whatever test
  script you pass it. **Use this to run any Playwright test** — a bare `node
  tests/e2e.test.js` will fail because nothing is listening on 5173/8000 yet.

**End-to-end proof the architecture works** (`frontend/tests/e2e.test.js`, run via
`scripts/run-e2e.sh`): a real Chromium session boots the page, confirms `SIMULATE`,
`OPTIMIZE`, `ROBUST`, and `COMPETITION` are all `undefined` in `window` (i.e. genuinely
absent from the browser, not just unused), fetches pools from the backend and confirms
they equal 297/215/492, runs a full 2-course simulation through the real API, confirms
every bid respects its cap, confirms modelled rival counts exceed seat counts (the
stress-first default working as intended), confirms both budget interpretations are
returned and compared, confirms the "honest language" rules hold (no bare `100%`, no
"guaranteed"/"certain", assumption disclosed), exercises the whole-plan stress-test tab
end to end including a determinism check (same seed -> byte-identical result), and
confirms the backend-unavailable banner actually appears when the backend is genuinely
unreachable. **41/41 passing** as of 2026-08-04 (see §4 for what changed to get there).

Two more real bugs found while getting the stress-test tab and an accessibility pass to
actually work end-to-end (2026-08-04), added to the running list in §8:

6. **Literal `\n` in the tab bar**: `frontend/src/ui/b_body.html` had a literal
   two-character `\n` (backslash-n, not a real newline) sitting as visible text between
   the "Stress-test plan" and "Specialisation" tab buttons — almost certainly a stray
   artifact from a scripted string edit in an earlier session. It rendered as a visible
   `\n` in the tab bar in every browser. Caught by reading the actual rendered page text
   in a browser, not by looking at the HTML source formatted normally. Fixed by
   replacing it with a real newline.
7. **Dead `LASTROBUST` auto-run check**: `frontend/src/ui/c_core.html`'s tab-click
   handler had `if(t.dataset.p==='stress'&&typeof LASTROBUST!=='undefined'&&LASTROBUST)
   void stressPlan();` — `LASTROBUST` was never declared anywhere in the codebase, so
   `typeof LASTROBUST` was always `'undefined'` and this branch never fired. Every other
   data-driven tab (sched/two/spec/rules/learn) auto-refreshes on tab switch; stress
   never did, silently, because of this dead reference. Fixed to check
   `RESULT && RESULT.recommendations` instead, matching the pattern `stressPlan()` itself
   already uses as its own guard. This is now covered by `e2e.test.js` §16.

---

## 3. What is NOT done — be precise about this, don't claim otherwise

- **Production deployment config** (Postgres instead of SQLite, reverse proxy, TLS,
  auth) — `.env.example` sketches the shape but none of it is implemented or tested.
  Docker (see §9) runs SQLite in a named volume; it does not add Postgres, TLS, or auth.
- A polished `docs/API_REFERENCE.md` doesn't fully exist yet — the source of truth for
  endpoints right now is `backend/app/main.py` directly, plus the auto-generated
  OpenAPI docs at `/docs` when the server is running.

Everything else this section used to list as not-done — the WCAG audit, Docker, and the
stress-test tab's end-to-end wiring — was completed and verified on 2026-08-04. See §9.

---

## 4. Two previously-failing tests — fixed and reverified 2026-08-04

Both root causes below were confirmed by directly re-running the failures before
touching any code, and matched exactly what this file already claimed.

**`plans.test.js` — "list is newest-first" failed intermittently.**
Root cause, confirmed directly: `Date.now()` can return the identical millisecond for
two `save()` calls executed back-to-back in a fast test run (verified with a direct
Node check: two immediate `Date.now()` calls returned an equal value; also reproduced by
running the suite 8x in a row and seeing it fail ~5 of 8 times). The sort in
`PLANS.list()` used `updated` timestamp only, so ties had undefined order.
**Fixed**: added a persisted monotonic `seq` counter (`nextSeq()` in `src/plans.js`),
bumped on every `save()`/`rename()`, and `list()` now sorts by `updated` descending then
`seq` descending as a tiebreaker. Reran 8x after the fix: 30/30 every time.

**`e2e.test.js` — all failures were in the "§9 BACKEND UNAVAILABLE" section.**
Root cause, confirmed directly: the test did
`await p.route('**/api/v1/**', r => r.abort())` to simulate an outage, but the health
check hits `/health/ready`, which does **not** match the `**/api/v1/**` glob pattern
(health endpoints are deliberately outside the `/api/v1` prefix). So the abort never
actually blocked the health probe, the backend still reported itself reachable, and the
whole "offline banner" assertion block failed because the app correctly never went
offline. This was a **test bug, not an app bug**.
**Fixed**: added `await p.route('**/health/**', r => r.abort())` (and the matching
`unroute`) alongside the existing `/api/v1/**` route in `frontend/tests/e2e.test.js`.

```bash
cd frontend && node tests/plans.test.js         # 30 passed, 0 failed
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js  # 41 passed, 0 failed
```

Along the way, running the full stack for the first time on this Windows machine
surfaced an unrelated real bug (the backend wouldn't start at all) — see bug #5 in §2.

---

## 5. Design decisions that must NOT be silently reverted

1. **Stress-first competition, not optimistic.** Every course defaults to "assumed
   oversubscribed" (rivals > seats is enforced as a floor). This was a direct response
   to a real failure mode: an earlier model recommended bidding 0 on a 120-seat course
   because it invented 55.6 expected rivals from category-level capacity math. If you
   ever see a recommendation that looks suspiciously comfortable, check whether this
   floor got weakened.
2. **No fabricated certainty.** The UI must never print a bare `100%`. Use `>99.9% in
   this model` instead — a model output is not a fact, and there is no historical
   SNU data of any kind to have calibrated against (first cohort).
3. **Both budget-mode readings, always shown together.** `BUDGET.SHARED_LIVE` is
   genuinely unresolved by any source document. Don't quietly pick one interpretation
   and hide the other — the comparison endpoint exists specifically so the student sees
   the disagreement.
4. **Backend is the only authority for computation.** The `build_frontend.py` leak
   detector exists to enforce this mechanically, not just by convention. If you add a
   new backend module that does real math, add its name to `FORBIDDEN_TOKENS` in that
   script.
5. **Cancellation must stay real, not cosmetic.** Don't "fix" a slow cancel by just
   hiding the UI faster while the worker keeps burning CPU — the measured 46ms worker
   stop time is a property of the `multiprocessing.Event` cooperative-cancellation
   design in `jobs.py`; preserve that architecture.
6. **Every simulation is seeded and reproducible.** Same seed + same inputs must
   produce byte-identical output. This is tested; don't introduce unseeded randomness
   (e.g. `Math.random()` / `time.time()`-based entropy) into any compute path.

---

## 6. How to run everything

```bash
# one-time setup
cd backend && pip install -r requirements.txt --break-system-packages
cd ../frontend && python3 build_frontend.py

# start both servers and open the app
cd .. && ./scripts/start-local.sh
#   App:      http://127.0.0.1:5173/
#   API docs: http://127.0.0.1:8000/docs
#   Health:   http://127.0.0.1:8000/health/ready

# run any test against a live stack (starts servers, waits for health, runs, tears down)
# note: paths are relative to backend/ and frontend/, and the script itself cd's into
# frontend/ before running node -- pass "tests/e2e.test.js", not "frontend/tests/..."
./scripts/run-e2e.sh tests/e2e.test.js
./scripts/run-e2e.sh tests/a11y-audit.js

# backend-only tests (no server needed, pure Python)
cd backend && python3 -c "from app.domain.pools import compute_pools; print(compute_pools('y4',7,9,4,11,6))"

# frontend unit tests (no server needed)
cd frontend && node tests/adapter.test.js && node tests/plans.test.js

# containerized (see §9) -- same app, no local Python/Node/pip setup required
docker compose build
docker compose up -d
#   App:      http://127.0.0.1:5173/
#   API docs: http://127.0.0.1:8000/docs
docker compose down
```

`nproc` on the machine this was built on was **1** — a process pool gives isolation
(so cancellation and crash-containment work) but zero parallel speedup. If you're now
on a multi-core machine, the architecture in `jobs.py` will parallelise across
scenarios with minimal changes; that wasn't done here because it couldn't be measured.

---

## 7. Source documents this project is built from

The original University documents (bidding announcement, concept note with point
formulas, unofficial student guide, CSE prospectus with specialisation buckets, and a
WhatsApp group export containing the academic calendar and student Q&A) were attached in
`snu-scheduler-files/` on 2026-08-04: `CourseBiddingTentativeGuide (1).pdf`,
`Course_Bid_Point_Allocation_Concept_Note_final.pdf`, `DOC-20250722-WA0020.pdf`,
`academic_calendar_monsoon_2026.pdf`, `Monsoon 2026 Timetable.xlsx`,
`List of Swayam Courses offered in Monsoon 2026 Semester_Final.xlsx`, and
`WhatsApp Chat with Shreyas for President.zip`, among others. Their content has already
been extracted, verified, and encoded into `backend/app/domain/rules.py` and
`docs/RULES_REFERENCE.md`. If a rule ever seems wrong, the fix is to re-derive it from
those source documents directly, not to guess.

---

## 8. Philosophy this project has earned the hard way

Every module in this codebase has had at least one real bug caught by testing rather
than by code review — the convergence bug, the Numba histogram bug, the dead optimizer
parameter, the `spawn` re-import gotcha, the leaked `ROBUST.PRIORITY` reference, the
`Date.now()` tie, the route-pattern test bug, the Windows `/tmp` path, the literal `\n`
in the tab bar, the dead `LASTROBUST` check, and (§10) an unrealistic test fixture that
ate 2GB of RAM, a too-strict process-exit timing assertion, a Node test that hung
forever on an unmocked endpoint, and a Docker verification that was silently hitting a
stale local process instead of the container because of a port collision. None of these
were hypothetical. **Treat "it looks right" as insufficient evidence for anything in
this codebase — write the test, run it, and read the actual output before believing a
change works. That applies to your own tests and profiling scripts too, not just the
product code.**

---

## 9. Session update — 2026-08-04: stress-test tab, WCAG audit, and Docker

All three items §3 used to list as not-done were completed and verified for real in
this session, on this machine (Windows, Docker Desktop 29.6.2, Node v24, Python 3.11.9).

**Whole-plan stress-test tab.** Added a real browser section to `e2e.test.js` (§16):
runs a 2-course simulation with one `MUST`-priority course, switches to the stress tab,
confirms the tab auto-runs (bug #7 in §2 — it never used to), checks the rendered
cohort count / all-must-haves rate / per-course failure table / methodology note, and
re-clicks the button to confirm the same seed reproduces a byte-identical result
(design decision §5.6). 41/41 passing.

**Accessibility audit.** Wrote `frontend/tests/a11y-audit.js`: real Playwright +
axe-core (not a static linter), walks all 11 tabs of the live, data-populated page,
including exercising the plan-toolbar controls (Save/Duplicate) on "Profile & budget"
before scanning it, per §3's flag that those controls were the least-reviewed markup.
Found 24 violation instances across two real rule types, all fixed:
- `landmark-one-main` / `region` (moderate, every tab): the page had no `<main>` or
  `<header>` landmark at all. Fixed by wrapping `.mast` + `#backendBar` in `<header>`
  and the tab bar + all panes in `<main>` in `b_body.html`.
- `empty-table-header` (minor, 4 instances across 3 tables): action-only columns
  (row-remove buttons, a "use this" button) had a bare `<th></th>` with no accessible
  name. Fixed with `<th><span class="sr-only">Remove</span></th>` (the `.sr-only` CSS
  class already existed in `a_head.html`, just unused for this).
Re-ran after fixing: 0 violations across all 11 tabs. Full suite (`e2e.test.js`,
`plans.test.js`, `adapter.test.js`) reconfirmed clean afterward — the markup changes
didn't regress anything else.

**Docker.** Docker Desktop was not running by default on this machine but *is*
installed (29.6.2 / Compose v5.3.1) — unlike the environment this handoff was written
in, which had no Docker binary at all. Wrote `backend/Dockerfile` (python:3.11-slim,
`python -m uvicorn app.main:app`, SQLite in a named volume via `SNU_DB=/data/snu.db`),
`frontend/Dockerfile` (multi-stage: python3 build_frontend.py — its leak detector runs
as part of the image build, so a bundle with a leaked engine reference can never ship —
then nginx:alpine serving the built `dist/index.html`), and a root `docker-compose.yml`
wiring both together with a backend healthcheck gate. **Actually built and ran it**:
`docker compose build` succeeded, `docker compose up -d` brought both containers up
(backend reported `healthy`), `curl .../health/ready` and the pools endpoint returned
297/215/492 through the container, and a real browser pointed at
`http://127.0.0.1:5173/` (the frontend container) round-tripped to the backend
container and rendered "FROM BACKEND · POOL.Y4" with the correct pools — confirmed with
`get_page_text`, not assumed. Torn down cleanly with `docker compose down` afterward.

None of the six design decisions in §5 were touched by any of this work; the
stress-first floor, the seeded-reproducibility guarantee, and the leak detector were all
re-verified as still intact rather than assumed.

---

## 10. Session update — 2026-08-04 (later): schedule search moved to the backend

A much larger request came in asking for a full production rebuild (React/TypeScript
frontend, Postgres/Redis, CI/CD, load testing, a security audit, a full doc suite - 29
sections). That is genuinely months of work and was **not** attempted wholesale; asked
the user to scope it down first. Their answer: skip the enterprise-checklist filler,
keep persistence anonymous/local (no auth built - "not sure yet, keep anonymous for
now"), keep SQLite/in-process (Postgres/Redis path documented only, not stood up), do
React "as its own tracked slice, after backend fixes," and make **the scheduler, the
backend, and the UI actually work properly**. This section is that first slice.

**The concrete problem (profiled before touching code).** The Schedule Builder tab
(`frontend/src/ui/i_build.html`'s old `enumerateSchedules()`/`buildSchedules()`) ran a
synchronous recursive backtracking search directly on the browser's main thread, default
budget "2M combinations tried," selectable up to 8M. Built a synthetic worst-case (20
courses x 6 mutually-compatible packages) and measured it live in a real browser tab via
a heartbeat probe: **100,000 nodes = 5.03s, tab fully unresponsive for the whole span**.
Extrapolated: the 2M default ~= 101s, the 8M option ~= 403s (6.7 minutes) of a frozen
tab - worse than the 81s freeze already documented in §1 for the pre-split single-file
version. The "browser is a thin client" claim (§5.4) was false for this one screen.

**What moved server-side.**
- `backend/app/domain/catalog.py` + `backend/app/data/courses.json` - the backend had
  **zero** course/package/meeting data before this; it all lived only in
  `frontend/src/data.json`, baked into the browser bundle. Copied it in (326 courses,
  859 packages - matches the README's own numbers exactly) so schedule search has
  authoritative data instead of trusting whatever the browser sends. Kept in sync via
  `backend/scripts/sync_course_data.py`; `tests/test_catalog.py` fails the build if the
  two copies ever drift (byte-comparison, skipped only when the frontend source isn't
  present in the build context). `SNU_CATALOG_PATH` env var overrides the path -
  primarily so tests can point a real spawned worker at a synthetic catalog.
- `backend/app/services/scheduler.py` - the search itself, ported faithfully (same
  most-constrained-first backtracking, same clash semantics) from the JS. **Measured
  faster per node than the browser**: 6.01us/node in Python vs 50.3us/node in the
  identical synthetic-worst-case JS benchmark - moving it server-side wasn't just "off
  the main thread," it's also roughly 8x more raw throughput per node. A second,
  heavier synthetic shape (deep-conflict, "BLOCKER" course that clashes with everything,
  used for the cancellation tests below) costs ~69us/node - conflict-check cost scales
  with search depth, so per-node cost is not a single constant; both numbers are in
  `tests/test_schedule_jobs.py`'s comments for whoever recalibrates fixtures later.
- `backend/app/workers/schedule_jobs.py` - `ScheduleJobManager`, a **separate** manager
  from `workers/jobs.py`'s simulation `JobManager`, deliberately not a generalisation of
  it. Same proven shape (spawn context, `multiprocessing.Event` cooperative
  cancellation, progress/result queues, a reaper thread) because that shape is what's
  already measured to work (§2's 46ms simulation stop time); didn't want to risk the
  already-verified simulation path by bending it to fit a second job kind under time
  pressure.
- `backend/app/main.py` - five new endpoints: `POST /api/v1/schedules/search`,
  `GET /{id}`, `GET /{id}/results` (paginated - `limit`/`offset`/`next_offset`, default
  page 60, never dumps the whole result set), `POST /{id}/cancel`, `GET /{id}/events`
  (SSE, same pattern as simulations). Course codes are validated against the catalog at
  submit time (422 with the specific unknown codes) before a worker process is ever
  spawned.
- `frontend/src/ui/i_build.html` - stripped down to **rendering only**
  (`renderSchedules`/`useSchedule`/`previewSchedule`/`renderMiniGrid`). The search
  function and its scoring function are gone from the browser entirely, not just
  unused - confirmed by grep and by the leak detector (see below).
- `frontend/src/glue.js` + `frontend/src/api.js` - the actual orchestration
  (`buildSchedules()`/`cancelBuild()`/`loadMoreSchedules()`, `API.runScheduleSearch()`
  etc.), mirroring `runOpt()`/`cancelRun()`/`API.runSimulation()` exactly (submit ->
  SSE-with-polling-fallback progress -> render; a monotonic run-token, **independent**
  of the simulation one, so an in-flight search and an in-flight simulation can never
  invalidate each other).
- `frontend/build_frontend.py` - `FORBIDDEN_TOKENS` extended with `"enumerateSchedules("`
  and `"scheduleStats("` per design decision §5.4 (any backend-only compute path added
  must be added here too).

**Verified, not assumed:**
- `backend/tests/test_scheduler.py` (11 tests) - pure algorithm: no overlaps in results,
  fixed/locked meetings are never moved by the search, same-course meetings never
  self-conflict, node-budget truncation, cancellation, deterministic ordering across
  repeated runs, most-constrained-first item ordering, all four sort keys.
- `backend/tests/test_schedule_jobs.py` (6 tests) - the real process-isolated manager:
  cancellation acknowledged in the same call (<100ms) and the actual OS process exits
  within 2s of reporting cancelled, a cancelled job never later reports a result,
  identical requests are a cache hit, unknown job ids, cancel-after-completion.
  **First draft of these tests was itself wrong twice** and both are worth knowing
  about: (1) a "slow" synthetic fixture used `max_results=10,000,000` - unrealistic,
  since the real API schema caps `max_results` at 5,000 - and a fully-compatible
  20x6 course set found so many valid leaves that scoring/sorting all of them ate 2GB
  of RAM and never finished; fixed by giving the fixture a `BLOCKER` course that
  conflicts with everything, so the search explores deeply while the result count stays
  at zero. (2) the cancellation test asserted `proc.is_alive()` was already `False` the
  same instant the job's state flipped to `"cancelled"` - too strict; there's a real
  (small) gap between the worker's out-queue message being drained and the OS process
  object reporting not-alive (interpreter teardown, queue feeder thread flush). Fixed by
  polling for up to 2s instead of asserting instantaneously.
- `frontend/tests/adapter.test.js` (+7 tests, 31 total) - the new API functions with a
  stubbed fetch, same patterns as the simulation adapter tests (stale-run handling,
  cancellation, SSE-vs-polling fallback, independent run tokens). First draft of one new
  test hung forever in Node - its mock didn't handle the `POST /schedules/search` call
  at all, so `job.job_id` was `undefined` and the status-poll loop retried against
  `/schedules/undefined` forever; fixed by adding the missing mock branch.
- `frontend/tests/e2e.test.js` (+7 tests, §17, 48 total) - real browser against the real
  backend: empty-shortlist message, **zero Long Tasks API entries during the search**
  (the direct regression check for "no long task on the main thread" - the old version
  would have shown one ~100s entry here), a real course search rendering the two
  actually-correct schedules for a known-compatible shortlist, preview, "use this"
  (confirms package reassignment and tab navigation), and sort-change re-triggering a
  (cached) server-side re-sort.
- `frontend/tests/a11y-audit.js` - reran after all UI changes: still 0 violations across
  all 11 tabs.
- Docker - rebuilt both images, ran the full stack, submitted a real schedule search
  through the container, confirmed `cache_hit:false` + correct results + it appeared in
  the container's own logs. **First attempt at this was silently wrong**: a leftover
  local `uvicorn` process from earlier manual testing was still bound to
  `127.0.0.1:8000` (Docker's container was bound to `0.0.0.0:8000`), and Windows routed
  requests to `127.0.0.1:8000` to the more-specific local process instead of the
  container - the response looked completely valid (including a `cache_hit:true`, since
  that stale process's in-memory cache already had the entry from earlier testing) but
  had never touched the container at all. Caught by checking the container's own logs
  for the request and finding nothing there. **If a Docker verification ever looks
  suspiciously instant or already-cached, check `netstat`/`docker port` for a port
  collision before trusting the response.**

```bash
cd backend && python3 -m pytest -q                                # 30 passed
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 31 + 30 passed
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                    # 48 passed
./scripts/run-e2e.sh tests/a11y-audit.js                          # 0 violations
```

**Explicitly not done in this slice** (by the user's own scoping, not an oversight):
React/TypeScript frontend (tracked as the next slice, after this one), Postgres/Redis
(design path only - see below - not implemented), authentication/accounts (anonymous
local persistence stays as-is), CI/CD, load testing, mutation-testing expansion, a
formal security audit, and the full 25-document doc suite the original giant request
asked for.

**Postgres/Redis - design path, not implemented.** If a future session needs to scale
beyond one API process: swap `app/persistence/store.py`'s raw `sqlite3` for SQLAlchemy +
Alembic migrations against Postgres (the schema is already simple - `plans`,
`observations`, `job_history` - this is a mechanical port, not a redesign); replace
`JobManager`/`ScheduleJobManager`'s in-process `dict`+reaper-thread state with a Redis-
backed job table so state survives an API restart and multiple API instances can share
one worker pool; keep the `multiprocessing.Event` cooperative-cancellation *design*
regardless of transport (measured 46ms stop time is a property of that pattern, not of
being in-process). Don't build this speculatively - there's no second machine or
multi-instance deployment target to actually verify it against yet, and an unverified
distributed-systems rewrite is worse than an honest "not done."

---

## 11. Session update — 2026-08-04 (later still): personal-data generalization,
## end-time display, data verification, and a real desktop .exe

**Personal data was baked into the shipped source, not just stored locally.** Found
that `frontend/src/ui/b_body.html`'s "Your degree position" inputs had one test student's
real numbers hardcoded as HTML `value=` defaults (`remME=9`, `remUWE=4`, `remCCC=11`,
`remFL=6`, `doneCr=105`, `remCore=9`, `remOther=16`), and `glue.js`'s `boot()`
hardcoded his actual pre-enrolled courses into `FIXED`/`MANUAL` unconditionally. Every
fresh load - including for anyone this app is ever shared with - started pre-filled
with his specific academic data. Separately, `doneCr`/`degCr`/`remCore`/`remOther` and
the CSD block selection were never even included in the autosave/restore payload, so
they'd have been silently lost on next reload regardless.
**Fixed properly, not just patched:**
- HTML defaults reset to neutral (`0`, or `160` for `degCr` and `28` for `capCr`, both
  University-wide constants rather than personal facts).
- `boot()`'s `FIXED`/`MANUAL` now start empty.
- `plans.js` schema bumped to v4: `doneCr`/`degCr`/`remCore`/`remOther`/`csdBlock` and
  the `fixed`/`manual` arrays are now part of the saved payload and the migration gives
  existing v<4 plans safe defaults. `currentPlanPayload()`/`restoreActivePlan()` in
  `glue.js` read and write all of it.
- Verified in a real browser: `localStorage.clear()` + reload -> every field is `0`,
  `FIXED`/`MANUAL` are empty (this is what anyone this app is shared with will see).
  Filled in the test student's numbers through the UI, clicked Save, reloaded -> all of it
  came back exactly, tied to a plan named "Student plan" that lives only in this
  browser's `localStorage` on this machine - never in the shipped files.
- `frontend/tests/plans.test.js` +2 tests (32 total) covering the v4 migration
  defaults. `e2e.test.js`'s pools check used to rely on the (now-removed) hardcoded
  defaults; fixed it to set `remME`/`remUWE`/`remCCC`/`remFL` explicitly instead, since
  a test silently depending on a personal-data default was itself part of the problem.
- **Known, deliberate boundary**: the "Specialisation" tab's pairing logic
  (`h_spec.html`) is hardcoded to check for `CSD211/CSD2003` + `MED2001` specifically -
  that's a product feature built around one test profile's CoA/backlog situation, not a
  personal-data leak in the same sense as the numbers above. Generalizing it to work
  for any two arbitrary courses is a real feature change, not a bug fix, and wasn't
  done here - flagging it rather than silently leaving it undocumented.

**End times were missing exactly where reported.** `d_sched.html`'s fixed-courses
table and the course-picker's meeting preview both rendered only `hm(m[1])` (start
time), never `hm(m[2])` (end time) - confirmed by grepping every `hm(` call site in the
frontend. The timetable grid itself (`e_tt.html`) already showed both via block height
+ a full tooltip, so that one was left alone; the two bare-text-list spots were fixed
to show `start–end` for every meeting.

**Data verification - checked against the source workbook, not assumed.** Cross-referenced
every one of the **1,168 meeting rows** in `Monsoon 2026 Timetable.xlsx` against every
meeting instance in `data.json` (used by both the frontend bundle and
`backend/app/data/courses.json`): **zero fabricated or incorrect entries**, and exactly
2 rows genuinely absent - both for `BIO2009`, which the dataset already correctly
excludes (`unsched: true`) because the *source workbook itself* double-books that
course's LEC and PRAC in the identical Tuesday slot. Also cross-checked seat capacity
for all 326 courses against the workbook's own "Section Capacity" column: zero
mismatches. This is a real, complete audit, not a spot check.

**A real Windows .exe, built and run, not just scaffolded.**
- `backend/desktop_launcher.py` - new entry point that serves the API *and* the built
  frontend bundle from one FastAPI process on one port (reusing the frontend's existing
  same-origin default, so no CORS/config changes needed), then opens a native window via
  `pywebview` (falls back to the default browser if WebView2 isn't available).
- `scripts/build-exe.sh` - `pyinstaller --onefile --windowed`, bundling `app/data/courses.json`
  and the built `frontend/dist/index.html` as data files.
- **Real bug caught before it shipped**: the first build's simulation/schedule-search
  jobs hung forever. Root cause: PyInstaller's frozen exe re-executes *itself* as the
  interpreter for spawned `multiprocessing` workers, and without
  `multiprocessing.freeze_support()` guarding the entry point, each worker reran the
  *entire desktop app* from scratch (visible in the log: a second "startup complete",
  then a crash trying to rebind the already-held port) instead of doing any actual work
  - the exact `spawn` re-import gotcha from bug #4 in §2, but at the whole-application
  level. Fixed by adding `multiprocessing.freeze_support()` before `main()` in
  `desktop_launcher.py`.
- Also needed: `catalog.py`'s data-file path is normally resolved relative to its own
  `__file__`, which isn't a reliable real filesystem path once frozen - the launcher
  sets `SNU_CATALOG_PATH` to the PyInstaller-extracted location explicitly before
  importing `app.main` (reusing the same env var override built for tests in §10).
- **Verified working, end to end, in the actual built .exe**: launched it, confirmed
  one process on one port serves both `/` (frontend) and `/api/v1/...`, ran a real
  2-course bid simulation through it (896ms, correct recommendations), ran a real
  schedule search through it (correct 2-schedule result for the known shortlist), and
  confirmed the SQLite-backed `/api/v1/jobs` history endpoint works inside the frozen
  build. Rebuilt with `--windowed` (no console flash) after that and reverified the
  same three things still work.

```bash
cd backend && python3 -m pytest -q                                    # 30 passed
cd frontend && node tests/adapter.test.js && node tests/plans.test.js # 31 + 32 passed
./scripts/run-e2e.sh tests/e2e.test.js                                # 48 passed
./scripts/run-e2e.sh tests/a11y-audit.js                              # 0 violations
./scripts/build-exe.sh                                                # -> backend/dist/SNU-Bid-Scheduler.exe
```

**Explicitly not done in this pass**: the React/TypeScript frontend slice is still
queued, not started - this session's time went to the concrete items above instead,
per direct instruction. Reducing the exe's size (it pulled in matplotlib/PIL/tkinter
transitively, landing at ~59MB for what should need far less) is a worthwhile future
cleanup but wasn't risked here against a build that's now verified working.

---

## 12. Session update — 2026-08-04 (yet later): the exe actually crashed, a real
## least-conflict solver, and two infrastructure bugs found while chasing a ghost

The exe from §11 crashed immediately for the user: `ValueError: Unable to configure
formatter 'default'` -> `AttributeError: 'NoneType' object has no attribute 'isatty'`
at `uvicorn/logging.py`. Root cause: a `--windowed` PyInstaller build has no console,
so `sys.stdout`/`sys.stderr` are `None`, not a harmless dummy stream as §11 assumed -
uvicorn's own logging setup calls `sys.stdout.isatty()` while configuring its default
formatter, before any app code runs, and crashes immediately. **Fixed** in
`desktop_launcher.py`: a `_NullStream` shim installed on `sys.stdout`/`sys.stderr` if
either is `None`, plus `log_config=None` on the uvicorn `Config` so it never runs its
own logging setup at all (the app's own JSON log handler is already sufficient).
Rebuilt, ran the actual `.exe`, confirmed no crash and that a real simulation, a real
schedule search, and plan persistence all still work inside it.

**Specialisation tab still had personal data**, missed in §11's audit: `h_spec.html`
hardcoded `DONE_ME=[{code:'CSD355',...},{code:'CSD360',...}]` - one test student's completed
major electives, baked in for everyone, driving the specialisation-bucket "banked
credit" numbers on every fresh install. A second hardcoded number ("you have **9**
major-elective credits left in total") was a literal string, not derived from the
`remME` field, so it never updated even when the profile numbers changed. **Fixed**:
`DONE_ME` moved to `c_core.html` as an empty default, a new editable list ("Already-
completed major electives") added to the Specialisation tab (`addDoneME`/`removeDoneME`/
`renderDoneME` in `h_spec.html`), persisted via `plans.js` schema v5
(`doneElectives`), and the credit-count sentence now reads `+$('remME').value` live and
reasons about whether route A is even numerically feasible instead of assuming route B.

**Meeting times: end time and component (LEC/TUT/PRAC) were both still missing** in
exactly the two places named - the Scheduler & clashes tab's timetable grid only ever
showed `LEC · 9:35AM` in the visible block label (full range was tooltip-only,
`e_tt.html`), and the course-picker/fixed-courses "When" text listed times with no
component tag at all (`d_sched.html`). Fixed both: the grid block now shows
`LEC / 9:35AM–11:00AM` on two lines (still clipped for very short blocks by design -
`overflow:hidden` - the tooltip remains the authoritative full detail); the text lists
now read `LEC Tue 9:35 AM–11:00 AM · LEC Thu ... · PRAC Mon ...`.

**A second in-browser heavy search, found while fixing the first thing asked for.**
`autoSolve()` ("Auto-resolve clashes", Course Picker tab) turned out to run its own
synchronous branch-and-bound search directly on the page's main thread, capped at
400,000 nodes - the same category of bug as the old Schedule Builder search from §10,
smaller in scale but structurally identical, and it ran *silently on every page boot*
(`autoSolve(true)`) even though by that point in `boot()` it was already a no-op (it ran
before `restoreActivePlan()` had populated `FIXED`/`PICK`, so `items.length` was always
0). Neither the request nor the "runs on every boot" behavior had been asked about
directly - it surfaced only because implementing the requested "cycle through
combinations and find the least-clash one" feature meant building a real least-conflict
solver, and it became obvious `autoSolve()` needed the exact same thing.

**What got built**, once, on the backend, shared by both features:
- `app/services/scheduler.py`: `enumerate_least_conflict()` - branch-and-bound over total
  pairwise clash count. A running clash count only ever *increases* as more courses are
  placed, so a partial assignment already at or above the best complete assignment found
  so far can never win and is pruned immediately - this is what keeps it tractable
  without weakening the existing zero-clash search's own pruning. `search_with_fallback()`
  tries the exact (zero-clash) search first; only if that finds nothing does it spend the
  extra work running the least-conflict pass. Verified the branch-and-bound actually finds
  the *true* minimum, not just the first complete assignment it stumbles into
  (`test_least_conflict_finds_the_true_minimum_not_just_the_first_found` - a 3-course case
  built specifically so a naive search would land on the worse pairing).
- `FixedCourse.locked` (default `True`) lets a "fixed" course join the search space
  instead of being held static, for Round 1/5 Major-Core swap semantics - `autoSolve()`
  now sends its unlocked pre-enrolled sections this way instead of running its own search.
- `autoSolve()` itself is now a thin async wrapper calling `API.runScheduleSearch` and
  applying the winning `assign` back onto `FIXED`/`PICK`, same shape as `buildSchedules()`.
  Removed from `boot()`'s synchronous local-render sequence entirely (matches every other
  network-backed feature in this app: explicit button, not automatic-and-silent).
- `frontend/build_frontend.py`: `FORBIDDEN_TOKENS` extended with `"nodes>400000"`, the
  literal bound of the removed client search, so it can't silently come back either.

**The bug that actually mattered most, and how it was found.** After all of the above
was unit-tested and passing (17 scheduler tests, 8 job-manager tests, all green), the
literal feature still didn't work end-to-end: a real browser test against two courses
with a provably unavoidable clash (verified against the source workbook directly - see
§11's audit) reported "1 valid clash-free schedule found." Isolated it methodically:
called `search_with_fallback()` directly in Python - correct (`least_conflict`,
`clash_count=1`). Called `_worker_entry()` directly, bypassing multiprocessing entirely -
still correct. The bug was in `main.py`'s `/results` route: its response dict was
built by hand, field by field, *before* `mode`/`clash_count` existed, and was never
updated when `search_with_fallback` added them - so every caller of the real HTTP API
silently got a response that looked like a clash-free success. **Every existing test
missed this because `test_schedule_jobs.py`'s tests call `ScheduleJobManager` directly
and read `job.result` in-process, never going through the actual route handler at all.**
Added `backend/tests/test_api_schedules.py` using FastAPI's `TestClient` against the
real ASGI app - genuinely through the HTTP layer, lifespan and all - specifically
because that layer had zero coverage. Confirmed it catches the exact regression by
reverting the fix and watching both new tests fail, then restored the fix and reran.

**A second bug, found while debugging the first one: the local test scripts were
sometimes silently testing stale code.** `run-e2e.sh`'s cleanup trap does
`kill "$p"` on the PID bash thinks it started - on this Git-Bash-on-Windows setup this
does not reliably reach the actual `uvicorn`/`http.server` child, so a process from an
earlier run can keep listening on port 8000 indefinitely. The *next* invocation's health
check then happily talks to that stale process, reports `stack up: api=ok`, and every
test that follows silently exercises old code while looking completely normal. This is
the same category of bug as the Docker port-collision gotcha in §10, now caught in the
everyday dev-loop scripts instead of a one-off Docker check - confirmed by checking
`netstat`/`tasklist` directly and finding processes from far earlier in the session still
holding the ports. **Fixed** in both `scripts/run-e2e.sh` and `scripts/start-local.sh`:
a `kill_port()` helper that kills by *port* (via `lsof` or, on Windows,
`netstat`+`taskkill.exe`) rather than trusting the PID bash captured, run both before
starting (clears anything leaked by a previous run) and in the exit trap (stops this
run from leaking into the next one). Verified with an explicit `netstat` check
immediately after a full e2e run completed - ports were actually clear, not just
assumed to be.

```bash
cd backend && python3 -m pytest -q                                     # 40 passed
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 31 + 33 passed
./scripts/run-e2e.sh tests/e2e.test.js                                 # 55 passed
./scripts/run-e2e.sh tests/a11y-audit.js                               # 0 violations
./scripts/build-exe.sh                                                 # rebuilt, ran, verified no crash
```

Design decisions in §5 and the leak-detector rule in §5.4 were re-verified intact, not
just assumed, including through the new least-conflict path (it still requires the
backend for all computation, still never fabricates a clash-free result when one
doesn't exist, and is exercised by the same leak detector that already covers §10's
schedule search).

---

## 13. Session update — 2026-08-04 (later still): wishlist scheduler + credit-policy
## phase, against two independent research reports

A large request came in ("Spring 2027 — what has to happen" planning context, plus two
attached research reports — `gemini-research.txt` and `deep-research-report.md`, both on
what an optimal SNU scheduler should look like) asking for a personalised, wishlist-
driven, credit-aware scheduler: a real student profile/credit-policy model, a real
wishlist with intent and choice groups, a CP-SAT backend optimizer, explainability, a
full Scheduler-page redesign, mobile IA, bid-outcome repair, a performance-benchmark
suite, and more — roughly 20 numbered sections' worth.

**This was explicitly scoped down, not attempted wholesale.** Both research reports were
read in full first (not summarized-then-guessed), then compared point-by-point against
the actual repo state in `docs/research/scheduler_v2_matrix.md` — that file is the
authoritative record of what was implemented, what was partially implemented, and what
was deferred, with the *why* stated for every deferral rather than a silent omission.
The short version: the architectural core (data model correctness, a real typed credit
policy, a real wishlist with choice groups, a genuine CP-SAT exact-optimization backend,
and tested explainability) was built and fully tested. A full three-pane/mobile-IA
redesign, a diversity layer (multiple ranked schedule families), cross-schedule
comparison, bid-outcome repair, a formal performance-benchmark report, and transcript
import were **not** attempted this session — see the matrix's own tables for exactly
why each one is a separately-scoped feature rather than a quick add-on.

**Another personal-data leak found and fixed, the same category as §11/§12's.**
`frontend/src/ui/b_body.html`'s "Semester credit cap (overload)" field defaulted to
`28` — not a University number, the original developer's own personal overload figure,
shipped as everyone's default. Fixed to `25` (`CEILING.STANDARD`).

**Credit-ceiling policy (`backend/app/services/credit_policy.py`).** Both research
reports independently flagged the same anti-pattern this app had: one ambiguous
"credit limit" field standing in for five different concepts (official ceiling /
personal target / approved overload / fixed credits / wishlist room). `resolve_ceiling()`
keeps them distinct and returns a `ceiling_mode` of `"standard"` / `"what_if"` /
`"approved_overload"` — never presenting an unconfirmed 30-credit fourth-year scenario as
a normal rule. Two new rules in `rules.py`: `CEILING.STANDARD` (25, official) and
`CEILING.APPROVED_OVERLOAD` (`Status.UNKNOWN`, deliberately has **no default numeric
value** — a caller must supply the overload ceiling explicitly, and it is always echoed
back labelled, never silently equated with the standard ceiling). New endpoint
`POST /api/v1/profiles/validate` returns the plain-language summary sentence the spec
asked for (`"15 fixed credits + up to 10 wishlist credits = 25-credit total ceiling"`).
14 tests in `backend/tests/test_credit_policy.py` cover Y1-Y4 profiles, the overload/
approved-exception distinction, invalid ceilings, ceiling-below-fixed-credits, decimal/
half-semester credits, and a stale-import scenario.

**Real wishlist with choice groups (`backend/app/models/profile_schemas.py`,
`backend/app/services/wishlist.py`).** The existing `PICK`/`PRIO` model on the Course
Picker tab already had a 4-value priority (`MUST`/`STRONG`/`BACKUP`/`OPTIONAL`) that maps
directly onto the spec's intent concept - reused it rather than building a second,
disconnected wishlist data structure (per the spec's own instruction: "integrate into the
current architecture, don't build a disconnected experimental page"). Added a typed,
server-validated `WishlistItem` (intent, priority, locked/excluded packages, instructor
preference, notes) and a `ChoiceGroup` (`exactly_one`/`at_least_one`/`at_most_one`/
`min_credits`). `wishlist_summary()` computes the eight display stats the spec asked for
(count, min/max possible credits, category composition, must-have/backup/impossible/
unconfirmed counts). New endpoint `POST /api/v1/wishlists/validate`.

**CP-SAT exact optimization layer (`backend/app/services/cp_scheduler.py`) - the
architectural piece both research reports converge on hardest.** Added `ortools` as a
new backend dependency (confirmed importable, confirmed working through a real spawned
worker process - `multiprocessing.get_context("spawn")` re-imports cleanly). Models
course inclusion and package selection as boolean variables, term-aware conflict
constraints (reusing the exact same `_meetings_overlap` function the proven shortlist
search already uses - H1/H2 half-semester disjointness included for free), the four
choice-group kinds, and hard credit-ceiling/floor bounds. Objective is a single weighted
sum with tier weights spaced 1000x apart (`credit-target deviation` >> `priority/value
proxy` >> `campus-day compactness`) - documented in the module's own docstring as a
deliberate simplification of a true lexicographic multi-solve, not a hidden one.
Deterministic: `num_search_workers=1` + fixed `random_seed`, same principle the
simulation engine already uses. **This is additive, not a replacement**: the existing
`app/services/scheduler.py` branch-and-bound path is completely untouched and still owns
the plain-shortlist "every clash-free combination" flow; the CP-SAT path only activates
when a request's `wishlist` field is non-empty (checked in
`app/workers/schedule_jobs.py::_solve_wishlist`).

Proven correct, not just plausible: `test_differential_against_brute_force_credit_target_fit`
compares the CP-SAT result's credit-target fit against a brute-force enumeration of every
subset of a synthetic 6-course wishlist, across 6 different targets - exact match every
time. 21 tests in `backend/tests/test_cp_scheduler.py` cover every hard constraint
(must-have retention, package locks/exclusions, fixed-course clash blocking, H1/H2
disjointness, all four choice-group kinds, ceiling enforcement, min-floor relaxation,
determinism) plus 5 `explain_omission` cases.

**Explainability (`explain_omission()` in the same module).** For any wishlist course
not in the returned schedule, returns one of: `time_clash_with_fixed`, `choice_group_rule`
(names the rival that was selected instead), `credit_ceiling` (states exactly how many
more credits would be needed), `no_valid_combination` (a genuine structural conflict), or
`lower_priority` (schedulable, just outscored). Checked cheapest-first so the common
cases never pay for an extra solver call; the genuinely ambiguous ceiling-vs-clash case is
resolved by re-solving once with the ceiling temporarily lifted, specifically so a real
time-clash is never misreported as "just raise the ceiling." New endpoint
`POST /api/v1/schedules/{job_id}/explain-exclusion`, run off the event loop via
`run_in_executor` since CP-SAT solves are CPU-bound (same "never block the event loop"
rule `main.py`'s own docstring already states). The main wishlist-search job eagerly
explains only the first 15 excluded courses (a full explanation can cost a couple of
extra solver calls; unbounded eager computation over up to 60 wishlist items was
rejected explicitly), and the on-demand endpoint covers the rest.

**`app/models/schedule_schemas.py::ScheduleSearchRequest` extended, not replaced.**
`shortlist` is no longer required to be non-empty (a wishlist-only request is now valid);
new optional `wishlist`/`choice_groups`/`credit_min`/`credit_target`/`credit_max` fields
activate the CP-SAT path when `wishlist` is non-empty (a model validator requires
`credit_max` in that case). Every existing shortlist-mode test still passes unchanged -
confirmed by rerunning the full suite, not assumed from reading the diff.

**Frontend: reused the existing UI, added the missing pieces, did not redesign the
page.** Course Picker tab: the "Chosen" table's priority selector is now documented as
also being the wishlist intent; added a "Choice groups" builder card underneath it.
Profile tab: new "Credit policy" card (minimum/target credit inputs, an overload
toggle that only reveals the what-if ceiling and the approved-exception checkbox when
turned on, and a `POST /profiles/validate`-backed summary). Schedule Builder tab: a new
"Personalised wishlist schedule (recommended)" section sits above the existing exhaustive
shortlist search (now labelled "(legacy)") rather than replacing it - `generateWishlistSchedule()`
reuses `API.runScheduleSearch` completely unchanged, just with a different request shape,
since the backend accepts both shapes on the same endpoint. Results show included/excluded
courses, credit math (`fixed X + wishlist Y = total`), and an inline why-not reason per
excluded course (or an on-demand "why not?" button when the eager explanation wasn't
computed for that item). Saved-plan schema bumped `v5 -> v6`
(`frontend/src/plans.js`): persists `choiceGroups` and `creditPolicy`, with a safe-default
migration for every plan saved under an older schema.

**A second, unrelated accessibility violation found and fixed while re-running the
audit for this phase.** `#ttTerm` on the Scheduler & clashes tab had a `<label>` with no
`for` attribute - pre-existing, not introduced by this session's changes (nothing in this
phase touched that tab), caught only because the a11y audit was rerun end-to-end rather
than assumed still clean. Fixed with one `for="ttTerm"` attribute. Re-ran: 0 violations
across all 11 tabs, both before and after this fix was isolated as the only cause.

```bash
cd backend && python3 -m pytest -q                                     # 92 passed (was 40)
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 37 + 35 passed (was 31 + 33)
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                        # 62 passed (was 55)
./scripts/run-e2e.sh tests/a11y-audit.js                               # 0 violations
```

**Explicitly not done in this phase** (named here, not silently skipped - full detail
and reasoning in `docs/research/scheduler_v2_matrix.md`): a diversity layer returning
multiple ranked schedule families per generation (one optimized schedule is returned per
call); cross-schedule comparison (`/schedules/compare`); a resilient plan graph and
minimal-disruption bid-outcome repair (needs the diversity layer and the existing bid-
simulation subsystem wired together, neither done here); a full three-pane desktop layout
and a mobile Discover/Wishlist/Schedules/Compare bottom-nav IA (the existing 11-tab
architecture was extended in place instead, per the spec's own instruction to integrate
rather than build a disconnected page); Simple/Advanced preference modes beyond credit
policy and choice groups (no time-of-day/lunch/instructor/day-avoidance constraints are
modeled in the CP-SAT layer yet); a formal performance-benchmark report across 5/10/15/25-
course wishlists (informally, the one real wishlist solve exercised end-to-end in
`e2e.test.js` §21 produced zero main-thread long tasks, which is the one non-negotiable
performance property both research reports insist on - but no systematic sweep or memory
profile was run); transcript import; Demo Mode.

**A real, confirmed regression in the desktop `.exe`, found by actually rebuilding and
running it, not assumed away.** `backend/requirements.txt` now includes `ortools>=9.10`.
Added `--collect-all ortools` to `scripts/build-exe.sh` and rebuilt: the build itself
succeeds (94MB, up from ~59MB - ortools alone is a large native dependency on top of the
matplotlib/PIL/tkinter bloat §11 already flagged as unnecessary). The **web app path
works perfectly** through the built exe - `/health/ready`, pools, and the plain shortlist
search all verified via direct `curl` against the running exe. **The wishlist/CP-SAT
path does not**: submitting a wishlist search returns `202`, the job then transitions
straight to `failed` with `"worker process exited without producing a result"` - the
spawned worker process is dying at the OS level before `_worker_entry`'s own
try/except can even run (compare: the exact same spawn mechanism, without OR-Tools,
already works in this same exe for the plain shortlist path). This was not present
before OR-Tools was added, and is not present when running unfrozen (`python3 -m
pytest`, the real dev-server e2e suite, and direct `TestClient` calls all exercise the
identical `ScheduleJobManager` spawn path successfully) - it is specific to OR-Tools'
native extension inside a PyInstaller-frozen, doubly-spawned (frozen exe re-executing
itself as its own worker interpreter) process. The `warn-*.txt` PyInstaller log shows no
obviously missing OR-Tools submodule, which points toward a native-extension/DLL-search-
path or duplicate-onefile-extraction issue rather than a missing-import issue, but this
was not root-caused further this session - continuing to debug a native-packaging
interaction blind, one multi-minute rebuild at a time, was judged a worse use of the
remaining session than reporting the finding accurately. **Anyone shipping the desktop
exe next should either root-cause this before relying on the wishlist feature there, or
gate the CP-SAT path behind a runtime check that falls back to the existing shortlist
search with a clear message when running under a frozen build** - the web app (dev
server / Docker) path is unaffected and fully verified.

## 14. Session update — 2026-08-04 (yet later): revised timetable import, the desktop
## bug actually fixed, and personal-profile portability

A follow-up request came in asking for three things, in full: (1) import the just-
published revised Monsoon 2026 timetable from a real external website, with a
repeatable importer, provenance, diff, and validation; (2) actually fix the desktop
OR-Tools crash flagged as a known limitation in §13 (not just re-diagnose it); (3) make
personal data restorable on a fresh install without becoming another user's default.
The request itself was extremely large (28 numbered sections covering a full
diagnostics UI, an installer, Tauri/Electron evaluation, exhaustive test matrices for
every scenario, and more) - scoped down explicitly, the same way §13 was: the real,
verifiable core of each of the three problems was solved end to end; a full
diagnostics view, an installer, and exhaustive per-scenario test suites were not
attempted and are named as deferred, not silently dropped.

### Revised timetable import - real data, real extraction, no fabrication

**How the site actually stores its data (found by inspection, not assumed).**
`https://snioe-monsoon2026-tt.netlify.app/` is a single self-contained HTML file - one
594KB inline `<script>`, zero external JS bundles, zero `fetch()`/XHR calls (confirmed:
`(s.match(/fetch\(/g)||[]).length` is 0). The entire timetable lives in one literal
assignment, `const DATA = {...};`, deep in that script; `window.PLANNER` exposes it via
a getter (`get data(){ return DATA; }`) and a second variable, `IDX`, is *derived* from
`DATA` at runtime by an IIFE for the page's own UI - not independent source data, so
the importer never reads it.

**`tools/import_netlify_timetable.py`** - a real, repeatable, re-runnable importer:
1. Fetches the raw HTML (or reads a saved snapshot with `--offline-html`, for tests).
2. Isolates the `const DATA = {...};` literal by brace-matching over the raw text
  (string-escape aware), never executing any JavaScript. Before parsing, scans the
  isolated substring for executable-looking tokens (`function`, `=>`, `eval(`, `new
  Function`, `require(`, `import(`) and aborts if any are found - a legitimate data
  literal should never contain them.
3. Parses the isolated substring with `json.loads` - it is already JSON-compatible
  (quoted string keys, no function values), confirmed by successfully round-tripping
  it before writing any of the surrounding importer logic.
4. Deduplicates rows the site lists once per cohort year-group with an identical
  `rowid` (found: 579 of 1770 raw rows were exactly this - a real, benign multi-cohort
  listing pattern, not a data error).
5. Builds packages via the same cross-product-plus-internal-conflict-rejection
  convention the currently-bundled dataset already uses (verified by inspecting
  `frontend/src/data.json`'s own multi-section courses - e.g. CSD211/CSD2003's 8
  packages are exactly 2 LEC × 4 PRAC × 1 TUT minus zero conflicting combos - *before*
  writing the importer's package-builder, not after).
6. Carries forward credit values, ME/UWE/CCC/CORE/NB category, school/department
  display names, and title from the previously-bundled dataset by course code - the
  site itself explicitly does not publish credits ("Credits are not in the view ...
  Contact hours are shown instead") and the CSE-major-specific category logic is this
  project's own undocumented business rule, not something an external site can supply.
  Detects **renames** (e.g. `CCC2101` → `CCC826/CCC2101`, sharing a `/`-separated code
  component) so a renamed course still carries its known credits/category forward
  instead of being wrongly treated as an unresolvable new course - this was a real bug
  caught mid-session (see below).
7. Validates (missing/invalid fields, impossible times, duplicate codes, zero-package
  courses) and writes a full report, a diff against the previous dataset, and a
  checksummed manifest entry - never applies a dataset with validation errors.

**A real bug found and fixed while building this, the same way every module in this
project has had one.** First run reported 7 errors: `MISSING_FIELD ... missing
['title']` for `CCC407/CCC2200`, `MED416`, `ECO354/ECO4201`. Checked the raw data
directly rather than assuming the site was broken: for `MED416` and `ECO354/ECO4201`,
*other rows for the same course* carried the title fine - only one specific
TUT2/PRAC2 row was blank, a minor real upstream data-entry gap, not grounds to reject
the whole course. Fixed by making title resolution a fallback chain (any row with a
non-empty title → the previous dataset's title for that code → an explicit
needs-review placeholder) instead of a hard per-row requirement - 0 errors, 5 honest
warnings after the fix.

**A second real bug, found only after applying the import once.** The first `--apply`
run produced a clean 0-error import, but subsequent backend tests referencing
`CCC2101` (used throughout the test suite as the known-unavoidable-clash-with-CCC2116
example) failed with `unknown course code(s): CCC2101` - because it had been renamed to
`CCC826/CCC2101` in the revision. Verified empirically (not assumed) that the same
clash still holds under the new data - both courses' one package each still overlap Fri
675-765 - then updated the affected test fixtures to the new code. **A third bug
surfaced fixing the second**: the renamed course's `cr` field came back `None`,
crashing a real wishlist search (`TypeError: float() argument ... not 'NoneType'`) -
the rename-detection logic only ran in the diff step, not in the credit-carry-forward
step during normalization, so a renamed course's own credits were never actually
looked up. Fixed by using the same rename-aware lookup (`find_existing_match`) in both
places. **A fourth bug, in the importer's own re-run workflow**: after the first
`--apply` swapped the active dataset, a corrective second run's diff came back "0
renamed, +0 added, -0 removed, 0 changed" - because the importer was diffing
`frontend/src/data.json` against itself (it *is* the revision now, post-apply).
Fixed by always diffing/carrying-forward against the frozen `monsoon-2026-excel-v1`
baseline archived separately, never against whatever the active file happens to be.

**Also found, unrelated to the timetable revision itself, while auditing the data
before using it as the importer's carry-forward baseline**: `CSD355` had `cat: "DONE"`
hardcoded directly in the *institutional* catalog file - not a frontend state default
like §11/§12's fixes, but baked into `frontend/src/data.json`/`backend/app/data/courses.json`
themselves, meaning every fresh install of the app, for every student, showed CSD355
as permanently already-completed (forced-checked, disabled checkbox) regardless of
whether that student had actually taken it. This is the same anti-pattern §11/§12 fixed
in frontend state, now found in the shared data file those fixes never looked at. Fixed:
category restored to `ME` (its real `ttype`, Major Elective), and the `cat==='DONE'`
special-casing removed from `frontend/src/ui/d_sched.html`'s render logic and the
Course Picker's category filter dropdown.

**Final numbers, real diff** (`docs/TIMETABLE_REVISION_DIFF_2026-08-04.md`,
generated programmatically, not hand-written): 326 courses (unchanged count), 988
packages (was 859), 2 courses renamed, 1 added (`CCC396/CCC2315`), 1 removed
(`STM205`, no replacement), 205 changed (real section time/room/instructor moves), 118
unchanged. Applied as the active dataset against a real live fetch (not the cached
offline snapshot) - source checksum `ecc63f7071704010`, dataset checksum
`b1997ae63bb359e2`, retrieved 2026-08-04T13:29:16Z.

**Dataset versioning** (`backend/app/domain/catalog.py`, `backend/app/data/dataset_manifest.json`,
`backend/app/data/timetable_versions/`): `monsoon-2026-excel-v1` (the original baseline,
archived and frozen) and `monsoon-2026-netlify-revision-2026-08-04` (now active), each
with a checksummed manifest entry. New endpoint `GET /api/v1/dataset`. **The schedule-
search cache key was found to be a real staleness bug while wiring this in**:
`app/services/runner.py::input_hash` only stamped the *static* `DATASET_VERSION`
string constant from `rules.py`, which never changes when the catalog file itself is
swapped - meaning a live dataset swap without a server restart could have silently
served a schedule-search result cached against the old timetable. Fixed by stamping
the *live* catalog checksum (`catalog.dataset_info()["dataset_checksum"]`) into the
hash too.

**Frontend**: fetches `/api/v1/dataset` once at boot; `currentPlanPayload()` now
records `datasetVersion`; on boot and on switching plans, `revalidatePlanAgainstDataset()`
compares the saved plan's recorded version against the active one and shows a banner
(`#datasetBanner`, new element in the page header) naming exactly what's provably wrong
(a saved course code no longer in the catalog) versus what merely needs review
(everything else, since detecting a moved-but-still-valid package needs the *old*
catalog for comparison, which the browser doesn't have - that comparison already
happened once, server-side, and is recorded in the diff doc). Never silently mutates
the saved plan; a "Mark reviewed" button re-saves it stamped with the current version.

### The desktop OR-Tools crash - actually fixed, not just documented

§13 left this as a known limitation with a recommendation to root-cause or add a
runtime fallback. This session did both, and the crash is now real evidence, not a
guess: see `docs/DESKTOP_PACKAGING.md` for the full writeup (a `--selftest-worker`
diagnostic flag added to `desktop_launcher.py`, a genuine SIGSEGV reproduced inside
`cp_model.CpSolver().Solve()` - not multiprocessing, not a missing DLL, both ruled out
with direct evidence - and a pure-Python `_solve_greedy_fallback()` in
`cp_scheduler.py` that the frozen build now uses instead, since **a segfault cannot be
caught by `try`/`except`, so the only correct fix is to never make the call at all when
frozen**). Verified against the actual rebuilt exe: a real wishlist search completes
correctly (`cp_status: "heuristic_fallback"`, correct must-have retention, correct
exclusion with a real reason), cancellation works, a fresh search after a cancelled one
returns correct fresh results, not stale ones. 7 new regression tests in
`test_cp_scheduler.py` (monkeypatched `RUNNING_FROZEN` + a `CpModel` replacement that
raises if ever constructed, proving the fallback path never touches CP-SAT).

Also measured and decided, with real numbers, not intuition: `--onedir` replaces
`--onefile` as the shipped desktop architecture (`scripts/build-exe.sh`) after
measuring a **~9x cold-start difference** (1.37s vs 12.7s) on this machine, because
onefile re-extracts its whole ~90MB archive to a fresh temp directory on every launch.
Onedir's larger on-disk footprint (215MB vs 90MB) was judged worth it for a desktop app
launched repeatedly - full reasoning and the alternatives considered (separate
launcher+backend+worker executables, a managed Python runtime, Tauri/Electron) are in
`docs/DESKTOP_PACKAGING.md`.

### Personal profile portability

The private bootstrap file (`user_profile.local.json`) is deliberately the *same
shape* `PLANS.exportJson()` already produces in the browser, plus a `datasetVersion`
field - so the browser's existing Import button already accepts it with zero code
changes. `tools/import_personal_profile.py` validates one before it's handed to a
student or staged (`--stage` copies it to `%LOCALAPPDATA%\SNU Scheduler\` on Windows /
`~/.config/SNU Scheduler/` elsewhere); `backend/desktop_launcher.py` gained the same
validation behind an `--import-profile <path>` CLI flag for the desktop build, staging
to the same per-user app-data directory (never beside the exe in Program Files).
Example schema with fake data only: `docs/examples/user_profile.example.json`.
`.gitignore` updated so a real `user_profile.local.json` is never committed.

**Frontend**: the existing Import flow (Profile tab) now shows a real preview - fixed/
wishlist/choice-group/credit-policy counts and the dataset version it was built against
- before committing anything, with an explicit "download a backup of my current plan
first" prompt when there's existing work in the browser (spec: "do not silently
overwrite current work"). Import always creates a *new* plan; it never overwrites the
active one.

**Honest limitation, stated plainly**: this session's environment is a fresh headless
sandbox, not the user's actual browser - there is no discoverable prior localStorage,
IndexedDB, or previous-install data *in this environment* to migrate from. The real
mechanism (export/import, the bootstrap file, the preview-before-import flow, the
per-user app-data staging for desktop) is built, tested, and documented; on the user's
own machine, where their actual browser profile lives, "Profile tab → Export JSON" on
the old install and "Import…" on the new one is the path to use it.

**Explicitly not done this session** (named, not hidden): a full standalone "Load My
Existing Work" wizard page with item-level selective merge and a side-by-side old-vs-
new comparison (the existing Profile tab's Plans card was extended with a real preview
step instead, per the same "integrate into the current architecture" principle §13
already established); a desktop diagnostics view with every field the request listed
(app version, worker health, log viewer, etc.) - the `--selftest-worker` flag and the
existing structured JSON logging cover the diagnostic need this session actually had;
an installer (MSI/Inno Setup) - the zipped one-folder distribution is the deliverable;
Tauri/Electron evaluation - rejected outright as solving the wrong layer (see
`docs/DESKTOP_PACKAGING.md`); exhaustive automated test coverage for every one of the
28 requested importer/migration/desktop scenarios - a real, substantial subset was
tested (see the counts below), not all of them; a formal performance-benchmark sweep
across 5/10/15/25-wishlist-course sizes - not run this session, on top of §13's same
already-stated gap.

```bash
cd backend && python3 -m pytest -q                                     # 100 passed (was 92)
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 38 + 35 passed (was 37 + 35)
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                        # 62 passed (unchanged count, one fixture updated for the new timetable)
./scripts/run-e2e.sh tests/a11y-audit.js                               # 0 violations
python3 tools/import_netlify_timetable.py                              # dry-run diff still reproducible
./scripts/build-exe.sh                                                 # --onedir, rebuilt, verified via real HTTP calls against the packaged exe
```


---

## 15. Session update — 2026-08-04 (latest): a real timetable poller, three-hash
## change detection, a checksum bug that would have broken it silently, and the
## update review/apply/rollback workflow

A follow-up request asked for a backend-owned timetable poller (not a browser
`setInterval()`), a full check/stage/validate/review/apply/rollback workflow, plan
revalidation and repair, and a large secondary list of deferred scheduler features
(diversity layer, comparison, post-bid repair, mobile audit) and application-wide
optimization work (dependency splitting, PyInstaller `.spec` file, formal benchmark
suite, source-ZIP hygiene). Per this project's established pattern for oversized
requests, this session built the centerpiece (the poller and the full update workflow)
completely and for real, and is explicit below about what from the long secondary list
was not attempted.

**Baseline verification found a real stale-process contamination before any new code
was written**: `SNU-Bid-Scheduler.exe` from the previous session's desktop testing was
still bound to port 8000. Killed before establishing any baseline numbers, per this
project's own repeatedly-learned lesson about stale processes silently serving old code.
See `docs/BASELINE_VERIFICATION_2026-08-04.md` for the full table.

### One canonical implementation, not two

`tools/import_netlify_timetable.py`'s fetch/parse/normalize/validate/diff logic was
refactored into `backend/app/timetable_updates/{source,parser,normalize,validate,diff,
apply,poller,models}.py`. The CLI script is now a thin wrapper calling these exact
modules; the backend's own poller calls the same modules. Verified the refactor changed
nothing observable: re-ran the CLI against the live site before and after and got
identical output (326 courses, 988 packages, 0 errors, 5 warnings, same diff).

### The three-hash design, and a real bug that would have silently broken it

Per the request: a **source hash** (raw HTML - detects any website change at all), an
**extracted-data hash** (the isolated `const DATA = {...}` literal - detects a change in
the timetable payload specifically), and a **normalized-dataset hash** (the canonical
course/package dataset - the only one that should ever create a new version).

**Found while wiring this in, not while writing new code**: `app/domain/catalog.py`'s
live checksum hashed the *raw file bytes on disk* (whatever indentation happened to be
there), while the newly-written `normalize.py` computed its checksum from a canonically
re-serialized form (`sort_keys=True`, compact separators) - a *different* convention for
what was supposed to be the same concept. Reproduced directly: fetching the live,
completely unchanged timetable and renormalizing it produced 326/326 byte-identical
courses and a byte-identical full-list JSON dump, yet the two "checksums" disagreed.
**This would have made every single poll, forever, report a false "update available"
regardless of whether the timetable had actually changed** - the exact failure mode
section 4 of the request explicitly calls out as unacceptable ("Do not create a new
dataset version" on cosmetic changes). Fixed with one function,
`catalog.canonical_checksum()`, that both modules now call exclusively; existing
manifest entries were regenerated to the new convention. Regression test:
`test_normalize_identical_content_produces_identical_hash_regardless_of_key_order`.

### The poller (`backend/app/timetable_updates/poller.py`)

One `UpdateService` instance, started once in `app.main`'s lifespan as an `asyncio.Task`
- not a browser `setInterval()`. Config via `SNU_TIMETABLE_UPDATE_ENABLED`,
`SNU_TIMETABLE_UPDATE_INTERVAL_MINUTES` (floor of 5 minutes, default 15),
`SNU_TIMETABLE_UPDATE_URL`, `SNU_TIMETABLE_AUTO_APPLY` (default `false` - review-before-
apply is the default policy). An `asyncio.Lock` makes concurrent checks return
`{"skipped": true}` rather than racing; small random jitter on both the initial delay
and each subsequent sleep interval; exponential backoff (30s → doubling, capped at 1hr)
on network failure, reset to zero on the next successful contact. Confirmed the live
site (`snioe-monsoon2026-tt.netlify.app`) actually serves an `ETag` header (`curl -I`),
so `If-None-Match` is real, not aspirational.

The state machine (`UpdateState` in `models.py`) has exactly the 13 states the request
specified. `_check_blocking()` (run off the event loop via `run_in_executor`, since
fetch+parse+normalize is blocking I/O and CPU work) walks the three hashes in order and
returns the earliest state that applies: `not_modified` → `source_changed_only` →
`no_dataset_change` → `update_available`/`failed`, never skipping ahead.

### Transactional apply + rollback (`apply.py`)

`apply_version(version_id, expected_checksum)` re-validates the staged candidate,
snapshots the currently-active files in memory, writes new files via `os.replace`
(atomic per-file on both Windows and POSIX), reloads the catalog
(`catalog.reload()` - new function), and runs a post-apply health check comparing the
reloaded checksum against what was just written - reverting everything if they don't
match. Documented honestly in the module's own docstring: each individual file replace
is atomic; the three-file sequence (frontend copy → backend copy → manifest) is not
wrapped in an actual filesystem transaction, since no such primitive exists without a
database - the residual risk window is the gap between the first and last `os.replace`.
Rollback reuses the exact same function: every version, applied or not, keeps its own
files under `timetable_versions/<id>/`, so "roll back to X" is just "apply_version(X)"
with no reconstruction needed. `discard_candidate()` refuses to remove anything that has
ever been applied (rollback is the only path away from an applied version).

### API endpoints

`GET status`, `POST check`, `GET candidate`, `GET diff`, `POST apply`, `POST discard`,
`POST rollback`, `GET history` under `/api/v1/timetable-updates/`. `apply` requires both
`candidate_version` and `candidate_checksum` and rejects a stale candidate (spec:
"reject application if the candidate changed between review and apply"). No `/events`
SSE endpoint was added this session (status polling was judged sufficient given the
check itself typically completes in under two seconds against the real site) - a real,
named scope cut, not an oversight.

### Frontend

A header bar (`#ttUpdateBar`) shows the current state in the plain-language wording the
spec asked for ("Checked just now. Your timetable is current." / "A revised timetable is
available." / "The timetable source is temporarily unavailable. Your existing data
remains safe.") plus a "Check timetable now" button. When a candidate is staged, a
review panel appears with the diff summary (renamed/added/removed/changed/unchanged
counts), Apply/Discard/Download-diff actions, and a confirmation dialog before applying.
Deliberately does **not** run its own poll loop - it reads the backend's own poller
state once at boot and after any action the student takes, which is what actually
satisfies "polling must not duplicate across tabs or hammer the source site" (a frontend
`setInterval()` would violate this even if individually well-intentioned).

### Tests

35 new tests in `backend/tests/test_timetable_updates.py` using a real local
`http.server` fixture (no live-Netlify dependency in the normal suite) - conditional
`304`/ETag behavior, oversized-response rejection, unreachable-host handling, forbidden-
token rejection in the parser, the exact checksum-convention bug as a permanent
regression test, rename detection, transactional apply success/stale-checksum-rejection/
validation-failure-rejection, discard, rollback, and the poller's own async lifecycle
(concurrent-check dedup, backoff increase/reset, auto-apply on/off, status shape). One
opt-in live test (`SNU_LIVE_TIMETABLE_TEST=1`), skipped by default.

```bash
cd backend && python3 -m pytest -q                                     # 135 passed, 1 skipped (was 100)
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 44 + 35 passed (was 38 + 35)
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                        # 62 passed
./scripts/run-e2e.sh tests/a11y-audit.js                               # 0 violations
```

### A second real bug, found only by actually running the packaged exe

The desktop build only ever bundled `app/data/courses.json` via PyInstaller's
`--add-data` - never `dataset_manifest.json` or `timetable_versions/`. The first
rebuild with the new timetable-update service started and ran correctly, but
`GET /api/v1/timetable-updates/status` reported `active_version: "unknown"` despite a
correct checksum - the frozen app genuinely could not identify its own active dataset
version, because the manifest file it needed to look that up simply wasn't there.
Compounding this, `app/timetable_updates/apply.py`'s path constants had been computed
independently from `catalog.__file__` rather than from `catalog`'s own already-
override-aware `_DATA_PATH`/`_MANIFEST_PATH` - the same category of "two
implementations of the same concept that quietly disagree" bug as the checksum-
convention issue above, just one layer down. Fixed both: `apply.py` now derives its
paths from `catalog`'s resolved paths; `desktop_launcher.py` sets
`SNU_DATASET_MANIFEST_PATH` alongside the existing `SNU_CATALOG_PATH`; `build-exe.sh`
now bundles `dataset_manifest.json` and `timetable_versions/`. Rebuilt and reverified
against the actual packaged exe: cold start 1855ms, `active_version` now correctly
resolves, a manual check against the real live site returns `not_modified` correctly,
and a real wishlist search still completes via the heuristic fallback exactly as
before - the fix touched nothing on the already-working wishlist path.

### Explicitly not done this session (named, not hidden)

The request's secondary list (sections 19-29) was large enough to be its own multi-week
phase on its own:

- **Schedule diversity, comparison, post-bid repair** (19A-C): the wishlist solver still
  returns one optimized schedule per generation, not multiple ranked families; no
  `/schedules/compare` endpoint; no bid-outcome repair engine. Unchanged from §14's own
  stated limitation.
- **Mobile/responsive audit** (19E): not performed this session; the existing 11-tab
  desktop layout is unchanged.
- **Full security/concurrency/state audit** (§20): not performed as a dedicated pass;
  the one real bug found this session (the checksum convention) was found through normal
  implementation and baseline verification, not a separate audit exercise.
- **Formal benchmark suite, dependency-group splitting, PyInstaller `.spec` file, OR-
  Tools desktop-exclusion experiment, frontend asset minification, clean source-ZIP
  packaging** (§21-28): none attempted. The desktop build was rebuilt and reverified
  after this session's backend changes (pure Python, no new native dependencies), but no
  new size/dependency optimization work was done beyond what §14 already measured.
- **Plan-impact/repair UI** (§14 of the request): the existing `revalidatePlanAgainstDataset()`
  from the prior session runs again after an apply, but no dedicated "this revision
  affects N courses in your current work, here's the automatic repair" screen was built
  - the review panel shows the *dataset-level* diff, not a *personal-plan-level* impact
  analysis. This is the single biggest real gap between what was asked and what was
  delivered, and is named here explicitly rather than implied to be covered by the
  review panel.

---

## 16. Session update — 2026-08-05: a second AI session's programme-catalogue work merged
## in, the exe crash it introduced, a real e2e regression, and dependency-file hygiene

A separate coding-agent session (a different tool, working from a transcript the user
pasted in) had independently added a full official-programme catalogue (44 entries from
https://snu.edu.in/programs/, each with source-linked requirement tables where the
university publishes one) and a `POST /api/v1/degree-audit` engine, then reported the
work as complete and verified. The user then reported the packaged desktop app was
showing a raw `{"detail":"Not Found"}` JSON response instead of the UI, and separately
that personal data was "still" present.

**This was not taken on faith.** Every claim was independently re-verified against the
actual code and a real rebuilt exe, per this project's own established culture.

**The programme-audit work itself checks out.** Read `backend/app/services/degree_audit.py`,
`backend/app/models/audit_schemas.py`, `backend/app/data/programs.json` (44 entries,
101KB), and `backend/tests/test_degree_audit.py` in full. The CSE B.Tech. requirement
table's numbers reconcile exactly against this project's own already-verified domain
knowledge (160 total credits; 18 CCC + 18 UWE individual minimums + 42 combined minimum,
which is deliberately *more* than 18+18 - the extra 6 credits must come from beyond each
individual floor, and the full requirement set sums to exactly 160 when accounting for
that overlap correctly). Ran a real degree audit against the live dev server and
separately against the rebuilt exe: 9/9 requirements complete, zero remaining credits,
matching the CSE test fixture exactly. The 11 programmes without a complete public
curriculum are honestly labelled `source_linked_partial` rather than guessed, and accept
a private-profile override (`auditRequirements`, schema v7) instead.

**The exe crash - root-caused and fixed, not just re-diagnosed.** Ran the exe directly
and captured a real traceback (a `--windowed` build has no visible console, but running
`desktop_launcher.py` unfrozen with output redirected to a file reproduces the exact
same import path): `FileNotFoundError` on `_internal\app\data\programs.json`.
`scripts/build-exe.sh`'s `--add-data` list was never updated when the programme catalogue
was added - `courses.json`/`dataset_manifest.json`/`timetable_versions/` all got the
onedir bundling fix from §15, but `programs.json` was added afterward and missed it.
This is the same "a new data dependency didn't get the same packaging treatment as the
existing ones" category of bug as §15's own manifest-bundling fix. **Whether this exact
crash is what the user's screenshot showed could not be fully confirmed** (that
screenshot's exe predates this fix and may have been stale for other reasons - see
below), but it is a real, 100%-reproducible crash that had to be fixed regardless.
Fixed by adding `--add-data "app/data/programs.json;app/data"`; rebuilt; reverified
directly against the actual packaged exe: root route serves the frontend correctly
(`HTTP 200 text/html`), `/api/v1/programmes` returns all 44 entries, a real degree audit
returns 9/9 complete, the timetable poller's own startup check correctly found and
staged a real upstream change (2 courses changed since the dataset was last applied,
0 validation errors), and a wishlist search still completes via the heuristic fallback
exactly as before.

**A second, real regression found only by rerunning the full e2e suite, not assumed
clean.** `frontend/tests/e2e.test.js`'s §20 test ("Specialisation: done electives are
local, not shipped") started failing consistently (2/2 reruns, not the session's known
transient long-task flake). Root cause: the other session's own edit to
`h_spec.html::drawSpec()` added an early return when no programme is selected (a
reasonable change - the CSE-specific specialisation buckets only make sense for that
one programme now that programmes are selectable) - but that early return sat *before*
the `renderDoneME()` call, which manages the general-purpose "already-completed major
electives" list that has nothing to do with which programme is selected. On a genuinely
fresh session (no programme selected, correctly, per generalization), the completed-
electives list simply never rendered its empty state. Fixed by moving `renderDoneME()`
to always run before the programme gate. Full suite reverified: 62/62 e2e passing again.

**Personal-data check: the screenshot's specific claim did not reproduce, but a real
false-positive was chased down and confirmed benign.** Loaded the app in a real browser
and found `remME=9`, `capCr=28` in the live page - which looked, at first glance,
exactly like the personal-data regression the user was worried about. Checked the actual
`b_body.html` source directly: the shipped defaults are correctly neutral (`remME
value="0"`, `capCr value="25"`). The `9`/`28` values were traced to this same browser
tab's own `localStorage`, populated by this session's *own* earlier manual testing
(setting the guide's worked-example numbers - 9/4/11/6/105/9/16/CSD21 - to verify the
pool-formula endpoint, back in an earlier phase of this same conversation). Cleared
localStorage and reconfirmed a genuinely fresh session shows every field neutral, no
programme pre-selected, and empty `FIXED`/`PICK`/`DONE_ME`. The Spring 2027/CSD317
personal narrative the user's screenshot context referenced was confirmed fully absent
from `g_two.html` (now rebuilt entirely as the programme-audit UI) and not found
anywhere else in the frontend source via a full-repository grep.

**One known, unchanged, previously-documented boundary, re-confirmed still present.**
`h_spec.html`'s core-swap block-optimizer is still hardcoded to check for
`CSD211/CSD2003` + `MED2001` specifically (§11's own documented limitation - a real
product feature built around one specific student's CoA situation, not a leak in the
same sense as the numbers above). Not touched this session; generalizing it to work for
an arbitrary programme's core-swap pairs is a real feature addition, not a bug fix, and
remains explicitly out of scope until asked for directly.

**Dependency hygiene: real bloat found and partially addressed.** `backend/requirements.txt`
mixed runtime and test-only packages (`pytest`, `pytest-asyncio`, `hypothesis`, `httpx`)
in one file with only a comment marking the boundary - confirmed via the PyInstaller
build log actually processing hooks for `pytest`, `py`, `parso`, and `pyzmq` during
analysis, none of which `desktop_launcher.py` ever imports. Split into `requirements.txt`
(runtime only: fastapi/uvicorn/pydantic/numpy/ortools) and `requirements-dev.txt` (adds
the test tooling, via `-r requirements.txt`). **Honestly scoped**: this machine's shared
Python environment already has pytest et al. installed globally from normal development
use, so this split does not shrink *this* build - PyInstaller's static analysis walks
whatever is importable in the active environment, not just requirements.txt. It does
mean a genuinely fresh clone + `pip install -r requirements.txt` + `build-exe.sh` (a
clean venv, never actually exercised this session) would no longer sweep test tooling
into the frozen app. Actually rebuilding from a dedicated clean venv to get a real
before/after size number was not attempted - named here as a real, safe, still-open
follow-up rather than claimed done.

```bash
cd backend && python3 -m pytest -q                                     # 139 passed, 1 skipped
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 44 + 35 passed
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                        # 62 passed (was 61/1 fail before the h_spec.html fix)
./scripts/run-e2e.sh tests/a11y-audit.js                               # 0 violations
./scripts/build-exe.sh                                                 # rebuilt twice more (programs.json fix, then h_spec.html fix); final exe verified directly: root route, /api/v1/programmes, /api/v1/degree-audit, /api/v1/timetable-updates/status, and a wishlist search all confirmed working against the actual packaged artifact
```

---

## 17. Session update — 2026-08-06: three revised official bidding documents + a Dean's
## Office rectification email, applied end to end, and a real formula bug they exposed

The user forwarded an email from Dean Academics (2026-08-05) stating two explicit
rectifications to a document sent earlier, plus three attached official PDFs read in
full: `Course_Bid_Point_Allocation_Concept_Note_revised_final.pdf`,
`Course_Bidding_Introduction.pdf`, and `Course_Enrolment_FAQ_1.pdf` (the last one
re-read directly from disk mid-session to verify an exact quote before committing to a
high-impact rule change — see below). Every rule in `rules.py` was cross-checked
against these documents; conflicting old rules were overridden per the user's explicit
instruction, and the two rectifications were treated as ground truth.

**Rectification 1 — Y2 completed-credit deduction: 10 points → 5 points per credit.**
Applying just the number swap in `compute_pools()`'s existing formula did **not**
reproduce the revised Concept Note's own Semester-3 worked example (UWE 30 + 40 - 15 =
55). The existing formula multiplied the release percentage by *remaining* credits,
summed across semesters using today's remaining value retroactively for every past
semester too; the document's actual mechanism multiplies the release percentage by the
category's *total* requirement (remaining + already-completed), accumulated normally
across semesters, then subtracts a flat 5-points-per-completed-credit deduction
afterward. These are genuinely different formulas whenever any credit has already been
completed — reproduced with the concrete numbers before writing the fix: the old
remaining-based approach gives 29.5 for that example, not 55. Fixed in
`backend/app/domain/pools.py`'s `y2` branch. The zero-completed-credit baseline (90/70/
40) this produces independently matches `Course_Enrolment_FAQ_1.pdf`'s own stated
"typical first-cycle average" for 2nd years — corroboration from a second, separately-
authored document, found by actually re-reading that FAQ in full rather than trusting
the prior session's own summary of it (see below).

The frontend's client-side `recalc()` fallback in `c_core.html` had the identical
formula bug (the "10 points per remaining credit" version) — this matters because it's
what the page shows before `refreshPoolsFromBackend()`'s real API call resolves, and
what it falls back to if the backend is genuinely unreachable. Fixed to match. Also
found: **no UI ever exposed a "credits already completed" input at all** — the backend
API (`app/models/schemas.py::PoolRequest`) and `api.js::calculateProfileBudget()` both
already had `done_me`/`done_uwe`/`done_ccc` parameters wired end-to-end, but nothing on
the Profile tab ever set them, so the deduction was completely inert in the shipped
app regardless of the formula's correctness. Added three new inputs
("ME/UWE/CCC credits already completed") to `b_body.html`, wired into
`refreshPoolsFromBackend()`, `currentPlanPayload()`, and `restoreActivePlan()` in
`glue.js`, and a new plans.js schema v10 migration (defaults to 0, so no existing saved
plan's computed pool changes).

**Rectification 2 — no minimum or maximum bid at all.** The prior `AUC.MAX_BID` rule
("a bid may not exceed 25 x course credits") is explicitly abolished by all three
documents plus the email. This cascaded further than the rule text: `pools.py::max_bid()`
now returns `None` (kept only for API-shape stability); `runner.py`'s `run_plan()` used
to set each course's simulation/optimizer cap to `max_bid(credits)` — now set to
`req["pools"][category]`, the student's own remaining pool for that course's category,
since that pool is now the *only* real ceiling (categories never subsidise each other,
`POOL.SEPARATE`). Verified end-to-end through the real API, not just unit-level: the
e2e suite's cap assertion now reports caps of `297`/`297` (the ME pool) for a two-ME-
course test plan, not the old `75`/`45` (25 x 3 / 25 x 1.8-ish credits). Frontend "Max
bid" columns (`b_body.html`, `d_sched.html`) recomputed `Math.floor(25*c.cr)` locally —
replaced with the student's actual live category pool (`BUD[cat]`), and the stale
"25 × the course's credits" line in the Rules tab (`g_two.html`) and Budget-flags copy
(`c_core.html`) rewritten.

**`BUDGET.SHARED_LIVE` resolved from unknown to officially confirmed — but verified by
re-reading the source PDF directly before committing to it**, since flipping this rule
changes the optimizer's actual behavior (§5.3 in this file explicitly calls this "large
impact"). The prior session's own compacted summary claimed the FAQ resolved this;
rather than trust that secondhand, `Course_Enrolment_FAQ_1.pdf` was re-read in full from
its original path. It does: *"Each category ... has its own balance. Points you place
on a bid are held against that balance and released if the bid is unsuccessful back to
the same category. You cannot commit more than you have."* Updated the rule's status,
`runner.py`'s `why_it_matters` copy, and every "not officially confirmed" /
"unresolved rule" string across `glue.js` and `plans.js` that presented SHARED_LIVE as
one of two equally-plausible readings — INDEPENDENT is now explicitly labelled a
hypothetical comparison only, the same treatment the "Optimistic" competition scenario
already gets. Caught one of these strings only because `plans.test.js`'s own assertion
name ("flags the unconfirmed budget rule") no longer matched reality once the rule
copy changed — fixed both the source string and the test's assertion together.

**Other genuinely new provisions from the same documents, added as rules (most
informational, since they don't require simulation code changes):** one swap per round
and whole-batch-only swaps (`ROUND.SWAP_NO_POINTS`, extended); Add/Drop-round drops get
no clearing-price refund, versus a full refund in the dedicated Major Elective Drop
round (new `ROUND.DROP_REFUND`); waitlist rounds settle by bid, not by who clicks first
(new `ROUND.WAITLIST_BY_BID`); tie-break numbers are assigned per course per round, not
once per add (updated `AUC.TIEBREAK`); failed-core retakes go through a Dean's Office
Google Form, with the email's own stated deadline of **7 August 2026** flagged as
imminent relative to this session's date (new `RETAKE.FAILED_CORE`); grade-improvement
retakes use a separate Add/Drop-period form, with an automatic attendance waiver for
clean retakes (new `RETAKE.GRADE_IMPROVEMENT`, `RETAKE.ATTENDANCE_WAIVER`); no extra bid
points for retakes (new `RETAKE.NO_EXTRA_POINTS`); SWAYAM/NPTEL courses skip bidding
entirely (new `ENROL.SWAYAM_NO_BID`); backend enrolment is discontinued except named
exceptions (new `ENROL.BACKEND_DISCONTINUED`); a future Spring-2027 feedback-form bonus
(new `POOL.FEEDBACK_BONUS_FUTURE`, deliberately not modelled yet). Citations for
`SET.ONE_CLASH`, `SET.NEVER_CLASH_CORE`, `SET.NET_CEILING`, and `ROUND.SEQUENCE` were
updated to point at the two new named PDFs instead of the superseded "Introduction"/
"guide" documents; `POOL.Y4_AVERAGE_ROW`'s disputed note now records that the FAQ
independently repeats the same unreconciled 297/125/238.5 figures in a second document,
strengthening rather than resolving the dispute.

**An unrelated, pre-existing environment gap found while running the real e2e suite**
(not caused by this session's changes): `scripts/run-e2e.sh` invokes `python3`, which on
this machine resolves to a different Python installation (Windows Store Python 3.11)
than the `python` this session had been using directly — one that was missing `pypdf`
and `cryptography`, dependencies a prior session had already added to
`requirements.txt` for the advisement-report PDF-parsing feature but never installed
into that specific environment. The backend crashed on import
(`ModuleNotFoundError: No module named 'pypdf'`), which surfaced as `stack up: api=DOWN`
with no further detail — diagnosed by running `python -m uvicorn` directly to see the
real traceback rather than guessing from the summary line. Fixed by installing both
packages into that environment; not a code change, but the exact "same category of bug
as the Docker port collision and the stale-process port issues" this file has flagged
twice before — an environment/dependency mismatch masquerading as a test failure.

Also reviewed, per the user's own instruction to check "changes made since last time":
`CODEX.md` (a new project-root file, evidence/verification guardrails, complementary to
this file — not authored this session, left as-is), and confirmed
`backend/app/services/credit_policy.py` has no reference to `AUC.MAX_BID` or the Y2
pool formula, so nothing there conflicted with this session's rule changes.

```bash
cd backend && python3 -m pytest -q                                     # 154 passed, 1 skipped (was 139)
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 44 + 35 passed
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                        # 64 passed (was 62)
./scripts/run-e2e.sh tests/a11y-audit.js                               # 0 violations
./scripts/build-exe.sh                                                 # rebuilt; launched the actual packaged exe and verified directly: /health/ready, POST /api/v1/pools (y2, rectified -> 90/55/40 exactly), GET /api/v1/max-bid (null, no cap), a real simulation (cap:90 = the ME pool, not the old 25x3=75), and the root route serving the frontend
```

**Explicitly not done this session**: a full re-audit of every Q&A item in Part 2 of the
FAQ (situation-specific student questions, mostly pointing back to Part 1 or to "contact
the office") was not turned into individual rule entries, since almost all of them
resolve to a rule already added or to genuine "contact the department" guidance with no
computable content; the exact reconciliation of `POOL.Y4_AVERAGE_ROW`'s disputed figures
is still unresolved by the University's own materials, not just by this project.

---

## 18. Session update — 2026-08-06 (later): tab consolidation, and a real
## CS-programme bias audit

Two requests in one message: reduce the 11-tab navigation to something that flows more
naturally, and audit the whole app for assuming every student is in CSE even though all
44 programmes' official requirement tables are already loaded.

**Tab consolidation, 11 -> 6.** `Course picker` + `Scheduler & clashes` + `Schedule
builder` merged into one **Courses & schedule** tab (fixed courses -> browse/pick ->
chosen + choice groups -> weekly timetable -> schedule generation, with the legacy
exhaustive search collapsed behind a `<details>` since the personalised generator above
it now covers the common case). `Bid simulator` + `Stress-test plan` merged into one
**Bid simulator** tab; `stressPlan()` now auto-runs at the end of every `runOpt()` instead
of on a tab-switch that no longer exists, since both cards now share a page. `Degree
audit & plan` + `Specialisation` merged, with the CSE-only specialisation tracker behind
a `<details>` labelled "(CSE B.Tech. only)". `Rules & provenance` + `Data & open
questions` merged, with the data/credit-provenance appendix behind a `<details>`.
`frontend/src/glue.js::useWishlistSchedule()` and `frontend/src/ui/i_build.html::useSchedule()`
both used to `.click()` a `[data-p="sched"]` tab button that no longer exists; fixed to
scroll to the (now same-pane) timetable and redraw it instead.

**The CS-bias audit found real bugs, not just tone.** Dispatched a research-only
subagent first rather than guessing:
- The "CSE block (optional; auto-loads core)" selector on the Profile tab
  (`frontend/src/ui/b_body.html`) rendered unconditionally for every programme -
  `frontend/src/ui/d_sched.html`'s `BLOCKS` array is filtered to `/^CSD/` blocks only, so
  a non-CSE student saw a dead, unexplained control. Now wrapped in `#blkWrap`, toggled
  by `applyProgrammeCategories()` to show only when the selected programme is CSE.
- "Count CSD336 RL as AI bucket?" sat in the Specialisation card's static header,
  *outside* the `#specBox` div that `drawSpec()` already correctly gates to CSE only -
  every other programme still saw and could interact with a CSE-specific control despite
  the gating note right below it. Moved inside a `#c336Wrap` toggled by the same
  CSE check in `drawSpec()` (`frontend/src/ui/h_spec.html`).
- `SPEC.REQUIREMENT`, `SPEC.BUCKET_TENTATIVE`, `SPEC.CSD336_AMBIGUOUS` in
  `backend/app/domain/rules.py` are CSE-prospectus-specific rules that showed up on the
  Rules tab for every student in every programme, with no way to tell they didn't apply.
  Added a `programme_scope` field to `Rule` (`None` = universal, the correct default for
  every other rule here), scoped those three to CSE, and `drawRules()` now hides
  out-of-scope rules by default with a one-line count and a "also show other programmes'
  rules" checkbox to reveal them - never silently dropped, always disclosed.
- `applyProgrammeCategories()` (`frontend/src/ui/g_two.html`) silently fell back to
  labelling every course `NB`/`UWE` for the 3 programmes with empty
  `department_keywords` in `programs.json` (`mba`, `accelerated-masters-program-with-asu`,
  `ba-research-interdisciplinary-humanities-and-social-sciences-ihs`) - meaning those
  students' own major courses were mislabelled "not my business" with no indication
  anything was wrong. CCC/UWE classification is unaffected (those come from the
  timetable's own flags, not department keywords); added a `#categoryCoverageNote` flag
  that names the programme and states plainly that ME/Core isn't distinguishable for it
  yet, rather than presenting a guess as fact.
- Confirmed clean by the same audit: `backend/app/services/degree_audit.py` is fully
  programme-agnostic already (reads each programme's own `requirements` list, supports
  `kind: "milestone"` for doctoral rows); `h_spec.html`'s bucket *display* logic was
  already correctly gated, it was specifically the header controls escaping that gate.

**Not done this pass, named rather than silently skipped**: doctoral/MBA/PhD students
still see the full bid-point budget and Bid simulator tabs even though
`ENROL.BACKEND_DISCONTINUED` states graduate enrolment mostly bypasses bidding - gating
those tabs by programme `level` is a real follow-up, not attempted here since it changes
what a whole tab is *for*, not just what one control shows; a full per-programme
department-keyword mapping for the 3 empty-keyword programmes (the actual fix, versus
this session's honest disclosure) needs more source data than is currently available.

```bash
cd backend && python3 -m pytest -q                                     # 161 passed, 1 skipped
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 44 + 35 passed
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                        # 64 passed
./scripts/run-e2e.sh tests/a11y-audit.js                               # 0 violations, across the new 6-tab structure with every <details> opened before scanning
```

---

## 19. Session update — 2026-08-06: programme-wide pathways and specialisations

Replaced the hardcoded CSE-only `SPEC` object with a source-linked pathway catalogue
(`backend/app/data/pathways.json`) containing exactly one record for every one of the
44 programme IDs in `programs.json`. `ProgrammeCatalog` merges the two files and fails
closed on missing or extra IDs; the packaged desktop build now includes the new file.

The UI distinguishes formal specialisations, B.Des. streams, ASU degree routes,
doctoral research areas, and programmes with no separately published specialisation.
It calculates completed + selected credit only when SNU publishes a course mapping;
otherwise it shows the official option and source without inventing a number. The
former CSE “Systems and Networks” bucket was removed: the current CSE prospectus names
exactly AI/ML, Data Science/Big Data Analytics, and Cyber Security/Privacy. Civil's
9-credit-plus-project rule, CSE's 12-or-6-plus-project rule, and the 12-credit ECE,
Mechanical, and Chemical models are kept separate. Mandatory course groups for
interdisciplinary ECE/Mechanical tracks are checked explicitly.

Verification performed: programme catalogue validator passed for all 44 IDs; full
backend suite passed (162 passed, 1 skipped); real-browser E2E passed (67/67), including
CSE, B.Des., and doctoral pathway renderings; frontend production bundle rebuilt to
922,806 bytes. Relevant prospectus pages were rendered and visually inspected for CSE,
Civil, ECE, Mechanical, and Chemical Engineering.

---

## 19. Session update — 2026-08-06 (still later): a real bid-allocation bug, more
## competition scenarios, and a layout that was 640px narrower than it needed to be

The user reported the bid simulator felt like it was "over-oversubscribing" - courses
were showing "target not reachable" even at the full pool cap - plus a request for
easier headline-scenario options, more customisability, and a much wider layout.

**Found a real bug in `minimal_robust_bid()` (`backend/app/optimization/robust.py`),
not just harsh tuning.** For STRONG/BACKUP/OPTIONAL priorities, EXTREME's own published
target is 0 - it's a stress test to report, never a bar those priorities must clear.
`brute_force()`'s own `meets()` oracle already encoded that correctly (`t[k]<=0 or
curves[k][b]>=t[k]`), but `minimal_robust_bid()`'s fast path had a first-pass gate that
compared the worst win-probability across *all three* mandatory tiers - EXTREME
included - against the HIGH tier's target, before it ever reached the (correct) per-tier
check. A course whose EXTREME performance was merely low - not a problem, since EXTREME
was never required - failed that gate on every bid up to and including the cap, so the
correct per-tier logic never ran. Reproduced with the exact numbers from the report
(HIGH 99.9%, VERY_HIGH 94%, EXTREME 56% at cap for a STRONG course): before the fix,
`target_met=False` at the full cap for all three robustness methods; after, `minimax`
returns bid 282 with `target_met=True` - the course was reachable the whole time. No
test had ever compared `minimal_robust_bid()` against `brute_force()` despite
`brute_force()` existing specifically for that - a real gap, not a hypothetical one.
Rewrote the function so minimax matches `brute_force()` exactly (verified by
`test_minimal_robust_bid_matches_brute_force_on_random_curves`, 25 random trials) and
mean/cvar blend probabilities only across the tiers a priority is actually held to,
compared against a matching blended target - genuinely more permissive than minimax
now, not just a broken gate. `backend/tests/test_robust.py` (new, 9 tests) covers this;
`summarise()`'s own `_worst`/`_mean`/`_cvar` display aggregates were also fixed to use
an allowlist of mandatory tiers (`k in STRESS`) instead of a denylist naming
`"OPTIMISTIC"` by name, so the fix generalizes to the two new modes below automatically.

**Added `LOW` and `MODERATE` competition modes** (`backend/app/simulation/engine.py`),
both `comparison_only=True` like `OPTIMISTIC` - never used for the conservative
recommendation itself (design decision #1 stands unchanged). `SimulationRequest`'s old
single `include_optimistic: bool` became `extra_scenarios: list[CompetitionMode]`
(defaults to `[LOW, MODERATE]` - visible without opting in; `OPTIMISTIC` stays opt-in),
with a validator rejecting anything that isn't one of the three comparison-only tiers
and a cross-field check that `headline_mode` is always one of the tiers actually being
simulated. Frontend: the single "Also show optimistic comparison" checkbox became three
(Low/Moderate checked, Optimistic unchecked), and the headline-scenario dropdown gained
Low and Moderate. `buildPlan()` now guards against sending an inconsistent request
(picking a comparison-only headline whose own checkbox is unticked still includes it).

**Found and fixed a second, real accessibility regression while re-running the a11y
audit** (not assumed clean): the scenario-comparison table dimmed comparison-only rows
via `opacity:.6` on the whole `<tr>` - fine when that row was `OPTIMISTIC` only and
opt-in, not fine now that Low/Moderate run by default and that row is the common case.
Faded text pushed already-borderline `.p-m`/`.mut` contrast below WCAG AA. Fixed by
replacing the opacity trick with a subtle background tint (`.comparison-row`) that
doesn't touch text contrast - the "comparison only" pill label alone already carries
the distinction. Re-ran the full 6-tab audit after the fix: 0 violations again.

**Layout width.** `.wrap` was capped at `max-width:1240px` - on a real 1512px laptop
viewport that's over 270px of dead margin on each side, confirmed by checking the
actual rendered `.wrap` width via a real Playwright-controlled Chromium page (the
`Browser` pane tool in this environment reports a 0x0 viewport and can't render for
real, so this was verified through the same Playwright harness the e2e suite already
uses, not assumed from the CSS alone). Widened to `max-width:1880px`; verified at both
1512px (fills the full width, no overflow) and 1920px (caps at 1880px, no overflow).
`.note`/`.sub` prose text got explicit `max-width` caps (105ch/90ch) so paragraph text
doesn't stretch uncomfortably wide just because the card around it now can; stat/grid
columns (`.g3`/`.g4`) gain an extra column at `min-width:1500px` so the recovered space
holds more content, not just more padding.

```bash
cd backend && python3 -m pytest -q                                     # 172 passed, 1 skipped (was 163)
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 44 + 36 passed
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                        # 70 passed (was 69)
./scripts/run-e2e.sh tests/a11y-audit.js                               # 0 violations (1 found and fixed mid-session)
```

**Not done this pass**: no page-level multi-column (sidebar) layout was added anywhere
- the width fix recovers space for existing grids/tables/cards to breathe, not a new
information-architecture change; a `.pane-cols` utility was drafted then removed
unused rather than left half-wired. Doctoral/MBA/PhD students still see the full bid
simulator tab even though most graduate enrolment bypasses bidding (`ENROL.BACKEND_DISCONTINUED`)
- named again as a real follow-up, not attempted.

---

## 20. Session update — 2026-08-09: COMPAS rename, a real Net Ceiling bug, four
## new official documents

The Dean Academics office announced the COMPAS mock round (10 August 2026) and
attached four new documents, read in full before any code changed:
`SNU - CPMS User Manual.pdf` (the vendor's own 61-page portal guide),
`Preference_Based_Course_Matching_Introduction.pdf`, `..._FAQ.pdf`, and
`..._Concept_Note.pdf`. The system is now officially "preference-based course
matching" / COMPAS (Course Matching using Preference Assigned Scores),
replacing the "bidding"/"auction" framing this project has used since
inception.

**Checked every number and formula in these four documents against what this
project already had - it is the same mechanism under a new official name, not
a new mechanism.** The Concept Note's steady-state (Y2), transition (Y3) and
graduating (Y4) pool formulas, worked examples, and per-semester release
schedules reproduce this project's existing `pools.py` formulas exactly,
number for number (e.g. the Y4 worked example's 342/215/342 and the disputed
297/125/238.5 average row, both already tracked here since earlier sessions).
The round sequence, tie-break design, clearing-price rule, and retake/waiver
provisions also match exactly. The vendor's own CPMS User Manual keeps using
"Bid Points"/"Bid Ledger"/"Bidders" internally throughout, even under the new
official student-facing name - so this was not treated as a mandate to purge
"bid" everywhere, only to align the app's own top-level, most-visible
vocabulary with what students now see in official communications.

**A real, confirmed bug found via the manual's "Net Credits Logic" section.**
The consideration-set ceiling ("you may bid for backups up to twice your
available credit limit") was already documented in `rules.py` as
`SET.NET_CEILING`: Req = min(graduation remaining, semester remaining,
category remaining), ceiling = Req x 2 - but the frontend's own "Net ceiling
(Req×2)" stat (`d_sched.html::renderChosen()`) only ever computed the
semester leg of that MIN (`cap - fixedCredits`), silently dropping the
graduation-remaining and category-remaining legs the rule itself already
documented. Reproduced the manual's own worked example directly in a real
browser session before and after the fix (`degCr=120, doneCr=9, capCr=25,
fixedCredits=9, remME=50` → `grad=111, sem=16, cat=50, Req=MIN(111,16,50)=16,
ceiling=32`) - byte-for-byte match. Fixed by adding `netCeiling(cat)` (new
function in `d_sched.html`), computed per elective category (ME/UWE/CCC each
get their own Req and ceiling, since the manual's own example computes Req
for the specific round/category being bid on, not one blended figure) using
data already collected on the Profile tab (`doneCr`/`degCr` for graduation,
`capCr` minus fixed credits for semester, `remME`/`remUWE`/`remCCC` for
category). `degCr` is explicitly optional, so an unset value (0) leaves the
graduation leg unconstraining (`Infinity`) rather than forcing Req to 0.

**Two genuinely new rules added to `rules.py`** (things stated in the new
documents that no earlier document had said): `ENROL.PREREQUISITES` -
prerequisites must be met to add a course, but no prerequisite data exists
anywhere in `courses.json`, so this is honestly marked `Status.UNKNOWN` and
not enforced; `ROUND.WAITLIST_POINTS_RESERVED` - a waitlisted bid's points
are held, not refunded, until the waitlist itself resolves (the manual's own
"Waitlist Processing" page) - not modelled in the Monte Carlo simulator or
optimizer, both of which reason about a single round's win/lose outcome, not
a multi-round reserved-points state machine. `SET.NET_CEILING`, `ROUND.SEQUENCE`
(rounds 10-12 are now explicitly named "2nd Half CCC Bidding/Waitlist/Add-Drop"
by the manual's own outline, previously only inferred) and `AUC.TIEBREAK` got
updated citations. `RULE_VERSION` bumped to `2026.M.4`.

**Terminology pass - scoped to the most visible, top-level student-facing
copy, not an exhaustive purge.** Updated: page `<title>`, the H1 (`Strategic
Bid Planner` → `Preference Matching Planner`), the `04` tab label
(`Strategic bids` → `Preference matching`), the journey-bar hints (both the
static initial span and the `JOURNEY` metadata object that overrides it per
tab), the `Bid-point budget` and `Every course you can bid for` card
titles/headers (the latter's rename required updating a `querySelector`
reference in `d_sched.html`'s "Collapse list" button, done in the same edit
so it didn't silently break), the picker's "biddable" filter option, the
pool-cap tooltips (both copies, `b_body.html` and `d_sched.html`), the
Strategic-planner section's intro paragraph and "Build strategic ... plan"
button (updated identically in both the static HTML and the `glue.js`
toggle that overwrites it), the budget-flags prose in `c_core.html`, a
category-migration notice and the Rules-tab glossary rows in `g_two.html`,
and a swap-round tooltip in `e_tt.html`. Also fixed an unrelated, real
staleness bug found while touching this exact header text: the masthead's
"326 offered courses · **988** valid section packages · **3,201**
package-meeting slots" line was hardcoded prose that never got updated after
this session's earlier `2026-08-07` batch-coherence fix changed the real
package count to 954 (3,095 meeting slots, recomputed directly from
`courses.json` rather than guessed). Every renamed `data-section-title` and
button-text string was checked against `frontend/tests/e2e.test.js` first;
the one real hit (`/Strategic bid planner/` in the tab-identity check) was
updated in the same change, not left to fail silently.

**Deliberately not touched, named rather than silently left inconsistent**:
`frontend/src/ui/k_learn.html` (the "Learn the system" tab's comparative
explainer, which correctly and factually describes *other* universities' own
still-actually-named "bid points" systems, e.g. Chicago Booth - blindly
renaming "bid" there would make true statements about other schools false);
`README.md`'s own prose; and the deeper, cross-file "opening bid" /
`bidPosture` / `bidReserve` / `BidStrategyRequest` vocabulary, which is this
project's own strategic-planning terminology (never an official University
term to begin with, and entangled with `plans.js`'s CSV export column header
and a `plans.test.js` assertion by exact string) - left as one internally
consistent term rather than a rushed, error-prone partial rename across a
CSV contract and its tests in the same sitting.

```bash
cd backend && python3 -m pytest -q                                     # 213 passed, 1 skipped (rules.py additions)
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # 48 + 39 passed
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                        # 69 passed
./scripts/run-e2e.sh tests/a11y-audit.js                               # 0 violations
```
