"""Point-pool formulas. Ported from core/engine.js, verified against the same fixtures."""
from __future__ import annotations
from .rules import RULES

UWE_RELEASE = {2: .15, 3: .20, 4: .20, 5: .20, 6: .15, 7: .10}
ME_CCC_RELEASE = {2: .075, 3: .175, 4: .20, 5: .20, 6: .20, 7: .15}
Y3_SHARE = {5: .40, 6: .30, 7: .30}


class RuleError(ValueError):
    pass


def _nonneg(v: float, name: str) -> float:
    if v is None or v != v or v in (float("inf"), float("-inf")):
        raise RuleError(f"{name} must be a finite number, got {v!r}")
    if v < 0:
        raise RuleError(f"{name} must be >= 0, got {v}")
    return float(v)


def max_bid(credits: float, multiplier: float | None = None) -> int | None:
    """No per-course bid cap exists (AUC.MAX_BID, rectified 2026-08-05): a student may bid
    any whole number up to their entire available category pool on a single course. This
    function is kept only so the /max-bid endpoint has a stable shape for existing callers;
    it always reports no cap unless a caller explicitly supplies one via `multiplier`."""
    _nonneg(credits, "credits")
    m = multiplier if multiplier is not None else RULES["AUC.MAX_BID"].value
    return None if m is None else int(m * credits)


def compute_pools(model: str, semester: int, rem_me: float, rem_uwe: float,
                  rem_ccc: float, floater: float,
                  done_me: float = 0, done_uwe: float = 0, done_ccc: float = 0) -> dict:
    _nonneg(rem_me, "rem_me"); _nonneg(rem_uwe, "rem_uwe")
    _nonneg(rem_ccc, "rem_ccc"); _nonneg(floater, "floater")
    _nonneg(done_me, "done_me"); _nonneg(done_uwe, "done_uwe"); _nonneg(done_ccc, "done_ccc")
    split = RULES["POOL.FLOATER_SPLIT"].value
    eff_uwe = rem_uwe + floater * split
    eff_ccc = rem_ccc + floater * split

    if model == "y4":
        out = {"ME": round(rem_me * 15 + 162),
               "UWE": round(eff_uwe * 15 + 110),
               "CCC": round(eff_ccc * 30 + 72)}
        rule = "POOL.Y4"
        detail = {"ME": f"{rem_me} x 15 + 162",
                  "UWE": f"({rem_uwe} + {floater*split}) x 15 + 110",
                  "CCC": f"({rem_ccc} + {floater*split}) x 30 + 72"}
    elif model == "y3":
        sh = Y3_SHARE.get(int(semester))
        if sh is None:
            raise RuleError(f"y3 model only defines Semesters 5, 6, 7; got {semester}")
        out = {"ME": round(rem_me * 10 * sh + 65),
               "UWE": round(eff_uwe * 10 * sh + 50),
               "CCC": round(eff_ccc * 10 * sh + 30)}
        rule = "POOL.Y3"
        detail = {"ME": f"{rem_me} x 10 x {sh} + 65",
                  "UWE": f"({rem_uwe} + {floater*split}) x 10 x {sh} + 50",
                  "CCC": f"({rem_ccc} + {floater*split}) x 10 x {sh} + 30"}
    elif model == "y2":
        # Total category requirement (not "remaining") is what the release schedule is a
        # fraction of - confirmed against the Concept Note's own Semester-3 worked example:
        # UWE 30 (Sem2 carry-forward) + 40 (Sem3 release) - 15 (3 completed credits x 5) = 55.
        # Using "remaining" directly in the release sum (the pre-2026-08-05 formula here)
        # reproduces that example only when 0 credits are completed; with 3 completed it gives
        # 29.5, not 55, so remaining-based release was a real bug, not just the point value.
        total_me = rem_me + done_me
        total_uwe = rem_uwe + done_uwe + floater * split
        total_ccc = rem_ccc + done_ccc + floater * split
        a = b = c = 0.0
        for s in range(2, int(semester) + 1):
            a += total_me * 10 * ME_CCC_RELEASE.get(s, 0)
            b += total_uwe * 10 * UWE_RELEASE.get(s, 0)
            c += total_ccc * 10 * ME_CCC_RELEASE.get(s, 0)
        a -= done_me * 5; b -= done_uwe * 5; c -= done_ccc * 5
        out = {"ME": round(max(0.0, a)), "UWE": round(max(0.0, b)), "CCC": round(max(0.0, c))}
        rule = "POOL.Y2"
        detail = {k: f"cumulative release on total requirement to Sem {semester}, minus 5 per completed credit"
                  for k in out}
    else:
        raise RuleError(f"unknown pool model: {model!r}")

    out["detail"] = detail
    out["rule_id"] = rule
    return out
