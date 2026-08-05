"""Typed models and the explicit update state machine for the timetable
update service. See app/timetable_updates/__init__.py for the module map and
docs/TIMETABLE_UPDATE_SERVICE.md for the full design writeup.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UpdateState(str, Enum):
    IDLE = "idle"
    CHECKING = "checking"
    NOT_MODIFIED = "not_modified"
    SOURCE_CHANGED_ONLY = "source_changed_only"
    NORMALIZING = "normalizing"
    VALIDATING = "validating"
    NO_DATASET_CHANGE = "no_dataset_change"
    UPDATE_AVAILABLE = "update_available"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    OFFLINE = "offline"
    ROLLBACK_AVAILABLE = "rollback_available"


@dataclass(frozen=True)
class FetchResult:
    """Raw HTTP fetch outcome - the source-document layer. `not_modified`
    means the server (or our own ETag comparison) confirmed nothing changed
    without us needing to re-parse anything."""
    not_modified: bool
    html: str | None
    status_code: int | None
    etag: str | None
    last_modified: str | None
    source_hash: str | None  # sha256[:16] of the raw HTML; None if not_modified
    byte_length: int | None
    retrieved_at: str
    error: str | None = None


@dataclass(frozen=True)
class ExtractResult:
    """The isolated `const DATA = {...}` literal - the extracted-source-data
    layer, distinct from both the raw HTML and the normalized dataset."""
    raw_literal: str
    extracted_hash: str  # sha256[:16] of raw_literal
    parsed: dict


@dataclass
class NormalizeResult:
    courses: list[dict]
    provenance: dict
    normalized_hash: str  # sha256[:16] of the deterministic serialized courses list
    stats: "ImportStats"


@dataclass
class ImportIssue:
    level: Literal["error", "warning"]
    code: str
    message: str
    course: str | None = None


@dataclass
class ImportStats:
    raw_rows: int = 0
    distinct_courses: int = 0
    packages_built: int = 0
    matched_existing: int = 0
    unmatched_new: int = 0
    issues: list[ImportIssue] = field(default_factory=list)

    def error(self, code: str, message: str, course: str | None = None) -> None:
        self.issues.append(ImportIssue("error", code, message, course))

    def warn(self, code: str, message: str, course: str | None = None) -> None:
        self.issues.append(ImportIssue("warning", code, message, course))

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.level == "warning")


# ---------------- API request/response models ----------------

class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False  # bypass ETag/conditional-request short-circuit for diagnostics


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_version: str
    candidate_checksum: str  # must match the currently-staged candidate exactly,
                             # or the apply is rejected (spec: "reject application
                             # if the candidate changed between review and apply")


class DiscardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_version: str


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_version: str


class UpdateStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poller_enabled: bool
    state: UpdateState
    active_version: str
    active_checksum: str
    last_check_started: float | None
    last_check_completed: float | None
    last_successful_contact: float | None
    next_scheduled_check: float | None
    update_available: bool
    candidate_version: str | None
    candidate_checksum: str | None
    change_counts: dict | None
    validation_errors: int
    validation_warnings: int
    error_detail: str | None
    backoff_seconds: float
    auto_apply: bool
    poll_interval_minutes: float
    source_url: str


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: float
    kind: Literal["check", "apply", "discard", "rollback"]
    state: UpdateState
    version_id: str | None = None
    detail: str | None = None
