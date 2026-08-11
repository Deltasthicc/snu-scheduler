"""A course with no published credit must degrade, never crash.

The University publishes timetables with no credit column, so a course that
appears for the first time - with no prior dataset entry to carry a value
forward from - genuinely has `cr: null` until a credit is published. That is
the honest representation and must not be replaced by a guessed number.

It was, however, a live HTTP 500: `float(course.get("cr", 0))` looks safe but
the key EXISTS with a null value, so the 0 default never applies and
float(None) raises. Two real courses in the shipped dataset
(CCC396/CCC2315 and MAT205/MAT2004) hit this, meaning any student who put
either in a wishlist during enrolment week got a server error.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.domain import catalog
from app.main import app


def test_credits_of_never_raises_on_a_null_or_missing_credit():
    assert catalog.credits_of({"cr": None}) == 0.0
    assert catalog.credits_of({}) == 0.0
    assert catalog.credits_of(None) == 0.0
    assert catalog.credits_of({"cr": "not a number"}) == 0.0
    assert catalog.credits_of({"cr": None}, default=1.5) == 1.5
    # a real value must still come through untouched, including as a string
    assert catalog.credits_of({"cr": 4}) == 4.0
    assert catalog.credits_of({"cr": "1.5"}) == 1.5


def test_null_credit_courses_are_still_representable_in_the_dataset():
    """Guards the honesty half of this: the fix must not have silently
    back-filled a fabricated credit into the shipped data. If the University
    later publishes credits for these, this test simply stops finding any
    null and still passes."""
    for course in catalog.all_courses():
        if course.get("cr") is None:
            assert course.get("crBasis"), (
                f"{course['code']} has no credit and no crBasis explaining why")


def test_wishlist_validate_does_not_500_on_a_course_with_no_published_credit():
    unknown = [c["code"] for c in catalog.all_courses() if c.get("cr") is None]
    if not unknown:
        import pytest
        pytest.skip("no null-credit course in the active dataset to exercise")
    with TestClient(app) as client:
        for code in unknown:
            response = client.post("/api/v1/wishlists/validate", json={
                "items": [{"code": code, "intent": "must_have"}],
                "choice_groups": [], "fixed_credits": 0,
            })
            assert response.status_code == 200, f"{code}: {response.text[:200]}"
