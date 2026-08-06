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
    Rule("AUC.MAX_BID", "No minimum or maximum bid (rectified 2026-08-05)",
         "A student may bid any whole number from zero up to their entire available pool for "
         "that category, on a single course. There is no per-course cap.",
         Status.OFFICIAL, "Dean Academics rectification email, 2026-08-05, item 2; "
         "Course_Bid_Point_Allocation_Concept_Note_revised_final.pdf s.5; "
         "Course_Enrolment_FAQ_1.pdf Part 1", value=None, configurable=False,
         note="Supersedes the earlier 25 x credits cap this rule previously stated. The only real "
              "ceiling left is the student's own category pool: ME/UWE/CCC bids never draw on each "
              "other (POOL.SEPARATE), so a course cannot be bid past whichever pool it belongs to."),
    Rule("AUC.MIN_BID", "No minimum bid",
         "Any whole number including zero. Zero wins when demand does not exceed seats.",
         Status.OFFICIAL, "Course_Bid_Point_Allocation_Concept_Note_revised_final.pdf s.5"),
    Rule("AUC.TIEBREAK", "Random tie-break number, assigned per course per round",
         "Equal bids separated by a random number assigned uniquely for each course, for each round. "
         "Not time-based. Withdrawing and rebidding for the same course in the same round keeps the "
         "same number; a new round assigns a fresh one.",
         Status.OFFICIAL, "Course_Bidding_Introduction.pdf, Eligibility/tie-break section"),
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
         Status.OFFICIAL, "Course_Bid_Point_Allocation_Concept_Note_revised_final.pdf s.4",
         verified="Reproduces the revised Concept Note's own example (12/4/6/6 -> 342/215/342) "
                  "exactly; unchanged from the prior document version."),
    Rule("POOL.Y3", "Third-year transition formula",
         "(remaining x 10 x semester share) + flat constant; shares 40/30/30 across Sem 5/6/7; "
         "constants +65 ME, +50 UWE, +30 CCC added in full each semester.",
         Status.OFFICIAL, "Course_Bid_Point_Allocation_Concept_Note_revised_final.pdf s.3",
         verified="Reproduces the revised doc's example (24/9/6/6 -> 161/98/66) and the full average "
                  "table; unchanged from the prior document version."),
    Rule("POOL.Y2", "Second-year steady-state formula (rectified 2026-08-05)",
         "10 points per credit of the category's TOTAL degree requirement (not remaining), released "
         "on a staggered schedule across Sem 2-7 and accumulated; 5 points deducted per completed "
         "credit (ME/UWE/CCC, excluding EVS) from the resulting total.",
         Status.OFFICIAL, "Dean Academics rectification email, 2026-08-05, item 1; "
         "Course_Bid_Point_Allocation_Concept_Note_revised_final.pdf s.2",
         verified="Reproduces the doc's Semester-3 worked example exactly: UWE 30 (Sem2 carry-"
                  "forward) + 40 (Sem3 release) - 15 (3 completed credits x 5) = 55. The rectified "
                  "point value is 5, replacing the prior document's 10.",
         note="The source PDF's own prose sentence for this example says '40 UWE points' immediately "
              "after showing the 30+40-15=55 arithmetic - an apparent typo in the document itself. "
              "Production trusts the arithmetic, consistent with how POOL.Y4_AVERAGE_ROW already "
              "resolves a similar prose-vs-calculation mismatch elsewhere in the same family of docs. "
              "The document demonstrates the completed-credit deduction only for the Semester-3 "
              "transition point; this implementation applies it on an ongoing basis every semester "
              "(each additional completed credit further reduces the pool), since the deduction's own "
              "stated purpose - 'so the allocation accurately reflects credits still remaining' - "
              "applies identically whether a credit was completed before bidding existed or through a "
              "later winning bid."),
    Rule("POOL.Y4_AVERAGE_ROW", "Disputed: Concept Note average fourth-year row",
         "The note prints 297/125/239 for 9 ME, ~0 UWE, 5.5 CCC, 6 floater. ME reconciles (9x15+162=297) but "
         "UWE gives 155 (or 110 without floater), never 125; CCC gives 327 (or 237), never 239.",
         Status.DISPUTED, "Concept Note s.4 summary table vs its own worked example",
         resolution="Production uses the FORMULA, which reproduces the worked example. The disputed row is "
                    "excluded from computation and surfaced to the user instead.",
         note="Course_Enrolment_FAQ_1.pdf independently repeats the same figures almost exactly "
              "(297 / 125 / 238.5) as a 'typical first-cycle average' for 4th years, in a second, "
              "separately-authored document. That strengthens the case this is a real unreconciled "
              "figure in the University's own materials, not a one-off typo in a single PDF - but it "
              "does not supply the missing reconciliation, so the disputed status stands."),
    Rule("SET.NET_CEILING", "Consideration set ceiling (Req x 2)",
         "Req = min(graduation, semester, major-elective remaining). Set may hold Req x 2 credits.",
         Status.OFFICIAL, "Course_Bidding_Introduction.pdf, Eligibility checks: 'you may bid for backups "
         "up to twice your available credit limit'", value=2, configurable=True),
    Rule("SET.ONE_CLASH", "One clash per course in bidding rounds",
         "Rounds 2/4/6: a course may clash with at most one other in the set.",
         Status.OFFICIAL, "Course_Bidding_Introduction.pdf, Eligibility checks"),
    Rule("SET.WAITLIST_RELAXED", "Clash limit lifted in waitlist rounds",
         "Rounds 7/8: a course may clash with any number of others.", Status.OFFICIAL, "Guide s.4, s.7",
         note="Not restated by the 2026-08-05 documents; not contradicted by them either. Left as-is."),
    Rule("SET.NEVER_CLASH_CORE", "Never clash with pre-enrolled core",
         "In every round a bid course may never clash with a pre-enrolled core course.",
         Status.OFFICIAL, "Course_Bidding_Introduction.pdf, Eligibility checks; "
         "Course_Enrolment_FAQ_1.pdf, 'Is the add/drop round for all course types?'"),
    Rule("SET.HALF_SEM_DISJOINT", "Opposite half-semesters cannot clash",
         "A first-half and a second-half course in the same weekly slot do not conflict.",
         Status.INFERRED, "Not stated. Follows from the timetable Term field and separate second-half rounds.",
         note="Low risk: the only coherent reading of a term-split timetable."),
    Rule("SET.CREDIT_CAP", "Semester credit cap",
         "25 credits per semester. Extra bid points cannot exceed it.",
         Status.OFFICIAL, "Course_Bidding_Introduction.pdf s.5; "
         "Course_Bid_Point_Allocation_Concept_Note_revised_final.pdf s.5", value=25, configurable=True,
         note="Configurable because an individual student may hold an approved overload. "
              "Course_Enrolment_FAQ_1.pdf confirms an approved-extension mechanism exists but states its "
              "exact portal handling 'will be confirmed' - still genuinely unresolved by the University "
              "itself, not just by this project."),
    Rule("ROUND.SEQUENCE", "Nine main rounds plus a three-round second-half CCC cycle",
         "1 swap, 2 ME bid, 3 ME drop, 4 CCC/UWE bid I, 5 swap II, 6 CCC/UWE bid II, "
         "7 ME waitlist, 8 CCC/UWE waitlist, 9 add/drop; then 10-12 for half-semester CCC.",
         Status.OFFICIAL, "Course_Bidding_Introduction.pdf s.4; "
         "Course_Enrolment_FAQ_1.pdf, 'What are the rounds?'"),
    Rule("ROUND.WAITLIST_BY_BID", "Waitlist order is by bid, not by who clicks first",
         "The Major Elective and CCC/UWE waitlist rounds settle exactly like a normal bidding round: "
         "highest bid wins, tie-break as usual. Speed of clicking has no effect.",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'How does the waitlist round work - by bid or "
         "first-come?'"),
    Rule("ROUND.SWAP_NO_POINTS", "Swap rounds use no bid points",
         "Core section swaps in rounds 1 and 5 are free, matched two-way in random tie-break order. Limited "
         "to one swap per round; a swap completes only if there is an open seat or a mutual counterparty "
         "swapping the other way. Only a whole batch can swap (e.g. L1-T1-P1 -> L2-T2-P2); individual "
         "components cannot be mixed (L1-T2-P1 is not valid).",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'Swaps (major-core sections)'"),
    Rule("ROUND.DROPPED_SEAT", "A dropped seat is not returned to the prior loser",
         "It re-enters the pool and is contested again in the waitlist round.",
         Status.OFFICIAL, "Guide s.4, s.9"),
    Rule("ROUND.DROP_REFUND", "Drop refund depends on which round you drop in",
         "Dropping a Major Elective in the dedicated Major Elective Drop round refunds the clearing price "
         "in full. Dropping any course (CCC/UWE/ME) in the Add/Drop round gets no refund of the clearing "
         "price already paid.",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'Can I drop a course after attending a class, get "
         "my points back, and take another?'",
         impact="A student weighing whether to drop late should know the Add/Drop round is not free, "
                "unlike the Major Elective Drop round."),
    Rule("BUDGET.SHARED_LIVE", "Simultaneous bids share a live category balance",
         "Points placed on a bid are held against that category's balance while the round is open, and "
         "released back to the same category if the bid is unsuccessful. A student cannot commit more "
         "points across simultaneous bids than they hold in that category.",
         Status.OFFICIAL,
         "Course_Enrolment_FAQ_1.pdf, 'If I bid on several courses in a round, do they all draw from one "
         "balance?': 'Each category ... has its own balance. Points you place on a bid are held against "
         "that balance ... You cannot commit more than you have.'",
         configurable=True, value="SHARED_LIVE",
         impact="Confirms the allocation problem is real: a student's category pool is a genuine shared "
                "constraint across every course bid in that category during an open round, not an "
                "independent per-course budget.",
         resolution="Resolved 2026-08-05 by the FAQ above; SHARED_LIVE is now the sole official reading. "
                    "The INDEPENDENT mode is kept in the optimizer purely as a labelled hypothetical "
                    "comparison (same treatment as the OPTIMISTIC competition scenario), never presented "
                    "as an equally-plausible alternative going forward."),
    Rule("RETAKE.FAILED_CORE", "Failed core courses: Google Form, not automatic re-enrolment",
         "A failed core (mandatory) course being re-offered is re-enrolled subject to filling the Google "
         "Form floated by the Dean's Office. That enrolment counts toward the 25-credit cap. Repeating a "
         "non-core course requires ordinary bidding, not this form.",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'Retakes and grade improvement'; "
         "Dean Academics email, 2026-08-05: 'the last date to fill this form is 7 August 2026'",
         note="The email's stated deadline (7 August 2026) is imminent relative to this document's most "
              "recent update (2026-08-06) - one day away. Surfaced prominently in the UI, not just here."),
    Rule("RETAKE.GRADE_IMPROVEMENT", "Grade-improvement retakes use a separate Add/Drop-period form",
         "Requests for grade improvement, for both core and elective courses, go through a separate "
         "Google Form during the Add/Drop period. Processed only if seats remain, the credit limit "
         "permits, and there is no timetable clash with confirmed enrolments.",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'How do I retake a course for grade improvement?'"),
    Rule("RETAKE.ATTENDANCE_WAIVER", "Automatic attendance waiver for clean retakes and grade improvement",
         "A course retake with no F*/Ab grade in the prior attempt gets an automatic attendance waiver, as "
         "does a grade-improvement retake. Enrolling despite a genuine timetable clash still needs "
         "instructor approval.",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'Can I get an attendance waiver to retake a course "
         "that clashes, as before?'"),
    Rule("RETAKE.NO_EXTRA_POINTS", "No extra bid points for retaking a course",
         "A retake draws on the student's ordinary category pool like any other bid; grade-improvement "
         "retakes are handled entirely through the separate form, not through bid points.",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'Do I get extra bid points for retaking a course?'"),
    Rule("ENROL.SWAYAM_NO_BID", "SWAYAM/NPTEL courses do not go through bidding",
         "No seat constraint, so no bidding round applies. Enrolment happens in the Add/Drop round, "
         "subject to the semester credit limit.",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'Swayam / NPTEL courses'"),
    Rule("ENROL.BACKEND_DISCONTINUED", "Backend (non-bid) enrolment is discontinued except for named exceptions",
         "Retained only for automatic re-enrolment into failed core courses, Course Audits, and "
         "Master's/PhD enrolment with the necessary approvals. Every other enrolment path goes through "
         "bidding.",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'Backend enrolment'"),
    Rule("POOL.FEEDBACK_BONUS_FUTURE", "Future: bonus bid points for course-feedback forms",
         "From Spring 2027 onward, filling course-feedback forms may award bonus bid points. Not yet in "
         "effect for Monsoon 2026 and not modelled in any pool formula here.",
         Status.OFFICIAL, "Course_Enrolment_FAQ_1.pdf, 'Are bid points based on CGPA?'",
         note="Purely forward-looking; deliberately excluded from compute_pools() until the University "
              "publishes the actual mechanism."),
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
