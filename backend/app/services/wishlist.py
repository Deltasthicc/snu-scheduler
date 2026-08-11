"""Wishlist summary and choice-group validation - the "what does my wishlist
actually add up to" service described in spec s.4. This is a fast, exact-
constraint-free estimate for display (min/max possible credits, category
composition, counts) - the *actual* feasible schedule set (which also
accounts for time clashes) is computed by app/services/cp_scheduler.py.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from app.domain.catalog import credits_of as course_credits
from app.models.profile_schemas import ChoiceGroup, ChoiceGroupKind, WishlistIntent, WishlistItem


def validate_choice_groups(items: list[WishlistItem], groups: list[ChoiceGroup]) -> list[str]:
    """Structural validation only (referential integrity + obvious
    contradictions). Time-clash/credit-ceiling feasibility is the scheduler's
    job, not this quick check's."""
    issues: list[str] = []
    by_code = {i.code: i for i in items}
    for gi, g in enumerate(groups):
        label = g.label or f"choice group #{gi + 1}"
        unknown = [m for m in g.members if m not in by_code]
        if unknown:
            issues.append(f"{label}: references course(s) not in the wishlist: {', '.join(unknown)}")
            continue
        must_haves = [m for m in g.members if by_code[m].intent == WishlistIntent.MUST_HAVE]
        if g.kind == ChoiceGroupKind.AT_MOST_ONE and len(must_haves) > 1:
            issues.append(
                f"{label}: is 'at most one' but {len(must_haves)} members are marked must-have "
                f"({', '.join(must_haves)}) - at most one of them can ever be scheduled")
        if g.kind == ChoiceGroupKind.EXACTLY_ONE and len(must_haves) > 1:
            issues.append(
                f"{label}: is 'exactly one' but {len(must_haves)} members are marked must-have "
                f"({', '.join(must_haves)}) - only one can be selected")
    return issues


@dataclass
class WishlistItemResolved:
    code: str
    intent: str
    credits: float
    category: str
    package_count: int
    credits_confirmed: bool
    is_impossible: bool  # zero usable packages after locks/exclusions
    unknown_in_catalog: bool


@dataclass
class WishlistSummary:
    count: int
    min_possible_credits: float
    max_possible_credits: float
    credits_currently_requested: float
    fixed_credits: float
    total_possible_semester_credits: float
    category_composition: dict[str, float]
    num_must_have: int
    num_backup: int
    num_impossible: int
    num_unconfirmed: int
    items: list[WishlistItemResolved] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _usable_package_count(item: WishlistItem, course: dict | None) -> int:
    if course is None:
        return 0
    n = len(course.get("pk", []))
    if item.locked_package is not None:
        return 1 if item.locked_package < n else 0
    return max(0, n - len(set(item.excluded_packages) & set(range(n))))


def wishlist_summary(
    items: list[WishlistItem],
    groups: list[ChoiceGroup],
    fixed_credits: float,
    courses: dict[str, dict],
) -> WishlistSummary:
    resolved: list[WishlistItemResolved] = []
    grouped_codes: dict[str, list[ChoiceGroup]] = {}
    for g in groups:
        for m in g.members:
            grouped_codes.setdefault(m, []).append(g)

    baseline_min = 0.0
    baseline_max = 0.0
    requested = 0.0
    cat_composition: dict[str, float] = {}
    num_must_have = num_backup = num_impossible = num_unconfirmed = 0

    for it in items:
        course = courses.get(it.code)
        credits = course_credits(course)
        category = course["cat"] if course else "unknown"
        pkg_count = _usable_package_count(it, course)
        confirmed = bool(course.get("crOfficial")) if course else False
        impossible = pkg_count == 0
        resolved.append(WishlistItemResolved(
            code=it.code, intent=it.intent.value, credits=credits, category=category,
            package_count=pkg_count, credits_confirmed=confirmed, is_impossible=impossible,
            unknown_in_catalog=course is None,
        ))
        if course is not None:
            cat_composition[category] = round(cat_composition.get(category, 0.0) + credits, 4)
        if it.intent == WishlistIntent.MUST_HAVE:
            num_must_have += 1
        if it.intent == WishlistIntent.BACKUP:
            num_backup += 1
        if impossible:
            num_impossible += 1
        if course is not None and not confirmed:
            num_unconfirmed += 1
        if it.intent != WishlistIntent.BACKUP:
            requested += credits
        if it.code not in grouped_codes:
            if it.intent == WishlistIntent.MUST_HAVE:
                baseline_min += credits
                baseline_max += credits
            elif it.intent == WishlistIntent.BACKUP:
                pass  # contingent: never counted toward either bound on its own
            else:
                baseline_max += credits

    by_code = {i.code: i for i in items}
    credits_of = {i.code: course_credits(courses.get(i.code)) for i in items}
    seen_groups: set[int] = set()
    notes: list[str] = []
    min_extra = max_extra = 0.0
    for gi, g in enumerate(groups):
        if id(g) in seen_groups:
            continue
        seen_groups.add(id(g))
        member_credits = [credits_of[m] for m in g.members if m in by_code]
        if not member_credits:
            continue
        if g.kind == ChoiceGroupKind.EXACTLY_ONE:
            min_extra += min(member_credits)
            max_extra += max(member_credits)
        elif g.kind == ChoiceGroupKind.AT_LEAST_ONE:
            min_extra += min(member_credits)
            max_extra += sum(member_credits)
        elif g.kind == ChoiceGroupKind.AT_MOST_ONE:
            max_extra += max(member_credits)
        elif g.kind == ChoiceGroupKind.MIN_CREDITS:
            total = sum(member_credits)
            if total < (g.min_credits or 0):
                notes.append(
                    f"{g.label or ('choice group #' + str(gi + 1))} asks for at least "
                    f"{g.min_credits} credits but its members only total {total}")
            min_extra += min(g.min_credits or 0, total)
            max_extra += total

    min_possible = round(baseline_min + min_extra, 4)
    max_possible = round(baseline_max + max_extra, 4)
    return WishlistSummary(
        count=len(items), min_possible_credits=min_possible, max_possible_credits=max_possible,
        credits_currently_requested=round(requested, 4), fixed_credits=fixed_credits,
        total_possible_semester_credits=round(fixed_credits + max_possible, 4),
        category_composition=cat_composition, num_must_have=num_must_have, num_backup=num_backup,
        num_impossible=num_impossible, num_unconfirmed=num_unconfirmed, items=resolved, notes=notes,
    )
