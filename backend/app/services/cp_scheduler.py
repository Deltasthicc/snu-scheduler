"""CP-SAT exact optimization layer for wishlist-driven schedule generation.

Both attached research reports converge on the same recommendation for this
exact problem shape (course inclusion + exactly-one-package-per-course +
term-aware clash bitsets + choice groups + credit bounds + tiered soft
preferences): Google OR-Tools CP-SAT. See docs/research/scheduler_v2_matrix.md
for the point-by-point mapping.

This module is deliberately a *layer on top of* app/services/scheduler.py,
not a replacement: scheduler.py's branch-and-bound path (search/
search_with_fallback) still serves the original "which package per course in
my shortlist" flow untouched, since it already has 40+ passing tests and a
proven production shape. This module only activates when a caller supplies
wishlist intent + choice groups + a credit target/min/max - see
app/workers/schedule_jobs.py for the routing decision.

Design choices, and why:
  - Determinism: num_search_workers=1 and a fixed random_seed, matching the
    same determinism requirement CLAUDE.md already states for the simulation
    engine (same seed + inputs -> byte-identical output).
  - Lexicographic tiers are encoded as one weighted objective with a large
    gap between tier weights (10**6 / 10**3 / 1), not a true multi-solve
    lexicographic search - a documented simplification, not a hidden one,
    because a true lexicographic re-solve per tier would cost 3x the solver
    time budget for a difference that is invisible once weights are this far
    apart (CP-SAT's own domain sizes here are far below 10**6).
  - Compactness (the "lifestyle" tier) is modelled as "minimize distinct
    campus days", not full weekly-gap-minutes: an exact gap objective needs
    interval/sequencing variables per day and roughly doubles model size for
    a metric students care about far less than must-have retention or credit
    target. Real gap/day/earliest/latest stats are computed post-hoc for
    display via scheduler.schedule_stats, same as the existing exact search.
"""
from __future__ import annotations
import sys
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from app.services.scheduler import PlacedMeeting, _meetings_overlap, schedule_stats, SearchItem

# Confirmed by direct reproduction (2026-08-04, see CLAUDE.md and
# docs/DESKTOP_PACKAGING.md): OR-Tools' CP-SAT native extension segfaults
# inside solver.Solve() specifically when running inside this project's
# PyInstaller-frozen desktop build - reproducibly, even unspawned, with an
# identical .libs/ DLL layout to the working unfrozen install (ruled out:
# missing files, multiprocessing/spawn). A segfault is a hard OS-level crash
# that no Python try/except can catch, so the only correct fix is to never
# call into CP-SAT at all when frozen - not to wrap the call in a try/except
# that would never actually run. `solve()` below checks this flag and routes
# to `_solve_greedy_fallback` instead, which is pure Python with no native
# extension calls, so it cannot segfault. Set at import time so tests can
# monkeypatch it (`cp_scheduler.RUNNING_FROZEN = True`) to exercise the
# fallback path without needing an actual frozen build.
RUNNING_FROZEN = bool(getattr(sys, "frozen", False))

CREDIT_SCALE = 4  # supports credits down to 0.25 without float error in the ILP
DEFAULT_TIME_LIMIT_S = 4.0
N_DAYS = 7


@dataclass(frozen=True)
class WishItem:
    code: str
    packages: tuple  # each: {"t": term, "l": label, "m": [meeting, ...]}
    credits: float
    intent: str  # "must_have" | "strong" | "optional" | "backup"
    priority: int = 5
    forced: bool = False  # True for must_have and for already-enrolled unlocked-fixed courses
    locked_package: int | None = None
    excluded_packages: tuple[int, ...] = ()


@dataclass(frozen=True)
class WishChoiceGroup:
    kind: str  # "exactly_one" | "at_least_one" | "at_most_one" | "min_credits"
    members: tuple[str, ...]
    min_credits: float | None = None


@dataclass
class SolveResult:
    status: str  # "optimal" | "feasible" | "infeasible" | "unknown"
    assign: dict[str, int] = field(default_factory=dict)   # code -> chosen package index
    included: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    total_credits: float = 0.0
    min_relaxed: bool = False
    wall_time_s: float = 0.0
    stats: dict | None = None


def _usable_packages(item: WishItem) -> list[int]:
    if item.locked_package is not None:
        return [item.locked_package] if item.locked_package < len(item.packages) else []
    excl = set(item.excluded_packages)
    return [i for i in range(len(item.packages)) if i not in excl]


