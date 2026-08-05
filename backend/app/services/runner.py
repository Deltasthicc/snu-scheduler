"""Simulation orchestration: runs a whole plan, with cooperative cancellation
checked at block boundaries inside the engine (not merely discarding stale
results after the CPU has already been burnt)."""
from __future__ import annotations
import hashlib, json, time
import numpy as np

from app.simulation.engine import (simulate_course, expected_rivals, MODES,
                                   STRESS_GRID, Cancelled)
from app.optimization.robust import allocate, summarise, PRIORITY
from app.domain.pools import max_bid
from app.domain.rules import RULE_VERSION, DATASET_VERSION, MODEL_VERSION


def input_hash(req: dict) -> str:
    """Deterministic cache key over normalised inputs, including all versions.

    Includes the *live* course-catalog checksum, not just the static
    DATASET_VERSION string in rules.py: that string only changes when someone
    edits rules.py by hand, so swapping the active timetable dataset (see
    tools/import_netlify_timetable.py) without also bumping it would leave a
    schedule-search result cached under the old data silently reused after
    the catalog changed underneath it - exactly the staleness the timetable-
    revision workflow must not allow. Harmless to include for the simulation
    cache path too, which doesn't depend on the catalog at all."""
    from app.domain import catalog  # local import: avoids a load-order cycle at module import time
    dataset_checksum = catalog.dataset_info().get("dataset_checksum", "unknown")
    norm = json.dumps(req, sort_keys=True, separators=(",", ":"), default=str)
    stamped = f"{RULE_VERSION}|{DATASET_VERSION}|{dataset_checksum}|{MODEL_VERSION}|{norm}"
    return hashlib.blake2b(stamped.encode(), digest_size=16).hexdigest()


