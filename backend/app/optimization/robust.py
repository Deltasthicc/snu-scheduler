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
    out = {}
    vals = []
    for k, c in curves.items():
        p = float(c[min(bid, len(c) - 1)])
        out[k] = p
        if k != "OPTIMISTIC":
            vals.append(p)
    vals.sort()
    out["_worst"] = vals[0] if vals else 0.0
    out["_mean"] = sum(vals) / len(vals) if vals else 0.0
    half = max(1, (len(vals) + 1) // 2)
    out["_cvar"] = sum(vals[:half]) / half if vals else 0.0
    return out


def minimal_robust_bid(curves: dict, cap: int, priority: str, method: str = "minimax") -> dict:
    tgt = PRIORITY.get(priority, PRIORITY[DEFAULT_PRIORITY])
    target = tgt["HIGH"]
    for b in range(cap + 1):
        s = summarise(curves, b)
        metric = s["_cvar"] if method == "cvar" else s["_mean"] if method == "mean" else s["_worst"]
        if metric < target:
            continue
        if method == "minimax":
            tiers = {k: tgt[k] for k in STRESS}
            ok = all(tiers[k] <= 0 or k not in curves or
                     curves[k][min(b, len(curves[k]) - 1)] >= tiers[k] for k in tiers)
            if not ok:
                continue
        return {"bid": b, "target_met": True, "scenarios": summarise(curves, b), "shortfall": []}

    s = summarise(curves, cap)
    tiers = {k: tgt[k] for k in STRESS}
    short = [{"tier": k, "need": tiers[k], "got": float(curves[k][cap])}
             for k in tiers if tiers[k] > 0 and k in curves and curves[k][cap] < tiers[k]]
    agg = s["_cvar"] if method == "cvar" else s["_mean"] if method == "mean" else s["_worst"]
    if agg < target:
        short.append({"tier": f"{method} aggregate", "need": target, "got": agg})
    return {"bid": cap, "target_met": False, "scenarios": s, "shortfall": short}


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
