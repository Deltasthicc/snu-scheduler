"""Properties of the default bid planner.

These tests assert mathematical invariants rather than one hand-picked output.
The planner must never reintroduce synthetic clearing prices through a future
refactor.
"""
from __future__ import annotations

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
        assert response.json()["strategy_version"] == "marginal-value-v2"

        payload["courses"][1]["code"] = "A"
        duplicate = client.post("/api/v1/bid-strategy", json=payload)
        assert duplicate.status_code == 422
