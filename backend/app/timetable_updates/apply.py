"""Transactional dataset application: stage -> validate -> backup -> atomic
replace -> hot reload -> health check -> commit to manifest, with rollback
on failure. Also implements rollback-to-a-previous-version by reusing the
same write/reload path with an older version's own preserved files as the
source (every version, applied or not, keeps its own courses.json under
timetable_versions/<id>/ - see normalize.py's caller in poller.py - so a
rollback target's files already exist on disk and never need reconstruction).

Honesty note on atomicity: each individual file replace (`os.replace`) is a
single atomic syscall on both Windows and POSIX for same-volume moves. The
three-file sequence (frontend copy -> backend copy -> manifest) is NOT
wrapped in a filesystem transaction - there is no such primitive available
without a database. The residual risk window is the gap between the first
and last `os.replace` call, during which a crash could leave the two data
copies updated but the manifest still pointing at the old version (or vice
versa for the very last file). This is mitigated by keeping the manifest
write last (so a crash before it leaves the manifest's own claim consistent
with what "active" means to load_manifest()) and by a startup consistency
check (see `verify_consistency`) that would surface such a state rather than
silently trusting it.
"""
from __future__ import annotations
import json
import os
import shutil
import time
from pathlib import Path

from app.domain import catalog
from app.timetable_updates.models import ImportStats
from app.timetable_updates.validate import validate_normalized

# Derived from catalog.py's OWN resolved paths (which already respect the
# SNU_CATALOG_PATH / SNU_DATASET_MANIFEST_PATH env var overrides used both by
# tests and by the frozen desktop build - see desktop_launcher.py) rather
# than independently recomputing from __file__. A real bug (found while
# smoke-testing the packaged desktop exe, 2026-08-04) came from exactly this
# kind of duplication: this module used to compute its own path from
# `catalog.__file__`, which does not honor the same override catalog.py
# itself uses, so the frozen build's active-version lookup silently pointed
# nowhere even after catalog.py's own override was fixed.
BACKEND_COURSES_PATH = Path(catalog._DATA_PATH)
BACKEND_DATA = BACKEND_COURSES_PATH.parent
MANIFEST_PATH = Path(catalog._MANIFEST_PATH)
VERSIONS_DIR = BACKEND_DATA / "timetable_versions"
# frontend/src/data.json is a dev-only concept - it does not exist in a
# frozen build or a backend-only container image. apply_version() already
# guards every write to this path with `.parent.exists()`, so a nonsensical
# path here is harmless, not a crash risk.
FRONTEND_DATA = BACKEND_DATA.parent.parent.parent / "frontend" / "src" / "data.json"


class ApplyError(RuntimeError):
    pass


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"active_version": None, "versions": []}
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{int(time.time() * 1000)}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic on the same volume, both Windows and POSIX


def stage_version(version_id: str, courses: list[dict], provenance: dict, manifest_entry: dict) -> Path:
    """Writes a candidate (or a freshly-imported version not yet applied) to
    its own versioned folder. Never touches the active files."""
    out_dir = VERSIONS_DIR / version_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "courses.json").write_text(json.dumps(courses, indent=2), encoding="utf-8")
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    (out_dir / "manifest_entry.json").write_text(json.dumps(manifest_entry, indent=2), encoding="utf-8")
    return out_dir


