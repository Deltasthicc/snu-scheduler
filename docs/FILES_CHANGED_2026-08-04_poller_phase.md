# Files created/changed this phase — timetable poller + update workflow (2026-08-04)

## Created

```
backend/app/timetable_updates/__init__.py
backend/app/timetable_updates/models.py
backend/app/timetable_updates/source.py
backend/app/timetable_updates/parser.py
backend/app/timetable_updates/normalize.py
backend/app/timetable_updates/validate.py
backend/app/timetable_updates/diff.py
backend/app/timetable_updates/apply.py
backend/app/timetable_updates/poller.py
backend/tests/test_timetable_updates.py
docs/BASELINE_VERIFICATION_2026-08-04.md
docs/KNOWN_LIMITATIONS_2026-08-04.md
docs/FILES_CHANGED_2026-08-04_poller_phase.md
```

## Changed

```
tools/import_netlify_timetable.py        # rewritten as a thin CLI wrapper over
                                          # app/timetable_updates/* - no more
                                          # duplicated fetch/parse/normalize/diff logic
backend/app/domain/catalog.py            # + canonical_checksum() (the fix for the
                                          # checksum-convention bug), + reload()
backend/app/services/runner.py           # unchanged this phase (already used the live
                                          # checksum from the prior phase's fix)
backend/app/main.py                      # + UpdateService wiring in lifespan,
                                          # + 8 /api/v1/timetable-updates/* endpoints
backend/pytest.ini                       # + asyncio_mode = auto
backend/desktop_launcher.py              # + SNU_DATASET_MANIFEST_PATH env var (real
                                          # bug fix: the frozen build could not find
                                          # its own active dataset version otherwise)
scripts/build-exe.sh                     # + --add-data for dataset_manifest.json and
                                          # timetable_versions/ (same bug's other half)
backend/app/data/dataset_manifest.json   # checksums regenerated to the new canonical
                                          # convention (both existing version entries)
backend/app/data/timetable_versions/*/manifest_entry.json  # same regeneration
frontend/src/api.js                      # + 8 timetable-update functions
frontend/src/glue.js                     # + timetable-update status/review/apply/
                                          # discard logic; boot() reads status once
frontend/src/ui/b_body.html              # + header status bar + review panel markup
frontend/tests/adapter.test.js           # + 6 timetable-update API adapter tests
README.md                                # + timetable-update section
CLAUDE.md                                # + §15 session update (moved to correct
                                          # position after §14, which was accidentally
                                          # inserted-before on the first edit and fixed)
```

## Not touched

Everything from the prior two phases (wishlist/CP-SAT scheduler, credit policy,
personal-profile bootstrap, the desktop packaging decision itself) is unchanged in
behavior - this phase only added the timetable-update service around it.
