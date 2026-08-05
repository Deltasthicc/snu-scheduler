"""University rule registry - single source of truth, ported from core/rules.js.

Every rule carries provenance so a caller can distinguish "the University wrote
this down" from "we inferred this because it is the only coherent reading".
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any

RULE_VERSION = "2026.M.3"
DATASET_VERSION = "monsoon-2026.1"
MODEL_VERSION = "competition-v3"


class Status(str, Enum):
    OFFICIAL = "official"
    PROSPECTUS = "prospectus"
    TIMETABLE = "timetable"
    INFERRED = "inferred"
    DISPUTED = "disputed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    desc: str
    status: Status
    source: str
    value: Any = None
    configurable: bool = False
    note: str | None = None
    resolution: str | None = None
    verified: str | None = None
    impact: str | None = None


_R: list[Rule] = [
    Rule("AUC.CLEARING_PRICE", "Uniform clearing price",
         "Every winner pays the lowest winning bid. A course with seats left over clears at zero.",
         Status.OFFICIAL, "Concept Note s.5; Introduction s.3"),
    Rule("AUC.MAX_BID", "Maximum bid per course", "A bid may not exceed 25 x course credits.",
         Status.OFFICIAL, "Concept Note s.5", value=25, configurable=True),
    Rule("AUC.MIN_BID", "No minimum bid",
         "Any whole number including zero. Zero wins when demand does not exceed seats.",
         Status.OFFICIAL, "Concept Note s.5", value=0),
    Rule("AUC.TIEBREAK", "Random tie-break number",
         "Equal bids separated by a random 6-digit number generated on add. Not time-based.",
         Status.OFFICIAL, "Introduction s.3; guide s.2"),
    Rule("AUC.REFUND", "Settlement and refunds",
         "Winners charged the clearing price and refunded the remainder. Losing bids refunded in full.",
         Status.OFFICIAL, "Introduction s.3"),
    Rule("AUC.INTEGER_BIDS", "Whole-number bids", "Students may bid any whole number of points.",
         Status.OFFICIAL, "Concept Note s.5"),
    Rule("POOL.SEPARATE", "Three non-interchangeable pools",
         "ME, UWE and CCC points are separate; one cannot subsidise another.",
         Status.OFFICIAL, "Concept Note s.1; guide s.7"),
    Rule("POOL.CARRY_FORWARD", "Unused points carry forward",
         "Points not spent roll into future bidding semesters.", Status.OFFICIAL, "Concept Note s.5"),
    Rule("POOL.FLOATER_SPLIT", "Floater credits split equally",
         "Remaining floater credits divided equally between UWE and CCC before the pool formula.",
         Status.OFFICIAL, "Concept Note s.3 and s.4", value=0.5,
         note="No rounding rule is stated for an odd floater count. The .5 is kept as a fraction and only "
              "the final pool is rounded, which reproduces every worked example exactly."),
    Rule("POOL.ROUNDING", "Pool rounding", "Allocations rounded to the nearest whole number.",
         Status.OFFICIAL, "Concept Note s.3", note="Applied once, to the final pool."),
    Rule("POOL.Y4", "Fourth-year pool formula",
         "ME = rem x 15 + 162; UWE = (rem + floater/2) x 15 + 110; CCC = (rem + floater/2) x 30 + 72.",
         Status.OFFICIAL, "Concept Note s.4",
         verified="Reproduces the Concept Note example (12/4/6/6 -> 342/215/342) exactly."),
    Rule("POOL.Y3", "Third-year transition formula",
         "(remaining x 10 x semester share) + flat constant; shares 40/30/30 across Sem 5/6/7; "
         "constants +65 ME, +50 UWE, +30 CCC added in full each semester.",
         Status.OFFICIAL, "Concept Note s.3",
         verified="Reproduces the doc example (24/9/6/6 -> 161/98/66) and the full average table."),
    Rule("POOL.Y2", "Second-year steady-state formula",
         "10 points per degree credit, released on a staggered schedule; 10 deducted per completed credit.",
         Status.OFFICIAL, "Concept Note s.2"),
    Rule("POOL.Y4_AVERAGE_ROW", "Disputed: Concept Note average fourth-year row",
         "The note prints 297/125/239 for 9 ME, ~0 UWE, 5.5 CCC, 6 floater. ME reconciles (9x15+162=297) but "
         "UWE gives 155 (or 110 without floater), never 125; CCC gives 327 (or 237), never 239.",
         Status.DISPUTED, "Concept Note s.4 summary table vs its own worked example",
         resolution="Production uses the FORMULA, which reproduces the worked example. The disputed row is "
                    "excluded from computation and surfaced to the user instead."),
    Rule("SET.NET_CEILING", "Consideration set ceiling (Req x 2)",
         "Req = min(graduation, semester, major-elective remaining). Set may hold Req x 2 credits.",
         Status.OFFICIAL, "Guide s.6", value=2, configurable=True),
    Rule("SET.ONE_CLASH", "One clash per course in bidding rounds",
         "Rounds 2/4/6: a course may clash with at most one other in the set.",
         Status.OFFICIAL, "Introduction s.3; guide s.7"),
    Rule("SET.WAITLIST_RELAXED", "Clash limit lifted in waitlist rounds",
         "Rounds 7/8: a course may clash with any number of others.", Status.OFFICIAL, "Guide s.4, s.7"),
    Rule("SET.NEVER_CLASH_CORE", "Never clash with pre-enrolled core",
         "In every round a bid course may never clash with a pre-enrolled core course.",
         Status.OFFICIAL, "Introduction s.3; guide s.7"),
    Rule("SET.HALF_SEM_DISJOINT", "Opposite half-semesters cannot clash",
         "A first-half and a second-half course in the same weekly slot do not conflict.",
         Status.INFERRED, "Not stated. Follows from the timetable Term field and separate second-half rounds.",
         note="Low risk: the only coherent reading of a term-split timetable."),
    Rule("SET.CREDIT_CAP", "Semester credit cap",
         "25 credits per semester. Extra bid points cannot exceed it.",
         Status.OFFICIAL, "Introduction s.5; Concept Note s.5", value=25, configurable=True,
         note="Configurable because an individual student may hold an approved overload."),
    Rule("ROUND.SEQUENCE", "Nine main rounds plus a three-round second-half CCC cycle",
         "1 swap, 2 ME bid, 3 ME drop, 4 CCC/UWE bid I, 5 swap II, 6 CCC/UWE bid II, "
         "7 ME waitlist, 8 CCC/UWE waitlist, 9 add/drop; then 10-12 for half-semester CCC.",
         Status.OFFICIAL, "Introduction s.4; guide s.3"),
    Rule("ROUND.SWAP_NO_POINTS", "Swap rounds use no bid points",
         "Core section swaps in rounds 1 and 5 are free, matched two-way in random tie-break order.",
         Status.OFFICIAL, "Guide s.4"),
    Rule("ROUND.DROPPED_SEAT", "A dropped seat is not returned to the prior loser",
         "It re-enters the pool and is contested again in the waitlist round.",
         Status.OFFICIAL, "Guide s.4, s.9"),
    Rule("BUDGET.SHARED_LIVE", "UNCONFIRMED: do simultaneous bids share a live pool?",
         "Whether points bid on course A are unavailable to course B before settlement.",
         Status.UNKNOWN,
         "No supplied document states this either way. A student reported the webinar implying shared "
         "commitment, which is hearsay, not documentation.",
         configurable=True, value="SHARED_LIVE",
         impact="Large. Under INDEPENDENT there is no reason not to bid the cap everywhere and the optimizer "
                "degenerates. Under SHARED_LIVE the allocation problem is real.",
         resolution="Both modes implemented and selectable; the active mode is returned with every result."),
    Rule("DATA.CREDITS", "Course credit provenance",
         "Only 33 of 326 offered courses have an official credit match; the rest are timetable-derived.",
         Status.DISPUTED, "scheduler workbook, Credit Status column",
         note="Each course carries its own flag; derived credits are marked distinctly."),
    Rule("DATA.ME_NOT_IN_PROSPECTUS", "Three major electives absent from the printed prospectus list",
         "CSD358, CSD361 and CSD457 appear at 3 credits. CSD365, CSD436 and CSD438 do not appear.",
         Status.DISPUTED, "prospectus Major Electives table vs Monsoon 2026 timetable",
         resolution="All treated as 3 credits and flagged as derived."),
    Rule("SPEC.REQUIREMENT", "Specialisation requirement",
         "12 credits in one area, or 6 credits plus Project-1 in that area. Requires overall CGPA >= 7 "
         "and specialisation-component CGPA >= 8.",
         Status.PROSPECTUS, "prospectus, Minimum Requirement for Specialization"),
    Rule("SPEC.BUCKET_TENTATIVE", "Specialisation buckets are explicitly tentative",
         "The prospectus footnotes that the lists are tentative and may be updated.",
         Status.PROSPECTUS, "prospectus footnote"),
    Rule("SPEC.CSD336_AMBIGUOUS", "Disputed: does CSD336 count toward the AI bucket?",
         "The AI bucket lists Reinforcement Learning by name, but CSD336 is a 4-credit Major CORE.",
         Status.DISPUTED, "prospectus AI bucket vs Major Core table",
         resolution="Excluded by default; user-toggleable so both readings can be compared."),
    Rule("COMP.STRESS_DEFAULT", "Competition is assumed, not observed",
         "No historical SNU clearing-price or bidder-count data exists. Every course is modelled as "
         "oversubscribed until live platform data proves otherwise.",
         Status.INFERRED, "Absence of any historical dataset; first cohort under bidding",
         note="A deliberate safety posture, not a claim about reality. A live bidder count replaces it."),

    # ---- credit-ceiling policy (scheduler v2: wishlist/CP-SAT phase) ----
    # SET.CREDIT_CAP above already states the number (25) and that it is
    # configurable "because an individual student may hold an approved
    # overload." These two rules make that distinction load-bearing in code
    # instead of leaving it as a single ambiguous integer: a caller must
    # explicitly choose which of the two modes below applies, and the second
    # one is never presented as a universal rule for every fourth-year student.
    Rule("CEILING.STANDARD", "Standard semester credit ceiling",
         "25 credits per semester for every student, in every year, unless an approved exception applies.",
         Status.OFFICIAL, "Introduction s.5; Concept Note s.5", value=25, configurable=False),
    Rule("CEILING.YEAR4_PLUS2", "Year IV two-credit extension",
         "Class of 2027 onward: an eligible Year IV student may enrol in two additional credits with the "
         "recommendation of their UG Advisor; Dean approval is not required for those two credits.",
         Status.OFFICIAL, "Credit-limit policy 4.7.9 and 4.8", value=27, configurable=False),
    Rule("CEILING.YEAR4_DEAN_EXTENSION", "Year IV extension beyond two credits",
         "Beyond the two additional credits in 4.7.9, UG-advisor recommendation and Dean approval are "
         "mandatory; the total cannot exceed 30 credits.",
         Status.OFFICIAL, "Credit-limit policy 4.7.10 and 4.8", value=30, configurable=False),
]

RULES: dict[str, Rule] = {r.id: r for r in _R}


def counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for r in RULES.values():
        out[r.status.value] = out.get(r.status.value, 0) + 1
    return out
