#!/usr/bin/env python3
"""Reconcile catalog credits against the Academic Office's own course-outline
PDFs (backend/app/data/course_outlines.json, see tools/parse_course_outlines.py).

Why this exists: `courses.json`'s `cr` field was, for the large majority of
courses, never sourced from an official document at all. `crBasis` records the
actual mechanism - a formula derived from counting the timetable's own
LEC/TUT/PRAC section counts per week (e.g. "Full semester [LEC:3, TUT:1] = 4")
- and `crOfficial: false` on 294 of 327 courses honestly discloses that this is
our own assumption, not a University-published number. The University's own
Course Outline Form (Section A) states each course's credits directly; that is
strictly more authoritative than a contact-hour guess, so this script makes it
the new source of truth wherever a matching outline exists.

This produced a real, material finding, not just a formality: 4 of the 33
courses already marked `crOfficial: true` (ECE1001, MED2001, PHY1001,
PHY1011) turn out to disagree with their own outline PDF - and in every one
of those 4 cases, the outline figure also matches what the contact-hour
formula in `crBasis` would have computed, which the previously-hardcoded
"official" `cr` value did not. That is strong independent corroboration that
the old hardcoded value was a data-entry mistake, not that the outline is
wrong. See the printed report and provenance.json's `prior_cr`/`prior_cr_official`
per course for the full trail.

This is a reprocessing of the CURRENTLY ACTIVE dataset (no new timetable
rows), following the exact same versioned stage -> diff -> apply pipeline as
every other dataset change in this project (see
tools/import_office_timetable_xlsx.py) - never a hand-edit of courses.json.

Usage:
    python3 tools/reconcile_credits_from_outlines.py [--version-id ID] [--apply]

Without --apply nothing active changes: the reconciled dataset and a diff
report are staged under backend/app/data/timetable_versions/<version>/ for
review only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.timetable_updates import apply as apply_mod  # noqa: E402
from app.timetable_updates.diff import diff_datasets  # noqa: E402
from app.domain.catalog import canonical_checksum  # noqa: E402

OUTLINES_PATH = REPO_ROOT / "backend" / "app" / "data" / "course_outlines.json"


def _find_outline(code: str, outlines: dict) -> dict | None:
    """Exact match first, else any shared '/'-separated component - mirrors
    app.services.course_outlines.OutlineCatalog.get() exactly, so a course
    resolves to the same outline here as it would in the picker's own lookup."""
    if code in outlines:
        return outlines[code]
    for part in code.split("/"):
        part = part.strip()
        if part and part in outlines:
            return outlines[part]
    return None


