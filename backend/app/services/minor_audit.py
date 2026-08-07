"""Undergraduate minor-programme eligibility and progress auditing.

A minor's requirements are structurally identical to a programme's: named
rows, each a credit threshold, a distinct-course-count threshold, or an
explicitly non-computable "confirm this yourself" row, matched against a
student's completed/planned courses by course code. This module reuses
`degree_audit.compute_requirement_rows` directly rather than re-implementing
that matching logic, so a minor gets exactly the same tested semantics a
programme audit already has.

What a minor needs on top of that, which a programme audit does not:

  * ELIGIBILITY. A programme is something you already are; a minor is
    something you may or may not be allowed to take. Several minors are
    explicitly closed to students already majoring in that same department -
    this is checked against the minor's own `restricted_major_programme_ids`,
    sourced from the circular's own "Open to" line, never inferred.
  * PATHWAY SELECTION. Several minors define more than one requirement basket
    (the CSE minor's Pathway A/B, the Civil/ECE/Physics minors' major-specific
    baskets). Where the minor's own data maps one major unambiguously to one
    pathway, it is auto-selected; otherwise the caller is asked to choose,
    never guessed.
  * NON-VERIFIABLE CONDITIONS. GPA minimums, specific grades, interviews and
    committee decisions are disclosed as `eligibility_conditions` exactly as
    published, and never silently treated as satisfied.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models.audit_schemas import AuditCourse
from app.models.minor_schemas import MinorAuditRequest, MinorOverviewRequest
from app.services.degree_audit import compute_requirement_rows

MINORS_FILE = Path(__file__).resolve().parents[1] / "data" / "minors.json"


class MinorCatalog:
    def __init__(self, path: Path = MINORS_FILE):
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.meta = {key: value for key, value in payload.items() if key != "minors"}
        self.minors = payload["minors"]
        self.by_id = {minor["id"]: minor for minor in self.minors}

    def list(self) -> list[dict[str, Any]]:
        return self.minors

    def get(self, minor_id: str) -> dict[str, Any] | None:
        return self.by_id.get(minor_id)


def _resolve_pathway(minor: dict[str, Any], pathway_id: str | None,
                     major_programme_id: str | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Pick the one pathway to audit, or return None plus the choices on offer.

    Auto-selection only fires when exactly one pathway names the given major
    in its `applies_to_major_programme_ids` - if two pathways both claim it
    (a data error) or none do, the caller must choose explicitly rather than
    have this silently guess.
    """
    pathways = minor.get("pathways", [])
    if pathway_id:
        match = next((p for p in pathways if p["id"] == pathway_id), None)
        return match, pathways
    if len(pathways) == 1:
        return pathways[0], pathways
    if major_programme_id:
        matches = [p for p in pathways
                  if p.get("applies_to_major_programme_ids") and major_programme_id in p["applies_to_major_programme_ids"]]
        if len(matches) == 1:
            return matches[0], pathways
    return None, pathways


def _eligibility(minor: dict[str, Any], major_programme_id: str | None) -> dict[str, Any]:
    restricted = set(minor.get("restricted_major_programme_ids", []))
    if major_programme_id and major_programme_id in restricted:
        return {
            "eligible": False,
            "reason": f"This minor is closed to students already majoring in {minor['department']} "
                      f"(published as: \"{minor['open_to_note']}\").",
        }
    conditions = minor.get("eligibility_conditions", [])
    return {
        "eligible": True,
        "reason": "Open, subject to the conditions below." if conditions else "Open to your major.",
        "needs_confirmation": bool(conditions),
    }


def audit_minor(request: MinorAuditRequest, catalog: MinorCatalog) -> dict[str, Any]:
    minor = catalog.get(request.minor_id)
    if minor is None:
        raise KeyError(request.minor_id)

    eligibility = _eligibility(minor, request.major_programme_id)
    pathway, pathways = _resolve_pathway(minor, request.pathway_id, request.major_programme_id)

    base = {
        "minor": {"id": minor["id"], "title": minor["title"], "department": minor["department"], "school": minor["school"]},
        "open_to_note": minor["open_to_note"],
        "eligibility": eligibility,
        "eligibility_conditions": minor.get("eligibility_conditions", []),
        "does_not_count_notes": minor.get("does_not_count_notes", []),
        "sources": minor.get("sources", []),
    }

    if pathway is None:
        return {
            **base,
            "pathway_required": True,
            "pathway_options": [{"id": p["id"], "label": p["label"], "applies_to_note": p.get("applies_to_note")}
                                for p in pathways],
            "requirements": [], "requirements_met": 0, "requirements_total": 0,
        }

    rows = compute_requirement_rows(pathway["requirements"], request.completed_courses,
                                    request.planned_courses, [], {})
    computable_rows = [row for row in rows if row["status"] != "needs_confirmation"]
    return {
        **base,
        "pathway_required": False,
        "pathway": {"id": pathway["id"], "label": pathway["label"], "applies_to_note": pathway.get("applies_to_note")},
        "pathway_options": [{"id": p["id"], "label": p["label"], "applies_to_note": p.get("applies_to_note")}
                            for p in pathways],
        "total_credits_published": pathway.get("total_credits"),
        "requirements": rows,
        "requirements_met": sum(row["status"] == "complete" for row in computable_rows),
        "requirements_total": len(computable_rows),
        "requirements_needing_confirmation": len(rows) - len(computable_rows),
    }


def minors_overview(request: MinorOverviewRequest, catalog: MinorCatalog) -> dict[str, Any]:
    """Sweep every catalogued minor: eligible / not eligible / needs a pathway
    choice, with a progress snapshot for whichever pathway auto-resolves."""
    rows = []
    for minor in catalog.list():
        eligibility = _eligibility(minor, request.major_programme_id)
        entry = {
            "id": minor["id"], "title": minor["title"], "department": minor["department"], "school": minor["school"],
            "open_to_note": minor["open_to_note"], "eligibility": eligibility,
        }
        if not eligibility["eligible"]:
            rows.append(entry)
            continue
        pathway, pathways = _resolve_pathway(minor, None, request.major_programme_id)
        if pathway is None:
            entry["pathway_required"] = True
            entry["pathway_options"] = [{"id": p["id"], "label": p["label"]} for p in pathways]
            rows.append(entry)
            continue
        pathway_rows = compute_requirement_rows(pathway["requirements"], request.completed_courses,
                                                request.planned_courses, [], {})
        computable = [row for row in pathway_rows if row["status"] != "needs_confirmation"]
        entry["pathway_required"] = False
        entry["pathway"] = {"id": pathway["id"], "label": pathway["label"]}
        entry["requirements_met"] = sum(row["status"] == "complete" for row in computable)
        entry["requirements_total"] = len(computable)
        entry["fully_complete"] = bool(computable) and entry["requirements_met"] == entry["requirements_total"]
        rows.append(entry)
    return {
        "major_programme_id": request.major_programme_id,
        "minors": rows,
        "eligible_count": sum(row["eligibility"]["eligible"] for row in rows),
        "total_count": len(rows),
    }
