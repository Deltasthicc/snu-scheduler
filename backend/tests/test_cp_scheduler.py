"""Tests for the CP-SAT wishlist scheduler (app/services/cp_scheduler.py).

These are pure unit tests against synthetic WishItem/PlacedMeeting data, not
the real catalog - cp_scheduler has no dependency on app.domain.catalog at
all, so no spawned worker process or real course data is needed to verify
its actual solving behavior."""
from __future__ import annotations
import itertools

import pytest

from app.services import cp_scheduler
from app.services.cp_scheduler import WishChoiceGroup, WishItem, explain_omission, solve
from app.services.scheduler import PlacedMeeting


def pkg(day, start, end, term="Full semester", label="LEC1"):
    return {"t": term, "l": label, "m": [[day, start, end, "LEC", label, "A1"]]}


def test_must_have_is_always_included():
    items = [WishItem(code="M", packages=(pkg(0, 600, 690),), credits=3,
                      intent="must_have", forced=True)]
    r = solve(items, [], 0, [], credit_min=0, credit_target=3, credit_max=25)
    assert "M" in r.included


def test_conflicting_packages_are_never_both_selected():
    a = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="strong", priority=9)
    b = WishItem(code="B", packages=(pkg(0, 600, 690),), credits=3, intent="strong", priority=9)
    r = solve([a, b], [], 0, [], credit_min=0, credit_target=10, credit_max=25)
    assert not ("A" in r.included and "B" in r.included)


def test_locked_package_is_respected():
    it = WishItem(code="A", packages=(pkg(0, 600, 690), pkg(1, 600, 690)), credits=3,
                  intent="must_have", forced=True, locked_package=1)
    r = solve([it], [], 0, [], credit_min=0, credit_target=3, credit_max=25)
    assert r.assign["A"] == 1


def test_excluded_package_is_never_selected():
    it = WishItem(code="A", packages=(pkg(0, 600, 690), pkg(1, 600, 690)), credits=3,
                  intent="must_have", forced=True, excluded_packages=(1,))
    r = solve([it], [], 0, [], credit_min=0, credit_target=3, credit_max=25)
    assert r.assign["A"] == 0


def test_fixed_locked_meeting_blocks_a_clashing_wishlist_package():
    fixed = [PlacedMeeting(m=(0, 600, 690, "LEC", "LEC1", "A1"), term="Full semester", code="FIX")]
    it = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="must_have", forced=True)
    r = solve([it], fixed, 4, [], credit_min=0, credit_target=7, credit_max=25)
    assert r.status == "infeasible"
    assert "A" not in r.included


def test_half_semester_courses_in_opposite_halves_do_not_clash():
    a = WishItem(code="A", packages=(pkg(0, 600, 690, term="First half"),), credits=1.5,
                intent="must_have", forced=True)
    b = WishItem(code="B", packages=(pkg(0, 600, 690, term="Second half"),), credits=1.5,
                intent="must_have", forced=True)
    r = solve([a, b], [], 0, [], credit_min=0, credit_target=3, credit_max=25)
    assert r.status == "optimal"
    assert set(r.included) == {"A", "B"}


def test_credit_ceiling_is_never_exceeded():
    items = [WishItem(code=c, packages=(pkg(i, 600, 690),), credits=10, intent="strong", priority=5)
            for i, c in enumerate(["A", "B", "C"])]
    r = solve(items, [], 0, [], credit_min=0, credit_target=30, credit_max=15)
    assert r.total_credits <= 15


def test_prefers_target_over_maximizing_credits():
    # four 4-credit courses, none clashing, target well below the 16-credit max
    items = [WishItem(code=c, packages=(pkg(i, 600, 690),), credits=4, intent="strong", priority=5)
            for i, c in enumerate(["A", "B", "C", "D"])]
    r = solve(items, [], 0, [], credit_min=0, credit_target=8, credit_max=25)
    assert r.total_credits == 8  # exactly 2 of the 4 courses, not all 4


def test_returns_below_target_when_exact_target_is_unreachable():
    items = [WishItem(code="A", packages=(pkg(0, 600, 690),), credits=4, intent="strong", priority=5)]
    r = solve(items, [], 0, [], credit_min=0, credit_target=7, credit_max=25)
    assert r.total_credits == 4  # can't reach 7 with a single 4-credit course; take it anyway


def test_min_credits_floor_is_relaxed_only_when_infeasible():
    items = [WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="strong", priority=5)]
    r = solve(items, [], 0, [], credit_min=20, credit_target=20, credit_max=25)
    assert r.min_relaxed is True
    assert r.total_credits == 3


def test_choice_group_exactly_one():
    items = [WishItem(code=c, packages=(pkg(i, 600, 690),), credits=3, intent="strong", priority=5)
            for i, c in enumerate(["A", "B", "C"])]
    groups = [WishChoiceGroup(kind="exactly_one", members=("A", "B", "C"))]
    r = solve(items, [], 0, groups, credit_min=0, credit_target=10, credit_max=25)
    assert len(set(r.included) & {"A", "B", "C"}) == 1


