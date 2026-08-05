"""Explicit API contracts. No loosely-shaped dicts cross the boundary."""
from __future__ import annotations
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field, field_validator, ConfigDict

MAX_COURSES = 120
MAX_TRIALS = 100_000
MAX_SCENARIOS = 4


class Category(str, Enum):
    ME = "ME"; UWE = "UWE"; CCC = "CCC"


class Priority(str, Enum):
    MUST = "MUST"; STRONG = "STRONG"; BACKUP = "BACKUP"; OPTIONAL = "OPTIONAL"


class BudgetMode(str, Enum):
    SHARED_LIVE = "SHARED_LIVE"; INDEPENDENT = "INDEPENDENT"


class CompetitionMode(str, Enum):
    HIGH = "HIGH"; VERY_HIGH = "VERY_HIGH"; EXTREME = "EXTREME"; OPTIMISTIC = "OPTIMISTIC"


class RobustMethod(str, Enum):
    MINIMAX = "minimax"; CVAR = "cvar"; MEAN = "mean"


class JobState(str, Enum):
    QUEUED = "queued"; STARTING = "starting"; RUNNING = "running"
    CANCELLING = "cancelling"; CANCELLED = "cancelled"
    COMPLETED = "completed"; FAILED = "failed"; EXPIRED = "expired"


class PoolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_year: Literal["y2", "y3", "y4"] = "y4"
    semester: int = Field(7, ge=1, le=8)
    rem_me: float = Field(..., ge=0, le=200)
    rem_uwe: float = Field(..., ge=0, le=200)
    rem_ccc: float = Field(..., ge=0, le=200)
    floater: float = Field(0, ge=0, le=100)
    done_me: float = Field(0, ge=0, le=200)
    done_uwe: float = Field(0, ge=0, le=200)
    done_ccc: float = Field(0, ge=0, le=200)


class PoolResponse(BaseModel):
    ME: int; UWE: int; CCC: int
    detail: dict[str, str]
    rule_id: str
    rule_version: str


class CourseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., min_length=1, max_length=40)
    title: str = Field("", max_length=200)
    category: Category
    credits: float = Field(..., gt=0, le=12)
    seats: int = Field(..., ge=0, le=5000)
    priority: Priority = Priority.STRONG
    section_count: int = Field(1, ge=1, le=300)
    open_as_uwe: bool = False
    convenient_slot: bool = False
    in_specialisation: bool = False
    user_popularity: float = Field(1.0, gt=0, le=5)
    live_bidders: int | None = Field(None, ge=0, le=20000)
    live_round: str | None = Field(None, max_length=40)
    live_observed_at: str | None = Field(None, max_length=40)


class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    courses: list[CourseInput] = Field(..., min_length=1, max_length=MAX_COURSES)
    pools: dict[str, int]
    trials: int = Field(8000, ge=100, le=MAX_TRIALS)
    seed: int = Field(20260802, ge=0, le=2**31 - 1)
    headline_mode: CompetitionMode = CompetitionMode.HIGH
    budget_mode: BudgetMode = BudgetMode.SHARED_LIVE
    robust_method: RobustMethod = RobustMethod.MINIMAX
    dispersion: float = Field(0.18, ge=0, le=0.6)
    include_optimistic: bool = False

    @field_validator("courses")
    @classmethod
    def unique_codes(cls, v):
        codes = [c.code for c in v]
        if len(set(codes)) != len(codes):
            raise ValueError("duplicate course codes are not allowed")
        return v

    @field_validator("pools")
    @classmethod
    def pools_sane(cls, v):
        for k in ("ME", "UWE", "CCC"):
            if k not in v:
                raise ValueError(f"pools must include {k}")
            if not (0 <= v[k] <= 100_000):
                raise ValueError(f"pool {k} out of range")
        return v


class DemandProvenance(BaseModel):
    source: Literal["live", "stress-default"]
    expected_rivals: float
    seats: int
    note: str
    factors: list[dict] = []


class ScenarioResult(BaseModel):
    mode: str
    label: str
    expected_rivals: float
    win_at_bid: float
    win_one_below: float
    win_at_cap: float
    comparison_only: bool = False


class CourseRecommendation(BaseModel):
    code: str
    category: Category
    priority: Priority
    cap: int
    bid: int
    bid_range: tuple[int, int]
    target_met: bool
    reduced_for_budget: bool
    shortfall: list[dict]
    worst_tested: float
    expected_charge: float
    expected_refund: float
    ci_halfwidth: float
    scenarios: list[ScenarioResult]
    demand: DemandProvenance
    price_to_beat_quantiles: dict


class CategoryAllocation(BaseModel):
    category: Category
    pool: int
    committed: int
    uncommitted: int | None
    feasible: bool
    sacrificed: list[dict]
    note: str


class SimulationResult(BaseModel):
    recommendations: list[CourseRecommendation]
    allocations: list[CategoryAllocation]
    budget_mode: BudgetMode
    headline_mode: CompetitionMode
    robust_method: RobustMethod
    trials: int
    seed: int
    scenarios_run: list[str]
    rule_version: str
    dataset_version: str
    model_version: str
    input_hash: str
    cache_hit: bool = False
    runtime_ms: float
    disclaimer: str = (
        "Conservative model outputs, not guarantees. No historical SNU clearing-price or "
        "bidder-count data exists, so competition is assumed rather than observed. Enter live "
        "bidder counts as soon as the platform shows them."
    )


class JobProgress(BaseModel):
    courses_done: int
    courses_total: int
    scenarios_done: int
    scenarios_total: int
    trials_done: int
    trials_total: int
    percent: float
    phase: str


class JobStatus(BaseModel):
    job_id: str
    state: JobState
    created_at: float
    updated_at: float
    input_hash: str
    progress: JobProgress
    error: str | None = None
    error_category: str | None = None
    rule_version: str
    model_version: str
    dataset_version: str
    seed: int
    cache_hit: bool = False
    expires_at: float | None = None


class SettlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seats: int = Field(..., ge=0, le=5000)
    cap: float | None = Field(None, ge=0, le=100000)
    seed: str = "default"
    bids: list[dict] = Field(..., max_length=20000)


class ValidationErrorOut(BaseModel):
    detail: str
    field: str | None = None
    limit: str | None = None