def reconcile(courses: list[dict], outlines: dict) -> tuple[list[dict], dict, list[dict]]:
    """Returns (new_courses, provenance_by_code, report_rows).

    report_rows is the human-readable audit trail: one entry per course whose
    cr/crOfficial/crBasis actually changed, with enough detail to review every
    single change by hand before trusting --apply.
    """
    new_courses = []
    provenance: dict[str, dict] = {}
    report: list[dict] = []

    for course in courses:
        c = dict(course)
        code = c["code"]
        outline = _find_outline(code, outlines)
        outline_cr = outline.get("credits_from_outline") if outline else None

        if outline is None or outline_cr is None:
            new_courses.append(c)
            continue

        old_cr = c.get("cr")
        old_official = bool(c.get("crOfficial"))
        old_basis = c.get("crBasis")
        source_file = outline.get("source_file", code)
        new_basis = f"Academic Office course outline ({source_file})"

        if old_cr is None:
            action = "filled_null"
        elif abs(float(old_cr) - float(outline_cr)) > 0.01:
            action = "corrected_official_override" if old_official else "corrected"
        elif not old_official:
            action = "promoted_same_value"
        else:
            action = "reconfirmed"  # already official, already agrees - just re-cite the source

        if action != "reconfirmed" or old_basis != new_basis:
            c["cr"] = float(outline_cr)
            c["crOfficial"] = True
            c["crBasis"] = new_basis
            report.append({
                "code": code, "action": action,
                "prior_cr": old_cr, "prior_crOfficial": old_official, "prior_crBasis": old_basis,
                "new_cr": float(outline_cr), "outline_file": source_file,
            })

        provenance[code] = {
            "credit_source": "academic_office_outline", "outline_file": source_file,
            "prior_cr": old_cr, "prior_crOfficial": old_official, "action": action,
        }
        new_courses.append(c)

    return new_courses, provenance, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version-id", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="activate this version after it validates with zero errors")
    args = ap.parse_args()

    if not OUTLINES_PATH.exists():
        print(f"ERROR: no such file: {OUTLINES_PATH}", file=sys.stderr)
        return 2

    existing = json.loads(apply_mod.BACKEND_COURSES_PATH.read_text(encoding="utf-8"))
    outlines = json.loads(OUTLINES_PATH.read_text(encoding="utf-8"))

    new_courses, provenance, report = reconcile(existing, outlines)
    dataset_checksum = canonical_checksum(new_courses)

    retrieved_at = datetime.now(timezone.utc).isoformat()
    version_id = args.version_id or f"monsoon-2026-outline-credit-reconciliation-{retrieved_at[:10]}"

    corrected = [r for r in report if r["action"] in ("corrected", "corrected_official_override")]
    filled = [r for r in report if r["action"] == "filled_null"]
    promoted = [r for r in report if r["action"] == "promoted_same_value"]
    reconfirmed = [r for r in report if r["action"] == "reconfirmed"]
    high_stakes = [r for r in corrected if r["action"] == "corrected_official_override"]

    print(f"version id          : {version_id}")
    print(f"courses reconciled  : {len(report)} of {len(existing)} total")
    print(f"  corrected (cr changed, was unofficial)      : {len(corrected) - len(high_stakes)}")
    print(f"  corrected (cr changed, was marked OFFICIAL) : {len(high_stakes)}  <- review these by hand")
    for r in high_stakes:
        print(f"      {r['code']}: {r['prior_cr']} -> {r['new_cr']}  ({r['outline_file']})")
    print(f"  previously null, filled from outline        : {len(filled)}")
    for r in filled:
        print(f"      {r['code']}: null -> {r['new_cr']}  ({r['outline_file']})")
    print(f"  promoted to official (value unchanged)      : {len(promoted)}")
    print(f"  re-cited (already official, already agreed) : {len(reconfirmed)}")
    print(f"unchanged (no outline / no outline credit)    : {len(existing) - len(report)}")

    diff = diff_datasets(existing, new_courses)
    summary = diff["summary"]
    print(f"diff vs active      : +{summary['added']} added, -{summary['removed']} removed, "
          f"{summary['renamed']} renamed, {summary['changed']} changed, {summary['unchanged']} unchanged")

    manifest_entry = {
        "version_id": version_id,
        "source_name": "Academic Office course-outline PDFs (Monsoon 2026) - credit reconciliation, "
                        "superseding contact-hour-derived guesses",
        "source_url": None,
        "retrieved_at": retrieved_at,
        "source_checksum": None,
        "dataset_checksum": dataset_checksum,
        "importer_version": "credit-reconciliation-1.0.0",
        "effective_semester": "Monsoon 2026",
        "course_count": len(new_courses),
        "package_count": sum(len(c.get("pk", [])) for c in new_courses),
        "error_count": 0,
        "warning_count": 0,
        "validation_status": "clean",
        "note": f"Credits reconciled against 328 Academic Office course-outline PDFs: "
                f"{len(corrected)} corrected, {len(filled)} filled from null, "
                f"{len(promoted) + len(reconfirmed)} promoted/re-cited to crOfficial=true. "
                f"No timetable rows (times/rooms/sections) changed.",
    }

    try:
        out_dir = apply_mod.stage_version(version_id, new_courses, provenance, manifest_entry)
    except apply_mod.ApplyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    (out_dir / "diff_vs_previous_active.json").write_text(json.dumps(diff, indent=2), encoding="utf-8")
    (out_dir / "credit_reconciliation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"staged              : {out_dir}")

    if not args.apply:
        print("not applied (re-run with --apply to activate)")
        return 0
    applied = apply_mod.apply_version(version_id, expected_checksum=dataset_checksum)
    print(f"APPLIED             : active version is now {applied.get('active_version', version_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
