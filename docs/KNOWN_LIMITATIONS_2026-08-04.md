# Known limitations — 2026-08-04, timetable-poller phase

Stated plainly, per this project's own established rule: "verify, don't assume" cuts
both ways — it also means naming what was *not* verified or built, rather than letting
a large deliverable list imply full coverage.

## Explicitly not built this session

- **Schedule diversity layer.** The wishlist CP-SAT/heuristic solver still returns one
  optimized schedule per generation call, not the requested families (best overall /
  fewest campus days / latest start / etc.). Unchanged since the prior session's own
  stated limitation.
- **Schedule comparison UI/API.** No `/schedules/compare` endpoint; no side-by-side
  2-4-plan comparison view.
- **Post-bid / post-update repair engine.** The existing `revalidatePlanAgainstDataset()`
  detects that a saved plan references a course no longer in the active catalog, but
  there is no automatic "replace the missing package with an unambiguous equivalent"
  repair step, and no bid-outcome (won/lost) repair at all.
- **Dedicated plan-impact screen for timetable updates.** The update-review panel shows
  the *dataset-level* diff (added/removed/renamed/changed course counts). It does not
  cross-reference that diff against the student's own fixed courses/wishlist/choice
  groups to produce a "this affects N courses in *your* plan" summary - this is the
  single largest gap between what section 14 of the request asked for and what was
  delivered.
- **Mobile/responsive audit.** Not performed. The existing 11-tab desktop-oriented
  layout is unchanged.
- **Full application-wide security/concurrency/state audit** (request §20). Not
  performed as a dedicated exercise. The one real bug found this session (the
  checksum-convention mismatch) surfaced through normal implementation and baseline
  verification, not a separate audit pass.
- **Formal benchmark suite** (request §28). No repeatable benchmark command was built.
  Real numbers reported in this session (desktop cold-start times, backend test
  durations) came from direct one-off measurement, not a saved, re-runnable harness.
- **Dependency-group splitting** (`requirements-runtime.txt` / `requirements-dev.txt`
  etc., request §24). `backend/requirements.txt` still mixes runtime and test/dev
  dependencies (`pytest`, `pytest-asyncio`, `hypothesis`, `httpx`).
- **Reviewed PyInstaller `.spec` file** (request §26). The build still uses
  `--collect-all` for `uvicorn` and `ortools` rather than an explicit, reviewed set of
  hidden imports/excludes. This is a real, known source of avoidable size (see
  `docs/DESKTOP_PACKAGING.md`'s own size-breakdown section from the prior session,
  unchanged this session).
- **OR-Tools desktop-exclusion experiment** (request §25). The frozen desktop already
  never calls into OR-Tools (the heuristic fallback exists specifically because the
  native call segfaults there), but OR-Tools is still bundled into the desktop build
  unnecessarily via `--collect-all ortools`. Excluding it entirely was not attempted
  this session despite being explicitly requested and being a plausible, real size win.
- **Frontend asset optimization, clean source-ZIP packaging** (request §23, §27). Not
  attempted.
- **SSE progress stream for the update checker.** `GET /timetable-updates/events` from
  the suggested endpoint list was not built; the frontend uses status polling only,
  judged sufficient given checks complete in a few seconds against the real site.

## Real, verified limitations of what *was* built

- **Cross-process hash caching does not persist.** `UpdateService._last_extracted_hash`
  is an in-memory attribute; a backend restart loses it, so the very first check after
  any restart always does a full normalize even if nothing changed (still correctly
  classified via the normalized-hash comparison against the catalog - just not able to
  short-circuit at the extracted-hash layer on that one check). Not persisted to disk.
- **Three-file apply is not a true filesystem transaction.** Documented explicitly in
  `apply.py`'s own docstring: each individual `os.replace` is atomic; the three-file
  sequence (frontend copy, backend copy, manifest) is not wrapped in an actual
  transaction, since no such primitive exists without a database. The residual risk
  window is real, if narrow, and is called out rather than glossed over as "atomic."
  - **This session found and fixed the exact category of bug this note warns about**:
    `apply.py`'s path constants were originally computed independently from
    `catalog.__file__` rather than from `catalog`'s own already-override-aware
    `_DATA_PATH`/`_MANIFEST_PATH`, which meant the frozen desktop build's timetable
    service could not find its own active version (`active_version: "unknown"`) even
    though the checksum itself was correct. Found by actually running the packaged exe
    and checking `/api/v1/timetable-updates/status`, not assumed from source review.
    Fixed by deriving `apply.py`'s constants from `catalog`'s resolved paths instead of
    recomputing them; also required bundling `dataset_manifest.json` and
    `timetable_versions/` into the desktop build, which the original `build-exe.sh`
    never did.
- **Choice-group satisfiability is not proven infeasible by the heuristic fallback.**
  Unchanged from the prior session: the frozen desktop's `_solve_greedy_fallback` does
  not model `at_least_one`/`min_credits` groups exactly - an unsatisfiable one is simply
  not enforced rather than reported as infeasible, in the fallback path only.
- **The intermittent e2e timing flake** (§18's long-task assertion, 53-58ms observed
  against a 50ms threshold) was not fixed - documented as a real, measured, catalog-
  size-correlated characteristic in `docs/BASELINE_VERIFICATION_2026-08-04.md` rather
  than patched under time pressure.

## What was verified for real this session (not assumed)

- The live site's actual `ETag` support (`curl -I`), confirming conditional requests are
  genuinely possible, not just theoretically supported by the design.
- A live, unchanged fetch-and-renormalize cycle against the real timetable, byte-for-byte
  proving the checksum-convention bug and then its fix.
- The full check → stage → review → apply → rollback cycle against a real local HTTP
  fixture server (not mocked at the Python-object level).
- The packaged desktop exe, in two passes: the **first** build (before the manifest-
  bundling fix) surfaced the `active_version: "unknown"` bug above by actually running
  it and checking `/api/v1/timetable-updates/status`. The **second**, fixed build was
  rebuilt and reverified end to end: cold start 1855ms (consistent with the prior
  session's ~1.4-1.9s onedir measurements), `active_version` now correctly resolves to
  `monsoon-2026-netlify-revision-2026-08-04`, a manual check against the real live site
  returned `not_modified` correctly, and a real wishlist search still completed via the
  heuristic fallback (`cp_status: "heuristic_fallback"`) exactly as before - the fix did
  not regress the already-working wishlist path.