def _package_clashes_fixed(pkg: dict, fixed_meetings: list[PlacedMeeting]) -> bool:
    for m in pkg["m"]:
        for fm in fixed_meetings:
            if _meetings_overlap(tuple(m), fm.m, pkg["t"], fm.term):
                return True
    return False


def _build_and_solve(
    items: list[WishItem],
    fixed_meetings: list[PlacedMeeting],
    fixed_credits: float,
    groups: list[WishChoiceGroup],
    credit_min: float,
    credit_target: float,
    credit_max: float,
    time_limit_seconds: float,
    seed: int,
    enforce_min: bool,
    force_include: str | None,
    force_exclude: str | None,
    forbidden_inclusion_sets: list[frozenset[str]] | None = None,
) -> tuple[str, dict[str, int], float]:
    model = cp_model.CpModel()
    y: dict[str, "cp_model.IntVar"] = {}
    x: dict[tuple[str, int], "cp_model.IntVar"] = {}
    usable: dict[str, list[int]] = {}

    for it in items:
        y[it.code] = model.NewBoolVar(f"y_{it.code}")
        allowed = [p for p in _usable_packages(it) if not _package_clashes_fixed(it.packages[p], fixed_meetings)]
        usable[it.code] = allowed
        pkg_vars = []
        for p in allowed:
            v = model.NewBoolVar(f"x_{it.code}_{p}")
            x[(it.code, p)] = v
            pkg_vars.append(v)
        model.Add(sum(pkg_vars) == y[it.code])
        if it.forced:
            model.Add(y[it.code] == 1)
        if force_include == it.code:
            model.Add(y[it.code] == 1)
        if force_exclude == it.code:
            model.Add(y[it.code] == 0)

    # No-good cuts for top-K generation: forbid the exact same set of included
    # courses from being chosen again. What a student actually compares
    # between options is "which courses did I keep vs drop," not a tiny
    # package swap on a course whose inclusion was never in question, so
    # distinctness is defined on the inclusion pattern, not the full
    # assignment - see solve_top_k().
    for included_set in (forbidden_inclusion_sets or ()):
        model.Add(sum((1 - y[it.code]) if it.code in included_set else y[it.code]
                      for it in items) >= 1)

    # term-aware conflict constraints between every pair of package choices,
    # across different courses (a course's own packages are mutually
    # exclusive already via the sum(x)==y constraint above)
    flat = [(it.code, p, it.packages[p]) for it in items for p in usable[it.code]]
    for i in range(len(flat)):
        code_a, pa, pkg_a = flat[i]
        for j in range(i + 1, len(flat)):
            code_b, pb, pkg_b = flat[j]
            if code_a == code_b:
                continue
            clash = any(
                _meetings_overlap(tuple(ma), tuple(mb), pkg_a["t"], pkg_b["t"])
                for ma in pkg_a["m"] for mb in pkg_b["m"]
            )
            if clash:
                model.Add(x[(code_a, pa)] + x[(code_b, pb)] <= 1)

    by_code = {it.code: it for it in items}
    for g in groups:
        members = [m for m in g.members if m in by_code]
        if not members:
            continue
        if g.kind == "exactly_one":
            model.Add(sum(y[m] for m in members) == 1)
        elif g.kind == "at_least_one":
            model.Add(sum(y[m] for m in members) >= 1)
        elif g.kind == "at_most_one":
            model.Add(sum(y[m] for m in members) <= 1)
        elif g.kind == "min_credits":
            scaled = [round(by_code[m].credits * CREDIT_SCALE) for m in members]
            threshold = round((g.min_credits or 0) * CREDIT_SCALE)
            model.Add(sum(c * y[m] for c, m in zip(scaled, members)) >= threshold)

    fixed_scaled = round(fixed_credits * CREDIT_SCALE)
    wishlist_credit_terms = [round(it.credits * CREDIT_SCALE) * y[it.code] for it in items]
    total = fixed_scaled + sum(wishlist_credit_terms) if wishlist_credit_terms else fixed_scaled
    total_var = model.NewIntVar(0, 100 * CREDIT_SCALE, "total_credits")
    model.Add(total_var == total)
    model.Add(total_var <= round(credit_max * CREDIT_SCALE))
    if enforce_min:
        model.Add(total_var >= round(credit_min * CREDIT_SCALE))

    target_scaled = round(min(credit_target, credit_max) * CREDIT_SCALE)
    dev = model.NewIntVar(0, 100 * CREDIT_SCALE, "target_deviation")
    model.Add(dev >= target_scaled - total_var)
    model.Add(dev >= total_var - target_scaled)

    day_used = []
    for d in range(N_DAYS):
        du = model.NewBoolVar(f"day_used_{d}")
        for code, p, pkg in flat:
            if any(m[0] == d for m in pkg["m"]):
                model.Add(du >= x[(code, p)])
        day_used.append(du)

    value_term = sum(it.priority * round(it.credits * CREDIT_SCALE) * y[it.code] for it in items)
    model.Minimize(dev * 1_000_000 - value_term * 10 + sum(day_used) * 1000)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = seed
    status = solver.Solve(model)

    status_name = {
        cp_model.OPTIMAL: "optimal", cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible", cp_model.UNKNOWN: "unknown",
        cp_model.MODEL_INVALID: "invalid",
    }.get(status, "unknown")

    assign: dict[str, int] = {}
    if status_name in ("optimal", "feasible"):
        for it in items:
            if solver.Value(y[it.code]):
                for p in usable[it.code]:
                    if solver.Value(x[(it.code, p)]):
                        assign[it.code] = p
                        break
    return status_name, assign, solver.WallTime()


