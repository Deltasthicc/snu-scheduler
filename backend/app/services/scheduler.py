"""Backend-authoritative schedule search.

This replaces the browser-side `enumerateSchedules()` in the old
frontend/src/ui/i_build.html, which ran a synchronous recursive backtracking
search directly on the page's main thread. Measured before this module existed:
a realistic 20-course shortlist with fully-compatible packages blocked a live
browser tab for ~5.03s per 100,000 search nodes; the UI's own default budget was
2,000,000 nodes (~101s projected) and its slowest option was 8,000,000
(~403s) - a fully frozen tab, worse than the 81s freeze already documented in
CLAUDE.md for the pre-split single-file version.

The algorithm itself (most-constrained-first backtracking, clash-checking
against a static fixed baseline) is ported faithfully from the JS so results are
the same schedules a student would have gotten before - only where the search
runs has changed.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable


# "Both half" arrived with the 2026-08-10 Academic Office draft workbook: a
# CCC offered in BOTH the first and second half at the same weekly slot, so it
# occupies that slot all semester exactly like "Full semester" does. Modelling
# terms as the set of halves they occupy handles all four values without
# special-casing, and - critically - stops a "Both half" course being read as
# non-overlapping with a "First half" one, which the old string equality would
# have done and would have hidden real clashes.
def _spans_both_halves(term: str) -> bool:
    return term in ("Full semester", "Both half")


def _term_overlap(a: str, b: str) -> bool:
    return a == b or _spans_both_halves(a) or _spans_both_halves(b)


def _meetings_overlap(a: tuple, b: tuple, term_a: str, term_b: str) -> bool:
    # meeting tuple: (day, start_minute, end_minute, component, section, room)
    return a[0] == b[0] and _term_overlap(term_a, term_b) and a[1] < b[2] and b[1] < a[2]


@dataclass(frozen=True)
class PlacedMeeting:
    m: tuple
    term: str
    code: str


def _conflicts(a_list: list[PlacedMeeting], b_list: list[PlacedMeeting]) -> bool:
    for a in a_list:
        for b in b_list:
            if a.code == b.code:
                continue
            if _meetings_overlap(a.m, b.m, a.term, b.term):
                return True
    return False


@dataclass(frozen=True)
class SearchItem:
    code: str
    packages: tuple  # each element: {"t": term, "l": label, "m": [meeting, ...]}


class SearchTimeoutOrCancel(Exception):
    pass


def enumerate_schedules(
    items: list[SearchItem],
    fixed_meetings: list[PlacedMeeting],
    max_results: int,
    max_nodes: int,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Most-constrained-first backtracking search for complete, clash-free
    section-package assignments across `items`, holding `fixed_meetings` static
    (locked/pre-enrolled sections never move - see CLAUDE.md design decisions).

    Deterministic: identical items + fixed_meetings + budgets always produce the
    same result list in the same order. Items are visited fewest-packages-first,
    tie-broken by course code, so hopeless branches die before the leaves.
    """
    ordered = sorted(items, key=lambda it: (len(it.packages), it.code))
    n = len(ordered)
    results: list[dict] = []
    nodes = 0
    truncated = False
    cancelled = False
    chosen: dict[str, int] = {}
    progress_every = max(1, max_nodes // 200)
    placed = list(fixed_meetings)

    def bt(i: int) -> None:
        nonlocal nodes, truncated, cancelled
        if cancelled or truncated or len(results) >= max_results:
            return
        nodes += 1
        if should_cancel is not None and nodes % 512 == 0 and should_cancel():
            cancelled = True
            return
        if on_progress is not None and nodes % progress_every == 0:
            on_progress(nodes, max_nodes)
        if nodes > max_nodes:
            truncated = True
            return
        if i == n:
            results.append(dict(chosen))
            return
        it = ordered[i]
        for pi, pkg in enumerate(it.packages):
            pkg_meetings = [PlacedMeeting(m=tuple(m), term=pkg["t"], code=it.code) for m in pkg["m"]]
            if _conflicts(pkg_meetings, placed):
                continue
            chosen[it.code] = pi
            placed.extend(pkg_meetings)
            bt(i + 1)
            del placed[len(placed) - len(pkg_meetings):]
            if cancelled or truncated or len(results) >= max_results:
                del chosen[it.code]
                return
            del chosen[it.code]

    bt(0)
    return {
        "results": results, "truncated": truncated, "cancelled": cancelled,
        "nodes": nodes, "item_order": [it.code for it in ordered],
    }


def _count_conflicts(a_list: list[PlacedMeeting], b_list: list[PlacedMeeting]) -> int:
    n = 0
    for a in a_list:
        for b in b_list:
            if a.code == b.code:
                continue
            if _meetings_overlap(a.m, b.m, a.term, b.term):
                n += 1
    return n


def enumerate_least_conflict(
    items: list[SearchItem],
    fixed_meetings: list[PlacedMeeting],
    max_nodes: int,
    max_results: int = 50,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """For when no zero-clash assignment exists: finds complete assignments
    minimizing total pairwise clash count, via branch-and-bound. A running
    clash count only ever increases as more courses are placed, so a partial
    assignment already at or above the best complete assignment found so far
    can never improve on it and is pruned immediately - this is what keeps
    the search tractable without needing to weaken the zero-clash search's
    own pruning. Ties at the minimum clash count are all returned, up to
    max_results.
    """
    ordered = sorted(items, key=lambda it: (len(it.packages), it.code))
    n = len(ordered)
    nodes = 0
    truncated = False
    cancelled = False
    best_count: int | None = None
    results: list[dict] = []
    chosen: dict[str, int] = {}
    placed = list(fixed_meetings)
    progress_every = max(1, max_nodes // 200)

    def bt(i: int, running_count: int) -> None:
        nonlocal nodes, truncated, cancelled, best_count
        if cancelled or truncated:
            return
        nodes += 1
        if should_cancel is not None and nodes % 512 == 0 and should_cancel():
            cancelled = True
            return
        if on_progress is not None and nodes % progress_every == 0:
            on_progress(nodes, max_nodes)
        if nodes > max_nodes:
            truncated = True
            return
        if best_count is not None and running_count > best_count:
            return  # bound: no completion of this branch can beat the best found
        if i == n:
            if best_count is None or running_count < best_count:
                best_count = running_count
                results.clear()
                results.append(dict(chosen))
            elif running_count == best_count and len(results) < max_results:
                results.append(dict(chosen))
            return
        it = ordered[i]
        for pi, pkg in enumerate(it.packages):
            pkg_meetings = [PlacedMeeting(m=tuple(m), term=pkg["t"], code=it.code) for m in pkg["m"]]
            new_count = running_count + _count_conflicts(pkg_meetings, placed)
            if best_count is not None and new_count > best_count:
                continue  # this package choice already can't win; try the next one
            chosen[it.code] = pi
            placed.extend(pkg_meetings)
            bt(i + 1, new_count)
            del placed[len(placed) - len(pkg_meetings):]
            del chosen[it.code]

    bt(0, 0)
    return {
        "results": results, "clash_count": best_count if best_count is not None else 0,
        "truncated": truncated, "cancelled": cancelled, "nodes": nodes,
        "item_order": [it.code for it in ordered],
    }


def schedule_stats(assign: dict[str, int], items_by_code: dict[str, SearchItem]) -> dict:
    """Per-schedule metrics used for sorting/browsing: total weekly idle time
    between classes, days on campus, and daily start/end envelopes."""
    by_day: dict[int, list[tuple]] = {}
    for code, pkg_idx in assign.items():
        pkg = items_by_code[code].packages[pkg_idx]
        for m in pkg["m"]:
            by_day.setdefault(m[0], []).append((m[1], m[2]))
    gap = 0
    day_starts, day_ends = [], []
    for _day, lst in by_day.items():
        lst.sort(key=lambda e: e[0])
        for k in range(1, len(lst)):
            gap += max(0, lst[k][0] - lst[k - 1][1])
        day_starts.append(lst[0][0])
        day_ends.append(lst[-1][1])
    return {
        "gap": gap,
        "days": len(by_day),
        "latest_end": max(day_ends) if day_ends else 0,
        "earliest_start": min(day_starts) if day_starts else 0,
        "avg_end": round(sum(day_ends) / len(day_ends), 2) if day_ends else 0,
        "avg_start": round(sum(day_starts) / len(day_starts), 2) if day_starts else 0,
    }


_SORT_KEYS = {
    "compact": lambda s: (s["stats"]["gap"], s["_code_key"]),
    "early": lambda s: (s["stats"]["avg_end"], s["_code_key"]),
    "late": lambda s: (-s["stats"]["avg_start"], s["_code_key"]),
    "days": lambda s: (s["stats"]["days"], s["_code_key"]),
}


def search(
    items: list[SearchItem],
    fixed_meetings: list[PlacedMeeting],
    max_results: int,
    max_nodes: int,
    sort: str = "compact",
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Full pipeline: search, score, and deterministically sort. Callers page
    the `schedules` list themselves (see the /results endpoint) rather than
    this function returning a fixed page, so the same search only ever runs once."""
    if sort not in _SORT_KEYS:
        raise ValueError(f"unknown sort key {sort!r}; must be one of {sorted(_SORT_KEYS)}")
    items_by_code = {it.code: it for it in items}
    raw = enumerate_schedules(items, fixed_meetings, max_results, max_nodes,
                              should_cancel=should_cancel, on_progress=on_progress)
    scored = []
    for assign in raw["results"]:
        stats = schedule_stats(assign, items_by_code)
        # stable tiebreak key: deterministic even when two schedules score identically
        code_key = "|".join(f"{c}:{assign[c]}" for c in sorted(assign))
        scored.append({"assign": assign, "stats": stats, "_code_key": code_key})
    scored.sort(key=_SORT_KEYS[sort])
    for s in scored:
        del s["_code_key"]
    return {
        "schedules": scored, "truncated": raw["truncated"], "cancelled": raw["cancelled"],
        "nodes": raw["nodes"], "total_found": len(scored), "sort": sort,
        "item_order": raw["item_order"], "mode": "exact", "clash_count": 0,
    }


def search_least_conflict(
    items: list[SearchItem],
    fixed_meetings: list[PlacedMeeting],
    max_nodes: int,
    sort: str = "compact",
    max_results: int = 50,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    if sort not in _SORT_KEYS:
        raise ValueError(f"unknown sort key {sort!r}; must be one of {sorted(_SORT_KEYS)}")
    items_by_code = {it.code: it for it in items}
    raw = enumerate_least_conflict(items, fixed_meetings, max_nodes, max_results=max_results,
                                   should_cancel=should_cancel, on_progress=on_progress)
    scored = []
    for assign in raw["results"]:
        stats = schedule_stats(assign, items_by_code)
        code_key = "|".join(f"{c}:{assign[c]}" for c in sorted(assign))
        scored.append({"assign": assign, "stats": stats, "_code_key": code_key})
    scored.sort(key=_SORT_KEYS[sort])
    for s in scored:
        del s["_code_key"]
    return {
        "schedules": scored, "truncated": raw["truncated"], "cancelled": raw["cancelled"],
        "nodes": raw["nodes"], "total_found": len(scored), "sort": sort,
        "item_order": raw["item_order"], "mode": "least_conflict", "clash_count": raw["clash_count"],
    }


def search_with_fallback(
    items: list[SearchItem],
    fixed_meetings: list[PlacedMeeting],
    max_results: int,
    max_nodes: int,
    sort: str = "compact",
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Tries for a real clash-free combination first; if none exists (whether
    because the search space is genuinely exhausted or because it was cut off
    before finding one), falls back to the best available combination ranked
    by fewest total clashes, using the same node budget again. Callers should
    check the `mode` field ("exact" vs "least_conflict") before presenting
    results as clash-free."""
    exact = search(items, fixed_meetings, max_results, max_nodes, sort=sort,
                   should_cancel=should_cancel, on_progress=on_progress)
    if exact["total_found"] > 0 or exact["cancelled"]:
        return exact
    fallback = search_least_conflict(items, fixed_meetings, max_nodes, sort=sort,
                                     max_results=max_results,
                                     should_cancel=should_cancel, on_progress=on_progress)
    # nodes/truncated should reflect the search that actually produced the
    # returned results, but keep a record that the exact pass was tried too
    fallback["exact_nodes"] = exact["nodes"]
    fallback["exact_truncated"] = exact["truncated"]
    return fallback
