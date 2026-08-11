"""Per-course outline lookup: objectives, weekly syllabus, grading breakdown,
prerequisites, and faculty, as published by the Academic Office ahead of the
COMPAS live round.

Source: backend/app/data/course_outlines.json, produced by
tools/parse_course_outlines.py from a zip of ~337 per-course PDFs (see that
script's own docstring for the extraction method and the data-quality issues
it found and handles: garbled table-cell text in a minority of files, and 9
files that turned out to be a blank instructions template rather than a real
outline). This module does no further interpretation - it serves exactly
what that script wrote, including nulls where the source genuinely had
nothing to extract.
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "course_outlines.json"


class OutlineCatalog:
    def __init__(self, path: Path = DATA_FILE):
        self._by_code: dict[str, dict] = {}
        if path.exists():
            self._by_code = json.loads(path.read_text(encoding="utf-8"))

    def codes(self) -> list[str]:
        """Every course code with a real (non-template) outline on file -
        cheap enough to fetch once at boot so the frontend can show an
        "outline available" affordance without a round trip per course."""
        return sorted(self._by_code)

    def get(self, code: str) -> dict | None:
        """Exact match first; falls back to a shared '/'-separated component,
        the same convention app/timetable_updates/normalize.py's
        find_existing_match() uses for renames. The Office files one outline
        PDF per submitting department, under that department's own course
        code ("AMP1001"), while the timetable's catalog code for the same
        cross-listed course joins both departments' codes ("ART202/AMP1001") -
        so a direct-key lookup on the catalog's own code would miss every
        cross-listed outline that exists."""
        if code in self._by_code:
            return self._by_code[code]
        parts = {p.strip() for p in code.split("/") if p.strip()}
        if not parts:
            return None
        for outline_code, outline in self._by_code.items():
            outline_parts = {p.strip() for p in outline_code.split("/") if p.strip()}
            if outline_parts & parts:
                return outline
        return None