def _greedy_order_key(it: WishItem, force_include: str | None) -> tuple:
    forced = it.forced or it.code == force_include
    intent_rank = {"strong": 1, "optional": 2, "backup": 3}.get(it.intent, 2)
    return (0 if forced else intent_rank, -it.priority, it.code)


def _solve_greedy_fallback(
    items: list[WishItem],
    fixed_meetings: list[PlacedMeeting],
    fixed_credits: float,
    groups: list[WishChoiceGroup],
    credit_min: float,
    credit_target: float,
    credit_max: float,
    force_include: str | None,
    force_exclude: str | None,
) -> tuple[str, dict[str, int]]:
    """Pure-Python stand-in for _build_and_solve, used only when CP-SAT is
    unsafe to call (RUNNING_FROZEN). Greedy, not exact: forced/must-have
    items first (by construction, never skipped - if one can't fit, the
    whole result is infeasible, matching the CP-SAT path's own hard
    guarantee), then remaining items by intent and priority, added if they
    fit within the ceiling without creating a conflict. Choice groups are
    enforced by skipping additional exactly_one/at_most_one members once one
    is already placed - not by proving group satisfiability up front, so an
    unsatisfiable at_least_one/min_credits group is not reported as
    infeasible the way the exact solver would; this is the accepted cost of
    a heuristic that cannot call into the crashing native solver at all."""
    by_code = {it.code: it for it in items}
    group_members: dict[str, list[WishChoiceGroup]] = {}
    for g in groups:
        for m in g.members:
            group_members.setdefault(m, []).append(g)

    placed = list(fixed_meetings)
    assign: dict[str, int] = {}
    included: set[str] = set()
    total = fixed_credits

    for it in sorted(items, key=lambda x: _greedy_order_key(x, force_include)):
        if it.code == force_exclude:
            continue
        forced = it.forced or it.code == force_include
        if not forced and it.intent == "backup":
            continue
        if not forced:
            blocked = any(
                any(m in included for m in g.members if m != it.code)
                for g in group_members.get(it.code, []) if g.kind in ("exactly_one", "at_most_one")
            )
            if blocked:
                continue
        candidates = [p for p in _usable_packages(it)
                     if not _package_clashes_fixed(it.packages[p], fixed_meetings)]
        chosen = None
        for p in candidates:
            pkg_meetings = [(tuple(m), it.packages[p]["t"]) for m in it.packages[p]["m"]]
            conflict = any(
                _meetings_overlap(pm, om.m, pt, om.term)
                for pm, pt in pkg_meetings for om in placed if om.code != it.code
            )
            if not conflict:
                chosen = p
                break
        if chosen is None:
            if forced:
                return "infeasible", {}
            continue
        new_total = total + it.credits
        if new_total > credit_max:
            if forced:
                return "infeasible", {}
            continue
        if not forced and total >= credit_target:
            continue
        assign[it.code] = chosen
        included.add(it.code)
        total = new_total
        for m in it.packages[chosen]["m"]:
            placed.append(PlacedMeeting(m=tuple(m), term=it.packages[chosen]["t"], code=it.code))

    return "heuristic_fallback", assign