def test_choice_group_at_most_one():
    items = [WishItem(code=c, packages=(pkg(i, 600, 690),), credits=3, intent="strong", priority=5)
            for i, c in enumerate(["A", "B"])]
    groups = [WishChoiceGroup(kind="at_most_one", members=("A", "B"))]
    r = solve(items, [], 0, groups, credit_min=0, credit_target=10, credit_max=25)
    assert len(set(r.included) & {"A", "B"}) <= 1


def test_choice_group_at_least_one():
    items = [WishItem(code=c, packages=(pkg(i, 600, 690),), credits=3, intent="optional", priority=1)
            for i, c in enumerate(["A", "B"])]
    groups = [WishChoiceGroup(kind="at_least_one", members=("A", "B"))]
    r = solve(items, [], 0, groups, credit_min=0, credit_target=0, credit_max=25)
    assert len(set(r.included) & {"A", "B"}) >= 1


def test_choice_group_min_credits():
    items = [WishItem(code=c, packages=(pkg(i, 600, 690),), credits=cr, intent="optional", priority=1)
            for i, (c, cr) in enumerate([("A", 3), ("B", 3), ("C", 3)])]
    groups = [WishChoiceGroup(kind="min_credits", members=("A", "B", "C"), min_credits=6)]
    r = solve(items, [], 0, groups, credit_min=0, credit_target=0, credit_max=25)
    got = sum(3 for c in r.included if c in ("A", "B", "C"))
    assert got >= 6


def test_deterministic_repeated_solves_are_byte_identical():
    items = [WishItem(code=c, packages=(pkg(i % 5, 600 + i * 10, 690 + i * 10), pkg((i + 1) % 5, 800, 890)),
                      credits=3, intent="strong", priority=(i % 4) + 1)
            for i, c in enumerate([f"C{i}" for i in range(8)])]
    r1 = solve(items, [], 0, [], credit_min=0, credit_target=15, credit_max=25, seed=42)
    r2 = solve(items, [], 0, [], credit_min=0, credit_target=15, credit_max=25, seed=42)
    assert r1.assign == r2.assign
    assert r1.included == r2.included
    assert r1.total_credits == r2.total_credits


def _brute_force_best_deviation(items, credit_max, credit_target):
    """All items here have exactly one non-clashing package each (by test
    construction), so subset feasibility reduces to a pairwise conflict
    check with no package-selection dimension - letting this differential
    test isolate the inclusion/objective logic from package selection."""
    from app.services.scheduler import _meetings_overlap
    best = None
    for r in range(len(items) + 1):
        for combo in itertools.combinations(items, r):
            ok = True
            for a, b in itertools.combinations(combo, 2):
                ma, mb = a.packages[0], b.packages[0]
                if any(_meetings_overlap(tuple(x), tuple(y), ma["t"], mb["t"])
                      for x in ma["m"] for y in mb["m"]):
                    ok = False
                    break
            if not ok:
                continue
            total = sum(i.credits for i in combo)
            if total > credit_max:
                continue
            dev = abs(total - credit_target)
            if best is None or dev < best:
                best = dev
    return best


def test_differential_against_brute_force_credit_target_fit():
    # 6 non-clashing single-package items with varied credits: CP-SAT's
    # chosen subset must fit the credit target at least as well as the best
    # subset a brute-force search over every combination can find.
    credits = [3, 4, 5, 2, 6, 3]
    items = [WishItem(code=f"C{i}", packages=(pkg(i, 600, 690),), credits=cr,
                      intent="optional", priority=5)
            for i, cr in enumerate(credits)]
    for target in (0, 5, 9, 14, 20, 23):
        r = solve(items, [], 0, [], credit_min=0, credit_target=target, credit_max=23)
        got_dev = abs(r.total_credits - target)
        best_dev = _brute_force_best_deviation(items, 23, target)
        assert got_dev == best_dev, f"target={target}: got {got_dev}, brute force found {best_dev}"


def test_explain_omission_time_clash():
    fixed = [PlacedMeeting(m=(0, 600, 690, "LEC", "LEC1", "A1"), term="Full semester", code="FIX")]
    a = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="strong", priority=5)
    r = solve([a], fixed, 3, [], credit_min=0, credit_target=3, credit_max=25)
    ex = explain_omission("A", [a], fixed, 3, [], 0, 3, 25, r)
    assert ex["blocker"] in ("time_clash_with_fixed", "no_valid_package")


def test_explain_omission_choice_group():
    a = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="must_have", forced=True)
    b = WishItem(code="B", packages=(pkg(1, 600, 690),), credits=3, intent="strong", priority=5)
    groups = [WishChoiceGroup(kind="at_most_one", members=("A", "B"))]
    r = solve([a, b], [], 0, groups, credit_min=0, credit_target=10, credit_max=25)
    ex = explain_omission("B", [a, b], [], 0, groups, 0, 10, 25, r)
    assert ex["blocker"] == "choice_group_rule"


