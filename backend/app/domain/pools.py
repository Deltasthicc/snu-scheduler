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


def max_bid(credits: float, multiplier: float | None = None) -> int:
    _nonneg(credits, "credits")
    m = multiplier if multiplier is not None else RULES["AUC.MAX_BID"].value
    return int(m * credits)


def compute_pools(model: str, semester: int, rem_me: float, rem_uwe: float,
                  rem_ccc: float, floater: float,
                  done_me: float = 0, done_uwe: float = 0, done_ccc: float = 0) -> dict:
    _nonneg(rem_me, "rem_me"); _nonneg(rem_uwe, "rem_uwe")
    _nonneg(rem_ccc, "rem_ccc"); _nonneg(floater, "floater")
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
        a = b = c = 0.0
        for s in range(2, int(semester) + 1):
            a += rem_me * 10 * ME_CCC_RELEASE.get(s, 0)
            b += eff_uwe * 10 * UWE_RELEASE.get(s, 0)
            c += eff_ccc * 10 * ME_CCC_RELEASE.get(s, 0)
        a -= done_me * 10; b -= done_uwe * 10; c -= done_ccc * 10
        out = {"ME": round(max(0.0, a)), "UWE": round(max(0.0, b)), "CCC": round(max(0.0, c))}
        rule = "POOL.Y2"
        detail = {k: f"staggered release to Sem {semester}, minus 10 per completed credit" for k in out}
    else:
        raise RuleError(f"unknown pool model: {model!r}")

    out["detail"] = detail
    out["rule_id"] = rule
    return out
