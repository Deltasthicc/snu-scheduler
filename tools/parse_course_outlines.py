#!/usr/bin/env python3
"""Parse the Academic Office's per-course "Course Outline" PDFs into compact,
structured JSON.

Source: a zip of ~337 PDFs (one per course code), each a fixed-template form
- Section A (course details), B (objectives/weekly modules), C (grading
type), D (component-weightage table), E/F (admin-only, not extracted). The
template is consistent across files but pdfplumber's table-row detection is
NOT perfectly consistent (the same logical field sometimes lands as its own
row, sometimes gets merged with a neighbour), so this parser works off KNOWN
LABEL TEXT rather than fixed row/column positions, and every field that
cannot be found with confidence is left null - never guessed.

Output is deliberately NOT the raw PDFs (25.9MB across 337 files - too big to
ship in a public repo/deployment) but the extracted text (a few MB), keyed by
the course code exactly as printed in the outline, plus a match against the
active timetable dataset so a mismatch is visible rather than silent.

Usage:
    python3 tools/parse_course_outlines.py <outlines.zip> [--write] [--sample N]
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "backend" / "app" / "data" / "course_outlines.json"

# Section-A fields are on ONE row: [.., 'LABEL*', 'VALUE', 'LABEL2*', 'VALUE2', ..]
SECTION_A_PAIRS = {
    "COURSE CODE*": "code", "COURSE TITLE*": "title_from_outline",
    "COURSE CREDITS*": "credits_from_outline", "SEMESTER*": "semester",
    "COMPONENT*": "component_breakdown", "SCHOOL*": "school",
    "SEATS*": "seats_from_outline", "DEPARTMENT*": "department",
    "METHOD OF INSTRUCTION*": "method_of_instruction", "FACULTY*": "faculty",
    "FACULTY EMAIL ADDRESS*": "faculty_email",
}

# These fields are a bare label on their own row, with the actual text as the
# NEXT row - unless the next row is itself another known label, which means
# this field was left blank in that particular outline.
LABEL_THEN_VALUE_ROW = {
    "COURSE PREREQUISITES": "prerequisites",
    "PROGRAM LEARNING GOALS": "program_learning_goals",
    "INTRODUCTION TO THE COURSE": "introduction",
    "COURSE OBJECTIVES*": "objectives",
    "LEARNING OUTCOMES*": "learning_outcomes",
    "SKILL DEVELOPMENT*": "skill_development",
    "TEXT BOOK(S), REFERENCE BOOK(S) AND ANY OTHER STUDY MATERIAL*": "textbooks",
}

WEEK_RE = re.compile(r"^COURSE MODULE\s*-\s*WEEK\s*(\d+)\*?$", re.IGNORECASE)
GRADING_TYPE_RE = re.compile(r"^GRADING TYPE\*?$", re.IGNORECASE)
KNOWN_COMPONENTS = {"MID SEM EXAM", "END SEM EXAM", "QUIZ(S)", "ASSIGNEMENT(S)", "ASSIGNMENT(S)",
                    "LAB", "PROJECT", "CASE STUDIES", "GROUP DISCUSSION",
                    "ANY OTHER COMPONENT", "CLASS PARTICIPATION"}


def _clean(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


# A minority of these PDFs (confirmed: CCC2116 and others) extract with their
# characters interleaved within words - "CNOonUeRSE PREREQUISITES" instead of
# "COURSE PREREQUISITES". Plain pypdf text extraction on the SAME file reads
# perfectly cleanly, so this is specific to pdfplumber's cell-reconstruction
# for that file's particular text-run layout, not a font/encoding problem in
# the PDF itself - but it means table-cell text cannot be trusted blindly.
# Real English text essentially never has a lowercase letter immediately
# followed by an uppercase letter more than once within one word; garbled
# text does this constantly. Below a rough threshold this reliably tells the
# two apart (checked against a known-clean and a known-garbled sample: scores
# 0.0 and 0.24). Better to store nothing than to ship a student a paragraph
# of scrambled text as if it were the real course description.
def _garble_score(text: str) -> float:
    words = text.split()
    if not words:
        return 0.0
    bad = 0
    for w in words:
        core = re.sub(r"[^A-Za-z]", "", w)
        if len(core) < 4:
            continue
        transitions = sum(1 for i in range(1, len(core)) if core[i - 1].islower() and core[i].isupper())
        if transitions >= 2:
            bad += 1
    return bad / len(words)


GARBLE_THRESHOLD = 0.08


def _short_token_ratio(text: str) -> float:
    """A second, independent garble signal: real prose averages ~4-5
    characters per word, but one observed corruption pattern scatters
    letters into space-separated 1-2 character fragments rather than
    scrambling case within longer words - which _garble_score's word-length
    >= 4 requirement cannot see at all (it skips exactly these fragments).
    Checked against a real fragmented sample: 94% short tokens, vs 11% for
    clean prose of comparable length."""
    words = text.split()
    if not words:
        return 0.0
    return sum(1 for w in words if len(w) <= 2) / len(words)


def _is_garbled(text: str) -> bool:
    if not text:
        return False
    if _garble_score(text) > GARBLE_THRESHOLD:
        return True
    words = text.split()
    return len(words) >= 15 and _short_token_ratio(text) > 0.35


def _is_known_label(text: str) -> bool:
    """True if `text` is itself a section/field label - used to detect that a
    LABEL_THEN_VALUE_ROW field was left blank (the "next row" is another
    label, not a value)."""
    if not text:
        return False
    if text in LABEL_THEN_VALUE_ROW or text in SECTION_A_PAIRS:
        return True
    if WEEK_RE.match(text) or GRADING_TYPE_RE.match(text):
        return True
    return text.upper().startswith("SECTION ")


INSTRUCTIONS_TEMPLATE_MARKER = "Instructions for Filling the Course Outline Form"


def _is_blank_instructions_template(pdf_bytes: bytes) -> bool:
    """9 files in the source zip are not a filled course outline at all - the
    Office's own blank instructions template, uploaded under a real course's
    filename by mistake (confirmed by opening two files that share a course
    code: one is the genuine filled outline, the other is this template,
    verbatim identical to this template in the other 8 cases where no
    correctly-filled duplicate exists at all). Must be excluded rather than
    parsed as if it described that course - every field would come back
    either null or, worse, a plausible-looking fragment of instructional
    boilerplate mistaken for real content."""
    from pypdf import PdfReader
    try:
        text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text() or ""
    except Exception:
        return False
    return text.strip().startswith(INSTRUCTIONS_TEMPLATE_MARKER)


CODE_TITLE_RE = re.compile(r"^([A-Z]{2,4}\d{2,4}(?:/[A-Z0-9]+)?)\s+(.+)$")
CREDITS_SEMESTER_RE = re.compile(r"^(\d+(?:\.\d+)?)\s+(Monsoon|Spring)\s+(\d{4})$")
SEATS_DEPT_RE = re.compile(r"^(\d{1,4})(?:\s+(.+))?$")
METHOD_FACULTY_RE = re.compile(r"^(In Person|Online(?: \([^)]+\))?|Hybrid|Blended)\s+(.+)$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def _plain_text_section_a_fallback(pdf_bytes: bytes) -> dict:
    """Section A via plain linear text (pypdf), matched by PATTERN rather than
    fixed line position.

    Table-cell extraction is unreliable for a minority of these files (see
    _is_garbled's docstring), but plain linear text reads perfectly cleanly on
    the exact same files - confirmed directly, not assumed. Position alone is
    not safe either, though: a genuinely blank field (no Component, or no
    Department) simply omits that line rather than leaving it empty, which
    shifts every later line up by one - confirmed across a random sample of
    real files, where "seats + department" sits at a different line index
    depending on whether "Component" was filled in. Matching each line
    against what that FIELD's value always looks like (course-code-shaped,
    "<number> Monsoon <year>"-shaped, "In Person <name>"-shaped, contains an
    '@') is immune to that shifting.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text() or ""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out: dict = {}
    for line in lines[:12]:
        if "code" not in out:
            m = CODE_TITLE_RE.match(line)
            if m:
                out["code"], out["title_from_outline"] = m.group(1), m.group(2)
                continue
        if "credits_from_outline" not in out:
            m = CREDITS_SEMESTER_RE.match(line)
            if m:
                out["credits_from_outline"] = float(m.group(1))
                out["semester"] = f"{m.group(2)} {m.group(3)}"
                continue
        if "method_of_instruction" not in out:
            m = METHOD_FACULTY_RE.match(line)
            if m:
                out["method_of_instruction"], out["faculty"] = m.group(1), m.group(2).strip()
                continue
        if "faculty_email" not in out:
            m = EMAIL_RE.search(line)
            if m:
                out["faculty_email"] = m.group(0)
        if "seats_from_outline" not in out and "department" not in out:
            m = SEATS_DEPT_RE.match(line)
            # exclude the credits+semester line and the "Please check..."
            # placeholder line, which can also start with digits in theory
            if m and "Please check" not in line and "@" not in line:
                out["seats_from_outline"] = int(m.group(1))
                if m.group(2):
                    out["department"] = m.group(2).strip()
    return out


