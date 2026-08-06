"""Deterministic, source-aware degree progress auditing.

Requirements are independent predicates. This is intentional: one CCC course must
count toward both the CCC minimum and the combined CCC/UWE minimum, but never twice
inside either predicate. No requirement is inferred when an official source does not
publish enough detail; a private profile may supply a cohort-specific override instead.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.models.audit_schemas import AuditCourse, AuditRequirementOverride, DegreeAuditRequest

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "programs.json"
PATHWAYS_FILE = Path(__file__).resolve().parents[1] / "data" / "pathways.json"


class ProgrammeCatalog:
    def __init__(self, path: Path = DATA_FILE):
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.meta = {key: value for key, value in payload.items() if key != "programs"}
        self.programs = payload["programs"]
        pathways_path = path.with_name("pathways.json")
        if pathways_path.exists():
            pathway_payload = json.loads(pathways_path.read_text(encoding="utf-8"))
            pathway_by_id = pathway_payload.get("programmes", {})
            program_ids = {program["id"] for program in self.programs}
            if set(pathway_by_id) != program_ids:
                missing = sorted(program_ids - set(pathway_by_id))
                extra = sorted(set(pathway_by_id) - program_ids)
                raise ValueError(f"pathway catalogue mismatch: missing={missing}, extra={extra}")
            for program in self.programs:
                program["pathways"] = pathway_by_id[program["id"]]
            self.meta["pathways"] = {
                key: value for key, value in pathway_payload.items() if key != "programmes"
            }
        self.by_id = {program["id"]: program for program in self.programs}

    def list(self) -> list[dict[str, Any]]:
        return self.programs

    def get(self, programme_id: str) -> dict[str, Any] | None:
        return self.by_id.get(programme_id)


def _normal_category(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_").replace("/", "_")


def _matches(course: AuditCourse, requirement: dict[str, Any]) -> bool:
    codes = {str(code).upper() for code in requirement.get("course_codes", [])}
    if codes and course.code.upper() in codes:
        return True
    categories = {_normal_category(cat) for cat in requirement.get("categories", [])}
    if not codes and not categories:
        return True  # total-credit rule
    category = _normal_category(course.category)
    aliases = {
        "me": "major_elective", "major": "major_core", "ccc_uwe": "ccc_uwe",
        "uwe_ccc": "ccc_uwe", "project_thesis_internship": "project",
    }
    category = aliases.get(category, category)
    if category in categories:
        return True
    return "ccc_uwe" in categories and category in {"ccc", "uwe"}


def _course_credits(courses: list[AuditCourse], requirement: dict[str, Any]) -> float:
    return round(sum(course.credits for course in courses if _matches(course, requirement)), 2)


def _as_requirement(rule: AuditRequirementOverride) -> dict[str, Any]:
    return {
        "id": rule.id, "label": rule.label, "kind": rule.kind,
        "required": 1 if rule.kind == "milestone" else rule.required, "categories": rule.categories,
        "course_codes": rule.course_codes, "note": rule.note,
        "source": "private profile override",
    }


def audit_degree(request: DegreeAuditRequest, catalog: ProgrammeCatalog) -> dict[str, Any]:
    program = catalog.get(request.programme_id)
    if program is None:
        raise KeyError(request.programme_id)

    requirements = list(program.get("requirements", []))
    if request.custom_requirements:
        custom = {_as_requirement(rule)["id"]: _as_requirement(rule) for rule in request.custom_requirements}
        requirements = [rule for rule in requirements if rule["id"] not in custom] + list(custom.values())

    completed_codes = {course.code.upper() for course in request.completed_courses}
    planned = [course for course in request.planned_courses if course.code.upper() not in completed_codes]
    milestones = {value.strip().lower() for value in request.completed_milestones}
    rows = []
    for rule in requirements:
        required = float(rule.get("required", 0))
        if rule.get("kind", "credits") == "milestone":
            done = 1.0 if rule["id"].lower() in milestones else 0.0
            projected = done
        else:
            detailed_done = _course_credits(request.completed_courses, rule)
            # The aggregate is a lower bound, not an amount to add: detailed rows
            # may be a subset of the same accepted record.
            done = max(detailed_done, float(request.completed_requirement_credits.get(rule["id"], 0)))
            projected = done + _course_credits(planned, rule)
        remaining = max(0.0, required - done)
        remaining_after = max(0.0, required - projected)
        rows.append({
            "id": rule["id"], "label": rule["label"], "kind": rule.get("kind", "credits"),
            "required": required, "completed": round(done, 2), "planned": round(projected - done, 2),
            "remaining": round(remaining, 2), "remaining_after_plan": round(remaining_after, 2),
            "status": "complete" if remaining == 0 else ("on_track" if remaining_after == 0 else "remaining"),
            "note": rule.get("note"), "source": rule.get("source"),
        })

    credit_rows = [row for row in rows if row["kind"] == "credits"]
    primary = next((row for row in credit_rows if row["id"] == "total"), None)
    if primary:
        remaining_credits = primary["remaining"]
        remaining_after = primary["remaining_after_plan"]
    else:
        # Do not sum overlapping minima. The largest unmet predicate is a safe lower bound.
        remaining_credits = max((row["remaining"] for row in credit_rows), default=0)
        remaining_after = max((row["remaining_after_plan"] for row in credit_rows), default=0)

    coverage = program.get("verification", "partial")
    if request.custom_requirements:
        coverage = "profile_overrides_applied"
    aggregate_applied = bool(request.completed_requirement_credits)
    return {
        "programme": {key: program.get(key) for key in ("id", "title", "level", "verification")},
        "cohort_year": request.cohort_year,
        "coverage": coverage,
        "coverage_note": program.get("coverage_note"),
        "courses_recorded": len(request.completed_courses),
        "courses_planned": len(planned),
        "requirements_met": sum(row["status"] == "complete" for row in rows),
        "requirements_total": len(rows),
        "remaining_credits": remaining_credits,
        "remaining_after_plan": remaining_after,
        "estimated_courses_left": math.ceil(remaining_credits / 3) if remaining_credits else 0,
        "estimated_courses_left_after_plan": math.ceil(remaining_after / 3) if remaining_after else 0,
        "estimate_note": "Course count is a 3-credit planning estimate; the requirement rows are authoritative.",
        "requirements": rows,
        "sources": program.get("sources", []),
        "warnings": ((["This public source does not expose a complete fixed curriculum; add cohort-specific requirements in the private profile JSON."]
                     if not requirements else []) +
                     (["Some completed progress comes from private aggregate/transfer totals; the named-course count only covers detailed records."]
                      if aggregate_applied else [])),
        "aggregate_progress_applied": aggregate_applied,
    }
