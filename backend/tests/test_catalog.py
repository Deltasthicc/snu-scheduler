from __future__ import annotations
import filecmp
from pathlib import Path

from app.domain import catalog

FRONTEND_SRC = Path(__file__).resolve().parent.parent.parent / "frontend" / "src" / "data.json"
BACKEND_COPY = Path(__file__).resolve().parent.parent / "app" / "data" / "courses.json"


def test_catalog_loads_326_courses():
    assert len(catalog.all_courses()) == 326


def test_catalog_has_988_packages():
    # was 859 under monsoon-2026-excel-v1; 988 under the revised
    # monsoon-2026-netlify-revision-2026-08-04 dataset - see
    # docs/TIMETABLE_REVISION_DIFF_2026-08-04.md for the full diff.
    assert sum(len(c["pk"]) for c in catalog.all_courses()) == 988


def test_course_codes_are_unique():
    codes = [c["code"] for c in catalog.all_courses()]
    assert len(codes) == len(set(codes))


def test_get_course_returns_none_for_unknown_code():
    assert catalog.get_course("NOT-A-REAL-COURSE") is None


def test_get_courses_silently_drops_unknown_codes():
    real = catalog.all_courses()[0]["code"]
    got = catalog.get_courses([real, "NOT-A-REAL-COURSE"])
    assert list(got.keys()) == [real]


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
