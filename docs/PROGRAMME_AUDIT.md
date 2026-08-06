# Programme catalogue and degree audit

The programme snapshot in `backend/app/data/programs.json` was checked on 2026-08-05
against Shiv Nadar University's official [programmes catalogue](https://snu.edu.in/programs/).
It contains all 44 programme entries shown there, their level, official page, source
links, and any requirement table that could be supported by an official public
programme page, brochure, prospectus, or university regulation.

The separate `backend/app/data/pathways.json` catalogue was checked on 2026-08-06.
It contains exactly one pathway record for each of the same 44 programme IDs. The
separation is deliberate: degree-completion minima and optional pathways are not the
same audit. The pathway UI distinguishes five evidence types:

- formal specialisations (including embedded named specialisations);
- programme streams or majors;
- partner/destination degree routes;
- supervisor-led doctoral research areas; and
- programmes for which no separate formal specialisation is published.

Only an official course-mapped option gets an automatic credit total. A published
option without a complete public course mapping is shown but not numerically inferred.
Current cohort-sensitive mappings include CSE (three options, not the former legacy
four-bucket display), Civil, ECE, Mechanical, Chemical Engineering, IHS, and B.Des.
The app also enforces published mandatory-course groups for interdisciplinary ECE and
Mechanical tracks when evaluating current progress.

## Coverage and deliberate limits

Thirty-three programmes have built-in auditable requirements. These include the
published credit structures for the covered taught degrees and the university-wide
milestones in the official Ph.D. Regulations for every doctoral programme. The audit
does not allocate the same course twice inside a combined minimum: for example, CCC,
UWE, and the CCC+UWE total are checked independently against the same course record.

The following 11 catalogue entries have official source links but no complete public
fixed requirement table in the material found:

- Accelerated Masters Program with ASU
- B.A. (Research) in English
- B.Sc. (Research) in Economics
- B.Sc. (Research) in Economics and Finance
- BA (Research) in International Relations
- BS Business (Dual Degree with ASU)
- BS Computer Science (Dual Degree with ASU)
- Integrated M.Sc.-Ph.D. in Life Sciences
- M.Sc. in Economics
- Master of Fine Arts
- MBA

They remain selectable and the app can audit them using `auditRequirements` imported
from the student's private plan JSON. Until those cohort-specific rules are supplied,
the UI explicitly labels the result as partial. This is intentional: absence of a
public curriculum is never replaced with a guessed total or category minimum.

## Student data format

Completed courses are entered one per line in the Profile tab:

```text
CCC101 | 3 | CCC
CSD101 | 4 | major_core
```

Supported categories are defined by the selected programme's requirement predicates;
common examples are `CCC`, `UWE`, `major_core`, `major_elective`, `basic_science`,
`engineering_science`, and `project`. Doctoral milestones are comma-separated IDs such
as `coursework`, `comprehensive_exam`, `thesis_submission`, and `defense`.

Selected courses from the current picker are shown under **Selected now**. They can
reduce **Left after plan**, but they are never silently added to completed work.

Private JSON overrides use this shape inside a schema-version 7 plan:

```json
{
  "auditRequirements": [
    {
      "id": "programme_total",
      "label": "Programme total",
      "kind": "credits",
      "required": 80,
      "categories": ["any"]
    }
  ]
}
```

Keep overrides private: the shipped catalogue and example profile contain no personal
student history.
