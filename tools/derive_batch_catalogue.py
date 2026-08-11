#!/usr/bin/env python3
"""Derive an ANONYMOUS batch catalogue from the Academic Office's student-batch
workbook.

PRIVACY - read before changing anything here.
=============================================
The source workbook is a student ROSTER: roll number, full name, year, batch
and major for ~2,700 named individuals. This repository is public and the app
is deployed publicly, so that file must never be committed, and no artifact
derived from it may carry a roll number or a name.

This script therefore reads the roster only to produce a per-BATCH aggregate:

    batch code -> {year, programme, size}

That is institutional structure (the same thing the timetable already names in
its "Student Block" column), not personal data - it says "CSD31 is a
third-year CSE batch of 42 students", never who is in it. The roster itself
stays outside the repo; only this aggregate is written into
backend/app/data/student_batches.json.

The aggregate is worth having because the timetable's batch tags previously
had nothing to validate against: batch codes were parsed out of free-text
cells and simply trusted. With this catalogue the importer's expansion of
range/compound tokens can be checked against the batches that actually exist,
and a batch size gives an honest floor for how many students are guaranteed to
need a given batch-locked core section.

Usage:
    python3 tools/derive_batch_catalogue.py <Student Batches.xlsx> [--write]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "backend" / "app" / "data" / "student_batches.json"

SHEET = "Data Shared By office"
COL_BATCH = "Student Groups"
COL_YEAR = "Year"
COL_MAJOR = "Major Code and Description"
# Deliberately never read: the roll-number and name columns. Listed so a future
# reader can see the omission is a decision, not an oversight.
PII_COLUMNS_NEVER_READ = ("Campus ID (Roll Number)", "Student Full Name")


def derive(workbook: Path) -> dict:
    import pandas as pd

    frame = pd.read_excel(workbook, sheet_name=SHEET)
    for column in (COL_BATCH, COL_YEAR, COL_MAJOR):
        if column not in frame.columns:
            raise SystemExit(f"ERROR: {workbook.name} has no {column!r} column; found {list(frame.columns)}")

    batches: dict[str, dict] = {}
    conflicts: list[str] = []
    for (batch, year, major), group in frame.groupby([COL_BATCH, COL_YEAR, COL_MAJOR]):
        code = str(batch).strip()
        if not code:
            continue
        if code in batches:
            # One batch mapping to two different (year, major) pairs would make
            # "which year is CSD31" ambiguous; report rather than pick one.
            conflicts.append(code)
            continue
        batches[code] = {
            "batch": code,
            "year": int(year),
            "programme": str(major).strip(),
            "size": int(len(group)),
        }
    if conflicts:
        raise SystemExit(f"ERROR: batches map to more than one (year, programme): {sorted(set(conflicts))}")
    return {
        "source": workbook.name,
        "note": ("Anonymous aggregate derived from the Academic Office student-batch workbook. "
                 "Contains no roll numbers and no names - only batch, year, programme and headcount."),
        "batch_count": len(batches),
        "student_count": int(len(frame)),
        "batches": [batches[c] for c in sorted(batches)],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", type=Path)
    ap.add_argument("--write", action="store_true", help=f"write {OUT_PATH.relative_to(REPO_ROOT)}")
    args = ap.parse_args()

    if not args.workbook.is_file():
        print(f"ERROR: no such workbook: {args.workbook}", file=sys.stderr)
        return 2

    catalogue = derive(args.workbook)
    print(f"batches   : {catalogue['batch_count']}")
    print(f"students  : {catalogue['student_count']} (aggregated only - no identity is carried through)")
    sizes = [b["size"] for b in catalogue["batches"]]
    print(f"batch size: min {min(sizes)}, median {sorted(sizes)[len(sizes)//2]}, max {max(sizes)}")
    by_year: dict[int, int] = {}
    for b in catalogue["batches"]:
        by_year[b["year"]] = by_year.get(b["year"], 0) + 1
    print(f"by year   : {dict(sorted(by_year.items()))}")

    if not args.write:
        print(f"dry run - re-run with --write to update {OUT_PATH.relative_to(REPO_ROOT)}")
        return 0
    OUT_PATH.write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
