"""Golden fixtures reproduced from docs/RULES_REFERENCE.md's own worked examples.
These pin down the exact numbers the Concept Note and the bidding guide use, so
a change that silently breaks pool math or settlement fails loudly here rather
than being caught by hand-verification in a chat session."""
from __future__ import annotations

from app.domain.auction import settle
from app.domain.pools import compute_pools, max_bid


def test_pools_match_the_concept_notes_own_worked_example():
    # 4th year, Sem 7: 9 ME / 4 UWE / 11 CCC remaining, 6 floater -> 297/215/492
    p = compute_pools("y4", 7, 9, 4, 11, 6)
    assert (p["ME"], p["UWE"], p["CCC"]) == (297, 215, 492)


def test_pools_match_the_secondary_worked_example():
    # 12/4/6/6 -> 342/215/342, per docs/RULES_REFERENCE.md #40
    p = compute_pools("y4", 7, 12, 4, 6, 6)
    assert (p["ME"], p["UWE"], p["CCC"]) == (342, 215, 342)


def test_y2_pool_matches_the_rectified_concept_note_semester_3_example():
    # 2nd year, Sem 3 first bidding cycle: 36 total ME / 16 total UWE / 12 total CCC
    # (excl EVS), 8 floater (split 4/4 UWE+CCC), 3 UWE credits already completed.
    # UWE: Sem2 carry-forward 30 + Sem3 release 40 - (3 credits x 5) = 55 exactly.
    # Rectified 2026-08-05: deduction is 5 per completed credit, not the prior 10, and
    # the release schedule is a fraction of the TOTAL requirement, not of "remaining".
    p = compute_pools("y2", 3, 36, 13, 12, 8, done_me=0, done_uwe=3, done_ccc=0)
    assert (p["ME"], p["UWE"], p["CCC"]) == (90, 55, 40)


def test_y2_pool_with_zero_completed_credits_matches_faq_typical_average():
    # Course_Enrolment_FAQ_1.pdf: "Typical first-cycle averages are about 90/70/40
    # (ME/UWE/CCC) for 2nd years" - the zero-completed-credit baseline, independently
    # corroborating the total-requirement-based release formula.
    p = compute_pools("y2", 3, 36, 16, 12, 8, done_me=0, done_uwe=0, done_ccc=0)
    assert (p["ME"], p["UWE"], p["CCC"]) == (90, 70, 40)


def test_y2_pool_never_goes_negative():
    # Course_Enrolment_FAQ_1.pdf: "it is floored at zero - it never goes negative."
    p = compute_pools("y2", 2, 0, 0, 0, 0, done_me=1000, done_uwe=1000, done_ccc=1000)
    assert (p["ME"], p["UWE"], p["CCC"]) == (0, 0, 0)


def test_y3_pool_matches_the_concept_notes_own_semester_5_worked_example():
    # Found missing entirely during a 2026-08-10 bias audit: y4 and y2 each had
    # dedicated golden tests but y3 (third-year transition) had none at all -
    # exactly the kind of gap a developer who only ever tested their own year
    # would leave unnoticed. Concept Note s.3: 3rd year, Sem 5, 24 ME / 9 UWE /
    # 6 CCC remaining, 6 floater (split 3/3) -> 161/98/66 exactly.
    p = compute_pools("y3", 5, 24, 9, 6, 6)
    assert (p["ME"], p["UWE"], p["CCC"]) == (161, 98, 66)


def test_y3_pool_semester_share_ratio_is_40_30_30_across_sem_5_6_7():
    # Same remaining-credit profile, only the semester differs - the 40/30/30
    # split of the credit-based component should show up directly in the ratio
    # of (pool - flat constant) across semesters 5/6/7.
    constants = {"ME": 65, "UWE": 50, "CCC": 30}
    shares = {5: 0.40, 6: 0.30, 7: 0.30}
    base = compute_pools("y3", 5, 20, 10, 10, 0)
    for sem in (6, 7):
        p = compute_pools("y3", sem, 20, 10, 10, 0)
        for cat in ("ME", "UWE", "CCC"):
            expected_ratio = shares[sem] / shares[5]
            actual_ratio = (p[cat] - constants[cat]) / (base[cat] - constants[cat])
            assert abs(actual_ratio - expected_ratio) < 0.02, (sem, cat)


def test_y3_pool_only_defines_semesters_5_6_7():
    import pytest
    from app.domain.pools import RuleError
    with pytest.raises(RuleError):
        compute_pools("y3", 4, 20, 10, 10, 0)


def test_y2_pool_cumulative_totals_match_the_full_semester_wise_release_table():
    # Concept Note s.2's own "Semester-wise release schedule" table, verified
    # cumulatively across every semester it defines (not just the Sem-3 point
    # the other y2 tests already cover), for the document's own standard
    # profile: 36 ME / 16 UWE / 12 CCC total requirement, 8 floater, nothing
    # completed yet. Per-semester releases from the doc: ME 0/27/63/72/72/72/54,
    # UWE 0/30/40/40/40/30/20, CCC 0/12/28/32/32/32/24 (Sem 1-7) - the pool at
    # any semester is the running cumulative sum of these.
    cumulative = {"ME": [0, 27, 90, 162, 234, 306, 360],
                  "UWE": [0, 30, 70, 110, 150, 180, 200],
                  "CCC": [0, 12, 40, 72, 104, 136, 160]}
    for sem in range(2, 8):
        p = compute_pools("y2", sem, 36, 16, 12, 8)
        for cat in ("ME", "UWE", "CCC"):
            assert p[cat] == cumulative[cat][sem - 1], (sem, cat)


def test_no_per_course_max_bid_by_default():
    # AUC.MAX_BID, rectified 2026-08-05: "There is no minimum or maximum bid. You can
    # go from zero to your entire pool of bid points for one course."
    assert max_bid(3) is None
    assert max_bid(6) is None


def test_settlement_has_no_cap_when_none_is_supplied():
    r = settle(1, [{"id": "a", "bid": 100_000}])
    assert r["winners"] == ["a"]
    assert r["rejected"] == []


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
