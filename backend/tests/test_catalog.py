from __future__ import annotations
import filecmp
from pathlib import Path

from app.domain import catalog

FRONTEND_SRC = Path(__file__).resolve().parent.parent.parent / "frontend" / "src" / "data.json"
BACKEND_COPY = Path(__file__).resolve().parent.parent / "app" / "data" / "courses.json"


def test_catalog_loads_326_courses():
    assert len(catalog.all_courses()) == 326


def test_catalog_has_954_packages():
    # 859 under monsoon-2026-excel-v1; 988 under monsoon-2026-netlify-revision-
    # 2026-08-04 (see docs/TIMETABLE_REVISION_DIFF_2026-08-04.md); now 954
    # under monsoon-2026-batch-coherence-fix-2026-08-07, which reprocesses that
    # same 08-04 source with a fixed build_packages(): the old cross-product
    # ignored per-section batch tags and offered 34 combinations across 8
    # courses that mixed sections restricted to disjoint student batches
    # (e.g. CSD211/CSD2003's CSD21-only practical crossed with its CSD23/24-
    # only tutorial) as though they were valid packages a real student could
    # actually be enrolled in.
    assert sum(len(c["pk"]) for c in catalog.all_courses()) == 954


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
