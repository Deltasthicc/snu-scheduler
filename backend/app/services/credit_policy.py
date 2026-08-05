"""Credit-ceiling policy: the five distinct numbers a wishlist scheduler must
never collapse into one ambiguous "credit limit" field (see CLAUDE.md, session
2026-08-04, scheduler v2 phase):

  1. official_ceiling    - CEILING.STANDARD, 25 for everyone, every year.
  2. personal_target     - the student's own chosen planning target, which
                            may sit below the ceiling.
  3. overload_ceiling    - only present when the student explicitly enables
                            an overload/what-if scenario. Never defaults to
                            a number: see rule CEILING.APPROVED_OVERLOAD.
  4. fixed_credits       - locked/pre-enrolled courses, already committed.
  5. wishlist_credits    - additional credits the wishlist may contribute.

The active ceiling used by the scheduler is official_ceiling unless the
student has explicitly turned on an overload scenario, in which case it is
overload_ceiling - and every response says so in plain language rather than
silently substituting one number for another.
"""
from __future__ import annotations
from dataclasses import dataclass

from app.domain.rules import RULES

STANDARD_CEILING = RULES["CEILING.STANDARD"].value


class CreditPolicyError(ValueError):
    """Raised for a credit-policy request that cannot be resolved at all
    (e.g. a ceiling below fixed credits) - distinct from RuleError so main.py
    can map it to its own 422 response without touching the pools/settlement
    exception handler."""


@dataclass(frozen=True)
class CreditPolicyResult:
    official_ceiling: float
    active_ceiling: float
    ceiling_mode: str  # standard | advisor_extension | dean_extension | what_if
    fixed_credits: float
    personal_target: float
    min_credits: float
    wishlist_room: float  # active_ceiling - fixed_credits
    is_overload: bool
    overload_confirmed: bool
    warnings: list[str]
    summary: str


def resolve_ceiling(
    *,
    fixed_credits: float,
    personal_target: float,
    min_credits: float,
    overload_ceiling: float | None = None,
    overload_mode: str = "what_if",  # "what_if" | "approved_overload"
    overload_confirmed: bool = False,
    current_year: int | None = None,
    eligibility_confirmed: bool = False,
    advisor_recommended: bool = False,
    dean_approved: bool = False,
) -> CreditPolicyResult:
    """Resolves the active ceiling for one student, given the numbers above.

    `overload_ceiling` being non-null is what turns on overload/what-if mode
    at all - the standard ceiling is always the default and is never silently
    raised. `overload_confirmed=True` means the student has stated they have
    an actual approved exception on file (not merely modelling "what if");
    the summary language differs accordingly, per the spec's requirement
    that an unconfirmed 30-credit scenario never reads as a normal rule.
    """
    if fixed_credits < 0 or personal_target < 0 or min_credits < 0:
        raise CreditPolicyError("credits cannot be negative")
    if overload_mode not in ("what_if", "approved_overload"):
        raise CreditPolicyError(f"unknown overload_mode {overload_mode!r}")

    warnings: list[str] = []
    is_overload = overload_ceiling is not None
    if is_overload:
        if overload_ceiling <= STANDARD_CEILING:
            raise CreditPolicyError(
                f"overload_ceiling ({overload_ceiling}) must exceed the standard ceiling "
                f"({STANDARD_CEILING}); otherwise omit it and use the standard ceiling")
        if overload_ceiling > 30:
            raise CreditPolicyError("the published Year IV extension ceiling cannot exceed 30 credits")
        active_ceiling = overload_ceiling
        # Backward compatibility is deliberately conservative: an old single
        # `overload_confirmed` flag cannot prove which approvals were obtained.
        extension_ok = current_year == 4 and eligibility_confirmed and advisor_recommended
        dean_ok = extension_ok and (overload_ceiling <= 27 or dean_approved)
        if dean_ok:
            ceiling_mode = "advisor_extension" if overload_ceiling <= 27 else "dean_extension"
        else:
            ceiling_mode = "what_if"
    else:
        active_ceiling = STANDARD_CEILING
        ceiling_mode = "standard"

    if fixed_credits > active_ceiling:
        raise CreditPolicyError(
            f"fixed/pre-enrolled credits ({fixed_credits}) already exceed the active ceiling "
            f"({active_ceiling}); the ceiling itself needs correcting, not the wishlist")
    if min_credits > active_ceiling - fixed_credits:
        warnings.append(
            f"your minimum acceptable credits ({min_credits}) leaves no room under the active "
            f"ceiling once fixed credits ({fixed_credits}) are counted; no schedule can satisfy it")
    if personal_target > active_ceiling:
        warnings.append(
            f"personal target ({personal_target}) exceeds the active ceiling ({active_ceiling}); "
            f"it will be treated as {active_ceiling}")
        personal_target = active_ceiling
    if is_overload and ceiling_mode == "what_if":
        missing = []
        if current_year != 4: missing.append("Year IV status")
        if not eligibility_confirmed: missing.append("eligibility confirmation")
        if not advisor_recommended: missing.append("UG-advisor recommendation")
        if overload_ceiling > 27 and not dean_approved: missing.append("Dean approval")
        warnings.append("this is only a planning what-if; missing: " + ", ".join(missing))

    wishlist_room = round(active_ceiling - fixed_credits, 4)
    summary = _summary(fixed_credits, wishlist_room, personal_target, active_ceiling,
                       ceiling_mode, STANDARD_CEILING)
    return CreditPolicyResult(
        official_ceiling=STANDARD_CEILING, active_ceiling=active_ceiling, ceiling_mode=ceiling_mode,
        fixed_credits=fixed_credits, personal_target=personal_target, min_credits=min_credits,
        wishlist_room=wishlist_room, is_overload=is_overload,
        overload_confirmed=ceiling_mode in {"advisor_extension", "dean_extension"},
        warnings=warnings, summary=summary,
    )


def _summary(fixed: float, room: float, target: float, active: float,
            mode: str, standard: float) -> str:
    fixed_s, room_s, target_s, active_s = (_fmt(x) for x in (fixed, room, target, active))
    base = (f"{fixed_s} fixed credits + up to {room_s} wishlist credits = "
            f"{active_s}-credit total ceiling")
    if mode == "standard":
        return base + f". Preferred target: {target_s} credits."
    if mode == "advisor_extension":
        return (base + f". Year IV extension up to 27: eligibility and UG-advisor recommendation recorded; "
                f"Dean approval is not required for these two additional credits. Preferred target: {target_s} credits.")
    if mode == "dean_extension":
        return (base + f". Year IV extension above 27: eligibility, UG-advisor recommendation, and Dean approval recorded. "
                f"Preferred target: {target_s} credits.")
    return (base + f". This is a what-if overload scenario, not a confirmed University rule - the "
            f"source-confirmed standard ceiling is {_fmt(standard)} credits. Verify approval before "
            f"registration. Preferred target: {target_s} credits.")


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"
