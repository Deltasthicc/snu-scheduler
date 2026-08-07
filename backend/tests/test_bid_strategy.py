"""Properties of the default bid planner.

These tests assert mathematical invariants rather than one hand-picked output.
The planner must never reintroduce synthetic clearing prices through a future
refactor.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import BidStrategyRequest
from app.services.bid_strategy import build_bid_strategy


def _course(code: str, category: str = "ME", priority: str = "STRONG",
            live_bidders: int | None = None, seats: int = 80) -> dict:
    return {
        "code": code, "title": code, "category": category, "credits": 4,
        "seats": seats, "priority": priority, "live_bidders": live_bidders,
    }


def _request(courses: list[dict], **overrides) -> BidStrategyRequest:
    payload = {
        "courses": courses,
        "pools": {"ME": 297, "UWE": 215, "CCC": 492},
        "reserve_percent": 20,
        "posture": "balanced",
        "semester": 7,
    }
    payload.update(overrides)
    return BidStrategyRequest.model_validate(payload)


def test_every_category_is_budget_feasible_and_reserve_is_protected():
    result = build_bid_strategy(_request([
        _course("M1"), _course("M2", priority="MUST"),
        _course("U1", category="UWE"), _course("C1", category="CCC"),
    ]))
    for row in result["categories"]:
        assert row["strategic_ceiling_total"] <= row["current_round_envelope"]
        assert row["current_round_envelope"] + row["carry_forward_reserve"] == row["pool"]
        assert row["strategic_ceiling_total"] + row["uncommitted_in_envelope"] == row["current_round_envelope"]
    assert all(result["invariants"].values())


def test_equal_priority_courses_are_diversified_instead_of_greedily_zeroed():
    result = build_bid_strategy(_request([_course("A"), _course("B"), _course("C")]))
    ceilings = {row["code"]: row["strategic_ceiling"] for row in result["courses"]}
    assert min(ceilings.values()) > 0
    assert max(ceilings.values()) - min(ceilings.values()) <= 1


def test_result_is_permutation_invariant():
    courses = [_course("Z", priority="OPTIONAL"), _course("A", priority="MUST"), _course("M")]
    forward = build_bid_strategy(_request(courses))
    reverse = build_bid_strategy(_request(list(reversed(courses))))
    f = {row["code"]: (row["opening_bid"], row["strategic_ceiling"]) for row in forward["courses"]}
    r = {row["code"]: (row["opening_bid"], row["strategic_ceiling"]) for row in reverse["courses"]}
    assert f == r


def test_single_course_does_not_consume_the_whole_pool_by_default():
    result = build_bid_strategy(_request([_course("CSD361")]))
    course = result["courses"][0]
    assert 0 < course["opening_bid"] <= course["strategic_ceiling"]
    assert course["strategic_ceiling"] < 297
    me = next(row for row in result["categories"] if row["category"].value == "ME")
    assert me["carry_forward_reserve"] == 59
    assert me["uncommitted_in_envelope"] > 0


def test_live_counts_reallocate_the_plan_toward_observed_pressure():
    low = build_bid_strategy(_request([_course("A", live_bidders=20)]))["courses"][0]
    high = build_bid_strategy(_request([_course("A", live_bidders=160)]))["courses"][0]
    assert low["strategic_ceiling"] < high["strategic_ceiling"]
    assert low["opening_bid"] <= high["opening_bid"]
    assert low["pressure"]["provenance"] == high["pressure"]["provenance"] == "live"


def test_response_exposes_bounded_probability_band_but_no_expected_price():
    result = build_bid_strategy(_request([_course("A")]))
    encoded = str(result).lower()
    assert "win_at_bid" not in encoded
    assert "expected_charge" not in encoded
    band = result["courses"][0]["win_probability_band"]
    assert 0 <= band["low"] <= band["central"] <= band["high"] <= 1
    assert band["basis"] in {"live bidder count", "modelled rival count"}


def test_a_bid_is_not_a_spend_so_a_bigger_bid_never_raises_the_modelled_price():
    """SNU charges the clearing price, not the bid, and the clearing price is set
    by other students. An earlier draft charged an opportunity cost for every
    point *committed*, which systematically under-bid because it billed the
    student for points that get refunded. The price must depend on the market
    (seats and rivals) and never on the student's own pool or bid."""
    small = build_bid_strategy(_request([_course("A", seats=20, live_bidders=140)],
                                        pools={"ME": 120, "UWE": 215, "CCC": 492}))
    large = build_bid_strategy(_request([_course("A", seats=20, live_bidders=140)],
                                        pools={"ME": 450, "UWE": 215, "CCC": 492}))
    price_small = small["courses"][0]["clearing_price_band"]
    price_large = large["courses"][0]["clearing_price_band"]
    assert price_small == price_large, "the clearing price must not track the user's own balance"
    # More budget must still buy a genuinely better chance - the v1 audit's
    # critical scale-invariance defect must not reappear.
    assert (large["courses"][0]["win_probability_band"]["central"]
            >= small["courses"][0]["win_probability_band"]["central"])