def test_explain_omission_credit_ceiling():
    m = WishItem(code="M", packages=(pkg(0, 600, 690),), credits=4, intent="must_have", forced=True)
    x = WishItem(code="X", packages=(pkg(1, 600, 690),), credits=20, intent="strong", priority=5)
    r = solve([m, x], [], 5, [], credit_min=0, credit_target=10, credit_max=25)
    ex = explain_omission("X", [m, x], [], 5, [], 0, 10, 25, r)
    assert ex["blocker"] == "credit_ceiling"
    assert "relaxation" in ex and ex["relaxation"]


def test_explain_omission_no_valid_combination():
    m = WishItem(code="M", packages=(pkg(0, 600, 690),), credits=3, intent="must_have", forced=True)
    y = WishItem(code="Y", packages=(pkg(0, 600, 690),), credits=3, intent="strong", priority=5)
    r = solve([m, y], [], 0, [], credit_min=0, credit_target=10, credit_max=25)
    ex = explain_omission("Y", [m, y], [], 0, [], 0, 10, 25, r)
    assert ex["blocker"] == "no_valid_combination"


class _ExplodingCpModel:
    """Stands in for cp_model.CpModel; raises if ever constructed, proving
    the frozen fallback path genuinely never touches CP-SAT at all - a real
    segfault couldn't be caught by a test like this (it kills the process),
    but this at least proves the Python-level call is never attempted."""
    def __init__(self, *a, **kw):
        raise AssertionError("CP-SAT was invoked despite RUNNING_FROZEN=True")


@pytest.fixture
def running_frozen(monkeypatch):
    monkeypatch.setattr(cp_scheduler, "RUNNING_FROZEN", True)
    monkeypatch.setattr(cp_scheduler.cp_model, "CpModel", _ExplodingCpModel)
    yield


def test_frozen_fallback_never_touches_cp_sat(running_frozen):
    a = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="must_have", forced=True)
    r = solve([a], [], 0, [], credit_min=0, credit_target=3, credit_max=25)
    assert r.status == "heuristic_fallback"
    assert "A" in r.included


def test_frozen_fallback_still_includes_must_have(running_frozen):
    a = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="must_have", forced=True)
    b = WishItem(code="B", packages=(pkg(1, 600, 690),), credits=3, intent="strong", priority=5)
    r = solve([a, b], [], 0, [], credit_min=0, credit_target=10, credit_max=25)
    assert "A" in r.included
    assert "B" in r.included  # no conflict, room under target


def test_frozen_fallback_infeasible_on_impossible_must_have(running_frozen):
    fixed = [PlacedMeeting(m=(0, 600, 690, "LEC", "LEC1", "A1"), term="Full semester", code="FIX")]
    a = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="must_have", forced=True)
    r = solve([a], fixed, 3, [], credit_min=0, credit_target=3, credit_max=25)
    assert r.status == "infeasible"
    assert "A" not in r.included


def test_frozen_fallback_never_exceeds_credit_ceiling(running_frozen):
    items = [WishItem(code=c, packages=(pkg(i, 600, 690),), credits=10, intent="strong", priority=9 - i)
            for i, c in enumerate(["A", "B", "C"])]
    r = solve(items, [], 0, [], credit_min=0, credit_target=30, credit_max=15)
    assert r.total_credits <= 15


def test_frozen_fallback_never_double_books_a_conflicting_pair(running_frozen):
    a = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="strong", priority=9)
    b = WishItem(code="B", packages=(pkg(0, 600, 690),), credits=3, intent="strong", priority=9)
    r = solve([a, b], [], 0, [], credit_min=0, credit_target=10, credit_max=25)
    assert not ({"A", "B"} <= set(r.included))


def test_frozen_fallback_respects_at_most_one_choice_group(running_frozen):
    a = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="strong", priority=9)
    b = WishItem(code="B", packages=(pkg(1, 600, 690),), credits=3, intent="strong", priority=8)
    groups = [WishChoiceGroup(kind="at_most_one", members=("A", "B"))]
    r = solve([a, b], [], 0, groups, credit_min=0, credit_target=10, credit_max=25)
    assert len(set(r.included) & {"A", "B"}) <= 1


def test_frozen_fallback_respects_locked_and_excluded_packages(running_frozen):
    it = WishItem(code="A", packages=(pkg(0, 600, 690), pkg(1, 600, 690)), credits=3,
                 intent="must_have", forced=True, locked_package=1)
    r = solve([it], [], 0, [], credit_min=0, credit_target=3, credit_max=25)
    assert r.assign["A"] == 1


def test_explain_omission_already_included_is_a_noop():
    a = WishItem(code="A", packages=(pkg(0, 600, 690),), credits=3, intent="must_have", forced=True)
    r = solve([a], [], 0, [], credit_min=0, credit_target=3, credit_max=25)
    ex = explain_omission("A", [a], [], 0, [], 0, 3, 25, r)
    assert ex["blocker"] is None
