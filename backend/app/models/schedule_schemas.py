"""Typed request/response models for the schedule-search endpoints. Kept in its
own module rather than growing schemas.py, since schedule search is a distinct
capability (see app/services/scheduler.py, app/workers/schedule_jobs.py)."""
from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.profile_schemas import ChoiceGroup, WishlistItem

MAX_SHORTLIST = 60
MAX_NODES = 8_000_000
MAX_RESULTS = 5_000

SortKey = Literal["compact", "early", "late", "days"]


class FixedCourse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    pkg: int = Field(ge=0)
    locked: bool = True  # False = swappable: searched over like a shortlist course,
                         # `pkg` is only its current/default choice, not a hard constraint.
                         # True = truly static, held out of the search entirely.


class ExternalFixedItem(BaseModel):
    """A mandatory credit-bearing item without timetable meetings (project/SWAYAM)."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=160)
    credits: float = Field(..., gt=0, le=24)


class ScheduleSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shortlist: list[str] = Field(default_factory=list, max_length=MAX_SHORTLIST)
    fixed: list[FixedCourse] = []
    external_fixed: list[ExternalFixedItem] = Field(default_factory=list, max_length=30)
    max_nodes: int = Field(2_000_000, ge=1_000, le=MAX_NODES)
    max_results: int = Field(300, ge=1, le=MAX_RESULTS)
    sort: SortKey = "compact"
    allow_least_conflict: bool = True  # fall back to a ranked best-effort combination
                                       # (see search_with_fallback) when no clash-free one exists

    # ---- wishlist mode (scheduler v2): activates when `wishlist` is non-empty.
    # Mutually additive with `shortlist`/`fixed`, not a replacement - a caller
    # can still combine a plain shortlist with a wishlist, though the common
    # case is wishlist-only. See app/services/cp_scheduler.py.
    wishlist: list[WishlistItem] = Field(default_factory=list, max_length=MAX_SHORTLIST)
    choice_groups: list[ChoiceGroup] = Field(default_factory=list, max_length=30)
    credit_min: float | None = Field(None, ge=0)
    credit_target: float | None = Field(None, ge=0)
    credit_max: float | None = Field(None, gt=0)

    @field_validator("shortlist")
    @classmethod
    def unique_codes(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("duplicate course codes in shortlist are not allowed")
        return v

    @model_validator(mode="after")
    def shortlist_or_wishlist_required(self):
        if not self.shortlist and not self.wishlist:
            raise ValueError("at least one of shortlist or wishlist is required")
        if self.wishlist and self.credit_max is None:
            raise ValueError("credit_max is required when wishlist is non-empty")
        if self.credit_min is not None and self.credit_max is not None and self.credit_min > self.credit_max:
            raise ValueError("credit_min cannot exceed credit_max")
        return self


class ScheduleJobProgress(BaseModel):
    nodes_done: int
    nodes_total: int
    percent: float
    phase: str


class ScheduleJobStatus(BaseModel):
    job_id: str
    state: str
    created_at: float
    updated_at: float
    input_hash: str
    progress: ScheduleJobProgress
    error: str | None
    rule_version: str
    model_version: str
    dataset_version: str
    cache_hit: bool
    expires_at: float