def test_a_scarcer_course_is_modelled_to_clear_higher():
    result = build_bid_strategy(_request([
        _course("SCARCE", seats=20, live_bidders=140),
        _course("PLENTIFUL", seats=90, live_bidders=95),
    ]))
    by_code = {row["code"]: row["clearing_price_band"] for row in result["courses"]}
    assert by_code["SCARCE"]["central"] > by_code["PLENTIFUL"]["central"]


def test_an_undersubscribed_course_is_modelled_to_clear_at_zero():
    """SNU states directly that a course with seats left over clears at zero."""
    result = build_bid_strategy(_request([_course("QUIET", seats=90, live_bidders=30)]))
    assert result["courses"][0]["clearing_price_band"]["high"] == 0


def test_plan_never_exceeds_the_envelope_across_many_shapes():
    """The one invariant that must hold unconditionally: points on live bids are
    held against the category balance, so the plan has to fit inside it."""
    shapes = [
        [_course("A", seats=20, live_bidders=200), _course("B", seats=300)],
        [_course(c, priority=p) for c, p in
         (("A", "MUST"), ("B", "STRONG"), ("C", "BACKUP"), ("D", "OPTIONAL"))],
        [_course("A", seats=5, live_bidders=400)],
    ]
    for posture in ("focused", "balanced", "diversified"):
        for reserve in (0, 20, 60):
            for shape in shapes:
                result = build_bid_strategy(_request(shape, posture=posture,
                                                     reserve_percent=reserve))
                for row in result["categories"]:
                    assert row["strategic_ceiling_total"] <= row["current_round_envelope"]
                    assert row["current_round_envelope"] + row["carry_forward_reserve"] == row["pool"]
                for row in result["courses"]:
                    assert 0 <= row["opening_bid"] <= row["strategic_ceiling"]