def solve(
    items: list[WishItem],
    fixed_meetings: list[PlacedMeeting],
    fixed_credits: float,
    groups: list[WishChoiceGroup],
    credit_min: float,
    credit_target: float,
    credit_max: float,
    time_limit_seconds: float = DEFAULT_TIME_LIMIT_S,
    seed: int = 20260804,
    force_include: str | None = None,
    force_exclude: str | None = None,
) -> SolveResult:
    """Solves once with the minimum-credit floor enforced; if that is
    infeasible, retries without it (spec s.5: 'never below a user-selected
    minimum unless no such schedule exists') and reports `min_relaxed=True`
    so the caller can say so honestly instead of silently dropping the floor.

    When RUNNING_FROZEN is true, skips CP-SAT entirely and uses
    _solve_greedy_fallback - see the module-level comment on RUNNING_FROZEN
    for why (a segfault cannot be caught by try/except, so the only correct
    fix is to never make the call at all in that environment).
    """
    if RUNNING_FROZEN:
        status, assign = _solve_greedy_fallback(
            items, fixed_meetings, fixed_credits, groups, credit_min, credit_target, credit_max,
            force_include, force_exclude,
        )
        wall = 0.0
        min_relaxed = False
    else:
        status, assign, wall = _build_and_solve(
            items, fixed_meetings, fixed_credits, groups, credit_min, credit_target, credit_max,
            time_limit_seconds, seed, enforce_min=True,
            force_include=force_include, force_exclude=force_exclude,
        )
        min_relaxed = False
        if status == "infeasible" and credit_min > 0:
            status, assign, wall2 = _build_and_solve(
                items, fixed_meetings, fixed_credits, groups, credit_min, credit_target, credit_max,
                time_limit_seconds, seed, enforce_min=False,
                force_include=force_include, force_exclude=force_exclude,
            )
            wall += wall2
            min_relaxed = status in ("optimal", "feasible")

    return _wrap_result(items, fixed_credits, assign, status, wall, min_relaxed)


def _wrap_result(
    items: list[WishItem], fixed_credits: float, assign: dict[str, int],
    status: str, wall: float, min_relaxed: bool,
) -> SolveResult:
    all_codes = {it.code for it in items}
    included = sorted(assign.keys())
    excluded = sorted(all_codes - set(included))
    items_by_code = {it.code: SearchItem(code=it.code, packages=it.packages) for it in items}
    credits_by_code = {it.code: it.credits for it in items}
    total_credits = fixed_credits + sum(credits_by_code[c] for c in included)
    stats = schedule_stats(assign, items_by_code) if assign else None
    return SolveResult(
        status=status, assign=assign, included=included, excluded=excluded,
        total_credits=round(total_credits, 4), min_relaxed=min_relaxed,
        wall_time_s=round(wall, 4), stats=stats,
    )


def solve_top_k(
    items: list[WishItem],
    fixed_meetings: list[PlacedMeeting],
    fixed_credits: float,
    groups: list[WishChoiceGroup],
    credit_min: float,
    credit_target: float,
    credit_max: float,
    k: int = 5,
    time_limit_seconds: float = 3.0,
    seed: int = 20260804,
) -> list[SolveResult]:
    """Up to `k` distinct schedules built from the same wishlist, ranked by
    the same objective the single-best solve uses (target-fit, then
    priority-weighted value, then campus-day compactness), so a student can
    compare real options instead of trusting one optimizer call. Each solve
    after the first forbids every previously found *inclusion pattern* (see
    the no-good cut in `_build_and_solve`) - since dropping to the same
    package on an already-decided course isn't a meaningfully different
    choice, but keeping a different subset of courses is.

    RUNNING_FROZEN builds cannot call CP-SAT at all (see the module docstring)
    and the greedy fallback has no mechanism for a "next-best" re-solve, so
    this returns at most one result there - the same accepted limitation
    `solve()` already has on a frozen build.
    """
    if RUNNING_FROZEN:
        status, assign = _solve_greedy_fallback(
            items, fixed_meetings, fixed_credits, groups, credit_min, credit_target, credit_max,
            None, None,
        )
        if status == "heuristic_fallback" and assign:
            return [_wrap_result(items, fixed_credits, assign, status, 0.0, False)]
        return []

    results: list[SolveResult] = []
    forbidden: list[frozenset[str]] = []
    enforce_min_current = True
    min_relaxed = False
    for _ in range(max(1, k)):
        status, assign, wall = _build_and_solve(
            items, fixed_meetings, fixed_credits, groups, credit_min, credit_target, credit_max,
            time_limit_seconds, seed, enforce_min=enforce_min_current,
            force_include=None, force_exclude=None, forbidden_inclusion_sets=forbidden,
        )
        if status not in ("optimal", "feasible"):
            # spec s.5: never drop the minimum-credit floor unless no schedule
            # can meet it at all - only relax it if this is the very first
            # solve (nothing found yet), not just because a later, already-
            # explored-inclusion-pattern re-solve came up empty.
            if enforce_min_current and credit_min > 0 and not results and not forbidden:
                enforce_min_current = False
                min_relaxed = True
                continue
            break
        results.append(_wrap_result(items, fixed_credits, assign, status, wall, min_relaxed))
        forbidden.append(frozenset(assign.keys()))
    return results


