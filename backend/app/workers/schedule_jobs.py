"""Process-isolated job manager for schedule search - the backend replacement
for the browser's synchronous enumerateSchedules()/buildSchedules(). Deliberately
a separate, small manager rather than a generalization of workers/jobs.py's
JobManager: that class is the verified, measured (46ms cancellation stop time)
simulation job engine, and this module intentionally does not touch it. The two
share the same proven shape (spawn context, cooperative cancellation via Event,
progress/result queues, a reaper thread) because that shape is what's already
been proven to work, not because the code is copy-pasted without reason.
"""
from __future__ import annotations
import multiprocessing as mp
import queue, threading, time, traceback, uuid
from dataclasses import dataclass, field

from app.domain.rules import RULE_VERSION, DATASET_VERSION, MODEL_VERSION

CTX = mp.get_context("spawn")
JOB_TTL_SECONDS = 3600
MAX_CONCURRENT = 2


def _worker_entry(req: dict, cancel_ev, prog_q, out_q) -> None:
    """Runs in a separate process. Imports inside so spawn does not re-import the API."""
    try:
        from app.domain import catalog
        from app.services.scheduler import PlacedMeeting, SearchItem, search, search_with_fallback

        def should_cancel() -> bool:
            return cancel_ev.is_set()

        def on_progress(nodes: int, total: int) -> None:
            try:
                prog_q.put_nowait((nodes, total))
            except Exception:
                pass

        shortlist = req["shortlist"]
        courses = catalog.get_courses(shortlist)
        items = [SearchItem(code=c, packages=tuple(courses[c]["pk"])) for c in shortlist]

        # locked fixed courses are held static; unlocked ("swappable") ones join
        # the search as items too - this is what lets auto-resolve-clashes
        # reconsider an unlocked pre-enrolled section, not just the shortlist.
        fixed_meetings = []
        fixed_credits = sum(float(item["credits"]) for item in req.get("external_fixed", []))
        fixed_course_codes = []
        for f in req.get("fixed", []):
            fc = catalog.get_course(f["code"])
            fixed_course_codes.append(f["code"])
            if f.get("locked", True):
                pkg = fc["pk"][f["pkg"]]
                for m in pkg["m"]:
                    fixed_meetings.append(PlacedMeeting(m=tuple(m), term=pkg["t"], code=f["code"]))
                fixed_credits += catalog.credits_of(fc)
            else:
                items.append(SearchItem(code=f["code"], packages=tuple(fc["pk"])))

        if req.get("wishlist"):
            result = _solve_wishlist(req, catalog, fixed_meetings, fixed_credits,
                                     should_cancel, on_progress)
            result["fixed_course_codes"] = fixed_course_codes
            result["external_fixed"] = req.get("external_fixed", [])
        else:
            search_fn = search_with_fallback if req.get("allow_least_conflict", True) else search
            result = search_fn(items, fixed_meetings, req["max_results"], req["max_nodes"],
                               sort=req["sort"], should_cancel=should_cancel, on_progress=on_progress)
        if result.get("cancelled"):
            out_q.put(("cancelled", None))
        else:
            out_q.put(("ok", result))
    except Exception:
        out_q.put(("error", traceback.format_exc(limit=6)))


def _solve_wishlist(req: dict, catalog, fixed_meetings, fixed_credits, should_cancel, on_progress) -> dict:
    """Wishlist mode: routes to the CP-SAT exact optimization layer instead of
    the shortlist branch-and-bound path. See app/services/cp_scheduler.py for
    why CP-SAT is the right tool for this specific shape (course inclusion +
    package selection + choice groups + credit bounds)."""
    from app.services.cp_scheduler import WishChoiceGroup, WishItem, explain_omission, solve as cp_solve

    on_progress(0, 1)
    items: list[WishItem] = []
    for w in req["wishlist"]:
        fc = catalog.get_course(w["code"])
        items.append(WishItem(
            code=w["code"], packages=tuple(fc["pk"]), credits=catalog.credits_of(fc),
            intent=w.get("intent", "strong"), priority=int(w.get("priority", 5)),
            forced=w.get("intent") == "must_have",
            locked_package=w.get("locked_package"),
            excluded_packages=tuple(w.get("excluded_packages", [])),
        ))
    groups = [WishChoiceGroup(kind=g["kind"], members=tuple(g["members"]), min_credits=g.get("min_credits"))
             for g in req.get("choice_groups", [])]

    credit_max = req["credit_max"]
    credit_target = req.get("credit_target") if req.get("credit_target") is not None else credit_max
    credit_min = req.get("credit_min") or 0.0

    result = cp_solve(items, fixed_meetings, fixed_credits, groups, credit_min, credit_target, credit_max)
    on_progress(1, 1)
    if should_cancel():
        return {"cancelled": True}

    schedules = [{"assign": result.assign, "stats": result.stats}] if result.assign else []
    why_not = []
    for code in result.excluded[:15]:  # bounded: full explanations are the on-demand endpoint's job
        why_not.append(explain_omission(code, items, fixed_meetings, fixed_credits, groups,
                                        credit_min, credit_target, credit_max, result,
                                        time_limit_seconds=1.5))
    return {
        "schedules": schedules, "truncated": False, "cancelled": False, "nodes": 0,
        "total_found": len(schedules), "sort": req.get("sort", "compact"),
        "item_order": [it.code for it in items], "mode": "optimized",
        "clash_count": 0, "cp_status": result.status, "total_credits": result.total_credits,
        "fixed_credits": fixed_credits, "included": result.included, "excluded": result.excluded,
        "min_relaxed": result.min_relaxed, "why_not": why_not,
        "credit_min": credit_min, "credit_target": credit_target, "credit_max": credit_max,
    }


