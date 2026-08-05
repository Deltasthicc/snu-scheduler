"""Parser for Shiv Nadar University advisement-report PDFs.

The report is an internal planning document, not an official graduation record. The
parser therefore returns its provenance and warnings with every result. It counts only
courses listed under a requirement's ``Courses Used`` table as completed. Course-history
rows graded F/F* or IP are returned separately and never counted as completed.
"""
from __future__ import annotations

import base64
import binascii
import re
from io import BytesIO
from typing import Any

from pypdf import PdfReader

MAX_PDF_BYTES = 8 * 1024 * 1024


class AdvisementParseError(ValueError):
    pass


PROGRAMME_ALIASES = (
    ("computer science and engineering", "b-tech-in-computer-science-and-engineering"),
    ("chemical engineering", "b-tech-in-chemical-engineering"),
    ("civil engineering", "b-tech-in-civil-engineering"),
    ("mechanical engineering", "b-tech-in-mechanical-engineering"),
    ("electrical and computer engineering", "b-tech-in-electrical-and-computer-engineering"),
)

SECTIONS = (
    ("major_core", "BTECH-CSD MAJOR CORE", "major_core"),
    ("major_elective", "BTECH-CSD MAJOR ELECTIVES", "major_elective"),
    ("project", "BTECH-CSD MAJOR PROJECT", "project"),
    ("basic_science", "Basic Sciences (BS)", "basic_science"),
    ("engineering_science", "Engineering Science (ES)", "engineering_science"),
    ("ccc", "Core Common Curriculum (CCC)", "ccc"),
    ("uwe", "UWE Requirements", "uwe"),
    ("ccc_uwe", "CCC and UWE combined requirement", "ccc_uwe"),
)

ROW_RE = re.compile(
    r"(?m)^(MONS|SPRG|SUMR)\s+(\d{4})\s+([A-Z]{2,5})\s+"
    r"([A-Z]{2,6}\d[\w/-]*)\s+(.+?)\s+(?:(A-|A|B-|B|C-|C|D|E|F\*?)\s+)?"
    r"(\d+(?:\.\d+)?)\s+(EN|IP)\s*$"
)
UNITS_RE = re.compile(
    r"Units:\s*(\d+(?:\.\d+)?)\s+required,\s*(\d+(?:\.\d+)?)\s+used,\s*"
    r"(\d+(?:\.\d+)?)\s+needed",
    re.I,
)


def _decode_pdf(content_base64: str) -> bytes:
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AdvisementParseError("PDF payload is not valid base64") from exc
    if not raw.startswith(b"%PDF"):
        raise AdvisementParseError("uploaded file is not a PDF")
    if len(raw) > MAX_PDF_BYTES:
        raise AdvisementParseError("PDF is larger than the 8 MB limit")
    return raw


def _extract_text(raw: bytes) -> tuple[str, int]:
    try:
        reader = PdfReader(BytesIO(raw))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise AdvisementParseError("password-protected PDFs are not supported")
        pages = [(page.extract_text() or "") for page in reader.pages]
    except AdvisementParseError:
        raise
    except Exception as exc:
        raise AdvisementParseError("the PDF could not be read") from exc
    text = "\n".join(pages)
    if "ADVISEMENT REPORT" not in text.upper() or "Courses Used" not in text:
        raise AdvisementParseError("this does not look like an SNU advisement report")
    return text, len(pages)


def _slice(text: str, start_label: str, end_labels: list[str]) -> str:
    start = text.find(start_label)
    if start < 0:
        return ""
    end_candidates = [text.find(label, start + len(start_label)) for label in end_labels]
    end_candidates = [value for value in end_candidates if value >= 0]
    return text[start:min(end_candidates) if end_candidates else len(text)]


def _rows(block: str, category: str) -> list[dict[str, Any]]:
    used_start = block.find("Courses Used")
    if used_start < 0:
        return []
    used = block[used_start:]
    available = used.find("Courses Available")
    if available >= 0:
        used = used[:available]
    out = []
    for match in ROW_RE.finditer(used):
        term, year, _subject, code, title, grade, units, kind = match.groups()
        grade = grade or ("IP" if kind == "IP" else "")
        if grade in {"F", "F*", "IP"}:
            continue
        out.append({
            "code": code.strip(), "title": title.strip(), "credits": float(units),
            "category": category, "grade": grade, "term": f"{term} {year}",
        })
    return out


def _history_rows(text: str) -> list[dict[str, Any]]:
    start = text.find("Course History")
    if start < 0:
        return []
    out = []
    for match in ROW_RE.finditer(text[start:]):
        term, year, _subject, code, title, grade, units, kind = match.groups()
        grade = grade or ("IP" if kind == "IP" else "")
        out.append({
            "code": code.strip(), "title": title.strip(), "credits": float(units),
            "grade": grade, "term": f"{term} {year}",
        })
    return out


