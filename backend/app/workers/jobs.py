"""Process-isolated job manager with genuine cooperative cancellation.

WHY PROCESSES ON A SINGLE CORE
This machine reports nproc == 1, so a process pool buys no parallel speedup.
It is still the right structure: it keeps CPU-bound simulation off the API event
loop so health checks, plan saves and cancellation stay responsive, and it gives
a hard kill path if a worker misbehaves. On multi-core hosts the same code
parallelises across scenarios with no changes.
"""
from __future__ import annotations
import multiprocessing as mp
import os, queue, threading, time, traceback, uuid
from dataclasses import dataclass, field

from app.domain.rules import RULE_VERSION, DATASET_VERSION, MODEL_VERSION

CTX = mp.get_context("spawn")
JOB_TTL_SECONDS = 3600
MAX_CONCURRENT = 2


def _worker_entry(req, cancel_ev, prog_q, out_q):
    """Runs in a separate process. Imports inside so spawn does not re-import the API."""
    try:
        from app.services.runner import run_plan_both_budget_modes as run_plan
        from app.simulation.engine import Cancelled

        def should_cancel():
            return cancel_ev.is_set()

        def on_progress(unit, total, code, mode):
            try:
                prog_q.put_nowait((unit, total, code, mode))
            except Exception:
                pass

        try:
            res = run_plan(req, should_cancel=should_cancel, on_progress=on_progress)
            out_q.put(("ok", res))
        except Cancelled:
            out_q.put(("cancelled", None))
    except Exception:
        out_q.put(("error", traceback.format_exc(limit=6)))


@dataclass
class Job:
    job_id: str
    input_hash: str
    seed: int
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    state: str = "queued"
    phase: str = "queued"
    courses_done: int = 0
    courses_total: int = 0
    scenarios_done: int = 0
    scenarios_total: int = 0
    trials_done: int = 0
    trials_total: int = 0
    result: dict | None = None
    error: str | None = None
    error_category: str | None = None
    cache_hit: bool = False
    proc: object | None = None
    cancel_ev: object | None = None
    prog_q: object | None = None
    out_q: object | None = None
    cancel_requested_at: float | None = None
    stopped_at: float | None = None

    def status(self) -> dict:
        pct = 0.0
        if self.scenarios_total:
            pct = round(100.0 * self.scenarios_done / self.scenarios_total, 1)
        return {
            "job_id": self.job_id, "state": self.state,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "input_hash": self.input_hash,
            "progress": {
                "courses_done": self.courses_done, "courses_total": self.courses_total,
                "scenarios_done": self.scenarios_done, "scenarios_total": self.scenarios_total,
                "trials_done": self.trials_done, "trials_total": self.trials_total,
                "percent": pct, "phase": self.phase,
            },
            "error": self.error, "error_category": self.error_category,
            "rule_version": RULE_VERSION, "model_version": MODEL_VERSION,
            "dataset_version": DATASET_VERSION, "seed": self.seed,
            "cache_hit": self.cache_hit,
            "expires_at": self.created_at + JOB_TTL_SECONDS,
        }