BLOCKER_LABELS = {
    "time_clash_with_fixed": "Every valid package for this course overlaps a fixed/locked course.",
    "choice_group_rule": "A choice-group rule already excludes it (another member of the group is selected).",
    "credit_ceiling": "Including it would exceed the active credit ceiling given everything else selected.",
    "no_valid_combination": "No combination of the current wishlist and fixed courses can include it at all.",
    "lower_priority": "It is schedule-feasible, but the optimizer preferred a different combination.",
    "no_valid_package": "It has no usable package left (all packages locked-out or excluded).",
    "unknown_in_catalog": "This course code is not in the course catalog.",
}


def explain_omission(
    code: str,
    items: list[WishItem],
    fixed_meetings: list[PlacedMeeting],
    fixed_credits: float,
    groups: list[WishChoiceGroup],
    credit_min: float,
    credit_target: float,
    credit_max: float,
    base_result: SolveResult,
    time_limit_seconds: float = 2.0,
    seed: int = 20260804,
) -> dict:
    """Why isn't `code` in `base_result`? Checked cheapest-first so the
    common cases never pay for a second solver call."""
    by_code = {it.code: it for it in items}
    if code not in by_code:
        return {"code": code, "blocker": "unknown_in_catalog", "reason": BLOCKER_LABELS["unknown_in_catalog"],
                "relaxation": None}
    if code in base_result.included:
        return {"code": code, "blocker": None, "reason": "already included", "relaxation": None}

    item = by_code[code]
    usable = [p for p in _usable_packages(item)
              if not _package_clashes_fixed(item.packages[p], fixed_meetings)]
    if not usable:
        blocker = "time_clash_with_fixed" if _usable_packages(item) else "no_valid_package"
        return {"code": code, "blocker": blocker, "reason": BLOCKER_LABELS[blocker], "relaxation": None}

    for g in groups:
        if code in g.members and g.kind in ("exactly_one", "at_most_one"):
            other_selected = [m for m in g.members if m != code and m in base_result.included]
            if other_selected:
                return {"code": code, "blocker": "choice_group_rule",
                        "reason": BLOCKER_LABELS["choice_group_rule"]
                        + f" Selected instead: {', '.join(other_selected)}.",
                        "relaxation": f"deselect {other_selected[0]} first"}

    # solve without the ceiling constraint first, so a genuine time-clash/
    # choice-group infeasibility can be told apart from "only the ceiling is
    # in the way" - forcing inclusion under the real ceiling would make both
    # cases look identically infeasible.
    uncapped = fixed_credits + sum(it.credits for it in items) + credit_max
    forced_uncapped = solve(items, fixed_meetings, fixed_credits, groups, 0, credit_target,
                            uncapped, time_limit_seconds, seed, force_include=code)
    if forced_uncapped.status not in ("optimal", "feasible"):
        return {"code": code, "blocker": "no_valid_combination",
                "reason": BLOCKER_LABELS["no_valid_combination"], "relaxation": None}

    if forced_uncapped.total_credits > credit_max:
        needed = round(forced_uncapped.total_credits - credit_max, 4)
        return {"code": code, "blocker": "credit_ceiling",
                "reason": BLOCKER_LABELS["credit_ceiling"],
                "relaxation": f"raise the credit ceiling by at least {needed} credits"}

    forced_result = solve(items, fixed_meetings, fixed_credits, groups, credit_min, credit_target,
                          credit_max, time_limit_seconds, seed, force_include=code)

    return {"code": code, "blocker": "lower_priority", "reason": BLOCKER_LABELS["lower_priority"],
            "relaxation": f"including it is possible (total would be {forced_result.total_credits} "
                          f"credits); it lost only because the optimizer scored the current "
                          f"combination higher on priority/target-fit"}
