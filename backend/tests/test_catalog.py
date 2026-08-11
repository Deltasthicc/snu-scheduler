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
    # 326 through monsoon-2026-batch-coherence-fix-2026-08-07; now 325 under
    # monsoon-2026-netlify-revision-2026-08-09 (caught up 2026-08-09/10 after
    # discovering the live site had moved on 5 times - 08-04/05/06/07/09 -
    # while our poller kept staging candidates nobody reviewed). Net -1: +1
    # course (MAT205/MAT2004), -2 courses (DES4001, HIS102), 3 renames
    # (CCC685/CCC2302->CCC685, CSD102/CSD1002->CSD102/CSD2001,
    # ECO2101->ECO2101/ECO221). Now 327 under
    # monsoon-2026-office-draft-2026-08-11, the Academic Office's own draft
    # workbook (mailed to students for the 11 Aug review window) rather than a
    # scrape of the public mirror: DES4001 and HIS102 are back, and both kept
    # their real 4.0 credits because the importer now carries values forward
    # from ARCHIVED versions, not just the active one.
    assert len(catalog.all_courses()) == 327


def test_catalog_has_987_packages():
    # 859 under monsoon-2026-excel-v1; 988 under monsoon-2026-netlify-revision-
    # 2026-08-04; 954 under monsoon-2026-batch-coherence-fix-2026-08-07 (that
    # session's own batch-coherence fix to build_packages()); 555 under
    # monsoon-2026-netlify-revision-2026-08-09 (the University now tags each
    # of PHY1011's sections to one of 8 specific first-year batches instead of
    # publishing them untagged, so the batch-coherence check correctly stops
    # treating every LECxTUTxPRAC cross-product as valid for every student);
    # now 559 under monsoon-2026-netlify-revision-2026-08-10, auto-applied by
    # the live poller (no courses added/removed/renamed) after ECE1001 and
    # PHY1011 each gained one new LEC+PRAC(+TUT) section - two brand-new
    # course sections opened, not a data-quality regression. Now 987 under
    # monsoon-2026-office-draft-2026-08-11. That jump is NOT a package-builder
    # regression - it is a real gap in the Office's draft workbook, which
    # dropped the "Student Block" tagging for nearly every first-year course
    # (18 of 332 first-year rows carry one, against 229 of 277 second-year
    # rows). PHY1011 alone goes 10 -> 372 because an untagged course is,
    # correctly, treated as every LECxTUTxPRAC combination being open to
    # everyone. No batch tags are fabricated to hide it; the importer instead
    # raises IMPLAUSIBLE_PACKAGE_COUNT so it can be reported upstream.
    assert sum(len(c["pk"]) for c in catalog.all_courses()) == 987


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
