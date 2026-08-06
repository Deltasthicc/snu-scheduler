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


def test_live_counts_change_opening_step_but_not_personal_ceiling():
    low = build_bid_strategy(_request([_course("A", live_bidders=20)]))["courses"][0]
    high = build_bid_strategy(_request([_course("A", live_bidders=160)]))["courses"][0]
    assert low["strategic_ceiling"] == high["strategic_ceiling"]
    assert low["opening_bid"] < high["opening_bid"]
    assert low["pressure"]["provenance"] == high["pressure"]["provenance"] == "live"


def test_response_contains_no_probability_or_expected_price_fields():
    result = build_bid_strategy(_request([_course("A")]))
    encoded = str(result).lower()
    assert "win_at_bid" not in encoded
    assert "expected_charge" not in encoded
    assert result["invariants"]["no_claimed_win_probabilities"]


def test_http_contract_rejects_duplicate_courses_and_returns_strategy():
    with TestClient(app) as client:
        payload = _request([_course("A"), _course("B")]).model_dump(mode="json")
        response = client.post("/api/v1/bid-strategy", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["strategy_version"] == "allocation-v1"

        payload["courses"][1]["code"] = "A"
        duplicate = client.post("/api/v1/bid-strategy", json=payload)
        assert duplicate.status_code == 422
