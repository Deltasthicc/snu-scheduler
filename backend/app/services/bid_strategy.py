"""Deterministic, reserve-aware bid planning.

This module intentionally does *not* simulate private rival bids.  SNU's
published material specifies the auction settlement and exposes live bidder
counts, but it does not publish historical clearing prices or the distribution
of rival bids.  Consequently, exact win probabilities and expected charges are
not identifiable from the available data.

The planner instead solves the part we can solve honestly: divide each shared
category balance across several courses while retaining a chosen carry-forward
reserve.  Priority is an ordinal utility proxy; the selected posture controls
concentration.  Every heuristic is visible in the returned explanation.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import floor

from app.models.schemas import BidStrategyRequest, Category, Priority, StrategyPosture

STRATEGY_VERSION = "allocation-v1"

PRIORITY_WEIGHT = {
    Priority.MUST: 8.0,
    Priority.STRONG: 5.0,
    Priority.BACKUP: 2.0,
    Priority.OPTIONAL: 1.0,
}

# Personal concentration guardrails, expressed as a fraction of the usable
# category envelope.  They are planning defaults, not University bid limits.
MAX_SHARE = {
    StrategyPosture.DIVERSIFIED: {
        Priority.MUST: 0.45, Priority.STRONG: 0.35,
        Priority.BACKUP: 0.25, Priority.OPTIONAL: 0.15,
    },
    StrategyPosture.BALANCED: {
        Priority.MUST: 0.60, Priority.STRONG: 0.45,
        Priority.BACKUP: 0.30, Priority.OPTIONAL: 0.20,
    },
    StrategyPosture.FOCUSED: {
        Priority.MUST: 0.85, Priority.STRONG: 0.65,
        Priority.BACKUP: 0.40, Priority.OPTIONAL: 0.25,
    },
}


@dataclass(frozen=True)
class _Candidate:
    code: str
    weight: float
    cap: int


def _pressure(course) -> tuple[dict, float, str]:
    """Return an observed pressure band, opening fraction and action.

    Counts are deliberately not mapped to a price or probability.  They only
    control how much of an independently-derived personal ceiling to expose in
    the opening bid.
    """
    if course.live_bidders is None or course.seats <= 0:
        return ({
            "label": "unknown", "live_bidders": course.live_bidders,
            "seats": course.seats, "bidder_to_seat_ratio": None,
            "provenance": "unknown",
        }, 0.50, "Use this as a provisional opening bid; update the live bidder count before the round closes.")

    ratio = course.live_bidders / course.seats
    if ratio < 0.80:
        label, fraction = "spare_capacity", 0.15
        action = "Start low and monitor; the observed bidder count is still below available seats."
    elif ratio <= 1.00:
        label, fraction = "near_capacity", 0.35
        action = "Use a moderate opening bid and re-check shortly before the round closes."
    elif ratio <= 1.50:
        label, fraction = "oversubscribed", 0.65
        action = "The live count exceeds seats; move toward your ceiling only if the course remains worth that trade-off."
    else:
        label, fraction = "heavily_oversubscribed", 0.85
        action = "Competition is visibly heavy; do not chase beyond your personal ceiling. Keep alternatives live."
    return ({
        "label": label, "live_bidders": course.live_bidders,
        "seats": course.seats, "bidder_to_seat_ratio": round(ratio, 3),
        "provenance": "live",
    }, fraction, action)


def _capped_proportional(candidates: list[_Candidate], budget: int) -> dict[str, int]:
    """Weighted water-filling with integer, deterministic rounding.

    Properties used by the UI/tests:
    - budget feasible: sum(result) <= budget;
    - capped: result[c] <= candidate.cap;
    - order independent: code is the only tie-breaker;
    - diversified: when budget permits, every candidate receives at least one
      point before the remaining envelope is allocated by marginal weight.
    """
    if budget <= 0 or not candidates:
        return {candidate.code: 0 for candidate in candidates}

    ordered = sorted(candidates, key=lambda candidate: candidate.code)
    allocation = {candidate.code: 0 for candidate in ordered}
    remaining = budget

    eligible = [candidate for candidate in ordered if candidate.cap > 0]
    if remaining >= len(eligible):
        for candidate in eligible:
            allocation[candidate.code] = 1
        remaining -= len(eligible)
    else:
        for candidate in sorted(eligible, key=lambda item: (-item.weight, item.code))[:remaining]:
            allocation[candidate.code] = 1
        return allocation

    active = {candidate.code: candidate for candidate in eligible
              if allocation[candidate.code] < candidate.cap}
    fractional: dict[str, float] = {}
    while remaining > 0 and active:
        total_weight = sum(candidate.weight for candidate in active.values())
        proposed = {
            code: remaining * candidate.weight / total_weight
            for code, candidate in active.items()
        }
        saturated = [code for code, amount in proposed.items()
                     if amount >= active[code].cap - allocation[code]]
        if saturated:
            for code in sorted(saturated):
                room = active[code].cap - allocation[code]
                allocation[code] += room
                remaining -= room
                del active[code]
            continue

        whole_used = 0
        fractional = {}
        for code, amount in proposed.items():
            whole = floor(amount)
            whole = min(whole, active[code].cap - allocation[code])
            allocation[code] += whole
            whole_used += whole
            fractional[code] = amount - floor(amount)
        remaining -= whole_used
        break

    if remaining > 0 and active:
        # Largest-remainder rounding. Weight and then code make ties stable.
        order = sorted(active, key=lambda code: (-fractional.get(code, 0.0),
                                                  -active[code].weight, code))
        while remaining > 0:
            progressed = False
            for code in order:
                if allocation[code] < active[code].cap:
                    allocation[code] += 1
                    remaining -= 1
                    progressed = True
                    if remaining == 0:
                        break
            if not progressed:
                break
    return allocation


def build_bid_strategy(request: BidStrategyRequest) -> dict:
    grouped = defaultdict(list)
    for course in request.courses:
        grouped[course.category].append(course)

    category_rows = []
    course_rows = []
    all_feasible = True

    for category in Category:
        pool = int(request.pools[category.value])
        reserve = round(pool * request.reserve_percent / 100)
        envelope = max(0, pool - reserve)
        courses = grouped[category]

        candidates = []
        for course in courses:
            max_share = MAX_SHARE[request.posture][course.priority]
            concentration_cap = min(envelope, floor(envelope * max_share))
            if envelope > 0 and concentration_cap == 0:
                concentration_cap = 1
            candidates.append(_Candidate(
                code=course.code,
                weight=PRIORITY_WEIGHT[course.priority],
                cap=concentration_cap,
            ))

        ceilings = _capped_proportional(candidates, envelope)
        ceiling_total = sum(ceilings.values())
        all_feasible = all_feasible and ceiling_total <= envelope <= pool

        category_rows.append({
            "category": category,
            "pool": pool,
            "carry_forward_reserve": reserve,
            "current_round_envelope": envelope,
            "strategic_ceiling_total": ceiling_total,
            "uncommitted_in_envelope": envelope - ceiling_total,
            "course_count": len(courses),
        })

        for course in sorted(courses, key=lambda item: item.code):
            pressure, opening_fraction, action = _pressure(course)
            ceiling = ceilings[course.code]
            opening = min(ceiling, round(ceiling * opening_fraction))
            if ceiling > 0 and opening_fraction > 0 and opening == 0:
                opening = 1
            share = 0.0 if pool == 0 else round(100 * ceiling / pool, 1)
            max_share = round(100 * MAX_SHARE[request.posture][course.priority])
            live_reason = ("No live bidder count supplied; price and win probability remain unknown."
                           if pressure["provenance"] == "unknown" else
                           f"Live pressure: {course.live_bidders} bidder(s) for {course.seats} seat(s); this changes the opening step, not the ceiling.")
            course_rows.append({
                "code": course.code,
                "title": course.title,
                "category": category,
                "priority": course.priority,
                "opening_bid": opening,
                "strategic_ceiling": ceiling,
                "allocation_share_percent": share,
                "pressure": pressure,
                "action": action,
                "rationale": [
                    f"{course.priority.value.title()} priority carries weight {PRIORITY_WEIGHT[course.priority]:g}.",
                    f"{request.posture.value.title()} posture limits this course to {max_share}% of the usable {category.value} envelope.",
                    live_reason,
                ],
            })

    course_rows.sort(key=lambda row: (row["category"].value, -PRIORITY_WEIGHT[row["priority"]], row["code"]))
    return {
        "strategy_version": STRATEGY_VERSION,
        "posture": request.posture,
        "reserve_percent": request.reserve_percent,
        "semester": request.semester,
        "courses": course_rows,
        "categories": category_rows,
        "invariants": {
            "shared_category_balances_respected": all_feasible,
            "carry_forward_reserve_protected": True,
            "no_synthetic_rival_bids": True,
            "no_claimed_win_probabilities": True,
        },
        "uncertainty": (
            "SNU publishes the settlement mechanism and live bidder counts, but not rival bids or a historical "
            "clearing-price distribution. A defensible win probability or expected charge cannot be calculated "
            "from the available information. Opening bids and ceilings are transparent planning heuristics, not guarantees."
        ),
        "official_mechanism": [
            "ME, UWE and CCC use separate live point balances.",
            "Simultaneous bids hold points against the relevant category balance.",
            "Winners pay the lowest winning bid; excess points and losing bids are refunded.",
            "Unused points carry forward; fourth-year students receive no fresh Semester 8 allocation.",
        ],
        "next_steps": [
            "Enter the opening bids and keep alternatives active in the same category.",
            "Refresh live bidder counts before the round closes; revise bids without exceeding personal ceilings.",
            "After settlement, re-plan with the refunded balance and actual course outcomes.",
        ],
    }
