"""Tests for app/services/credit_policy.py - the five-distinct-numbers credit
ceiling model (official / personal target / overload / fixed / wishlist room).
"""
from __future__ import annotations
import pytest

from app.services.credit_policy import CreditPolicyError, STANDARD_CEILING, resolve_ceiling


def test_first_year_profile_uses_standard_ceiling():
    r = resolve_ceiling(fixed_credits=10, personal_target=18, min_credits=15)
    assert r.ceiling_mode == "standard"
    assert r.active_ceiling == STANDARD_CEILING == 25
    assert r.is_overload is False
    assert r.wishlist_room == 15


def test_second_year_profile_uses_standard_ceiling():
    r = resolve_ceiling(fixed_credits=12, personal_target=22, min_credits=16)
    assert r.ceiling_mode == "standard"
    assert r.active_ceiling == 25


def test_third_year_profile_uses_standard_ceiling():
    r = resolve_ceiling(fixed_credits=14, personal_target=24, min_credits=18)
    assert r.ceiling_mode == "standard"
    assert r.active_ceiling == 25


def test_fourth_year_standard_ceiling_without_overload():
    r = resolve_ceiling(fixed_credits=15, personal_target=24, min_credits=20)
    assert r.ceiling_mode == "standard"
    assert r.active_ceiling == 25
    assert r.is_overload is False


def test_fourth_year_30_credit_planning_scenario_is_labelled_what_if():
    r = resolve_ceiling(fixed_credits=15, personal_target=28, min_credits=20, overload_ceiling=30)
    assert r.ceiling_mode == "what_if"
    assert r.active_ceiling == 30
    assert r.is_overload is True
    assert r.overload_confirmed is False
    assert any("only a planning what-if" in w for w in r.warnings)
    assert "what-if" in r.summary
    assert "not a confirmed University rule" in r.summary


def test_year4_dean_extension_requires_all_recorded_conditions():
    r = resolve_ceiling(fixed_credits=15, personal_target=28, min_credits=20,
                        overload_ceiling=30, current_year=4, eligibility_confirmed=True,
                        advisor_recommended=True, dean_approved=True)
    assert r.ceiling_mode == "dean_extension"
    assert not any("planning what-if" in w for w in r.warnings)
    assert "Dean approval recorded" in r.summary


def test_other_years_can_only_model_extension_as_what_if():
    r = resolve_ceiling(fixed_credits=10, personal_target=27, min_credits=20, overload_ceiling=28)
    assert r.is_overload is True
    assert r.active_ceiling == 28
    assert r.ceiling_mode == "what_if"
    assert any("Year IV status" in w for w in r.warnings)


def test_year4_plus_two_does_not_require_dean_approval():
    r = resolve_ceiling(fixed_credits=10, personal_target=27, min_credits=12,
                        overload_ceiling=27, current_year=4,
                        eligibility_confirmed=True, advisor_recommended=True)
    assert r.ceiling_mode == "advisor_extension"
    assert r.overload_confirmed is True
    assert "Dean approval is not required" in r.summary


def test_extension_above_thirty_is_rejected():
    with pytest.raises(CreditPolicyError):
        resolve_ceiling(fixed_credits=10, personal_target=30, min_credits=12,
                        overload_ceiling=31, current_year=4)


def test_invalid_ceiling_below_standard_is_rejected():
    with pytest.raises(CreditPolicyError):
        resolve_ceiling(fixed_credits=10, personal_target=20, min_credits=15, overload_ceiling=20)


def test_ceiling_below_fixed_credits_is_rejected():
    with pytest.raises(CreditPolicyError):
        resolve_ceiling(fixed_credits=28, personal_target=20, min_credits=15)


def test_negative_credits_are_rejected():
    with pytest.raises(CreditPolicyError):
        resolve_ceiling(fixed_credits=-1, personal_target=20, min_credits=15)


def test_decimal_and_half_semester_credits():
    r = resolve_ceiling(fixed_credits=13.5, personal_target=21.5, min_credits=16.5)
    assert r.active_ceiling == 25
    assert r.wishlist_room == 11.5
    assert "13.5" in r.summary
    assert "11.5" in r.summary


def test_min_credits_leaving_no_room_produces_a_warning_not_a_crash():
    r = resolve_ceiling(fixed_credits=20, personal_target=24, min_credits=6)
    assert any("no room under the active ceiling" in w for w in r.warnings)


def test_personal_target_above_ceiling_is_clamped_with_a_warning():
    r = resolve_ceiling(fixed_credits=10, personal_target=40, min_credits=5)
    assert r.personal_target == 25
    assert any("exceeds the active ceiling" in w for w in r.warnings)


def test_imported_profile_with_outdated_ceiling_still_resolves_against_current_standard():
    # an "imported" profile is just a caller supplying old numbers; the
    # service always resolves against today's STANDARD_CEILING, it never
    # trusts a ceiling value baked into old client-side data.
    stale_client_ceiling_hint = 20  # pretend an old export believed the cap was 20
    r = resolve_ceiling(fixed_credits=12, personal_target=stale_client_ceiling_hint, min_credits=10)
    assert r.active_ceiling == STANDARD_CEILING
    assert r.personal_target == 20  # target itself is honoured, just not treated as the ceiling
