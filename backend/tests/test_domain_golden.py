"""Golden fixtures reproduced from docs/RULES_REFERENCE.md's own worked examples.
These pin down the exact numbers the Concept Note and the bidding guide use, so
a change that silently breaks pool math or settlement fails loudly here rather
than being caught by hand-verification in a chat session."""
from __future__ import annotations

from app.domain.auction import settle
from app.domain.pools import compute_pools


def test_pools_match_the_concept_notes_own_worked_example():
    # 4th year, Sem 7: 9 ME / 4 UWE / 11 CCC remaining, 6 floater -> 297/215/492
    p = compute_pools("y4", 7, 9, 4, 11, 6)
    assert (p["ME"], p["UWE"], p["CCC"]) == (297, 215, 492)


def test_pools_match_the_secondary_worked_example():
    # 12/4/6/6 -> 342/215/342, per docs/RULES_REFERENCE.md #40
    p = compute_pools("y4", 7, 12, 4, 6, 6)
    assert (p["ME"], p["UWE"], p["CCC"]) == (342, 215, 342)


def test_settlement_example_1_all_three_win_lowest_bid_clears():
    r = settle(3, [{"id": "a", "bid": 900}, {"id": "b", "bid": 800}, {"id": "c", "bid": 650}])
    assert r["clearing_price"] == 650
    assert r["winners"] == ["a", "b", "c"]
    refunds = {x["id"]: x["refunded"] for x in r["results"]}
    assert refunds == {"a": 250, "b": 150, "c": 0}


def test_settlement_example_2_unfilled_seats_clear_at_zero():
    r = settle(3, [{"id": "a", "bid": 900}, {"id": "b", "bid": 800}])
    assert r["clearing_price"] == 0
    assert r["seats_unfilled"] == 1
    assert all(x["refunded"] == x["bid"] for x in r["results"])


def test_settlement_example_3_tiebreak_never_overrides_bid_size():
    # 2 seats, bids 800/700/700/600; D has the lowest tie-break but loses on bid size
    r = settle(2, [
        {"id": "a", "bid": 800, "tie_break": 253456},
        {"id": "b", "bid": 700, "tie_break": 403456},
        {"id": "c", "bid": 700, "tie_break": 653456},
        {"id": "d", "bid": 600, "tie_break": 103456},
    ])
    assert set(r["winners"]) == {"a", "b"}
    assert r["clearing_price"] == 700


def test_points_are_conserved():
    r = settle(3, [{"id": "a", "bid": 900}, {"id": "b", "bid": 800}, {"id": "c", "bid": 650},
                   {"id": "d", "bid": 400}])
    assert r["conservation_ok"] is True
    assert r["totals"]["charged"] + r["totals"]["refunded"] == r["totals"]["bid"]


def test_bids_above_cap_are_rejected_not_silently_clamped():
    r = settle(1, [{"id": "a", "bid": 999}], cap=75)
    assert r["rejected"][0]["id"] == "a"
    assert r["winners"] == []
