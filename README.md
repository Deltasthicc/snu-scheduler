# SNU Bid & Schedule Simulator

An unofficial planning tool for Shiv Nadar University's Monsoon 2026 bid-point course
enrolment system. Simulates the bid auction under adversarial competition assumptions,
generates clash-free (or best-available) timetables, and now supports a personalised,
credit-aware wishlist scheduler backed by Google OR-Tools CP-SAT.

**This is a student-built unofficial tool, not a University product.** Every rule it
applies traces to a source document; every assumption is labelled as an assumption.

Full technical history and design decisions: [CLAUDE.md](CLAUDE.md). Read it before
changing anything — it is the canonical handoff document for this project.

## What it does

- **Bid simulator**: stress-tests a bid plan against synthetic competitive cohorts,
  recommends bids that survive a worst-case scenario.
- **Schedule builder**: generates clash-free timetables from a course shortlist
  (exhaustive branch-and-bound search, server-side).
- **Wishlist scheduler**: given course intent (must-have / strong / optional / backup),
  choice groups, and a credit target/ceiling, generates the best subset of your
  wishlist under real constraints — backed by OR-Tools CP-SAT. Every excluded course
  gets a specific reason, not silence.
- **Credit policy**: distinguishes official ceiling, personal target, and
  overload/what-if scenarios — never presents an unconfirmed rule as a normal one.
- **Timetable revision tracking**: imports the University's published timetable
  planner, versions the dataset, diffs it against the previous version, and flags
  saved plans that were built against an older timetable.
- **Automatic timetable update checks**: a backend-owned poller checks the published
  timetable on a schedule (default every 15 minutes, configurable), using conditional
  HTTP requests so it never re-downloads unchanged content. Distinguishes a cosmetic
  website change from an actual timetable-data change from a real course-schedule
  change — only the last one ever creates a new dataset version. Review-before-apply
  is the default; validated auto-apply is opt-in. See "Timetable updates" below.
- **Personal profile portability**: your data lives in this browser's local storage
  only, but can be exported/imported as a private bootstrap file to move to a new
  install (`tools/import_personal_profile.py`) — it is never bundled into the app
  itself and never becomes another user's default.
- **Programme-aware degree audit**: all 44 programmes in the University's current
  public catalogue are selectable. The audit keeps completed work separate from this
  semester's planned courses, reports each published minimum independently, and links
  every rule back to an official programme page, brochure, prospectus, or regulation.
  Where a complete cohort curriculum is not public, it says so and accepts exact
  private-profile overrides instead of inventing requirements. See
  [docs/PROGRAMME_AUDIT.md](docs/PROGRAMME_AUDIT.md).
- **Programme pathways and specialisations**: every catalogue entry has a source-linked
  pathway view. The UI keeps formal specialisations, B.Des. streams, ASU routes, and
  doctoral research areas distinct, and calculates progress only where SNU publishes
  a course mapping. Cohort notes and official sources remain visible beside the result.

## Running it

```bash
# one-time setup
cd backend && pip install -r requirements.txt --break-system-packages
# to also run the test suite: pip install -r requirements-dev.txt --break-system-packages
cd ../frontend && python3 build_frontend.py

# start both servers
cd .. && ./scripts/start-local.sh
#   App:      http://127.0.0.1:5173/
#   API docs: http://127.0.0.1:8000/docs

# containerized
docker compose up -d
```

## Production web deployment

The production image serves the frontend and API from one origin, so PDF import,
degree audits, timetable checks, schedule solving, and simulations work online without
pointing a visitor's browser at `127.0.0.1`. Build and verify it locally with:

```bash
docker build -t snu-scheduler .
docker run --rm -p 8000:8000 snu-scheduler
# app and API: http://127.0.0.1:8000/
```

[`render.yaml`](render.yaml) is a Render Blueprint for the same image. GitHub Pages is
not used because it can host only static files and cannot execute the Python/OR-Tools
API that makes the scheduler functional.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Deltasthicc/snu-scheduler)

Desktop (Windows): `./scripts/build-exe.sh` builds a one-folder distribution at
`backend/dist/SNU-Bid-Scheduler/` and zips it to
`backend/dist/SNU-Bid-Scheduler-portable.zip`. See
[docs/DESKTOP_PACKAGING.md](docs/DESKTOP_PACKAGING.md) for why one-folder, not
one-file, and the real measurements behind that choice.

## Updating the institutional timetable

```bash
# dry run - shows the diff without changing anything
python3 tools/import_netlify_timetable.py

# apply as the active dataset (only if validation reports zero errors)
python3 tools/import_netlify_timetable.py --apply
```

See [docs/TIMETABLE_REVISION_DIFF_2026-08-04.md](docs/TIMETABLE_REVISION_DIFF_2026-08-04.md)
for the most recent revision's full diff, and
`backend/app/data/timetable_versions/` for every dataset version kept for comparison.

### Automatic checks (the running application)

The backend polls the same source on its own, using the header's "Check timetable now"
button for a manual check or letting the scheduled poll run on its configured interval:

```bash
SNU_TIMETABLE_UPDATE_ENABLED=true            # default
SNU_TIMETABLE_UPDATE_INTERVAL_MINUTES=15     # floor of 5
SNU_TIMETABLE_UPDATE_URL=https://snioe-monsoon2026-tt.netlify.app/
SNU_TIMETABLE_AUTO_APPLY=false               # default: review before applying
```

`GET /api/v1/timetable-updates/status` shows the current state (`idle` / `checking` /
`not_modified` / `source_changed_only` / `no_dataset_change` / `update_available` /
`applying` / `applied` / `failed` / `offline` / `rollback_available`), last/next check
times, and — when a candidate is staged — its diff summary and validation counts.
`POST .../apply` requires the exact candidate version and checksum you reviewed, and is
rejected if the candidate changed since (no applying a stale diff).

## Restoring your own data on a new install

Your profile, wishlist, and saved plans live in this browser's local storage only —
they are never part of the shipped app. To move them to a new install:

1. On the old install: Profile tab → **Export JSON**.
2. On the new install: Profile tab → **Import…**, select the file, review the preview,
   confirm.

Or validate a file before handing it to someone / staging it for the desktop app:

```bash
python3 tools/import_personal_profile.py --input my-plan.json --stage
```

See `docs/examples/user_profile.example.json` for the file shape (fake data only).

## Tests

```bash
cd backend && python3 -m pytest -q                                     # backend
cd frontend && node tests/adapter.test.js && node tests/plans.test.js  # frontend units
cd .. && ./scripts/run-e2e.sh tests/e2e.test.js                        # real browser + backend
./scripts/run-e2e.sh tests/a11y-audit.js                               # accessibility
```

## Known limitations

See [CLAUDE.md](CLAUDE.md)'s latest session section for the full, current list —
summarized: no diversity layer (one optimized wishlist schedule per generation, not
several ranked families), no cross-plan comparison, no bid-outcome repair engine, no
mobile-specific redesign, no formal performance-benchmark suite, no transcript import,
no Demo Mode, no dedicated personal-plan-impact repair screen for timetable updates
(the review panel shows the dataset-level diff; a saved plan is revalidated against the
new dataset after apply, but not walked through a guided repair flow), no SSE progress
stream for the update checker (status polling only).
