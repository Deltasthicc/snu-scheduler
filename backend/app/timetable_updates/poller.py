"""Background timetable update service: owns the state machine, the poll
loop, locking, backoff, and the check/apply/discard/rollback lifecycle. One
instance per backend process, started once from app.main's lifespan.
"""
from __future__ import annotations
import asyncio
import os
import random
import time
from dataclasses import dataclass, field

from app.domain import catalog
from app.timetable_updates import apply as apply_mod
from app.timetable_updates import normalize as normalize_mod
from app.timetable_updates import parser as parser_mod
from app.timetable_updates import source as source_mod
from app.timetable_updates.diff import diff_datasets
from app.timetable_updates.models import UpdateState

MIN_INTERVAL_MINUTES = 5.0
MAX_BACKOFF_SECONDS = 3600.0
HISTORY_LIMIT = 100


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v) if v is not None else default
    except ValueError:
        return default


@dataclass
class Candidate:
    version_id: str
    dataset_checksum: str
    source_hash: str
    extracted_hash: str
    diff: dict
    manifest_entry: dict
    error_count: int
    warning_count: int
    staged_at: float


@dataclass
class UpdateService:
    url: str = field(default_factory=lambda: os.environ.get(
        "SNU_TIMETABLE_UPDATE_URL", "https://snioe-monsoon2026-tt.netlify.app/"))
    enabled: bool = field(default_factory=lambda: _env_bool("SNU_TIMETABLE_UPDATE_ENABLED", True))
    poll_interval_minutes: float = field(default_factory=lambda: max(
        MIN_INTERVAL_MINUTES, _env_float("SNU_TIMETABLE_UPDATE_INTERVAL_MINUTES", 15.0)))
    # Default flipped to True 2026-08-09/10: the poller had been correctly
    # detecting real site changes for days (checksums differed on 08-04, 05,
    # 06, 07, 09) but manual review never happened, so the active dataset
    # silently sat 5+ days behind the live timetable. A clean (zero-error)
    # candidate now applies itself; a candidate with any validation error
    # still always waits for manual review regardless of this flag - see the
    # error_count == 0 gate in check() below. SNU_TIMETABLE_AUTO_APPLY=false
    # opts back out.
    auto_apply: bool = field(default_factory=lambda: _env_bool("SNU_TIMETABLE_AUTO_APPLY", True))

    state: UpdateState = UpdateState.IDLE
    last_check_started: float | None = None
    last_check_completed: float | None = None
    last_successful_contact: float | None = None
    next_scheduled_check: float | None = None
    known_etag: str | None = None
    known_source_hash: str | None = None
    candidate: Candidate | None = None
    error_detail: str | None = None
    backoff_seconds: float = 0.0
    history: list = field(default_factory=list)

    def __post_init__(self):
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        # small initial jitter so multiple installations started at the same
        # moment (e.g. a fleet of desktop installs) don't all hit the source
        # site in the same instant
        await asyncio.sleep(random.uniform(1, 5))
        while not self._stop.is_set():
            try:
                await self.check(force=False, is_scheduled=True)
            except Exception as e:  # noqa: BLE001 - the poll loop must never die
                self._record_history("check", UpdateState.FAILED, detail=f"unexpected poller error: {e}")
            sleep_s = self.poll_interval_minutes * 60 + self.backoff_seconds
            sleep_s += random.uniform(0, min(30, sleep_s * 0.05))  # jitter
            self.next_scheduled_check = time.time() + sleep_s
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass

    # ---------------- history ----------------
    def _record_history(self, kind: str, state: UpdateState, version_id: str | None = None,
                        detail: str | None = None) -> None:
        self.history.append({"at": time.time(), "kind": kind, "state": state.value,
                             "version_id": version_id, "detail": detail})
        if len(self.history) > HISTORY_LIMIT:
            self.history = self.history[-HISTORY_LIMIT:]

    # ---------------- check ----------------
    async def check(self, force: bool = False, is_scheduled: bool = False) -> dict:
        if self._lock.locked():
            return {"skipped": True, "reason": "a check is already in progress"}
        async with self._lock:
            self.state = UpdateState.CHECKING
            self.last_check_started = time.time()
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(None, self._check_blocking, force)
            except Exception as e:  # noqa: BLE001
                self.state = UpdateState.FAILED
                self.error_detail = str(e)
                self.last_check_completed = time.time()
                self._apply_backoff()
                self._record_history("check", self.state, detail=self.error_detail)
                return {"state": self.state.value, "error": self.error_detail}
            self.last_check_completed = time.time()
            self._record_history("check", self.state,
                                 version_id=self.candidate.version_id if self.candidate else None)
            if self.auto_apply and self.state == UpdateState.UPDATE_AVAILABLE and self.candidate:
                if self.candidate.error_count == 0:
                    try:
                        await self.apply(self.candidate.version_id, self.candidate.dataset_checksum)
                    except apply_mod.ApplyError as e:
                        self.state = UpdateState.FAILED
                        self.error_detail = f"auto-apply failed: {e}"
                        self._record_history("apply", self.state, detail=self.error_detail)
            return result

    def _check_blocking(self, force: bool) -> dict:
        """Runs off the event loop: network I/O + CPU-bound normalization."""
        fetch = source_mod.fetch(self.url, known_etag=self.known_etag,
                                 known_source_hash=self.known_source_hash, force=force)
        if fetch.error:
            self.state = UpdateState.OFFLINE
            self.error_detail = fetch.error
            self._apply_backoff()
            return {"state": self.state.value, "message": "The timetable source is temporarily "
                   "unavailable. Your existing data remains safe.", "error": fetch.error}

        self.last_successful_contact = time.time()
        self.backoff_seconds = 0.0  # reset on any successful contact, per spec

        if fetch.not_modified:
            self.state = UpdateState.NOT_MODIFIED
            self.error_detail = None
            return {"state": self.state.value, "message": "No website change detected."}

        self.known_etag = fetch.etag
        new_source_hash = fetch.source_hash
        source_changed = new_source_hash != self.known_source_hash
        self.known_source_hash = new_source_hash

        self.state = UpdateState.NORMALIZING
        try:
            extracted = parser_mod.parse(fetch.html)
        except parser_mod.ParseError as e:
            self.state = UpdateState.FAILED
            self.error_detail = f"the timetable source structure appears to have changed: {e}"
            return {"state": self.state.value, "message": "The published data could not be validated, "
                   "so it was not applied.", "error": self.error_detail}

        prev_extracted_hash = getattr(self, "_last_extracted_hash", None)
        if prev_extracted_hash and extracted.extracted_hash == prev_extracted_hash:
            self.state = UpdateState.SOURCE_CHANGED_ONLY if source_changed else UpdateState.NOT_MODIFIED
            self.error_detail = None
            return {"state": self.state.value,
                   "message": "The website changed, but its timetable data did not change."}
        self._last_extracted_hash = extracted.extracted_hash

        existing_by_code = {c["code"]: c for c in catalog.all_courses()}
        norm = normalize_mod.normalize(extracted.parsed, existing_by_code)

        active_checksum = catalog.dataset_info()["dataset_checksum"]
        if norm.normalized_hash == active_checksum:
            self.state = UpdateState.NO_DATASET_CHANGE
            self.error_detail = None
            return {"state": self.state.value, "message": "The published data changed in formatting or "
                   "duplicate representation, but the scheduler's normalized timetable is unchanged."}

        self.state = UpdateState.VALIDATING
        diff = diff_datasets(catalog.all_courses(), norm.courses)
        version_id = f"monsoon-2026-netlify-revision-{fetch.retrieved_at[:10]}"
        manifest_entry = {
            "version_id": version_id, "source_name": "SNU Monsoon 2026 Timetable Planner (Netlify)",
            "source_url": self.url, "retrieved_at": fetch.retrieved_at, "source_checksum": new_source_hash,
            "dataset_checksum": norm.normalized_hash, "importer_version": "1.0.0",
            "effective_semester": "Monsoon 2026", "course_count": len(norm.courses),
            "package_count": sum(len(c["pk"]) for c in norm.courses),
            "error_count": norm.stats.error_count, "warning_count": norm.stats.warning_count,
            "validation_status": "clean" if norm.stats.error_count == 0 else "has_errors",
        }
        apply_mod.stage_version(version_id, norm.courses, norm.provenance, manifest_entry)

        self.candidate = Candidate(
            version_id=version_id, dataset_checksum=norm.normalized_hash, source_hash=new_source_hash,
            extracted_hash=extracted.extracted_hash, diff=diff, manifest_entry=manifest_entry,
            error_count=norm.stats.error_count, warning_count=norm.stats.warning_count, staged_at=time.time(),
        )
        if norm.stats.error_count > 0:
            self.state = UpdateState.FAILED
            self.error_detail = f"{norm.stats.error_count} validation error(s) in the candidate dataset"
            return {"state": self.state.value, "message": "The published data could not be validated, "
                   "so it was not applied.", "candidate_version": version_id}

        self.state = UpdateState.UPDATE_AVAILABLE
        self.error_detail = None
        return {"state": self.state.value, "message": "A revised timetable is available.",
               "candidate_version": version_id, "diff_summary": diff["summary"]}

    def _apply_backoff(self) -> None:
        self.backoff_seconds = min(MAX_BACKOFF_SECONDS, max(30.0, self.backoff_seconds * 2 or 30.0))

    # ---------------- apply / discard / rollback ----------------
    async def apply(self, candidate_version: str, candidate_checksum: str) -> dict:
        if not self.candidate or self.candidate.version_id != candidate_version:
            raise apply_mod.ApplyError(f"{candidate_version!r} is not the currently staged candidate")
        self.state = UpdateState.APPLYING
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, apply_mod.apply_version, candidate_version, candidate_checksum)
        except apply_mod.ApplyError as e:
            self.state = UpdateState.FAILED
            self.error_detail = str(e)
            self._record_history("apply", self.state, version_id=candidate_version, detail=str(e))
            raise
        self.state = UpdateState.APPLIED
        self.error_detail = None
        applied_candidate = self.candidate
        self.candidate = None
        self._record_history("apply", self.state, version_id=candidate_version)
        return {"result": result, "diff": applied_candidate.diff}

    def discard(self, candidate_version: str) -> None:
        if self.candidate and self.candidate.version_id == candidate_version:
            self.candidate = None
        apply_mod.discard_candidate(candidate_version)
        self.state = UpdateState.IDLE
        self._record_history("discard", self.state, version_id=candidate_version)

    async def rollback(self, target_version: str) -> dict:
        manifest = apply_mod.load_manifest()
        if not any(v["version_id"] == target_version for v in manifest.get("versions", [])):
            raise apply_mod.ApplyError(f"{target_version!r} has never been applied; nothing to roll back to")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, apply_mod.apply_version, target_version, None)
        self.state = UpdateState.ROLLBACK_AVAILABLE
        self.candidate = None
        self._record_history("rollback", self.state, version_id=target_version)
        return result

    # ---------------- status ----------------
    def status(self) -> dict:
        info = catalog.dataset_info()
        return {
            "poller_enabled": self.enabled, "state": self.state.value,
            "active_version": info["active_version"], "active_checksum": info["dataset_checksum"],
            "last_check_started": self.last_check_started, "last_check_completed": self.last_check_completed,
            "last_successful_contact": self.last_successful_contact,
            "next_scheduled_check": self.next_scheduled_check,
            "update_available": self.state == UpdateState.UPDATE_AVAILABLE and self.candidate is not None,
            "candidate_version": self.candidate.version_id if self.candidate else None,
            "candidate_checksum": self.candidate.dataset_checksum if self.candidate else None,
            "change_counts": self.candidate.diff["summary"] if self.candidate else None,
            "validation_errors": self.candidate.error_count if self.candidate else 0,
            "validation_warnings": self.candidate.warning_count if self.candidate else 0,
            "error_detail": self.error_detail, "backoff_seconds": self.backoff_seconds,
            "auto_apply": self.auto_apply, "poll_interval_minutes": self.poll_interval_minutes,
            "source_url": self.url,
        }
