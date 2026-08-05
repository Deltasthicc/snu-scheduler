"""Typed request/response models for the wishlist-driven scheduler phase:
profile/credit-policy validation and wishlist validation. Kept separate from
schedule_schemas.py because these describe *intent* (what the student wants
and why), while schedule_schemas.py describes the search job itself - see
docs/research/scheduler_v2_matrix.md for how this maps to the two research
reports' recommendations.

Every model is strict (extra="forbid"): CLAUDE.md's anti-pattern list is
explicit that FairnessAuditPayload-style silent-drop permissiveness is worse
than a loud 422, and the same principle applies here.
"""
from __future__ import annotations
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_WISHLIST = 60
MAX_CHOICE_GROUPS = 30


class WishlistIntent(str, Enum):
    MUST_HAVE = "must_have"
    STRONG = "strong"
    OPTIONAL = "optional"
    BACKUP = "backup"


class ChoiceGroupKind(str, Enum):
    EXACTLY_ONE = "exactly_one"
    AT_LEAST_ONE = "at_least_one"
    AT_MOST_ONE = "at_most_one"
    MIN_CREDITS = "min_credits"


class ChoiceGroup(BaseModel):
    """One of the "more expressive than a flat list" rules from both research
    reports: exactly-one, at-least-one, at-most-one, or a minimum-credit
    threshold across a named subset of the wishlist. `members` are course
    codes; every member must also appear in the wishlist itself (checked in
    app/services/wishlist.py, not here, since that needs the wishlist list)."""
    model_config = ConfigDict(extra="forbid")
    kind: ChoiceGroupKind
    members: list[str] = Field(..., min_length=2, max_length=MAX_WISHLIST)
    min_credits: float | None = Field(None, gt=0)
    label: str | None = Field(None, max_length=160)

    @field_validator("members")
    @classmethod
    def unique_members(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("duplicate course codes in a choice group")
        return v

    @model_validator(mode="after")
    def min_credits_required_for_its_kind(self):
        if self.kind == ChoiceGroupKind.MIN_CREDITS and self.min_credits is None:
            raise ValueError("min_credits is required when kind is 'min_credits'")
        if self.kind != ChoiceGroupKind.MIN_CREDITS and self.min_credits is not None:
            raise ValueError("min_credits only applies to kind 'min_credits'")
        return self


class WishlistItem(BaseModel):
    """A course the student is considering, not yet committed to (spec s.4).
    Priority/package-preference/notes are deliberately separate fields from
    intent - collapsing them into one 'want level' was the old PICK model's
    limitation (see frontend/src/ui/c_core.html's PICK.want)."""
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., min_length=1, max_length=40)
    intent: WishlistIntent = WishlistIntent.STRONG
    priority: int = Field(5, ge=1, le=10)
    locked_package: int | None = Field(None, ge=0)
    excluded_packages: list[int] = Field(default_factory=list)
    preferred_instructor: str | None = Field(None, max_length=120)
    avoided_instructor: str | None = Field(None, max_length=120)
    notes: str | None = Field(None, max_length=500)

    @field_validator("excluded_packages")
    @classmethod
    def unique_excluded(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("duplicate package indices in excluded_packages")
        return v

    @model_validator(mode="after")
    def locked_not_excluded(self):
        if self.locked_package is not None and self.locked_package in self.excluded_packages:
            raise ValueError(f"{self.code}: locked_package cannot also be in excluded_packages")
        return self


class WishlistValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[WishlistItem] = Field(..., min_length=1, max_length=MAX_WISHLIST)
    choice_groups: list[ChoiceGroup] = Field(default_factory=list, max_length=MAX_CHOICE_GROUPS)
    fixed_credits: float = Field(0, ge=0)

    @field_validator("items")
    @classmethod
    def unique_codes(cls, v):
        codes = [i.code for i in v]
        if len(set(codes)) != len(codes):
            raise ValueError("duplicate course codes in wishlist")
        return v


class CreditPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixed_credits: float = Field(..., ge=0)
    personal_target: float = Field(..., ge=0)
    min_credits: float = Field(0, ge=0)
    overload_ceiling: float | None = Field(None, gt=0)
    overload_mode: Literal["what_if", "approved_overload"] = "what_if"
    overload_confirmed: bool = False
    current_year: Literal[1, 2, 3, 4] | None = None
    eligibility_confirmed: bool = False
    advisor_recommended: bool = False
    dean_approved: bool = False


class ProfileValidateRequest(BaseModel):
    """First-use / profile-summary endpoint input. Intentionally accepts
    'unknown' for the fields that must never be silently forced to zero (spec
    s.2): a null remaining-credit field means "unknown", not "0 remaining"."""
    model_config = ConfigDict(extra="forbid")
    programme: str | None = None
    cohort_year: int | None = Field(None, ge=2000, le=2100)
    current_year: Literal[1, 2, 3, 4] | None = None
    current_semester: int | None = Field(None, ge=1, le=8)
    credit_policy: CreditPolicyRequest
    completed_courses: list[str] = Field(default_factory=list, max_length=500)
    remaining_me_credits: float | None = Field(None, ge=0)
    remaining_uwe_credits: float | None = Field(None, ge=0)
    remaining_ccc_credits: float | None = Field(None, ge=0)
    floater_credits: float | None = Field(None, ge=0)
