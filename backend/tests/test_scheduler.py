from __future__ import annotations

from app.services.scheduler import (
    PlacedMeeting, SearchItem, enumerate_least_conflict, enumerate_schedules,
    schedule_stats, search, search_least_conflict, search_with_fallback,
)


def _pkg(term, label, meetings):
    return {"t": term, "l": label, "m": [list(m) for m in meetings]}


def _item(code, *packages):
    return SearchItem(code=code, packages=tuple(packages))


FULL = "Full semester"


def test_no_overlaps_in_any_returned_assignment():
    # two courses, each with a package that overlaps the other's -- only one
    # combination (their non-overlapping packages) should ever validate
    a = _item("A", _pkg(FULL, "a0", [(0, 540, 600, "LEC", "1", "R1")]),
              _pkg(FULL, "a1", [(0, 700, 760, "LEC", "1", "R1")]))
    b = _item("B", _pkg(FULL, "b0", [(0, 540, 600, "LEC", "1", "R1")]),  # clashes with a0
              _pkg(FULL, "b1", [(0, 800, 860, "LEC", "1", "R1")]))
    r = enumerate_schedules([a, b], [], max_results=1000, max_nodes=1000)
    assert r["results"], "expected at least one clash-free combination"
    for assign in r["results"]:
        pa = a.packages[assign["A"]]["m"][0]
        pb = b.packages[assign["B"]]["m"][0]
        assert not (pa[0] == pb[0] and pa[1] < pb[2] and pb[1] < pa[2])


def test_fixed_baseline_meetings_are_never_placed_by_the_search():
    # the only candidate package for A clashes with a fixed (locked) meeting,
    # so the search must find zero valid assignments rather than moving the fixed course
    fixed = [PlacedMeeting(m=(0, 540, 600, "LEC", "1", "R1"), term=FULL, code="FIXEDCOURSE")]
    a = _item("A", _pkg(FULL, "a0", [(0, 540, 600, "LEC", "1", "R1")]))
    r = enumerate_schedules([a], fixed, max_results=10, max_nodes=1000)
    assert r["results"] == []


def test_same_course_meetings_never_conflict_with_themselves():
    # a package with two non-overlapping meetings for the same course must not
    # be rejected by comparing the course against its own meetings
    a = _item("A", _pkg(FULL, "a0", [(0, 540, 600, "LEC", "1", "R1"),
                                     (2, 540, 600, "LEC", "1", "R1")]))
    r = enumerate_schedules([a], [], max_results=10, max_nodes=1000)
    assert len(r["results"]) == 1


def test_node_budget_truncates_and_reports_it():
    items = _fully_compatible_items(n_courses=10, n_packages=6)
    r = enumerate_schedules(items, [], max_results=10_000_000, max_nodes=5)
    assert r["truncated"] is True
    assert r["nodes"] > 5


def _fully_compatible_items(n_courses, n_packages):
    # every package gets its own globally-unique day+hour slot, so nothing ever
    # conflicts and the search branches to its full width instead of pruning early
    items = []
    for c in range(n_courses):
        pkgs = []
        for k in range(n_packages):
            slot = c * n_packages + k
            day, hour = slot % 6, slot // 6
            pkgs.append(_pkg(FULL, f"p{k}", [(day, hour * 60, hour * 60 + 50, "LEC", "1", "R1")]))
        items.append(_item(f"C{c}", *pkgs))
    return items


def test_cancellation_stops_the_search_immediately():
    # should_cancel is only polled every 512 nodes, so this must force the search
    # to actually visit that many nodes before it would otherwise finish or truncate
    items = _fully_compatible_items(n_courses=10, n_packages=6)
    r = enumerate_schedules(items, [], max_results=10_000_000, max_nodes=10_000_000,
                            should_cancel=lambda: True)
    assert r["cancelled"] is True
    assert r["nodes"] <= 1024  # cancellation is checked every 512 nodes; should stop close to the first checkpoint


def test_deterministic_ordering_across_repeated_runs():
    items = [_item(f"C{i}", _pkg(FULL, "p0", [(i % 6, 0, 50, "LEC", "1", "R1")]),
                    _pkg(FULL, "p1", [(i % 6, 60, 110, "LEC", "1", "R1")]))
             for i in range(6)]
    r1 = enumerate_schedules(items, [], max_results=1000, max_nodes=1_000_000)
    r2 = enumerate_schedules(items, [], max_results=1000, max_nodes=1_000_000)
    assert r1["results"] == r2["results"]
    assert r1["item_order"] == r2["item_order"]


def test_most_constrained_first_ordering():
    a = _item("HAS3", _pkg(FULL, "x", [(0, 0, 50, "LEC", "1", "R1")]),
              _pkg(FULL, "y", [(1, 0, 50, "LEC", "1", "R1")]),
              _pkg(FULL, "z", [(2, 0, 50, "LEC", "1", "R1")]))
    b = _item("HAS1", _pkg(FULL, "x", [(3, 0, 50, "LEC", "1", "R1")]))
    r = enumerate_schedules([a, b], [], max_results=10, max_nodes=1000)
    assert r["item_order"] == ["HAS1", "HAS3"]


