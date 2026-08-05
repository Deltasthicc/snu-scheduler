"""Tests for app/services/wishlist.py: choice-group structural validation and
the wishlist summary numbers (spec s.4's required display fields)."""
from __future__ import annotations

from app.models.profile_schemas import ChoiceGroup, WishlistItem
from app.services.wishlist import validate_choice_groups, wishlist_summary

COURSES = {
    "A": {"cr": 3.0, "cat": "ME", "crOfficial": True, "pk": [{"m": []}]},
    "B": {"cr": 4.0, "cat": "UWE", "crOfficial": False, "pk": [{"m": []}, {"m": []}]},
    "C": {"cr": 3.0, "cat": "CCC", "crOfficial": True, "pk": [{"m": []}]},
}


def test_choice_group_referencing_unknown_wishlist_course_is_flagged():
    items = [WishlistItem(code="A")]
    groups = [ChoiceGroup(kind="exactly_one", members=["A", "Z"])]
    issues = validate_choice_groups(items, groups)
    assert any("Z" in i for i in issues)


def test_at_most_one_with_two_must_haves_is_a_contradiction():
    items = [WishlistItem(code="A", intent="must_have"), WishlistItem(code="B", intent="must_have")]
    groups = [ChoiceGroup(kind="at_most_one", members=["A", "B"])]
    issues = validate_choice_groups(items, groups)
    assert issues and "at most one" in issues[0]


def test_exactly_one_with_two_must_haves_is_a_contradiction():
    items = [WishlistItem(code="A", intent="must_have"), WishlistItem(code="B", intent="must_have")]
    groups = [ChoiceGroup(kind="exactly_one", members=["A", "B"])]
    issues = validate_choice_groups(items, groups)
    assert issues


def test_clean_wishlist_has_no_issues():
    items = [WishlistItem(code="A", intent="must_have"), WishlistItem(code="B", intent="strong")]
    groups = [ChoiceGroup(kind="at_most_one", members=["A", "B"])]
    assert validate_choice_groups(items, groups) == []


def test_summary_counts_and_credits_no_groups():
    items = [
        WishlistItem(code="A", intent="must_have"),
        WishlistItem(code="B", intent="strong"),
        WishlistItem(code="C", intent="backup"),
    ]
    s = wishlist_summary(items, [], fixed_credits=10, courses=COURSES)
    assert s.count == 3
    assert s.num_must_have == 1
    assert s.num_backup == 1
    assert s.num_unconfirmed == 1  # B has crOfficial False
    assert s.fixed_credits == 10
    assert s.min_possible_credits == 3  # only the must-have (A, 3cr) is floor-guaranteed
    assert s.max_possible_credits == 7  # A(3) + B(4); backup C never counts toward max on its own
    assert s.total_possible_semester_credits == 17
    assert s.credits_currently_requested == 7  # A + B (non-backup)
    # composition covers the whole wishlist including backups (shows its full
    # shape), separately from credits_currently_requested which excludes them
    assert s.category_composition == {"ME": 3.0, "UWE": 4.0, "CCC": 3.0}


def test_summary_impossible_when_all_packages_excluded():
    items = [WishlistItem(code="B", intent="strong", excluded_packages=[0, 1])]
    s = wishlist_summary(items, [], fixed_credits=0, courses=COURSES)
    assert s.num_impossible == 1
    assert s.items[0].is_impossible is True


def test_summary_unknown_catalog_course():
    items = [WishlistItem(code="ZZZ", intent="strong")]
    s = wishlist_summary(items, [], fixed_credits=0, courses={})
    assert s.items[0].unknown_in_catalog is True
    assert s.items[0].credits == 0
    assert s.num_impossible == 1  # zero packages available for an unknown course


def test_summary_exactly_one_group_bounds():
    items = [WishlistItem(code="A", intent="strong"), WishlistItem(code="C", intent="strong")]
    groups = [ChoiceGroup(kind="exactly_one", members=["A", "C"])]
    s = wishlist_summary(items, groups, fixed_credits=0, courses=COURSES)
    # both cost 3 credits, so min == max == 3 regardless of which is picked
    assert s.min_possible_credits == 3
    assert s.max_possible_credits == 3


def test_summary_min_credits_group_shortfall_is_noted():
    items = [WishlistItem(code="A", intent="optional"), WishlistItem(code="C", intent="optional")]
    groups = [ChoiceGroup(kind="min_credits", members=["A", "C"], min_credits=10)]
    s = wishlist_summary(items, groups, fixed_credits=0, courses=COURSES)
    assert s.notes  # A(3) + C(3) = 6 < 10, should be flagged
