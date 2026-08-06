"""Robust bid recommendation: smallest bid clearing a reliability target in the
worst tested scenario. Ported from core/robust.js with the dead-method bug
already fixed (mutation testing caught that the method parameter did nothing)."""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass

PRIORITY = {
    "MUST":     {"label": "Must have",           "HIGH": 0.99, "VERY_HIGH": 0.95, "EXTREME": 0.0, "rank": 0},
    "STRONG":   {"label": "Strongly preferred",  "HIGH": 0.95, "VERY_HIGH": 0.875, "EXTREME": 0.0, "rank": 1},
    "BACKUP":   {"label": "Useful backup",       "HIGH": 0.80, "VERY_HIGH": 0.0,  "EXTREME": 0.0, "rank": 2},
    "OPTIONAL": {"label": "Optional",            "HIGH": 0.50, "VERY_HIGH": 0.0,  "EXTREME": 0.0, "rank": 3},
}
DEFAULT_PRIORITY = "STRONG"
STRESS = ("HIGH", "VERY_HIGH", "EXTREME")


def summarise(curves: dict, bid: int) -> dict:
    """_worst/_mean/_cvar reflect only the mandatory stress tiers (STRESS) - an
    allowlist, not a denylist naming "OPTIMISTIC" specifically, so any future
    comparison-only tier (LOW, MODERATE, ...) is excluded from these display
    aggregates the same way without needing this function to know its name."""
    out = {}
    vals = []
    for k, c in curves.items():
        p = float(c[min(bid, len(c) - 1)])
        out[k] = p
        if k in STRESS:
            vals.append(p)
    vals.sort()
    out["_worst"] = vals[0] if vals else 0.0
    out["_mean"] = sum(vals) / len(vals) if vals else 0.0
    half = max(1, (len(vals) + 1) // 2)
    out["_cvar"] = sum(vals[:half]) / half if vals else 0.0
    return out


def _relevant_tiers(tgt: dict, curves: dict) -> list[str]:
    """Tiers this priority is actually held to (published target > 0). EXTREME=0
    for STRONG/BACKUP/OPTIONAL (and every non-MUST tier below its own rank) means
    that scenario is reported to the student as a stress test, never a bar this
    priority must clear - matching brute_force()'s own meets() oracle exactly."""
    return [k for k in STRESS if tgt.get(k, 0) > 0 and k in curves]


def _blend(values: list[float], method: str) -> float:
    vals = sorted(values)
    if method == "mean":
        return sum(vals) / len(vals)
    if method == "cvar":
        half = max(1, (len(vals) + 1) // 2)
        return sum(vals[:half]) / half
    return vals[0]  # minimax / worst


def minimal_robust_bid(curves: dict, cap: int, priority: str, method: str = "minimax") -> dict:
    """Smallest bid clearing every tier this priority is actually held to.

    minimax requires each relevant tier to individually clear its own published
    target (identical to brute_force()'s meets() oracle - verified by
    test_minimal_robust_bid_matches_brute_force_on_random_curves). mean/cvar
    relax that to a blended probability against a matching blended target, so a
    tier that overshoots its own bar can offset one that undershoots, while a
    tier this priority was never held to (target 0) still never enters either
    the probability blend or the target blend for any method.
    """
    tgt = PRIORITY.get(priority, PRIORITY[DEFAULT_PRIORITY])
    relevant = _relevant_tiers(tgt, curves)
    if not relevant:
        return {"bid": 0, "target_met": True, "scenarios": summarise(curves, 0), "shortfall": []}
    blended_target = _blend([tgt[k] for k in relevant], method)

    for b in range(cap + 1):
        probs = {k: float(curves[k][min(b, len(curves[k]) - 1)]) for k in relevant}
        if method == "minimax":
            ok = all(probs[k] >= tgt[k] for k in relevant)
        else:
            ok = _blend(list(probs.values()), method) >= blended_target
        if ok:
            return {"bid": b, "target_met": True, "scenarios": summarise(curves, b), "shortfall": []}

    probs = {k: float(curves[k][cap]) for k in relevant}
    short = [{"tier": k, "need": tgt[k], "got": probs[k]} for k in relevant if probs[k] < tgt[k]]
    if method != "minimax":
        agg = _blend(list(probs.values()), method)
        if agg < blended_target:
            short.append({"tier": f"{method} aggregate", "need": round(blended_target, 4), "got": round(agg, 4)})
    return {"bid": cap, "target_met": False, "scenarios": summarise(curves, cap), "shortfall": short}


def allocate(items: list[dict], pool: int, budget_mode: str = "SHARED_LIVE",
             method: str = "minimax") -> dict:
    order = sorted(items, key=lambda it: PRIORITY.get(it.get("priority", DEFAULT_PRIORITY),
                                                      PRIORITY[DEFAULT_PRIORITY])["rank"])
    res = {}
    for it in order:
        r = minimal_robust_bid(it["curves"], it["cap"], it.get("priority", DEFAULT_PRIORITY), method)
        res[it["code"]] = {"code": it["code"], "cap": it["cap"],
                           "priority": it.get("priority", DEFAULT_PRIORITY),
                           "bid": r["bid"], "target_met": r["target_met"],
                           "shortfall": r["shortfall"], "scenarios": r["scenarios"],
                           "reduced": False}
    committed = sum(v["bid"] for v in res.values())
    if budget_mode == "INDEPENDENT":
        return {"bids": res, "committed": committed, "pool": pool, "budget_mode": budget_mode,
                "feasible": True, "sacrificed": [],
                "note": "Each bid is provisional; no aggregate limit applies before settlement."}

    sacrificed = []
    if committed > pool:
        for it in reversed(order):
            if committed <= pool:
                break
            r = res[it["code"]]
            cut = min(committed - pool, r["bid"])
            if cut > 0:
                r["bid"] -= cut; r["reduced"] = True; r["target_met"] = False
                r["scenarios"] = summarise(it["curves"], r["bid"])
                committed -= cut
                sacrificed.append({"code": it["code"], "cut": cut, "priority": r["priority"]})
    return {"bids": res, "committed": committed, "pool": pool, "budget_mode": budget_mode,
            "feasible": committed <= pool, "sacrificed": sacrificed,
            "note": ("Your category budget could not protect every course at its reliability target. "
                     "Points were removed from the lowest-priority courses first.")
            if sacrificed else "All selected courses meet their reliability target within the pool."}


def brute_force(items: list[dict], pool: int, method: str = "minimax") -> dict:
    """Exhaustive minimum-cost feasible allocation, for validating `allocate`."""
    n = len(items)
    best, best_cost = None, float("inf")
    cur = [0] * n

    def meets(i, b):
        t = PRIORITY.get(items[i].get("priority", DEFAULT_PRIORITY), PRIORITY[DEFAULT_PRIORITY])
        return all(t[k] <= 0 or k not in items[i]["curves"] or
                   items[i]["curves"][k][min(b, len(items[i]["curves"][k]) - 1)] >= t[k] for k in STRESS)

    def rec(i, spent):
        nonlocal best, best_cost
        if spent > pool:
            return
        if i == n:
            if all(meets(k, cur[k]) for k in range(n)) and spent < best_cost:
                best_cost = spent; best = cur[:]
            return
        for b in range(items[i]["cap"] + 1):
            cur[i] = b
            rec(i + 1, spent + b)
        cur[i] = 0

    rec(0, 0)
    return {"bids": best, "committed": None if best is None else best_cost}
