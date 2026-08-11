"""Course catalog: the frontend-bundled dataset, loaded once here so schedule
search has authoritative data to compute against instead of trusting whatever
the browser sends. See app/data/courses.json and scripts/sync_course_data.py
for how this file is kept in sync with the frontend's copy at
frontend/src/data.json, and app/data/dataset_manifest.json + timetable_versions/
for the versioned-dataset history (see tools/import_netlify_timetable.py and
docs/TIMETABLE_REVISION_DIFF_2026-08-04.md).
"""
from __future__ import annotations
import hashlib
import json
import os

# override for tests: a spawned worker process inherits env vars but not
# in-process monkeypatches, so this is how integration tests point a real
# worker at a synthetic catalog instead of the production course data.
#
# normpath() here is load-bearing, not cosmetic: a literal, un-collapsed ".."
# segment (what os.path.join produces on its own) opens the file just fine -
# the OS resolves ".." at open() time - but breaks any downstream code that
# walks the path with Path.parent, since parent only pops the last literal
# segment and has no idea ".." means "go up an extra level". app/timetable_
# updates/apply.py derives frontend/src/data.json's location by chaining
# three .parent calls off this module's own path, and with the un-normalized
# join that chain landed on backend/app/frontend/src/data.json - a directory
# that has never existed, so apply.py's own "skip if the parent doesn't
# exist" guard silently ate the frontend write on every single apply. Found
# while applying the batch-coherence fix below and confirming
# frontend/src/data.json actually picked up the new dataset.
_DATA_PATH = os.environ.get(
    "SNU_CATALOG_PATH", os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "courses.json")))
_MANIFEST_PATH = os.environ.get(
    "SNU_DATASET_MANIFEST_PATH",
    os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "dataset_manifest.json")))

_CATALOG: list[dict] | None = None
_BY_CODE: dict[str, dict] | None = None
_DATASET_CHECKSUM: str | None = None
_MANIFEST: dict | None = None


def canonical_checksum(courses: list[dict]) -> str:
    """The ONE canonical serialization+hash convention for a course list -
    both this module's own live checksum and
    app/timetable_updates/normalize.py's `normalized_hash` must call this
    exact function, never their own ad-hoc json.dumps. A real bug (found
    2026-08-04) came from exactly this: catalog.py used to hash the raw file
    bytes on disk (whatever indentation happened to be there) while
    normalize.py hashed a differently-formatted re-serialization of the same
    data - two conventions for 'the same' checksum that could never agree
    even for byte-for-byte identical course data, which broke the entire
    three-hash change-detection design (every check falsely reported an
    update available). Hashing content, not incidental file formatting, is
    what makes this checksum meaningful at all."""
    canonical = json.dumps(courses, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def reload() -> None:
    """Forces the next all_courses()/get_course()/dataset_info() call to
    re-read the data + manifest files from disk. Called after the timetable
    update service atomically replaces the active dataset files - the whole
    point of a hot reload is that a running process picks up the new data
    without a restart (see app/timetable_updates/apply.py)."""
    global _CATALOG, _BY_CODE, _DATASET_CHECKSUM, _MANIFEST
    _CATALOG = None
    _BY_CODE = None
    _DATASET_CHECKSUM = None
    _MANIFEST = None
    _load()


def _load() -> None:
    global _CATALOG, _BY_CODE, _DATASET_CHECKSUM, _MANIFEST
    if _CATALOG is not None:
        return
    with open(_DATA_PATH, encoding="utf-8") as f:
        raw = f.read()
    _CATALOG = json.loads(raw)
    _BY_CODE = {c["code"]: c for c in _CATALOG}
    _DATASET_CHECKSUM = canonical_checksum(_CATALOG)
    try:
        with open(_MANIFEST_PATH, encoding="utf-8") as f:
            _MANIFEST = json.load(f)
    except (OSError, json.JSONDecodeError):
        _MANIFEST = None


def dataset_info() -> dict:
    """Active dataset identity for the /api/v1/dataset endpoint and for
    cache-key construction - callers should never treat a schedule-search
    cache entry as valid across a dataset change without this."""
    _load()
    active = None
    if _MANIFEST:
        active_id = _MANIFEST.get("active_version")
        for v in _MANIFEST.get("versions", []):
            if v.get("version_id") == active_id:
                active = v
                break
    return {
        "active_version": (active or {}).get("version_id", "unknown"),
        "source_name": (active or {}).get("source_name"),
        "retrieved_at": (active or {}).get("retrieved_at"),
        "source_checksum": (active or {}).get("source_checksum"),
        "dataset_checksum": _DATASET_CHECKSUM,
        "course_count": len(_CATALOG or []),
        "package_count": sum(len(c.get("pk", [])) for c in (_CATALOG or [])),
        "known_versions": [v.get("version_id") for v in (_MANIFEST or {}).get("versions", [])],
    }


def credits_of(course: dict | None, default: float = 0.0) -> float:
    """Null-safe credit read. Returns `default` when the course is missing OR
    its credit is null.

    A null credit is a real, legitimate state in this dataset: the University
    publishes timetables with no credit column, so a course appearing for the
    very first time (with no prior dataset entry to carry a value forward
    from) genuinely has no known credit until one is published. `cr: null` is
    the honest representation of that and must not be replaced by a guess.

    What it must NOT do is crash. `float(course.get("cr", 0))` looks safe but
    is not: the key EXISTS with a null value, so the 0 default never applies
    and float(None) raises. That was a live HTTP 500 on
    /api/v1/wishlists/validate for two real courses (CCC396/CCC2315,
    MAT205/MAT2004) that a student could put in their wishlist during
    enrolment week. Callers that want to SHOW the distinction should read
    `cr` directly and test for None; callers doing arithmetic use this.
    """
    if not course:
        return default
    value = course.get("cr")
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def all_courses() -> list[dict]:
    _load()
    return _CATALOG


def get_course(code: str) -> dict | None:
    _load()
    return _BY_CODE.get(code)


def get_courses(codes: list[str]) -> dict[str, dict]:
    """Returns only the codes that actually exist; callers must check for gaps
    themselves (schedule search treats an unknown code as a 422, not a silent skip)."""
    _load()
    return {c: _BY_CODE[c] for c in codes if c in _BY_CODE}