def apply_version(version_id: str, expected_checksum: str | None = None) -> dict:
    """Activates `version_id` from its preserved folder under
    timetable_versions/. Used both for a fresh candidate's apply and for
    rollback to any historical version - same code path either way.

    Raises ApplyError on any failure; the active files are left untouched
    (or restored) in every failure case.
    """
    out_dir = VERSIONS_DIR / version_id
    entry_path = out_dir / "manifest_entry.json"
    courses_path = out_dir / "courses.json"
    if not entry_path.exists() or not courses_path.exists():
        raise ApplyError(f"staged version {version_id!r} no longer exists on disk - it may have been "
                         f"discarded or never staged")

    manifest_entry = json.loads(entry_path.read_text(encoding="utf-8"))
    courses_text = courses_path.read_text(encoding="utf-8")
    if expected_checksum is not None and manifest_entry.get("dataset_checksum") != expected_checksum:
        raise ApplyError(
            f"the staged candidate's checksum ({manifest_entry.get('dataset_checksum')}) no longer "
            f"matches what was reviewed ({expected_checksum}) - it may have been re-staged since; "
            f"re-check before applying")

    courses = json.loads(courses_text)
    revalidate_stats = ImportStats()
    validate_normalized(courses, revalidate_stats)
    if revalidate_stats.error_count:
        raise ApplyError(f"re-validation before apply found {revalidate_stats.error_count} error(s); refusing")

    # backup: the currently-active bytes, held in memory so a failed
    # multi-file replace can be reverted without needing the old version's
    # folder to still exist under a different name
    prev_frontend = FRONTEND_DATA.read_text(encoding="utf-8") if FRONTEND_DATA.exists() else None
    prev_backend = BACKEND_COURSES_PATH.read_text(encoding="utf-8") if BACKEND_COURSES_PATH.exists() else None
    prev_manifest = MANIFEST_PATH.read_text(encoding="utf-8") if MANIFEST_PATH.exists() else None

    manifest = load_manifest()
    versions = [v for v in manifest.get("versions", []) if v["version_id"] != version_id]
    versions.append(manifest_entry)
    new_manifest_text = json.dumps({"active_version": version_id, "versions": versions}, indent=2)

    replaced = []
    try:
        if FRONTEND_DATA.parent.exists():
            _write_atomic(FRONTEND_DATA, courses_text)
            replaced.append("frontend")
        _write_atomic(BACKEND_COURSES_PATH, courses_text)
        replaced.append("backend")
        _write_atomic(MANIFEST_PATH, new_manifest_text)
        replaced.append("manifest")
    except OSError as e:
        # best-effort revert of whatever was already replaced - see module
        # docstring for the honest limits of this guarantee
        if "frontend" in replaced and prev_frontend is not None:
            _write_atomic(FRONTEND_DATA, prev_frontend)
        if "backend" in replaced and prev_backend is not None:
            _write_atomic(BACKEND_COURSES_PATH, prev_backend)
        if "manifest" in replaced and prev_manifest is not None:
            _write_atomic(MANIFEST_PATH, prev_manifest)
        raise ApplyError(f"file replace failed ({e}); reverted what had already changed") from e

    catalog.reload()
    health = catalog.dataset_info()
    if health["dataset_checksum"] != manifest_entry.get("dataset_checksum"):
        # the write succeeded but the reloaded catalog doesn't match what we
        # just wrote - revert everything and refuse to call this a success
        if prev_frontend is not None and FRONTEND_DATA.parent.exists():
            _write_atomic(FRONTEND_DATA, prev_frontend)
        if prev_backend is not None:
            _write_atomic(BACKEND_COURSES_PATH, prev_backend)
        if prev_manifest is not None:
            _write_atomic(MANIFEST_PATH, prev_manifest)
        catalog.reload()
        raise ApplyError("post-apply health check failed: reloaded catalog checksum did not match the "
                         "candidate that was just written; reverted")

    return {"version_id": version_id, "dataset_checksum": health["dataset_checksum"],
            "course_count": health["course_count"], "package_count": health["package_count"]}


def discard_candidate(version_id: str) -> None:
    """Removes a staged-but-never-applied candidate's folder. Never called on
    an applied version - history is only ever pruned by explicit retention
    policy, not by discard."""
    out_dir = VERSIONS_DIR / version_id
    manifest = load_manifest()
    if any(v["version_id"] == version_id for v in manifest.get("versions", [])):
        raise ApplyError(f"{version_id!r} has been applied at least once; discard only applies to "
                         f"never-applied candidates - use rollback instead if you want to move away from it")
    if out_dir.exists():
        shutil.rmtree(out_dir)
