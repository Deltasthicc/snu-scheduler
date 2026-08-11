"""Course-outline lookup: the Academic Office's per-course outline PDFs
(objectives, weekly syllabus, grading breakdown, prerequisites), parsed by
tools/parse_course_outlines.py into backend/app/data/course_outlines.json.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.course_outlines import OutlineCatalog


def test_catalog_loads_a_real_outline_by_its_own_code():
    catalog = OutlineCatalog()
    assert "AMP1001" in catalog.codes()
    outline = catalog.get("AMP1001")
    assert outline is not None
    assert outline["code"] == "AMP1001"


def test_catalog_resolves_a_cross_listed_catalog_code_via_shared_component():
    # The timetable's own catalog joins ART202 and AMP1001 as one cross-listed
    # course ("ART202/AMP1001"); the Office files one outline PDF under just
    # AMP1001's own code. A lookup by the catalog's combined code must still
    # find it - this is exactly what a course-picker row for this course
    # would ask for.
    catalog = OutlineCatalog()
    outline = catalog.get("ART202/AMP1001")
    assert outline is not None
    assert outline["code"] == "AMP1001"


def test_catalog_returns_none_for_an_unknown_code():
    catalog = OutlineCatalog()
    assert catalog.get("NOT-A-REAL-COURSE") is None


def test_catalog_never_stores_a_blank_instructions_template_as_a_real_outline():
    """9 files in the source zip were the Office's own blank instructions
    template uploaded under a real course's filename by mistake, not a
    filled outline - confirmed directly against the source PDFs. None of the
    codes they were misnamed under should carry that template's boilerplate
    as if it described the course."""
    catalog = OutlineCatalog()
    for code in ("ECO373", "ECO495", "MAT1006", "MED2003", "PHY2003"):
        outline = catalog.get(code)
        if outline is not None:
            assert outline.get("introduction") != "General Guidelines"


def test_no_outline_field_contains_scrambled_table_extraction_text():
    """A minority of source PDFs extract with pdfplumber's table-cell text
    interleaved within words ("CNOonUeRSE" instead of "COURSE") - confirmed
    directly against the source PDFs, and against plain linear-text
    extraction of the SAME files reading perfectly cleanly. The parser drops
    any field that fails a garble check rather than shipping scrambled text;
    this is the regression test for that check never regressing to "ship it
    anyway"."""
    import re

    def garble_score(text: str) -> float:
        words = text.split()
        if not words:
            return 0.0
        bad = 0
        for w in words:
            core = re.sub(r"[^A-Za-z]", "", w)
            if len(core) < 4:
                continue
            transitions = sum(1 for i in range(1, len(core)) if core[i - 1].islower() and core[i].isupper())
            if transitions >= 2:
                bad += 1
        return bad / len(words)

    catalog = OutlineCatalog()
    text_fields = ("title_from_outline", "faculty", "department", "prerequisites",
                  "objectives", "learning_outcomes", "introduction")
    for code in catalog.codes():
        outline = catalog.get(code)
        for field in text_fields:
            value = outline.get(field)
            if isinstance(value, str):
                assert garble_score(value) <= 0.08, f"{code}.{field} looks garbled: {value!r}"


def test_outline_codes_endpoint_returns_a_real_nonempty_list():
    with TestClient(app) as client:
        r = client.get("/api/v1/course-outlines")
        assert r.status_code == 200, r.text
        codes = r.json()["codes"]
        assert len(codes) > 100
        assert "AMP1001" in codes


def test_outline_lookup_endpoint_finds_a_real_outline():
    with TestClient(app) as client:
        r = client.post("/api/v1/course-outlines/lookup", json={"code": "AMP1001"})
        assert r.status_code == 200, r.text
        assert r.json()["code"] == "AMP1001"


def test_outline_lookup_endpoint_resolves_a_slash_joined_catalog_code():
    with TestClient(app) as client:
        r = client.post("/api/v1/course-outlines/lookup", json={"code": "ART202/AMP1001"})
        assert r.status_code == 200, r.text
        assert r.json()["code"] == "AMP1001"


def test_outline_lookup_endpoint_404s_for_an_unknown_code():
    with TestClient(app) as client:
        r = client.post("/api/v1/course-outlines/lookup", json={"code": "NOT-A-REAL-COURSE"})
        assert r.status_code == 404


def test_outline_lookup_endpoint_422s_when_code_is_missing():
    with TestClient(app) as client:
        r = client.post("/api/v1/course-outlines/lookup", json={})
        assert r.status_code == 422
