from __future__ import annotations
import filecmp
import json
from pathlib import Path

from app.domain import catalog
from app.timetable_updates.diff import diff_datasets

FRONTEND_SRC = Path(__file__).resolve().parent.parent.parent / "frontend" / "src" / "data.json"
BACKEND_COPY = Path(__file__).resolve().parent.parent / "app" / "data" / "courses.json"
VERSIONS = BACKEND_COPY.parent / "timetable_versions"


def test_catalog_loads_327_courses():
    # 326 through monsoon-2026-batch-coherence-fix-2026-08-07; 325 under
    # monsoon-2026-netlify-revision-2026-08-09; 327 under
    # monsoon-2026-office-draft-2026-08-11 (the Academic Office's own draft
    # workbook); 328 under monsoon-2026-netlify-revision-2026-08-11 (Dean
    # Academics' "timetable updated" email, live site re-synced). Now back
    # to 327 under monsoon-2026-netlify-revision-2026-08-12 - the "final"
    # timetable the Student Council's own email flagged as having "minimal
    # changes": HIS102 and the previously-separate IHS1003 entry consolidated
    # into one cross-listed course (HIS102/IHS1003), a real net -1, not a
    # data-loss regression.
    assert len(catalog.all_courses()) == 327


def test_catalog_has_563_packages():
    # 859 under monsoon-2026-excel-v1; 988 under monsoon-2026-netlify-revision-
    # 2026-08-04; 954 under monsoon-2026-batch-coherence-fix-2026-08-07 (that
    # session's own batch-coherence fix to build_packages()); 555 under
    # monsoon-2026-netlify-revision-2026-08-09 (the University now tags each
    # of PHY1011's sections to one of 8 specific first-year batches instead of
    # publishing them untagged, so the batch-coherence check correctly stops
    # treating every LECxTUTxPRAC cross-product as valid for every student);
    # 987 under monsoon-2026-office-draft-2026-08-11, NOT a regression but a
    # real gap in the Office's draft workbook, which dropped "Student Block"
    # tagging for nearly every first-year course; 564 under
    # monsoon-2026-netlify-revision-2026-08-11, the live site's re-sync
    # bringing real batch tagging back. Now 563 under
    # monsoon-2026-netlify-revision-2026-08-12 - a handful of real section
    # time/room moves (ART342, ECE1001, INT104/INT2002, MGT2001, PHY1007,
    # STM2001 all show packages_moved in the diff) plus the HIS102/IHS1003
    # consolidation losing IHS1003's own separately-counted packages, not a
    # data-quality regression.
    assert sum(len(c["pk"]) for c in catalog.all_courses()) == 563


def test_course_codes_are_unique():
    codes = [c["code"] for c in catalog.all_courses()]
    assert len(codes) == len(set(codes))


def test_get_course_returns_none_for_unknown_code():
    assert catalog.get_course("NOT-A-REAL-COURSE") is None


def test_get_courses_silently_drops_unknown_codes():
    real = catalog.all_courses()[0]["code"]
    got = catalog.get_courses([real, "NOT-A-REAL-COURSE"])
    assert list(got.keys()) == [real]


def test_csd211_packages_are_batch_coherent_not_a_cross_batch_cross_product():
    """CSD211/CSD2003 is the exact course the Student Council's own grievance
    deck flagged: which tutorial (T1 vs T2) a student's elective clashes with
    depends on which batch they're in, because T1 (CSD21/22) and T2 (CSD23/24)
    are genuinely different sections, not a free choice. Before the
    batch-coherence fix this course offered 8 packages - one of which paired
    CSD21's own practical with CSD23/24's tutorial, a combination no CSD21
    student could ever actually be enrolled in. Pinned here at the live
    catalog level, not just in the normalizer's own unit tests, so a future
    re-import can't quietly regress it."""
    course = catalog.get_course("CSD211/CSD2003")
    assert course is not None
    assert len(course["pk"]) == 4
    batches_per_package = {tuple(sorted(p.get("batches", []))) for p in course["pk"]}
    assert batches_per_package == {("CSD21",), ("CSD22",), ("CSD23",), ("CSD24",)}


def test_credits_reconciled_against_outline_pdfs_not_the_contact_hour_guess():
    """4 courses were hardcoded `crOfficial: true` with a credit value that
    turned out to be a data-entry mistake - confirmed against the Academic
    Office's own course-outline PDFs directly, byte for byte, before this
    was trusted (see tools/reconcile_credits_from_outlines.py and CLAUDE.md
    s.22). Pinned at the live catalog level so a future re-import that
    carries the *old* wrong value forward (the normal, correct behaviour for
    a course the new source doesn't mention) can't silently regress this."""
    expected = {
        "ECE1001": 3.0, "MED2001": 3.0, "PHY1001": 1.0, "PHY1011": 5.0,
        "MAT205/MAT2004": 3.0,  # previously null - the exact course test_null_credits.py exists for
    }
    for code, cr in expected.items():
        course = catalog.get_course(code)
        assert course is not None, code
        assert course["cr"] == cr, f"{code}: expected {cr}, got {course['cr']}"
        assert course["crOfficial"] is True, f"{code}: should be sourced from the outline PDF"


def test_office_draft_audit_diff_matches_the_actual_previous_version():
    previous = json.loads((VERSIONS / "monsoon-2026-netlify-revision-2026-08-10" /
                           "courses.json").read_text(encoding="utf-8"))
    office_dir = VERSIONS / "monsoon-2026-office-draft-2026-08-11"
    current = json.loads((office_dir / "courses.json").read_text(encoding="utf-8"))
    recorded = json.loads((office_dir / "diff_vs_previous_active.json").read_text(encoding="utf-8"))
    expected = diff_datasets(previous, current)

    assert recorded == expected
    assert recorded["summary"] == {
        "renamed": 0, "added": 2, "removed": 0, "changed": 325, "unchanged": 0,
    }
    assert recorded["added_courses"] == ["DES4001", "HIS102"]


def test_backend_copy_matches_frontend_source():
    """Drift detector: app/data/courses.json must stay byte-identical to
    frontend/src/data.json (run scripts/sync_course_data.py after editing the
    frontend copy). Skipped when the frontend source isn't present, e.g. a
    container image built from backend/ alone with no sibling frontend/ dir."""
    if not FRONTEND_SRC.exists():
        import pytest
        pytest.skip("frontend/src/data.json not present in this build context")
    assert filecmp.cmp(FRONTEND_SRC, BACKEND_COPY, shallow=False), (
        "backend/app/data/courses.json has drifted from frontend/src/data.json - "
        "run backend/scripts/sync_course_data.py"
    )