@dataclass
class ScheduleJob:
    job_id: str
    input_hash: str
    max_nodes: int
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    state: str = "queued"
    phase: str = "queued"
    nodes_done: int = 0
    nodes_total: int = 0
    result: dict | None = None
    error: str | None = None
    cache_hit: bool = False
    proc: object | None = None
    cancel_ev: object | None = None
    prog_q: object | None = None
    out_q: object | None = None
    cancel_requested_at: float | None = None
    stopped_at: float | None = None
    req: dict | None = None  # kept so /explain-exclusion can re-derive the same wishlist
                             # model without the caller re-sending the whole request

    def status(self) -> dict:
        pct = round(100.0 * self.nodes_done / self.nodes_total, 1) if self.nodes_total else 0.0
        return {
            "job_id": self.job_id, "state": self.state,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "input_hash": self.input_hash,
            "progress": {"nodes_done": self.nodes_done, "nodes_total": self.nodes_total,
                        "percent": pct, "phase": self.phase},
            "error": self.error, "rule_version": RULE_VERSION, "model_version": MODEL_VERSION,
            "dataset_version": DATASET_VERSION, "cache_hit": self.cache_hit,
            "expires_at": self.created_at + JOB_TTL_SECONDS,
        }


class ScheduleJobManager:
    """Same lifecycle shape as workers.jobs.JobManager: submit, cancel, a reaper
    thread that drains progress/result queues, and a small result cache keyed by
    input hash so an identical search (same shortlist+fixed+budget+sort) doesn't
    redo the work."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT, cache_size: int = 32):
        self.jobs: dict[str, ScheduleJob] = {}
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

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self.jobs.values()
                       if j.state in ("queued", "starting", "running", "cancelling"))

    def submit(self, req: dict, input_hash: str) -> ScheduleJob:
        with self._lock:
            if self.active_count() >= self._max:
                raise RuntimeError(
                    f"too many concurrent schedule searches (limit {self._max}); cancel one or retry")
            jid = uuid.uuid4().hex[:16]
            j = ScheduleJob(job_id=jid, input_hash=input_hash, max_nodes=req["max_nodes"],
                            nodes_total=req["max_nodes"], req=req)
            self.jobs[jid] = j
            self.metrics["submitted"] += 1

            hit = self.cache.get(input_hash)
            if hit is not None:
                j.state = "completed"; j.phase = "cache"
                j.result = dict(hit); j.result["cache_hit"] = True
                j.cache_hit = True
                j.nodes_done = j.nodes_total
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

    def cancel(self, jid: str) -> bool:
        with self._lock:
            j = self.jobs.get(jid)
            if j is None or j.state in ("completed", "failed", "cancelled", "expired"):
                return False
            j.state = "cancelling"; j.phase = "cancelling"
            j.cancel_requested_at = time.time()
            j.updated_at = time.time()
            if j.cancel_ev is not None:
                j.cancel_ev.set()
            return True

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
                            nodes, total = j.prog_q.get_nowait()
                            j.nodes_done = nodes; j.nodes_total = total
                            j.phase = f"searched {nodes:,} of up to {total:,} combinations"
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
                            j.nodes_done = j.nodes_total
                            self._store(j.input_hash, payload)
                            self.metrics["completed"] += 1
                        elif kind == "cancelled":
                            j.state = "cancelled"; j.phase = "cancelled"
                            j.stopped_at = time.time()
                            self.metrics["cancelled"] += 1
                        else:
                            j.state = "failed"; j.phase = "failed"
                            j.error = str(payload)[-800:]
                            self.metrics["failed"] += 1
                        j.updated_at = now
                    except queue.Empty:
                        pass
                    except Exception:
                        pass
                if j.state in ("starting", "running", "cancelling") and j.proc is not None \
                        and not j.proc.is_alive():
                    if j.state == "cancelling":
                        j.state = "cancelled"; j.phase = "cancelled"
                        j.stopped_at = j.stopped_at or time.time()
                        self.metrics["cancelled"] += 1
                    elif j.result is None:
                        j.state = "failed"; j.phase = "worker exited"
                        j.error = "worker process exited without producing a result"
                        self.metrics["failed"] += 1
                    j.updated_at = now
                if j.state == "cancelling" and j.cancel_requested_at and \
                        now - j.cancel_requested_at > 3.0 and j.proc is not None and j.proc.is_alive():
                    j.proc.terminate()
                    j.phase = "worker terminated after grace period"
                if j.state in ("completed", "failed", "cancelled") and \
                        now - j.created_at > JOB_TTL_SECONDS:
                    j.state = "expired"; j.result = None; j.updated_at = now

    def get(self, jid: str) -> ScheduleJob | None:
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
