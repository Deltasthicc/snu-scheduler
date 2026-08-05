# Baseline verification — 2026-08-04, before the timetable-poller phase

Every row below was actually run this session, not assumed from CLAUDE.md's own claims.

| Area | Claimed state (CLAUDE.md) | Actual result | Command | Status |
|---|---|---|---|---|
| Stale process check | — | Found `SNU-Bid-Scheduler.exe` still bound to port 8000 from the prior session's testing | `netstat -ano \| grep :8000`, `taskkill` | **Fixed before proceeding** |
| Backend tests | 100 passed | 100 passed (38.85s — slower than usual, no functional cause found) | `python3 -m pytest -q` | PASS |
| Frontend adapter tests | 38 passed | 38 passed | `node tests/adapter.test.js` | PASS |
| Saved-plan tests | 35 passed | 35 passed | `node tests/plans.test.js` | PASS |
| End-to-end tests | 62 passed | 61/62 first run, 62/62 on 3 reruns | `./scripts/run-e2e.sh tests/e2e.test.js` | PASS (known intermittent flake, see below) |
| Accessibility audit | 0 violations | 0 violations | `./scripts/run-e2e.sh tests/a11y-audit.js` | PASS |
| Timetable importer dry run | reproducible, 0 errors | First attempt hit a transient DNS failure (`getaddrinfo failed`); correctly reported the failure and left the active dataset untouched rather than erroring destructively. Confirmed reachable via `curl` immediately after (transient network blip, not a code issue). Re-ran importer: 326 courses, 988 packages, 0 errors, 5 warnings, diff vs baseline unchanged (2 renamed, +1/-1, 205 changed) | `python3 tools/import_netlify_timetable.py` | PASS (offline-safety behavior incidentally verified for real) |
| Dataset endpoint | active version + checksum | `active_version: monsoon-2026-netlify-revision-2026-08-04`, `dataset_checksum` present | `GET /api/v1/dataset` | PASS |
| Dataset manifest | 2 versions, checksums recorded | 2 versions present; checksums were **stale under the old raw-file-hash convention** — see bug below | `cat backend/app/data/dataset_manifest.json` | **Bug found and fixed** |
| Active dataset checksum | stable | Found to disagree with a fresh normalization of identical content — see the checksum-convention bug below | direct Python reproduction | **Bug found and fixed** |
| Desktop build | onedir, ~1.4s cold start | Rebuilt after adding the timetable-updates module (pure Python, no new native deps); see verification section in CLAUDE.md §15 | `./scripts/build-exe.sh` | PASS (rebuilt and reverified this session) |
| Desktop wishlist search | works via heuristic fallback | Not re-run against the desktop build specifically this session (unchanged code path); web-deployment CP-SAT path re-verified instead | `curl` against dev server | PASS (web path only, this pass) |
| Desktop cancellation | works | Not re-exercised against the desktop build this session | — | not re-verified this pass |
| Web CP-SAT search | works, `cp_status: optimal` | Confirmed: real search returned `cp_status: "optimal"` (unfrozen path) | `curl -X POST /api/v1/schedules/search` against dev server | PASS |
| Personal-profile import | validated, staged | Not re-exercised this session (unchanged since last phase) | — | not re-verified this pass |
| Cache invalidation by dataset checksum | works | Confirmed via the checksum-convention fix: `input_hash()` already included the live checksum from the prior phase; this phase's fix makes that checksum actually mean what it claims to | direct test in `test_timetable_updates.py` | PASS |

## Real bug found while establishing the baseline (not from writing new code)

**Two different "dataset checksum" conventions that could never agree.** `app/domain/catalog.py`'s live checksum hashed the *raw file bytes* on disk (whatever indentation happened to be there from whichever tool last wrote the file). This session's new `app/timetable_updates/normalize.py` computed its own checksum via a canonical re-serialization (`sort_keys=True`, compact separators) - a *different* convention. Reproduced directly: fetching the live, completely unchanged timetable and renormalizing it produced 326/326 byte-identical courses and a byte-identical full-list JSON dump, yet the two "checksums" disagreed (`b1997ae63bb359e2` vs `9f5ff5a611844334`). This would have made the entire three-hash change-detection design (the centerpiece of this phase) report a false "update available" on *every single check*, forever, regardless of whether the timetable had actually changed.

**Fix**: one canonical function, `catalog.canonical_checksum()`, is now the only place either module computes a dataset checksum. Existing manifest entries were regenerated to the new convention. Regression test: `test_normalize_identical_content_produces_identical_hash_regardless_of_key_order` in `backend/tests/test_timetable_updates.py`.

## Known intermittent flake (pre-existing, not introduced this session)

`e2e.test.js` §18's "auto-resolve does not block the main thread either" assertion (a Performance-Observer long-task check with a 50ms threshold) has failed 2 of roughly 8 runs across this session and the two prior sessions, always by single-digit milliseconds (53-58ms observed). Every failure has cleared on immediate rerun. This is a real, measured characteristic tied to the catalog growing from 859 to 988 packages (a larger course list means `renderPick()`'s full-table rebuild runs closer to the 50ms threshold), not a functional regression - documented rather than dismissed, and flagged as legitimate future frontend-optimization work (incremental rendering instead of full-table rebuilds) rather than patched under time pressure this session.
