"""minimal_robust_bid() had no dedicated tests before this file, despite
brute_force() existing specifically to validate it - the exact gap that let a
real bug through: the fast-path's first-pass gate compared the worst win
probability across ALL stress tiers (including EXTREME, whose published
target is 0 for every priority except MUST-adjacent ones) against the HIGH
tier's own target, so a course that only needed to clear HIGH/VERY_HIGH could
be reported "target not reachable" purely because EXTREME - a tier this
priority was never held to - happened to be low. Fixed 2026-08-06."""
from __future__ import annotations
import numpy as np
import pytest

from app.optimization.robust import minimal_robust_bid, brute_force, PRIORITY, STRESS


def _step_curves(caps: dict[str, float], length: int) -> dict:
    """A curve that reaches its cap-probability at bid 0 and stays flat -
    the simplest shape that isolates the target-comparison logic itself
    from win-probability-curve shape."""
    return {k: np.full(length + 1, v) for k, v in caps.items()}


def test_a_course_that_clears_its_required_tiers_is_reported_reachable():
    # Regression for the exact shape reported live: HIGH and VERY_HIGH both
    # comfortably clear STRONG's own targets (0.95, 0.875); EXTREME does not,
    # but STRONG's own EXTREME target is 0 - not a bar this priority faces.
    cap = 297
    curves = {
        "HIGH": np.concatenate([np.linspace(0, 0.999, cap), [0.999]]),
        "VERY_HIGH": np.concatenate([np.linspace(0, 0.94, cap), [0.94]]),
        "EXTREME": np.concatenate([np.linspace(0, 0.56, cap), [0.56]]),
    }
    for method in ("minimax", "mean", "cvar"):
        r = minimal_robust_bid(curves, cap, "STRONG", method)
        assert r["target_met"] is True, f"{method}: {r}"
        assert r["bid"] < cap, f"{method} should not need the full cap: {r}"


def test_extreme_never_gates_a_priority_that_does_not_require_it():
    cap = 100
    curves = _step_curves({"HIGH": 1.0, "VERY_HIGH": 1.0, "EXTREME": 0.0}, cap)
    for priority in ("STRONG", "BACKUP", "OPTIONAL"):
        r = minimal_robust_bid(curves, cap, priority, "minimax")
        assert r["target_met"] is True, f"{priority}: {r}"
        assert r["bid"] == 0


def test_must_priority_still_requires_high_and_very_high_not_extreme():
    cap = 100
    curves = _step_curves({"HIGH": 0.99, "VERY_HIGH": 0.95, "EXTREME": 0.0}, cap)
    r = minimal_robust_bid(curves, cap, "MUST", "minimax")
    assert r["target_met"] is True
    curves_short = _step_curves({"HIGH": 0.98, "VERY_HIGH": 0.95, "EXTREME": 0.0}, cap)
    r2 = minimal_robust_bid(curves_short, cap, "MUST", "minimax")
    assert r2["target_met"] is False
    assert any(s["tier"] == "HIGH" for s in r2["shortfall"])


def test_minimax_matches_brute_force_on_random_curves():
    rng = np.random.default_rng(7)
    for trial in range(25):
        cap = int(rng.integers(3, 12))
        priority = rng.choice(["MUST", "STRONG", "BACKUP", "OPTIONAL"])
        # monotone non-decreasing win curves, as any real simulate_course() output is
        curves = {k: np.sort(rng.random(cap + 1)) for k in STRESS}
        got = minimal_robust_bid(curves, cap, priority, "minimax")
        want = brute_force([{"code": "x", "cap": cap, "priority": priority, "curves": curves}], cap, "minimax")
        want_bid = None if want["bids"] is None else want["bids"][0]
        if want_bid is None:
            assert got["target_met"] is False, f"trial {trial}: {got} vs brute_force {want}"
        else:
            assert got["target_met"] is True, f"trial {trial}: {got} vs brute_force {want}"
            assert got["bid"] == want_bid, f"trial {trial}: {got['bid']} != brute_force {want_bid}"


def test_mean_and_cvar_can_accept_a_bid_minimax_would_reject():
    # mean/cvar are deliberately more permissive than minimax: one relevant tier
    # overshooting its own target can offset another undershooting slightly.
    # STRONG needs HIGH>=0.95, VERY_HIGH>=0.875. HIGH overshoots by 0.049,
    # VERY_HIGH undershoots by 0.025 - blended prob 0.9245 clears blended
    # target 0.9125 even though VERY_HIGH alone does not clear its own bar.
    cap = 100
    curves = _step_curves({"HIGH": 0.999, "VERY_HIGH": 0.85, "EXTREME": 0.0}, cap)
    minimax = minimal_robust_bid(curves, cap, "STRONG", "minimax")
    mean = minimal_robust_bid(curves, cap, "STRONG", "mean")
    assert minimax["target_met"] is False
    assert mean["target_met"] is True


@pytest.mark.parametrize("priority", list(PRIORITY.keys()))
def test_a_priority_with_no_relevant_tiers_is_never_reported_unreachable(priority):
    # BACKUP/OPTIONAL only require HIGH>0; confirm nothing weird happens when a
    # course has zero win probability everywhere except an irrelevant tier.
    cap = 10
    curves = _step_curves({"HIGH": 0.0, "VERY_HIGH": 0.0, "EXTREME": 1.0}, cap)
    r = minimal_robust_bid(curves, cap, priority, "minimax")
    tgt = PRIORITY[priority]
    if all(tgt.get(k, 0) <= 0 for k in STRESS):
        assert r["target_met"] is True
    else:
        assert r["target_met"] is False
