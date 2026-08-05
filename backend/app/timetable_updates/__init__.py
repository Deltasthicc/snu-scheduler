"""Timetable update service: the one canonical implementation of fetching,
parsing, normalizing, validating, diffing, and applying the public timetable
source. Both `tools/import_netlify_timetable.py` (CLI) and the backend's own
background poller (`poller.py`, wired in app.main's lifespan) call these
modules - there is no second, drifting implementation of any of these steps.

Module map:
    models.py    - UpdateState enum, dataclasses, Pydantic API models
    source.py    - conditional HTTP fetch (ETag / source-hash short-circuit)
    parser.py    - safe extraction of the `const DATA = {...}` literal
    normalize.py - canonical course/package construction
    validate.py  - row- and dataset-level validation
    diff.py      - added/removed/renamed/changed comparison
    apply.py     - transactional dataset activation + rollback
    poller.py    - the UpdateService: state machine, poll loop, locking, backoff
"""
