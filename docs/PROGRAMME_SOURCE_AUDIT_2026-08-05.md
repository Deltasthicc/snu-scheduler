# Programme source audit — 5 August 2026

## Evidence rules

- Requirements come only from SNU programme pages, SNU prospectuses/brochures,
  or University regulations linked in `backend/app/data/programs.json`.
- A timetable proves meetings, sections, blocks and capacities—not credits or
  degree requirements.
- Missing public requirements stay `source_linked_partial`. A private COS may
  fill them, and the app visibly identifies the resulting override.
- Transfer/lateral-entry credit remains an aggregate lower bound; it is never
  expanded into invented SNU courses or categories.

## Independent Scooby cross-check

`rohitjg13/Scooby` and this scheduler both preserve 1,770 Monsoon 2026 timetable
rows (1,191 unique meetings). Canonical comparison is identical across code,
title, type, UWE flag, component, section, block, term, day, time, room,
instructor, capacity, note and source row id.

Scooby's parser explicitly assigns `credits: 0`, documenting that its timetable
JSON contains no credit hours. It is therefore an independent timetable check,
not a source for degree credits, degree rules or bidding policy.

## Corrections from current official structure pages

- **BA (Research) International Relations:** complete 150-credit breakdown—44
  IR core, 12 School compulsory, 40 major elective, 12 dissertation, 24 UWE,
  and 18 CCC.
- **Master of Fine Arts:** 64 total—16 Practice Core, 16 Theory Core and 32
  elective credits.
- **BA (Research) English:** 24 four-credit English courses and a 12-credit
  UGSRP are confirmed. Its complete degree-wide basket is not stated there, so
  coverage remains partial.

Re-run the audit with:

```powershell
python tools\validate_program_catalog.py
python tools\validate_program_catalog.py --check-urls
```