def parse_advisement_report(content_base64: str, filename: str) -> dict[str, Any]:
    raw = _decode_pdf(content_base64)
    text, page_count = _extract_text(raw)

    roll = re.search(r"Advisement Report-Roll No:\s*([^\s]+)", text, re.I)
    student = re.search(r"For\s+(.+?)\s+prepared on\s+([0-9/.-]+)", text, re.I)
    cgpa = re.search(r"C\.G\.P\.A\.:\s*(\d+(?:\.\d+)?)", text, re.I)
    programme = re.search(r"Bachelor of Technology in\s+([^\n]+)", text, re.I)
    programme_title = ("Bachelor of Technology in " + programme.group(1).strip()) if programme else None
    programme_id = None
    low_title = (programme_title or "").lower()
    for needle, value in PROGRAMME_ALIASES:
        if needle in low_title:
            programme_id = value
            break

    cohort_match = re.search(r"Bachelor of Technology Program\s+Monsoon\s+(\d{4})", text, re.I)
    cohort_year = int(cohort_match.group(1)) if cohort_match else None

    title_start = text.find(programme_title) if programme_title else -1
    first_major = text.find("Major Requirements", max(0, title_start))
    overall_block = text[title_start:first_major] if title_start >= 0 and first_major > title_start else text[:4000]
    overall_units = UNITS_RE.search(overall_block)
    total_required, total_used, total_needed = (map(float, overall_units.groups())
                                                 if overall_units else (None, None, None))

    requirements = []
    completed_by_code: dict[str, dict[str, Any]] = {}
    labels = [label for _, label, _ in SECTIONS] + ["REAL, VELS & GIS credits requirement", "Course History"]
    for req_id, label, category in SECTIONS:
        block = _slice(text, label, [other for other in labels if other != label])
        units = UNITS_RE.search(block)
        if not units:
            continue
        required, used, needed = map(float, units.groups())
        requirements.append({
            "id": req_id, "label": label, "required": required,
            "used": used, "needed": needed, "category": category,
        })
        if req_id != "ccc_uwe":
            for course in _rows(block, category):
                completed_by_code[course["code"].upper()] = course

    completed = sorted(completed_by_code.values(), key=lambda row: (row["term"], row["code"]))
    history = _history_rows(text)
    in_progress = [row for row in history if row["grade"] == "IP"]
    failed = [row for row in history if row["grade"] in {"F", "F*"}]

    req_by_id = {row["id"]: row for row in requirements}
    combined_needed = req_by_id.get("ccc_uwe", {}).get("needed")
    ccc_needed = req_by_id.get("ccc", {}).get("needed")
    uwe_needed = req_by_id.get("uwe", {}).get("needed")
    floater = None
    if None not in (combined_needed, ccc_needed, uwe_needed):
        floater = max(0.0, round(combined_needed - ccc_needed - uwe_needed, 2))

    suggestions = {
        "done_credits": total_used, "degree_credits": total_required,
        "remaining_total": total_needed,
        "remaining_major_core": req_by_id.get("major_core", {}).get("needed"),
        "remaining_major_elective": req_by_id.get("major_elective", {}).get("needed"),
        "remaining_project": req_by_id.get("project", {}).get("needed"),
        "remaining_basic_science": req_by_id.get("basic_science", {}).get("needed"),
        "remaining_engineering_science": req_by_id.get("engineering_science", {}).get("needed"),
        "remaining_ccc": ccc_needed, "remaining_uwe": uwe_needed,
        "remaining_floater": floater,
    }
    return {
        "format": "snu_advisement_report", "filename": filename, "page_count": page_count,
        "student": {
            "name": student.group(1).strip() if student else None,
            "roll_number": roll.group(1).strip() if roll else None,
            "report_date": student.group(2) if student else None,
            "cgpa": float(cgpa.group(1)) if cgpa else None,
        },
        "programme_id": programme_id, "programme_title": programme_title,
        "cohort_year": cohort_year,
        "totals": {"required": total_required, "used": total_used, "needed": total_needed},
        "requirements": requirements, "completed_courses": completed,
        "in_progress_courses": in_progress, "failed_courses": failed,
        "profile_suggestions": suggestions,
        "warnings": [
            "The University labels this report as an internal planning tool, not an official graduation record.",
            "Only courses listed in a requirement's Courses Used table were counted as completed.",
            "F, F*, and IP course-history rows were not counted as completed.",
        ],
    }
