"""Request models for the undergraduate minor-programme checker."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.audit_schemas import AuditCourse


class MinorAuditRequest(BaseModel):
    """Progress against one minor. `pathway_id` is explicit where a minor
    defines more than one requirement basket (e.g. the CSE minor's Pathway A
    / Pathway B) - the service auto-selects a pathway from `major_programme_id`
    only when the minor's data unambiguously maps one major to one pathway,
    and otherwise asks rather than guessing."""

    model_config = ConfigDict(extra="forbid")
    minor_id: str = Field(..., min_length=1, max_length=120)
    pathway_id: str | None = Field(None, max_length=80)
    major_programme_id: str | None = Field(None, max_length=120)
    completed_courses: list[AuditCourse] = Field(default_factory=list, max_length=1000)
    planned_courses: list[AuditCourse] = Field(default_factory=list, max_length=200)


class MinorOverviewRequest(BaseModel):
    """Sweep every catalogued minor for one student: which are open to their
    major, and - for the ones that are - a quick progress snapshot."""

    model_config = ConfigDict(extra="forbid")
    major_programme_id: str | None = Field(None, max_length=120)
    completed_courses: list[AuditCourse] = Field(default_factory=list, max_length=1000)
    planned_courses: list[AuditCourse] = Field(default_factory=list, max_length=200)