def test_schedule_stats_computes_gap_and_days():
    items_by_code = {
        "A": _item("A", _pkg(FULL, "p", [(0, 540, 600, "LEC", "1", "R1")])),
        "B": _item("B", _pkg(FULL, "p", [(0, 660, 720, "LEC", "1", "R1")])),
    }
    stats = schedule_stats({"A": 0, "B": 0}, items_by_code)
    assert stats["days"] == 1
    assert stats["gap"] == 60  # 660 - 600
    assert stats["earliest_start"] == 540
    assert stats["latest_end"] == 720


def test_search_sorts_by_compact_ascending_gap():
    items = [
        _item("A", _pkg(FULL, "tight", [(0, 540, 600, "LEC", "1", "R1"), (0, 600, 660, "LEC", "1", "R1")]),
              _pkg(FULL, "loose", [(0, 540, 600, "LEC", "1", "R1"), (0, 780, 840, "LEC", "1", "R1")])),
    ]
    r = search(items, [], max_results=10, max_nodes=1000, sort="compact")
    gaps = [s["stats"]["gap"] for s in r["schedules"]]
    assert gaps == sorted(gaps)


def test_search_rejects_unknown_sort_key():
    import pytest
    with pytest.raises(ValueError):
        search([], [], max_results=10, max_nodes=1000, sort="nonsense")


def test_search_result_stats_have_no_internal_sort_key_leaked():
    items = [_item("A", _pkg(FULL, "p", [(0, 540, 600, "LEC", "1", "R1")]))]
    r = search(items, [], max_results=10, max_nodes=1000, sort="days")
    assert all("_code_key" not in s for s in r["schedules"])


def test_least_conflict_finds_zero_when_a_perfect_assignment_exists():
    a = _item("A", _pkg(FULL, "x", [(0, 0, 50, "LEC", "1", "R1")]))
    b = _item("B", _pkg(FULL, "x", [(1, 0, 50, "LEC", "1", "R1")]))
    r = enumerate_least_conflict([a, b], [], max_nodes=1000)
    assert r["clash_count"] == 0
    assert len(r["results"]) >= 1


def test_least_conflict_finds_the_true_minimum_not_just_the_first_found():
    # A and B always clash with each other (1 conflict, unavoidable). C has one
    # package that clashes with A's, and one that doesn't clash with anything -
    # a naive first-found-solution search could easily land on the worse pairing
    # if it doesn't keep searching after finding *a* complete assignment.
    a = _item("A", _pkg(FULL, "a0", [(0, 0, 50, "LEC", "1", "R1")]))
    b = _item("B", _pkg(FULL, "b0", [(0, 0, 50, "LEC", "1", "R1")]))  # always clashes with A
    c = _item("C",
              _pkg(FULL, "c0", [(0, 0, 50, "LEC", "1", "R1")]),   # clashes with both A and B
              _pkg(FULL, "c1", [(2, 0, 50, "LEC", "1", "R1")]))   # clashes with neither
    r = enumerate_least_conflict([a, b, c], [], max_nodes=100_000)
    assert r["clash_count"] == 1  # A vs B is unavoidable; C should pick c1
    assert all(assign["C"] == 1 for assign in r["results"])


def test_least_conflict_respects_node_budget_and_cancellation():
    items = _fully_compatible_items(n_courses=11, n_packages=6)
    r = enumerate_least_conflict(items, [], max_nodes=5)
    assert r["truncated"] is True

    r2 = enumerate_least_conflict(items, [], max_nodes=10_000_000, should_cancel=lambda: True)
    assert r2["cancelled"] is True


def test_search_with_fallback_returns_exact_mode_when_a_perfect_combo_exists():
    a = _item("A", _pkg(FULL, "x", [(0, 0, 50, "LEC", "1", "R1")]))
    b = _item("B", _pkg(FULL, "x", [(1, 0, 50, "LEC", "1", "R1")]))
    r = search_with_fallback([a, b], [], max_results=10, max_nodes=1000)
    assert r["mode"] == "exact"
    assert r["clash_count"] == 0
    assert r["total_found"] >= 1


def test_search_with_fallback_falls_back_when_nothing_is_clash_free():
    a = _item("A", _pkg(FULL, "x", [(0, 0, 50, "LEC", "1", "R1")]))
    b = _item("B", _pkg(FULL, "x", [(0, 0, 50, "LEC", "1", "R1")]))  # only package always clashes with A
    r = search_with_fallback([a, b], [], max_results=10, max_nodes=1000)
    assert r["mode"] == "least_conflict"
    assert r["clash_count"] == 1
    assert r["total_found"] >= 1
    assert "exact_nodes" in r and "exact_truncated" in r


def test_search_least_conflict_sorts_and_scores_like_search():
    items = [_item("A", _pkg(FULL, "x", [(0, 0, 50, "LEC", "1", "R1")]))]
    r = search_least_conflict(items, [], max_nodes=1000, sort="days")
    assert r["schedules"] and "stats" in r["schedules"][0]
    assert all("_code_key" not in s for s in r["schedules"])