def parse_outline(pdf_bytes: bytes, source_name: str) -> dict:
    import pdfplumber

    rows: list[list[str]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for raw_row in table:
                    rows.append([_clean(c) for c in raw_row])

    out: dict = {
        "source_file": source_name, "code": None, "credits_from_outline": None,
        "faculty": None, "faculty_email": None, "department": None, "school": None,
        "component_breakdown": None, "method_of_instruction": None,
        "prerequisites": None, "objectives": None, "learning_outcomes": None,
        "skill_development": None, "program_learning_goals": None, "introduction": None,
        "textbooks": None, "weekly_modules": {}, "grading_type": None,
        "grading_notes": None, "assessment_components": [],
        "parse_warnings": [],
    }

    for i, row in enumerate(rows):
        non_empty = [c for c in row if c]
        if not non_empty:
            continue

        # Section A: label/value pairs sitting in the same row.
        for j, cell in enumerate(row):
            field = SECTION_A_PAIRS.get(cell)
            if field and j + 1 < len(row) and row[j + 1]:
                value = row[j + 1]
                if value.upper().startswith("PLEASE CHECK RELEVANT BOX"):
                    continue  # a checkbox field with nothing checked, not a real value
                out[field] = value

        first = row[0]

        # "Label on its own row, value on the next row" fields.
        field = LABEL_THEN_VALUE_ROW.get(first)
        if field and out.get(field) is None:
            nxt = rows[i + 1] if i + 1 < len(rows) else []
            nxt_first = nxt[0] if nxt else ""
            if nxt_first and not _is_known_label(nxt_first):
                out[field] = nxt_first

        # Weekly modules: "COURSE MODULE - WEEK N*" label, value on next row.
        m = WEEK_RE.match(first)
        if m:
            nxt = rows[i + 1] if i + 1 < len(rows) else []
            nxt_first = nxt[0] if nxt else ""
            if nxt_first and not _is_known_label(nxt_first):
                out["weekly_modules"][int(m.group(1))] = nxt_first

        # Grading type: label on its own row, value in the SAME row for some
        # outlines and the next row for others - checked directly against a
        # handful of real files before trusting either shape.
        if GRADING_TYPE_RE.match(first):
            if len(row) > 1 and row[1]:
                out["grading_type"] = row[1]
            else:
                nxt = rows[i + 1] if i + 1 < len(rows) else []
                if nxt and nxt[0] and not _is_known_label(nxt[0]):
                    out["grading_type"] = nxt[0]

        # Section D: a component-weightage table row looks like
        # ['MID SEM EXAM', '25%', 'Awarded post approval...', 'Prohibited...']
        # or all of that jammed into row[0] as one multi-line cell (both
        # shapes were observed across real files).
        if first in KNOWN_COMPONENTS or any(
                first.upper().startswith(c) for c in KNOWN_COMPONENTS):
            weightage = None
            if len(row) > 1:
                for c in row[1:]:
                    wm = re.search(r"(\d{1,3})\s*%", c or "")
                    if wm:
                        weightage = int(wm.group(1))
                        break
            if weightage is None:
                wm = re.search(r"(\d{1,3})\s*%", first)
                if wm:
                    weightage = int(wm.group(1))
            if weightage is not None:
                out["assessment_components"].append({
                    "component": first.split("\n")[0][:40], "weightage_pct": weightage})

    # A row like "SECTION D...\nCOMPONENT* WEIGHTAGE %*...\nMID SEM EXAM 25%..."
    # sometimes arrives as ONE multi-line cell rather than separate rows -
    # covered as a fallback so components are not silently missed.
    if not out["assessment_components"]:
        for row in rows:
            for cell in row:
                if cell and "COMPONENT EVALUATION" in cell.upper():
                    for comp in KNOWN_COMPONENTS:
                        cm = re.search(re.escape(comp) + r"\s+(\d{1,3})\s*%", cell, re.IGNORECASE)
                        if cm:
                            out["assessment_components"].append(
                                {"component": comp, "weightage_pct": int(cm.group(1))})

    # Garble sweep: drop any free-text field the table extraction produced
    # that scores as scrambled, and fill Section A from the plain-text
    # fallback wherever the table method left a field null or garbled -
    # never overwriting a value that was already clean.
    text_fields = ["title_from_outline", "component_breakdown", "school", "department",
                  "method_of_instruction", "faculty", "prerequisites", "objectives",
                  "learning_outcomes", "skill_development", "program_learning_goals",
                  "introduction", "textbooks", "grading_type", "grading_notes"]
    garbled_fields = []
    for f in text_fields:
        if _is_garbled(out.get(f)):
            garbled_fields.append(f)
            out[f] = None
    for wk, val in list(out["weekly_modules"].items()):
        if _is_garbled(val):
            garbled_fields.append(f"weekly_modules[{wk}]")
            del out["weekly_modules"][wk]
    if garbled_fields:
        out["parse_warnings"].append(f"dropped garbled table text for: {garbled_fields}")

    needs_fallback = not out["code"] or _is_garbled(out.get("title_from_outline"))
    if needs_fallback:
        try:
            fallback = _plain_text_section_a_fallback(pdf_bytes)
        except Exception as exc:  # pragma: no cover - defensive, a bad PDF must not crash the batch
            fallback = {}
            out["parse_warnings"].append(f"plain-text fallback raised {exc!r}")
        for field, value in fallback.items():
            if out.get(field) is None:
                out[field] = value
        if fallback:
            out["parse_warnings"].append("Section A filled from plain-text fallback (table extraction failed)")

    if not out["code"]:
        out["parse_warnings"].append("no COURSE CODE* row found")
    if not out["assessment_components"]:
        out["parse_warnings"].append("no assessment/weightage components found")
    if out["credits_from_outline"] is not None:
        try:
            out["credits_from_outline"] = float(out["credits_from_outline"])
        except ValueError:
            out["parse_warnings"].append(f"credits value not numeric: {out['credits_from_outline']!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip_path", type=Path)
    ap.add_argument("--write", action="store_true", help=f"write {OUT_PATH.relative_to(REPO_ROOT)}")
    ap.add_argument("--sample", type=int, default=0, help="only parse the first N files (for a quick check)")
    args = ap.parse_args()

    if not args.zip_path.is_file():
        print(f"ERROR: no such file: {args.zip_path}", file=sys.stderr)
        return 2

    catalog_path = REPO_ROOT / "backend" / "app" / "data" / "courses.json"
    catalog_codes = set()
    if catalog_path.is_file():
        catalog_codes = {c["code"] for c in json.loads(catalog_path.read_text(encoding="utf-8"))}
        # a course outline is filed under its OWN department code, which may be
        # one component of a slash-joined catalog code ("CSD2003" vs the
        # catalog's "CSD211/CSD2003") - index every component too.
        for code in list(catalog_codes):
            for part in code.split("/"):
                catalog_codes.add(part)

    zf = zipfile.ZipFile(args.zip_path)
    pdf_names = sorted(n for n in zf.namelist() if n.lower().endswith(".pdf"))
    if args.sample:
        pdf_names = pdf_names[:args.sample]

    outlines: dict[str, dict] = {}
    unmatched, warnings_count, skipped_templates, collisions = [], 0, [], []
    for name in pdf_names:
        data = zf.read(name)
        if _is_blank_instructions_template(data):
            skipped_templates.append(name)
            continue
        parsed = parse_outline(data, Path(name).name)
        file_code = re.sub(r"_Monsoon_2026$", "", Path(name).stem.strip(), flags=re.IGNORECASE)
        code = parsed["code"] or file_code
        if code in outlines:
            collisions.append(code)
            continue  # first (alphabetically earliest filename) wins; both are logged either way
        if code != parsed["code"] and parsed["code"]:
            parsed["parse_warnings"].append(
                f"filename code {file_code!r} != in-PDF COURSE CODE {parsed['code']!r}")
        if code not in catalog_codes:
            unmatched.append(code)
        if parsed["parse_warnings"]:
            warnings_count += 1
        outlines[code] = parsed

    print(f"parsed        : {len(outlines)} outlines")
    print(f"skipped (blank instructions template, not a real outline): {len(skipped_templates)}")
    if skipped_templates:
        print(f"  -> {[Path(n).name for n in skipped_templates]}")
    if collisions:
        print(f"duplicate code after normalising filename ({len(collisions)}): {collisions}")
    print(f"with warnings : {warnings_count}")
    print(f"not in active catalog: {len(unmatched)} -> {unmatched[:15]}{' ...' if len(unmatched) > 15 else ''}")
    have_prereq = [c for c, o in outlines.items() if o.get("prerequisites")]
    print(f"non-empty prerequisites field: {len(have_prereq)} -> {have_prereq[:10]}")
    have_components = sum(1 for o in outlines.values() if o["assessment_components"])
    print(f"outlines with an assessment breakdown: {have_components}")

    if not args.write:
        print("dry run - re-run with --write to update", OUT_PATH.relative_to(REPO_ROOT))
        return 0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(outlines, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} ({OUT_PATH.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