def test_http_contract_rejects_duplicate_courses_and_returns_strategy():
    with TestClient(app) as client:
        payload = _request([_course("A"), _course("B")]).model_dump(mode="json")
        response = client.post("/api/v1/bid-strategy", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["strategy_version"] == "marginal-value-v3"

        payload["courses"][1]["code"] = "A"
        duplicate = client.post("/api/v1/bid-strategy", json=payload)
        assert duplicate.status_code == 422


# --------------------------------------------------------------------------
# marginal-value-v3: the reported defect was two Major Electives that differed
# only in seat count (CSD358 at 120 seats, CSD361 at 80) being planned at 166
# and 72 points. These pin the properties that made that possible.
# --------------------------------------------------------------------------


def _ceilings(courses: list[dict], **overrides) -> dict[str, int]:
    result = build_bid_strategy(_request(courses, **overrides))
    return {row["code"]: row["strategic_ceiling"] for row in result["courses"]}


def test_two_identical_courses_get_identical_bids():
    """The objective is symmetric under swapping identical courses, so the plan
    has to be. The DP returns *an* optimum and used to return a lopsided one."""
    for seats in (40, 80):
        plan = _ceilings([_course("A", seats=seats), _course("B", seats=seats)])
        assert plan["A"] == plan["B"], f"{seats} seats: {plan}"


def test_three_identical_courses_split_evenly():
    plan = _ceilings([_course("A"), _course("B"), _course("C")])
    assert len(set(plan.values())) == 1, plan


def test_the_even_split_is_always_an_available_breadth_option():
    """A cap ladder of fixed shares could not express one-over-n, so for two
    courses the even split (share 0.50) was never offered and two identical
    courses came back at 130/108 with the even split costing only 0.24%."""
    from app.services.bid_strategy import _cap_candidates
    for count in (2, 3, 4, 5, 7):
        candidates = _cap_candidates(count)
        assert candidates[0] == pytest.approx(1.0 / count), (count, candidates)
        # Nothing tighter: a cap below 1/n forces idle budget and is weakly
        # dominated, since win curves are monotone non-decreasing.
        assert min(candidates) >= 1.0 / count - 1e-12


def test_a_two_seat_change_cannot_swing_the_plan():
    """The staircase objective flipped a 22-point allocation between two courses
    on an objective gap of 3.4e-05. Sweeping one course's seat count must now
    move the plan smoothly, not oscillate."""
    previous = None
    flips = 0
    for seats in range(70, 120, 2):
        plan = _ceilings([_course("X", seats=120), _course("Y", seats=seats)])
        if previous is not None and abs(plan["Y"] - previous) > 8:
            flips += 1
        previous = plan["Y"]
    assert flips == 0, f"{flips} noise-driven swings across the sweep"


def test_priority_still_outranks_a_scarcity_tie_break():
    """The tie-break may only fire where the model genuinely cannot separate two
    courses. A must-have beside an optional is separable by 8% of plan value and
    must never be reordered, however the seats fall."""
    plan = _ceilings([_course("BIG", seats=300, priority="MUST"),
                      _course("SMALL", seats=20, priority="OPTIONAL")])
    assert plan["BIG"] > plan["SMALL"]


def test_an_observed_count_still_outranks_a_scarcity_tie_break():
    plan = _ceilings([_course("BUSY", seats=200, live_bidders=600),
                      _course("QUIET", seats=60, live_bidders=20)])
    assert plan["BUSY"] > plan["QUIET"]


def test_win_curve_is_not_a_staircase():
    """The three-atom mixture left 47-56% of the budget range buying literally
    nothing, which is what made the allocator solve a knapsack over cliffs."""
    result = build_bid_strategy(_request([_course("A", seats=80)]))
    assert result["courses"][0]["strategic_ceiling"] > 0
    # Re-derive the curve the optimiser actually saw.
    import numpy as np
    from app.services.bid_strategy import (
        Category, _log_factorial_table, _market_nodes, _rivals_at,
        _rival_outrank_at_concentration, _win_curve,
    )
    envelope, seats = 238, 80
    grid = np.arange(298, dtype=float)
    nodes = _market_nodes()
    log_fact = _log_factorial_table(2000)
    course = _request([_course("A", seats=seats)]).courses[0]
    curve = np.zeros(envelope + 1)
    for rivals_per_seat, concentration in nodes:
        outrank = _rival_outrank_at_concentration(grid, Category.ME, concentration)
        curve += _win_curve(seats, _rivals_at(course, rivals_per_seat),
                            outrank[: envelope + 1], log_fact)
    curve /= len(nodes)
    dead = sum(1 for b in range(1, envelope + 1) if abs(curve[b] - curve[b - 1]) < 1e-6)
    assert dead / envelope < 0.25, f"{dead}/{envelope} of the bid range buys nothing"
    assert np.all(np.diff(curve) >= -1e-12), "win curve must never fall as the bid rises"


def test_named_readings_are_quantiles_of_the_distribution_that_is_optimised():
    """The reported band and the objective have to describe one belief, not two."""
    from app.services.bid_strategy import SCENARIOS, _ANCHOR_QUANTILES, _market_state
    assert len(_ANCHOR_QUANTILES) == len(SCENARIOS)
    for quantile, scenario in zip(_ANCHOR_QUANTILES, SCENARIOS):
        rivals, concentration = _market_state(quantile)
        assert rivals == pytest.approx(scenario.rivals_per_seat)
        assert concentration == pytest.approx(scenario.courses_per_rival)
    # Monotone in tightness, and never extrapolated past the published anchors.
    states = [_market_state(k / 200) for k in range(201)]
    assert all(a[0] <= b[0] + 1e-12 for a, b in zip(states, states[1:]))
    assert all(a[1] >= b[1] - 1e-12 for a, b in zip(states, states[1:]))
    assert min(s[0] for s in states) == pytest.approx(SCENARIOS[0].rivals_per_seat)
    assert max(s[0] for s in states) == pytest.approx(SCENARIOS[-1].rivals_per_seat)