class JobManager:
    """Owns job lifecycle, the result cache, and the reaper thread."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT, cache_size: int = 64):
        self.jobs: dict[str, Job] = {}
        self.cache: dict[str, dict] = {}
        self._cache_order: list[str] = []
        self._cache_size = cache_size
        self._lock = threading.RLock()
        self._max = max_concurrent
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._pump, daemon=True)
        self._reaper.start()
        self.metrics = {"submitted": 0, "completed": 0, "cancelled": 0,
                        "failed": 0, "cache_hits": 0}

    # ---------------- cache ----------------
    def cached(self, h: str) -> dict | None:
        with self._lock:
            return self.cache.get(h)

    def _store(self, h: str, res: dict) -> None:
        with self._lock:
            if h in self.cache:
                return
            self.cache[h] = res
            self._cache_order.append(h)
            while len(self._cache_order) > self._cache_size:
                self.cache.pop(self._cache_order.pop(0), None)

    def invalidate_cache(self) -> int:
        with self._lock:
            n = len(self.cache)
            self.cache.clear(); self._cache_order.clear()
            return n

    # ---------------- submit ----------------
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self.jobs.values()
                       if j.state in ("queued", "starting", "running", "cancelling"))

    def submit(self, req: dict, input_hash: str) -> Job:
        with self._lock:
            if self.active_count() >= self._max:
                raise RuntimeError(
                    f"too many concurrent simulations (limit {self._max}); cancel one or retry")
            jid = uuid.uuid4().hex[:16]
            grid = 3 + len(req.get("extra_scenarios", []))
            j = Job(job_id=jid, input_hash=input_hash, seed=int(req.get("seed", 0)),
                    courses_total=len(req["courses"]),
                    scenarios_total=len(req["courses"]) * grid,
                    trials_total=len(req["courses"]) * grid * int(req["trials"]))
            self.jobs[jid] = j
            self.metrics["submitted"] += 1

            hit = self.cache.get(input_hash)
            if hit is not None:
                j.state = "completed"; j.phase = "cache"
                j.result = dict(hit); j.result["cache_hit"] = True
                j.cache_hit = True
                j.scenarios_done = j.scenarios_total
                j.trials_done = j.trials_total
                j.updated_at = time.time()
                self.metrics["cache_hits"] += 1
                return j

            j.cancel_ev = CTX.Event()
            j.prog_q = CTX.Queue(maxsize=512)
            j.out_q = CTX.Queue(maxsize=4)
            j.proc = CTX.Process(target=_worker_entry,
                                 args=(req, j.cancel_ev, j.prog_q, j.out_q), daemon=True)
            j.state = "starting"; j.phase = "spawning worker"
            j.proc.start()
            return j

    # ---------------- cancel ----------------
    def cancel(self, jid: str) -> bool:
        with self._lock:
            j = self.jobs.get(jid)
            if j is None or j.state in ("completed", "failed", "cancelled", "expired"):
                return False
            j.state = "cancelling"; j.phase = "cancelling"
            j.cancel_requested_at = time.time()
            j.updated_at = time.time()
            if j.cancel_ev is not None:
                j.cancel_ev.set()          # cooperative: engine checks at block boundaries
            return True

    # ---------------- pump ----------------
    def _pump(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.03)
            with self._lock:
                jobs = list(self.jobs.values())
            now = time.time()
            for j in jobs:
                if j.prog_q is not None:
                    try:
                        while True:
                            unit, total, code, mode = j.prog_q.get_nowait()
                            j.scenarios_done = unit
                            j.scenarios_total = total
                            j.courses_done = min(j.courses_total, unit // max(1, total // max(1, j.courses_total)))
                            j.trials_done = int(j.trials_total * (unit / max(1, total)))
                            j.phase = f"simulating {code} under {mode}"
                            if j.state == "starting":
                                j.state = "running"
                            j.updated_at = now
                    except queue.Empty:
                        pass
                    except Exception:
                        pass
                if j.out_q is not None:
                    try:
                        kind, payload = j.out_q.get_nowait()
                        if kind == "ok":
                            j.result = payload; j.state = "completed"; j.phase = "done"
                            j.scenarios_done = j.scenarios_total
                            j.trials_done = j.trials_total
                            self._store(j.input_hash, payload)
                            self.metrics["completed"] += 1
                        elif kind == "cancelled":
                            j.state = "cancelled"; j.phase = "cancelled"
                            j.stopped_at = time.time()
                            self.metrics["cancelled"] += 1
                        else:
                            j.state = "failed"; j.phase = "failed"
                            j.error = str(payload)[-800:]
                            j.error_category = "worker_exception"
                            self.metrics["failed"] += 1
                        j.updated_at = now
                    except queue.Empty:
                        pass
                    except Exception:
                        pass
                # worker died without reporting
                if j.state in ("starting", "running", "cancelling") and j.proc is not None \
                        and not j.proc.is_alive():
                    if j.state == "cancelling":
                        j.state = "cancelled"; j.phase = "cancelled"
                        j.stopped_at = j.stopped_at or time.time()
                        self.metrics["cancelled"] += 1
                    elif j.result is None:
                        j.state = "failed"; j.phase = "worker exited"
                        j.error = "worker process exited without producing a result"
                        j.error_category = "worker_died"
                        self.metrics["failed"] += 1
                    j.updated_at = now
                # hard stop if a cancelling worker refuses to yield
                if j.state == "cancelling" and j.cancel_requested_at and \
                        now - j.cancel_requested_at > 3.0 and j.proc is not None and j.proc.is_alive():
                    j.proc.terminate()
                    j.phase = "worker terminated after grace period"
                if j.state in ("completed", "failed", "cancelled") and \
                        now - j.created_at > JOB_TTL_SECONDS:
                    j.state = "expired"; j.result = None; j.updated_at = now

    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self.jobs.get(jid)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for j in self.jobs.values():
                if j.cancel_ev is not None:
                    j.cancel_ev.set()
                if j.proc is not None and j.proc.is_alive():
                    j.proc.terminate()
