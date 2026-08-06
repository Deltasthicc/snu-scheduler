# CODEX.md - evidence and verification guardrails

Read this file and `CLAUDE.md` before changing the project. `CLAUDE.md` is the
technical history; this file is the shorter checklist for preventing repeat mistakes.

## Source hierarchy

When two inputs disagree, use this order and show the disagreement to the student:

1. Student-confirmed accepted transfer credit and other official records that a
   University advisement report may omit.
2. A current student-specific University advisement report for the courses and
   requirement rows it actually contains.
3. A current University policy or handbook for enrolment rules.
4. The programme's current official page/prospectus for curriculum totals.
5. University bidding documents for pool and settlement formulas.
6. A student's locally imported JSON for preferences and planning assumptions.
7. Inference. Inferences must be labelled and must never overwrite a higher source.

Scooby's Monsoon 2026 timetable is a valid independent meeting-data cross-check,
but its parser assigns zero credits because the source has no credit field. Never
use Scooby as authority for credits, bidding policy, or degree requirements.

The shipped app contains institutional data only. Names, roll numbers, grades,
backlogs, completed courses, planned courses, and personal credit totals belong only
in a student's local plan. Never copy a real student's data into defaults, examples,
tests, screenshots, or the institutional course catalogue.

## Definition of done

Do not say a feature works until the applicable rows below have been run and read:

- Pure logic: unit tests include boundary, malformed-input, and contradictory-input cases.
- API: request goes through the real ASGI route, not only a service function.
- UI: fresh browser storage, imported-profile storage, and visible error state are exercised.
- Desktop: every new runtime data file/dependency is included and the rebuilt artifact is launched.
- Timetable: the app-open path triggers a backend check and never reports `never` as if it were success.
- PDF import: extracted text and rendered pages are both inspected; failed/IP courses are not completed.
- Privacy: scan source/defaults and test a fresh browser. Dataset course codes are not personal data.
- Performance: report measured bundle/artifact size; do not infer a size win from dependency files.

Before testing, confirm ports 5173 and 8000 are not held by stale processes. After
testing, stop only the exact processes created for the test.

## Current authoritative facts (2026-08-05)

- Standard undergraduate semester ceiling: 25 credits.
- Class of 2027 onward: Years I-II minimum 15; Years III-IV minimum 12 unless fewer
  than 12 credits remain for graduation.
- No extension in Years I-III.
- Eligible Year IV students may take 2 additional credits with UG-advisor
  recommendation; no Dean approval is required for that 27-credit case.
- Beyond those 2 credits requires UG-advisor recommendation and Dean approval;
  no extension may exceed 30 total credits. Additional eligibility conditions apply.
- CSE programme: 160 total; 61 major core; 15 major elective; 17 basic science;
  13 engineering science; 12 project; CCC/UWE 42 combined with at least 18 each.
- CSE's current published optional specialisations are exactly three: Artificial
  Intelligence and Machine Learning, Data Science and Big Data Analytics, and Cyber
  Security and Privacy. Do not revive the legacy fourth "Systems and Networks" bucket.
- Programme pathway labels are not interchangeable. A B.Des. stream, ASU partner route,
  doctoral research area, minor, and formal award-bearing specialisation must remain
  distinct. Only calculate pathway credit when an official course mapping is present.
- Bid pools follow the published bidding concept-note formulas, but the concept note's
  own average fourth-year example contains unresolved arithmetic inconsistencies.
  Show formula provenance and input values; never call the disputed example verified.

## Known input conflict for the attached private profile

The student has confirmed that the reconstructed schema-6 JSON's 105-credit planning
total is intentional: 35 credits visible as used in the attached advisement report,
62 accepted transfer credits from lateral entry, and 8 Summer credits not yet posted
to that report. The PDF is an example import format and a partial view, not the sole
authority for this student's courseload. Keep the three buckets visible and separate;
do not replace 105 with 35, and do not fabricate course codes/categories for transfer
credit. F/F* remain excluded. IP credits are included in the planning aggregate only
when the student explicitly supplies or confirms that supplemental amount.

## Communication rule

Report what was implemented, what was actually exercised, and what remains uncertain
as three separate statements. A partial public curriculum, unavailable dependency,
or untested packaged path is a named limitation, not a footnote hidden behind “done”.
