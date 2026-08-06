"""Demand-model calibration: the popularity multiplier and its cap, and the
removal of the automatic MUST-priority -> graduation_critical demand bump.
Neither had any test coverage before this file - a real gap, given every
other module in this codebase has had at least one bug caught by testing."""
from __future__ import annotations

from app.simulation.engine import popularity, expected_rivals, POPULARITY_CAP, MODES


def test_popularity_cap_fires_when_several_factors_stack():
    course = {"category": "CCC", "seats": 20, "convenient_slot": True,
              "title": "Machine Learning for Everyone"}
    p = popularity(course, {"in_specialisation": True})
    assert p["capped"] is True
    assert p["multiplier"] == POPULARITY_CAP


def test_popularity_cap_does_not_fire_for_a_single_factor():
    course = {"category": "ME", "seats": 80, "section_count": 3}
    p = popularity(course, {})
    assert p["capped"] is False
    assert p["multiplier"] == 1.0


def test_popularity_cap_leaves_a_lightly_stacked_course_uncapped():
    # sole_section only (1.2x) - well under the cap, should pass through untouched
    course = {"category": "ME", "seats": 80, "section_count": 1}
    p = popularity(course, {})
    assert p["capped"] is False
    assert p["multiplier"] == 1.2


def test_user_estimate_is_applied_after_the_cap_and_is_not_itself_capped():
    course = {"category": "CCC", "seats": 20, "convenient_slot": True,
              "title": "Machine Learning for Everyone"}
    p = popularity(course, {"in_specialisation": True, "user_popularity": 2.0})
    assert p["multiplier"] == POPULARITY_CAP * 2.0


def test_priority_no_longer_inflates_modelled_demand_on_its_own():
    # Rectified 2026-08-05 session: "MUST priority" is no longer silently treated as
    # "everyone else also desperately wants this" - that conflated the caller's own
    # priority with a claim about rival demand no one actually asserted. A caller
    # that wants extra demand must say so explicitly via graduation_critical/opts,
    # not by tying it to the bid-sizing priority tier.
    course = {"category": "ME", "seats": 80, "section_count": 1}
    a = expected_rivals(course, "HIGH", {"graduation_critical": True})
    b = expected_rivals(course, "HIGH", {})
    assert a["lambda"] == b["lambda"]


def test_stress_modes_always_float_rivals_above_seats():
    course = {"category": "ME", "seats": 50, "section_count": 4}
    for mid in ("HIGH", "VERY_HIGH", "EXTREME"):
        er = expected_rivals(course, mid, {})
        assert er["lambda"] + 1 >= 50 * 1.25 - 1e-9


def test_optimistic_mode_is_flagged_comparison_only_and_can_undersubscribe():
    course = {"category": "ME", "seats": 200, "section_count": 4}
    er = expected_rivals(course, "OPTIMISTIC", {})
    assert MODES["OPTIMISTIC"].comparison_only is True
    assert er["lambda"] + 1 < 200
