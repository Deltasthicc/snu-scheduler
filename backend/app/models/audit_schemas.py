"""Strict request models for the source-backed degree audit service."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuditCourse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., min_length=1, max_length=60)
    title: str | None = Field(None, max_length=240)
    credits: float = Field(..., gt=0, le=24)
    category: str = Field(..., min_length=1, max_length=80)

    @field_validator("code", "category")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()


class AuditRequirementOverride(BaseModel):
    """Optional private-profile rule for cohorts whose authoritative COS differs."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., min_length=1, max_length=80)
    label: str = Field(..., min_length=1, max_length=240)
    kind: Literal["credits", "milestone"] = "credits"
    required: float = Field(0, ge=0)
    categories: list[str] = Field(default_factory=list, max_length=30)
    course_codes: list[str] = Field(default_factory=list, max_length=500)
    note: str | None = Field(None, max_length=1000)


class DegreeAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    programme_id: str = Field(..., min_length=1, max_length=120)
    cohort_year: int | None = Field(None, ge=2000, le=2100)
    completed_courses: list[AuditCourse] = Field(default_factory=list, max_length=1000)
    planned_courses: list[AuditCourse] = Field(default_factory=list, max_length=200)
    completed_milestones: list[str] = Field(default_factory=list, max_length=100)
    custom_requirements: list[AuditRequirementOverride] = Field(default_factory=list, max_length=100)
    # Private aggregate progress covers accepted transfer/legacy credit for which
    # the student has no SNU course-code rows. Values are lower bounds per rule,
    # never extra credits to add to the detailed courses.
    completed_requirement_credits: dict[str, float] = Field(default_factory=dict)

    @field_validator("completed_requirement_credits")
    @classmethod
    def valid_requirement_credits(cls, values: dict[str, float]) -> dict[str, float]:
        if len(values) > 100:
            raise ValueError("too many aggregate requirement entries")
        for key, value in values.items():
            if not key.strip() or len(key) > 80 or value < 0 or value > 1000:
                raise ValueError("invalid aggregate requirement credit")
        return values

    @field_validator("completed_courses", "planned_courses")
    @classmethod
    def unique_course_codes(cls, courses: list[AuditCourse]) -> list[AuditCourse]:
        codes = [course.code.upper() for course in courses]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate course code")
        return courses