def run_plan(req: dict, should_cancel=None, on_progress=None) -> dict:
    t0 = time.perf_counter()
    courses = req["courses"]
    grid = list(STRESS_GRID)
    if req.get("include_optimistic"):
        grid.append("OPTIMISTIC")

    total_units = len(courses) * len(grid)
    unit = 0
    per_course = {}

    for c in courses:
        cap = max_bid(c["credits"])
        curves, paid, ci, beat_q, demand = {}, {}, {}, {}, None
        for mid in grid:
            if should_cancel is not None and should_cancel():
                raise Cancelled()
            opts = {
                "live_bidders": c.get("live_bidders"),
                "live_round": c.get("live_round"),
                "live_observed_at": c.get("live_observed_at"),
                "user_popularity": c.get("user_popularity", 1.0),
                "in_specialisation": c.get("in_specialisation", False),
                "graduation_critical": c.get("priority") == "MUST",
            }
            er = expected_rivals(c, mid, opts)
            r = simulate_course(
                seats=c["seats"], lam=er["lambda"], cap=cap, mode=MODES[mid],
                trials=req["trials"], seed=req["seed"], key=f"{c['code']}|{mid}",
                dispersion=req.get("dispersion", 0.18),
                should_cancel=should_cancel,
            )
            curves[mid] = r.win; paid[mid] = r.paid; ci[mid] = r.ci; beat_q[mid] = r.beat_q
            if mid == req.get("headline_mode") or demand is None:
                demand = er
            unit += 1
            if on_progress:
                on_progress(unit, total_units, c["code"], mid)
        per_course[c["code"]] = {"course": c, "cap": cap, "curves": curves,
                                 "paid": paid, "ci": ci, "beat_q": beat_q, "demand": demand}

    # robust allocation per category
    allocations, bids = [], {}
    for cat in ("ME", "UWE", "CCC"):
        items = [{"code": k, "cap": v["cap"], "priority": v["course"].get("priority", "STRONG"),
                  "curves": v["curves"]}
                 for k, v in per_course.items() if v["course"]["category"] == cat]
        if not items:
            continue
        a = allocate(items, int(req["pools"][cat]), req.get("budget_mode", "SHARED_LIVE"),
                     req.get("robust_method", "minimax"))
        bids.update(a["bids"])
        allocations.append({
            "category": cat, "pool": a["pool"], "committed": a["committed"],
            "uncommitted": None if a["budget_mode"] == "INDEPENDENT" else a["pool"] - a["committed"],
            "feasible": a["feasible"], "sacrificed": a["sacrificed"], "note": a["note"],
        })

    headline = req.get("headline_mode", "HIGH")
    recs = []
    for code, v in per_course.items():
        b = bids.get(code, {"bid": 0, "target_met": False, "shortfall": [], "reduced": False})
        bid = int(b["bid"])
        s = summarise(v["curves"], bid)
        scen = []
        for mid in grid:
            m = MODES[mid]
            er = expected_rivals(v["course"], mid, {
                "live_bidders": v["course"].get("live_bidders"),
                "user_popularity": v["course"].get("user_popularity", 1.0),
                "graduation_critical": v["course"].get("priority") == "MUST"})
            scen.append({
                "mode": mid, "label": m.label, "expected_rivals": round(er["lambda"], 1),
                "win_at_bid": float(v["curves"][mid][bid]),
                "win_one_below": float(v["curves"][mid][max(0, bid - 1)]),
                "win_at_cap": float(v["curves"][mid][v["cap"]]),
                "comparison_only": m.comparison_only,
            })
        charge = float(v["paid"][headline][bid]) if headline in v["paid"] else 0.0
        recs.append({
            "code": code, "category": v["course"]["category"],
            "priority": v["course"].get("priority", "STRONG"),
            "cap": v["cap"], "bid": bid,
            "bid_range": (max(0, bid - 3), min(v["cap"], bid + 5)),
            "target_met": bool(b["target_met"]), "reduced_for_budget": bool(b.get("reduced", False)),
            "shortfall": b.get("shortfall", []),
            "worst_tested": s["_worst"],
            "expected_charge": round(charge, 2),
            "expected_refund": round(max(0.0, bid - charge), 2),
            "ci_halfwidth": float(v["ci"][headline][bid]) if headline in v["ci"] else 0.0,
            "scenarios": scen,
            "demand": {
                "source": v["demand"]["source"],
                "expected_rivals": round(v["demand"]["lambda"], 1),
                "seats": v["course"]["seats"],
                "note": v["demand"]["note"],
                "factors": (v["demand"]["popularity"]["reasons"]
                            if v["demand"].get("popularity") else []),
            },
            "price_to_beat_quantiles": v["beat_q"][headline] if headline in v["beat_q"] else {},
        })

    return {
        "recommendations": recs, "allocations": allocations,
        "budget_mode": req.get("budget_mode", "SHARED_LIVE"),
        "headline_mode": headline, "robust_method": req.get("robust_method", "minimax"),
        "trials": req["trials"], "seed": req["seed"], "scenarios_run": grid,
        "rule_version": RULE_VERSION, "dataset_version": DATASET_VERSION,
        "model_version": MODEL_VERSION,
        "input_hash": input_hash(req),
        "runtime_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# ---------------------------------------------------------------- §12 / §16

def run_plan_both_budget_modes(req: dict, should_cancel=None, on_progress=None) -> dict:
    """Simulate once, then allocate under BOTH budget interpretations.

    The shared-live rule is genuinely unresolved (rule BUDGET.SHARED_LIVE), so the
    student should see both readings side by side without paying for a second
    full simulation. The expensive part is the Monte Carlo; allocation is cheap.
    """
    primary = req.get("budget_mode", "SHARED_LIVE")
    other = "INDEPENDENT" if primary == "SHARED_LIVE" else "SHARED_LIVE"

    base = run_plan(req, should_cancel=should_cancel, on_progress=on_progress)
    alt_req = dict(req); alt_req["budget_mode"] = other
    # reuse the simulation by re-running allocation only: cheap, no Monte Carlo
    alt = _reallocate(base, alt_req)

    changed = []
    a = {r["code"]: r["bid"] for r in base["recommendations"]}
    b = {r["code"]: r["bid"] for r in alt["recommendations"]}
    for code in a:
        if a[code] != b.get(code):
            changed.append({"code": code, primary.lower(): a[code], other.lower(): b.get(code)})

    base["budget_comparison"] = {
        "primary_mode": primary,
        "alternate_mode": other,
        "primary": _budget_summary(base),
        "alternate": _budget_summary(alt),
        "courses_changed": changed,
        "why_it_matters": (
            "No University document states whether bids on different courses in one round draw "
            "from a single live pool. Under the independent reading there is little reason not to "
            "bid the cap on every must-have course. Under the shared reading your category budget "
            "forces real tradeoffs. Neither is officially confirmed."
        ),
        "rule_id": "BUDGET.SHARED_LIVE",
    }
    return base


def _budget_summary(res: dict) -> dict:
    return {
        "budget_mode": res["budget_mode"],
        "allocations": res["allocations"],
        "total_committed": sum(a["committed"] for a in res["allocations"]),
        "expected_charge": round(sum(r["expected_charge"] for r in res["recommendations"]), 1),
        "recommendations": [{"code": r["code"], "bid": r["bid"], "target_met": r["target_met"],
                             "worst_tested": r["worst_tested"]} for r in res["recommendations"]],
    }


def _reallocate(base: dict, req: dict) -> dict:
    """Re-run only the allocation step against already-simulated curves.

    We do not keep raw curves in the API response (they are large), so this
    rebuilds the minimal per-course structure the allocator needs from the
    per-scenario win values that were retained.
    """
    import numpy as np
    from app.optimization.robust import allocate as _alloc, summarise as _sum

    per_cat: dict[str, list] = {}
    curve_cache: dict[str, dict] = {}
    for r in base["recommendations"]:
        cap = r["cap"]
        curves = {}
        for sc in r["scenarios"]:
            # a step curve reconstructed at the three probed bid levels is enough
            # for the allocator's threshold comparisons; exact curves stay server-side
            arr = np.zeros(cap + 1)
            below = max(0, r["bid"] - 1)
            arr[:below] = sc["win_one_below"]
            arr[below:r["bid"] + 1] = sc["win_at_bid"]
            arr[r["bid"] + 1:] = sc["win_at_cap"]
            arr = np.maximum.accumulate(arr)
            curves[sc["mode"]] = arr
        curve_cache[r["code"]] = curves
        per_cat.setdefault(r["category"], []).append(
            {"code": r["code"], "cap": cap, "priority": r["priority"], "curves": curves})

    allocations, bids = [], {}
    pools = {a["category"]: a["pool"] for a in base["allocations"]}
    for cat, items in per_cat.items():
        a = _alloc(items, pools.get(cat, 0), req.get("budget_mode"), req.get("robust_method", "minimax"))
        bids.update(a["bids"])
        allocations.append({"category": cat, "pool": a["pool"], "committed": a["committed"],
                            "uncommitted": None if a["budget_mode"] == "INDEPENDENT" else a["pool"] - a["committed"],
                            "feasible": a["feasible"], "sacrificed": a["sacrificed"], "note": a["note"]})

    recs = []
    for r in base["recommendations"]:
        b = bids.get(r["code"], {})
        nb = int(b.get("bid", r["bid"]))
        s = _sum(curve_cache[r["code"]], nb)
        recs.append({**r, "bid": nb, "target_met": bool(b.get("target_met", False)),
                     "worst_tested": s["_worst"],
                     "expected_charge": round(r["expected_charge"] * (nb / max(1, r["bid"])), 1)})
    out = dict(base)
    out["recommendations"] = recs
    out["allocations"] = allocations
    out["budget_mode"] = req.get("budget_mode")
    return out


def stress_test_plan(result: dict, credit_cap: float, fixed_credits: float,
                     cohorts: int = 4000, seed: int = 0) -> dict:
    """Whole-plan adversarial test: resolve every course under a randomly drawn
    stress scenario per cohort, then apply the credit cap in priority order."""
    import numpy as np
    from app.optimization.robust import PRIORITY

    recs = result["recommendations"]
    if not recs:
        return {"cohorts": 0, "note": "no courses in plan"}
    rng = np.random.default_rng(seed)
    grid = [s for s in ("HIGH", "VERY_HIGH", "EXTREME")]
    probs = {}
    for r in recs:
        probs[r["code"]] = {sc["mode"]: sc["win_at_bid"] for sc in r["scenarios"]}
    musts = [r for r in recs if r["priority"] == "MUST"]
    order = sorted(recs, key=lambda r: PRIORITY[r["priority"]]["rank"])
    credits = {r["code"]: 0 for r in recs}
    # credits are not in the recommendation payload; caller supplies them
    all_must = any_must = 0
    cred_sum = 0.0
    worst_cred = float("inf")
    fails = {r["code"]: 0 for r in recs}

    for _ in range(cohorts):
        g = grid[int(rng.integers(0, len(grid)))]
        won = []
        for r in order:
            if rng.random() < probs[r["code"]].get(g, 0.0):
                won.append(r)
            else:
                fails[r["code"]] += 1
        cred = 0.0
        confirmed = []
        for r in won:
            c = float(r.get("credits", 3))
            if cred + fixed_credits + c <= credit_cap:
                cred += c
                confirmed.append(r["code"])
        got = sum(1 for m in musts if m["code"] in confirmed)
        if musts and got == len(musts):
            all_must += 1
        if got > 0:
            any_must += 1
        cred_sum += cred
        worst_cred = min(worst_cred, cred)

    return {
        "cohorts": cohorts,
        "all_must_have_rate": round(all_must / cohorts, 4) if musts else None,
        "any_must_have_rate": round(any_must / cohorts, 4) if musts else None,
        "must_have_count": len(musts),
        "expected_elective_credits": round(cred_sum / cohorts, 2),
        "worst_case_credits": 0 if worst_cred == float("inf") else worst_cred,
        "failure_rates": [{"code": k, "rate": round(v / cohorts, 4)}
                          for k, v in sorted(fails.items(), key=lambda x: -x[1])],
        "note": ("Each cohort draws a scenario at random from High, Very high and Extreme, resolves "
                 "every course, then applies your credit cap in priority order - so a course can be "
                 "lost even after winning its bid."),
    }
