#!/usr/bin/env python3
"""Import an Academic Office timetable workbook (.xlsx) into the versioned
dataset pipeline.

This is a second SOURCE for the same canonical pipeline, not a second
pipeline. It converts the workbook's rows into exactly the row shape
`app.timetable_updates.normalize.flatten_rows` already consumes, then calls
the unchanged `normalize()` - so package construction, batch coherence,
validation, checksums, diffing and the transactional apply all stay in one
implementation shared with the Netlify importer and the backend poller (see
tools/import_netlify_timetable.py and app/timetable_updates/poller.py).

Why a separate entry point at all: the Netlify source is a scraped public
mirror whose inline `DATA` literal we parse; this is the Office's own
workbook, mailed directly to students, and it carries three columns the
mirror never had - "Major for Programme", "Major Elective for Programmes"
and a populated "Student Block" - which is authoritative programme scoping
rather than the department-name guesswork the mirror forced on us.

Usage:
    python3 tools/import_office_timetable_xlsx.py <workbook.xlsx> \
        [--version-id ID] [--label TEXT] [--apply]

Without --apply nothing active changes: the normalized dataset, provenance
and a diff report are written under
backend/app/data/timetable_versions/<version>/ for review only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.timetable_updates import apply as apply_mod  # noqa: E402
from app.timetable_updates import normalize as normalize_mod  # noqa: E402
from app.timetable_updates.diff import diff_datasets  # noqa: E402

# The workbook's own header names. Declared up front and checked before any
# parsing so a re-exported or re-ordered sheet fails loudly with the exact
# missing column, instead of silently importing a dataset full of blanks.
COL_CODE = "Course Code"
COL_TITLE = "Course Title"
COL_SCHOOL = "School"
COL_DEPT = "Department"
COL_MAJOR_FOR = "Major for Programme"
COL_TYPE = "Course Type"
COL_ME_FOR = "Major Elective for Programmes"
COL_UWE = "Open as UWE"
COL_COMPONENT = "Component"
COL_SECTION = "Section"
COL_BLOCK = "Student Block"
COL_TERM = "Term"
COL_DAY = "Day"
COL_START = "Start Time"
COL_END = "End Time"
COL_ROOM = "Room"
COL_INSTRUCTOR = "Instructor(s)"
COL_CAPACITY = "Section Capacity"

REQUIRED_COLUMNS = (
    COL_CODE, COL_TITLE, COL_SCHOOL, COL_DEPT, COL_MAJOR_FOR, COL_TYPE, COL_ME_FOR,
    COL_UWE, COL_COMPONENT, COL_SECTION, COL_BLOCK, COL_TERM, COL_DAY, COL_START,
    COL_END, COL_ROOM, COL_INSTRUCTOR, COL_CAPACITY,
)


class WorkbookError(RuntimeError):
    """The workbook is not in the expected shape. Never recovered from by
    guessing - an Office re-export that drops or renames a column must be
    looked at by a human, not silently imported with holes."""


def _fmt_time(value) -> str:
    """-> "1:00 PM", the format validate.to_minutes() parses. Accepts the
    datetime.time openpyxl yields for a real time cell, and a string for a
    sheet where the column was stored as text."""
    if value is None or value == "":
        return ""
    if hasattr(value, "hour") and hasattr(value, "minute"):
        hour, minute = value.hour, value.minute
        suffix = "AM" if hour < 12 else "PM"
        display = hour % 12 or 12
        return f"{display}:{minute:02d} {suffix}"
    return str(value).strip()


def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _dept3(code: str) -> str:
    """The leading letters of the course code ("CSD211" -> "CSD",
    "ART202/AMP1001" -> "ART"). The Netlify source keyed its rows by this same
    3-letter department code and normalize.derive_category() still reads it,
    so deriving it identically here keeps one categorisation path rather than
    introducing a second notion of "which department is this"."""
    head = code.split("/")[0].strip()
    letters = "".join(c for c in head if c.isalpha())
    return letters[:3].upper()


def _programmes(value: str) -> list[str]:
    """"BIO1YR, BIO2YR" -> ["BIO1YR", "BIO2YR"]. Order-preserving and
    de-duplicated; an empty cell means the workbook scopes nothing here and
    yields an empty list rather than a fabricated default."""
    out: list[str] = []
    for part in _clean(value).replace(";", ",").split(","):
        token = part.strip()
        if token and token not in out:
            out.append(token)
    return out


def read_workbook(path: Path) -> tuple[dict, dict]:
    """Workbook -> (data in flatten_rows' shape, per-course programme scoping).

    The scoping dict is returned separately because it is genuinely new
    information with no equivalent in the Netlify source, so it is attached to
    courses after normalize() rather than smuggled through a shared row field.
    """
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise WorkbookError("openpyxl is required to read .xlsx workbooks "
                            "(pip install -r backend/requirements.txt)") from exc

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = book.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    try:
        header = [_clean(h) for h in next(rows)]
    except StopIteration:
        raise WorkbookError(f"{path.name} has no rows at all")

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise WorkbookError(f"{path.name} is missing required column(s): {missing}. "
                            f"Found: {header}")
    idx = {name: header.index(name) for name in header if name}

    def cell(record, column):
        position = idx.get(column)
        return record[position] if position is not None and position < len(record) else None

    data: dict[str, dict[str, list[dict]]] = {}
    scoping: dict[str, dict] = {}
    for row_number, record in enumerate(rows, start=2):
        if record is None:
            continue
        code = _clean(cell(record, COL_CODE))
        if not code:
            continue  # trailing blank row
        dept = _dept3(code)
        data.setdefault(dept, {}).setdefault("ALL", []).append({
            "rowid": f"{path.name}#{row_number}",
            "code": code,
            "title": _clean(cell(record, COL_TITLE)),
            "type": _clean(cell(record, COL_TYPE)) or "Major",
            "uwe": _clean(cell(record, COL_UWE)),
            "block": _clean(cell(record, COL_BLOCK)),
            "term": _clean(cell(record, COL_TERM)) or "Full semester",
            "comp": _clean(cell(record, COL_COMPONENT)),
            "sec": _clean(cell(record, COL_SECTION)),
            "day": _clean(cell(record, COL_DAY)),
            "start": _fmt_time(cell(record, COL_START)),
            "end": _fmt_time(cell(record, COL_END)),
            "room": _clean(cell(record, COL_ROOM)),
            "instructor": _clean(cell(record, COL_INSTRUCTOR)),
            "cap": _clean(cell(record, COL_CAPACITY)),
        })
        entry = scoping.setdefault(code, {"majorFor": [], "meFor": [], "school": "", "dept": ""})
        for programme in _programmes(cell(record, COL_MAJOR_FOR)):
            if programme not in entry["majorFor"]:
                entry["majorFor"].append(programme)
        for programme in _programmes(cell(record, COL_ME_FOR)):
            if programme not in entry["meFor"]:
                entry["meFor"].append(programme)
        entry["school"] = entry["school"] or _clean(cell(record, COL_SCHOOL))
        entry["dept"] = entry["dept"] or _clean(cell(record, COL_DEPT))
    book.close()

    for code in scoping:
        scoping[code]["majorFor"].sort()
        scoping[code]["meFor"].sort()
    return data, scoping


def attach_scoping(courses: list[dict], scoping: dict[str, dict]) -> int:
    """Adds the workbook's own programme scoping to each course.

    `majorFor`/`meFor` are the Office's authoritative answer to "whose major
    core is this, and who may take it as a major elective" - a question the
    frontend previously had to guess at by substring-matching department
    names, which silently failed for every programme whose public page
    publishes no department keywords. School/department strings are refreshed
    from the workbook too, since it is a more direct source than the mirror.
    """
    attached = 0
    for course in courses:
        entry = scoping.get(course["code"])
        if not entry:
            continue
        course["majorFor"] = entry["majorFor"]
        course["meFor"] = entry["meFor"]
        if entry["school"]:
            course["school"] = entry["school"]
        if entry["dept"]:
            course["dept"] = entry["dept"]
        attached += 1
    return attached


def historical_fallback(active_by_code: dict[str, dict]) -> dict[str, dict]:
    """Course identity (credits/category) for codes absent from the ACTIVE
    dataset but present in an archived one, newest archive first.

    A course can drop out of one revision and come back in the next - both
    DES4001 and HIS102 did exactly that between the 08-09 mirror scrape and
    this workbook. Matching only against the active dataset would treat the
    returning course as brand new and emit `cr: null`, and a null credit is
    not a cosmetic gap: it crashed a real wishlist solve with
    `TypeError: float() argument must be... not 'NoneType'` (CLAUDE.md s.14).
    Every archived version is a dataset this project itself published, so
    reusing its credits is carrying forward a known value, not inventing one.
    """
    fallback: dict[str, dict] = {}
    try:
        manifest = json.loads(apply_mod.MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    for entry in reversed(manifest.get("versions", [])):
        courses_path = apply_mod.VERSIONS_DIR / entry.get("version_id", "") / "courses.json"
        if not courses_path.is_file():
            continue
        try:
            archived = json.loads(courses_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for course in archived:
            code = course.get("code")
            if code and code not in active_by_code and code not in fallback:
                fallback[code] = course
    return fallback


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", type=Path)
    ap.add_argument("--version-id", default=None,
                    help="dataset version id (default: date plus source-checksum suffix)")
    ap.add_argument("--label", default="Academic Office timetable workbook",
                    help="human-readable source name recorded in the manifest")
    ap.add_argument("--apply", action="store_true",
                    help="activate this version after it validates with zero errors")
    args = ap.parse_args()

    if not args.workbook.is_file():
        print(f"ERROR: no such workbook: {args.workbook}", file=sys.stderr)
        return 2

    raw_bytes = args.workbook.read_bytes()
    source_checksum = hashlib.sha256(raw_bytes).hexdigest()[:16]
    retrieved_at = datetime.now(timezone.utc).isoformat()
    # Date-only ids collided when a workbook was reissued or the importer was
    # rerun later on the same day.  The short source hash is deterministic for
    # identical bytes and unique for a genuinely new snapshot.
    version_id = args.version_id or (
        f"monsoon-2026-office-xlsx-{retrieved_at[:10]}-{source_checksum[:8]}"
    )

    try:
        data, scoping = read_workbook(args.workbook)
    except WorkbookError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    existing = json.loads(apply_mod.BACKEND_COURSES_PATH.read_text(encoding="utf-8"))
    existing_by_code = {c["code"]: c for c in existing}
    revived = historical_fallback(existing_by_code)
    if revived:
        print(f"history       : {len(revived)} code(s) available from archived versions for "
              f"carry-forward if the workbook lists them again")

    result = normalize_mod.normalize(data, {**revived, **existing_by_code})
    attached = attach_scoping(result.courses, scoping)
    # attach_scoping mutates the course dicts, so the hash must be recomputed
    # from the final content rather than reusing normalize()'s own value.
    from app.domain.catalog import canonical_checksum
    dataset_checksum = canonical_checksum(result.courses)

    stats = result.stats
    errors = [i for i in stats.issues if i.level == "error"]
    warnings = [i for i in stats.issues if i.level == "warning"]
    package_count = sum(len(c.get("pk", [])) for c in result.courses)

    print(f"source        : {args.workbook.name} ({len(raw_bytes):,} bytes, sha {source_checksum})")
    print(f"version id    : {version_id}")
    print(f"courses       : {len(result.courses)}")
    print(f"packages      : {package_count}")
    print(f"scoping added : {attached} course(s) carry majorFor/meFor from the workbook")
    print(f"errors        : {len(errors)}")
    print(f"warnings      : {len(warnings)}")
    for issue in errors[:20]:
        print(f"  ERROR   {issue.code}: {issue.message}")
    for issue in warnings[:12]:
        print(f"  warning {issue.code}: {issue.message}")
    if len(warnings) > 12:
        print(f"  ... and {len(warnings) - 12} more warning(s)")

    diff = diff_datasets(existing, result.courses)
    summary = diff["summary"]
    print(f"diff vs active: +{summary['added']} added, -{summary['removed']} removed, "
          f"{summary['renamed']} renamed, {summary['changed']} changed, "
          f"{summary['unchanged']} unchanged")

    manifest_entry = {
        "version_id": version_id,
        "source_name": args.label,
        "source_url": None,
        "source_file": args.workbook.name,
        "retrieved_at": retrieved_at,
        "source_checksum": source_checksum,
        "dataset_checksum": dataset_checksum,
        "importer_version": "xlsx-1.0.0",
        "effective_semester": "Monsoon 2026",
        "course_count": len(result.courses),
        "package_count": package_count,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "validation_status": "has_errors" if errors else "clean",
    }
    provenance = dict(result.provenance)
    provenance["source"] = {
        "kind": "academic_office_xlsx", "file": args.workbook.name,
        "checksum": source_checksum, "retrieved_at": retrieved_at, "label": args.label,
    }

    try:
        out_dir = apply_mod.stage_version(version_id, result.courses, provenance, manifest_entry)
    except apply_mod.ApplyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    (out_dir / "diff_vs_previous_active.json").write_text(json.dumps(diff, indent=2), encoding="utf-8")
    print(f"staged        : {out_dir}")

    if not args.apply:
        print("not applied (re-run with --apply to activate)")
        return 1 if errors else 0
    if errors:
        print("REFUSING to apply: the candidate has validation errors", file=sys.stderr)
        return 1
    applied = apply_mod.apply_version(version_id, expected_checksum=dataset_checksum)
    print(f"APPLIED       : active version is now {applied.get('active_version', version_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
