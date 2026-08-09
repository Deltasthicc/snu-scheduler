"""FastAPI application. CPU-bound work never runs on the event loop."""
from __future__ import annotations
import asyncio, json, logging, os, sys, time, uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.domain.rules import RULES, RULE_VERSION, DATASET_VERSION, MODEL_VERSION, counts
from app.domain.pools import compute_pools, max_bid, RuleError
from app.domain.auction import settle
from app.domain import catalog
from app.models.schemas import (PoolRequest, PoolResponse, SimulationRequest,
                                SettlementRequest, JobStatus, MAX_COURSES, MAX_TRIALS,
                                BidStrategyRequest, BidStrategyResponse)
from app.models.schedule_schemas import ScheduleJobStatus, ScheduleSearchRequest
from app.models.profile_schemas import (CreditPolicyRequest, ProfileValidateRequest,
                                        WishlistValidateRequest)
from app.models.audit_schemas import DegreeAuditRequest
from app.models.minor_schemas import MinorAuditRequest, MinorOverviewRequest
from app.models.advisement_schemas import AdvisementParseRequest
from app.services.runner import input_hash, stress_test_plan
from app.services.bid_strategy import STRATEGY_VERSION, build_bid_strategy
from app.services.credit_policy import CreditPolicyError, resolve_ceiling
from app.services.wishlist import validate_choice_groups, wishlist_summary
from app.services.degree_audit import ProgrammeCatalog, audit_degree
from app.services.minor_audit import MinorCatalog, audit_minor, minors_overview
from app.services.advisement_report import AdvisementParseError, parse_advisement_report
from app.workers.jobs import JobManager
from app.workers.schedule_jobs import ScheduleJobManager
from app.persistence import store
from app.timetable_updates import apply as tt_apply
from app.timetable_updates.models import (ApplyRequest, CheckRequest, DiscardRequest,
                                          RollbackRequest, UpdateStatusResponse)
from app.timetable_updates.poller import UpdateService

# ---------------- structured logging ----------------
class JsonLog(logging.Formatter):
    def format(self, r):
        base = {"ts": round(time.time(), 3), "level": r.levelname, "msg": r.getMessage(),
                "logger": r.name}
        for k in ("request_id", "job_id", "worker_id", "runtime_ms", "cache", "category"):
            v = getattr(r, k, None)
            if v is not None:
                base[k] = v
        base["rule_version"] = RULE_VERSION
        base["model_version"] = MODEL_VERSION
        return json.dumps(base)

_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(JsonLog())
log = logging.getLogger("snu")
log.setLevel(logging.INFO)
log.handlers = [_h]
log.propagate = False

MANAGER: JobManager | None = None
SCHED_MANAGER: ScheduleJobManager | None = None
UPDATE_SERVICE: UpdateService | None = None
PROGRAMMES = ProgrammeCatalog()
MINORS = MinorCatalog()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MANAGER, SCHED_MANAGER, UPDATE_SERVICE
    store.migrate()
    MANAGER = JobManager()
    SCHED_MANAGER = ScheduleJobManager()
    catalog.all_courses()  # fail fast at startup if the catalog file is missing/corrupt
    PROGRAMMES.list()  # fail fast if the programme catalog is missing/corrupt
    UPDATE_SERVICE = UpdateService()
    UPDATE_SERVICE.start()
    log.info("startup complete")
    yield
    if MANAGER:
        MANAGER.shutdown()
    if SCHED_MANAGER:
        SCHED_MANAGER.shutdown()
    if UPDATE_SERVICE:
        await UPDATE_SERVICE.stop()
    log.info("shutdown complete")


app = FastAPI(title="SNU Scheduler API", version="3.1.0", lifespan=lifespan,
              description="Backend for the Shiv Nadar IoE schedule, degree-audit, and strategic bid planner. "
                          "The default bid planner does not invent rival bids or win probabilities.")
_local_origins = ["http://127.0.0.1:5173", "http://localhost:5173"]
_configured_origins = [value.strip() for value in os.getenv("SNU_CORS_ORIGINS", "").split(",") if value.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_local_origins + _configured_origins,
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    resp = await call_next(request)
    resp.headers["x-request-id"] = rid
    log.info("%s %s -> %d" % (request.method, request.url.path, resp.status_code),
             extra={"request_id": rid, "runtime_ms": round((time.perf_counter() - t0) * 1000, 1)})
    return resp


@app.exception_handler(RuleError)
async def rule_error(_r, exc: RuleError):
    return JSONResponse(status_code=422, content={"detail": str(exc), "field": None, "limit": None})


@app.exception_handler(CreditPolicyError)
async def credit_policy_error(_r, exc: CreditPolicyError):
    return JSONResponse(status_code=422, content={"detail": str(exc), "field": None, "limit": None})


@app.exception_handler(AdvisementParseError)
async def advisement_parse_error(_r, exc: AdvisementParseError):
    return JSONResponse(status_code=422, content={"detail": str(exc), "field": "file", "limit": "8 MB"})


# ---------------- health & meta ----------------
@app.get("/health/live", tags=["health"])
async def live():
    return {"status": "live", "rule_version": RULE_VERSION}


@app.get("/health/ready", tags=["health"])
async def ready():
    ok = MANAGER is not None and SCHED_MANAGER is not None
    return JSONResponse(status_code=200 if ok else 503, content={
        "status": "ready" if ok else "starting",
        "active_jobs": MANAGER.active_count() if MANAGER else None,
        "active_schedule_jobs": SCHED_MANAGER.active_count() if SCHED_MANAGER else None,
        "rule_version": RULE_VERSION, "dataset_version": DATASET_VERSION,
        "model_version": MODEL_VERSION, "strategy_version": STRATEGY_VERSION,
    })


@app.get("/api/v1/metrics", tags=["health"])
async def metrics():
    m = dict(MANAGER.metrics) if MANAGER else {}
    total = max(1, m.get("submitted", 0))
    return {**m, "active_jobs": MANAGER.active_count() if MANAGER else 0,
            "cache_hit_rate": round(m.get("cache_hits", 0) / total, 3),
            "cache_entries": len(MANAGER.cache) if MANAGER else 0,
            "limits": {"max_courses": MAX_COURSES, "max_trials": MAX_TRIALS,
                       "max_concurrent_jobs": MANAGER._max if MANAGER else None}}


@app.get("/api/v1/dataset", tags=["rules"])
async def dataset():
    """Active institutional timetable dataset identity - see
    tools/import_netlify_timetable.py and docs/TIMETABLE_REVISION_DIFF_2026-08-04.md.
    The frontend uses this to detect 'this saved plan was built against an
    older timetable' (spec: never silently mutate a saved schedule underneath
    the student)."""
    return catalog.dataset_info()


# ---------------- timetable update service ----------------
# See app/timetable_updates/ for the canonical fetch/parse/normalize/validate/
# diff/apply implementation this endpoint set drives - the CLI importer
# (tools/import_netlify_timetable.py) calls the exact same modules.
@app.get("/api/v1/timetable-updates/status", response_model=UpdateStatusResponse, tags=["timetable-updates"])
async def tt_status():
    return UPDATE_SERVICE.status()


@app.post("/api/v1/timetable-updates/check", tags=["timetable-updates"])
async def tt_check(req: CheckRequest = CheckRequest()):
    result = await UPDATE_SERVICE.check(force=req.force)
    if result.get("skipped"):
        raise HTTPException(409, result["reason"])
    return result


@app.get("/api/v1/timetable-updates/candidate", tags=["timetable-updates"])
async def tt_candidate():
    c = UPDATE_SERVICE.candidate
    if not c:
        raise HTTPException(404, "no candidate is currently staged")
    return {"version_id": c.version_id, "dataset_checksum": c.dataset_checksum,
            "source_hash": c.source_hash, "extracted_hash": c.extracted_hash,
            "manifest_entry": c.manifest_entry, "error_count": c.error_count,
            "warning_count": c.warning_count, "staged_at": c.staged_at, "diff_summary": c.diff["summary"]}


@app.get("/api/v1/timetable-updates/diff", tags=["timetable-updates"])
async def tt_diff():
    c = UPDATE_SERVICE.candidate
    if not c:
        raise HTTPException(404, "no candidate is currently staged")
    return c.diff


@app.post("/api/v1/timetable-updates/apply", tags=["timetable-updates"])
async def tt_apply_route(req: ApplyRequest):
    try:
        return await UPDATE_SERVICE.apply(req.candidate_version, req.candidate_checksum)
    except tt_apply.ApplyError as e:
        raise HTTPException(409, str(e))


@app.post("/api/v1/timetable-updates/discard", tags=["timetable-updates"])
async def tt_discard_route(req: DiscardRequest):
    try:
        UPDATE_SERVICE.discard(req.candidate_version)
    except tt_apply.ApplyError as e:
        raise HTTPException(409, str(e))
    return {"discarded": req.candidate_version}


@app.post("/api/v1/timetable-updates/rollback", tags=["timetable-updates"])
async def tt_rollback_route(req: RollbackRequest):
    try:
        return await UPDATE_SERVICE.rollback(req.target_version)
    except tt_apply.ApplyError as e:
        raise HTTPException(409, str(e))


@app.get("/api/v1/timetable-updates/history", tags=["timetable-updates"])
async def tt_history(limit: int = 50):
    return {"history": UPDATE_SERVICE.history[-min(200, max(1, limit)):][::-1]}


@app.get("/api/v1/timetable-updates/changelog", tags=["timetable-updates"])
async def tt_changelog():
    """Every real timetable revision ever applied, diffed against its
    predecessor, oldest first - unlike /history above (this process's own
    in-memory check/apply log), this reads dataset_manifest.json's persisted
    version list, so it survives a restart and covers versions applied in an
    earlier process too."""
    return {"changelog": tt_apply.changelog()}


@app.get("/api/v1/rules", tags=["rules"])
async def rules():
    return {"version": RULE_VERSION, "dataset_version": DATASET_VERSION,
            "model_version": MODEL_VERSION, "counts": counts(),
            "rules": [{"id": r.id, "name": r.name, "desc": r.desc, "status": r.status.value,
                       "source": r.source, "value": r.value, "configurable": r.configurable,
                       "note": r.note, "resolution": r.resolution, "verified": r.verified,
                       "impact": r.impact, "programme_scope": list(r.programme_scope) if r.programme_scope else None}
                      for r in RULES.values()]}


# ---------------- deterministic rule endpoints ----------------
@app.post("/api/v1/pools", response_model=PoolResponse, tags=["rules"])
async def pools(req: PoolRequest):
    p = compute_pools(req.model_year, req.semester, req.rem_me, req.rem_uwe,
                      req.rem_ccc, req.floater, req.done_me, req.done_uwe, req.done_ccc)
    return PoolResponse(ME=p["ME"], UWE=p["UWE"], CCC=p["CCC"], detail=p["detail"],
                        rule_id=p["rule_id"], rule_version=RULE_VERSION)


@app.get("/api/v1/max-bid", tags=["rules"])
async def maxbid(credits: float):
    """No per-course bid cap exists (rectified 2026-08-05): a student may bid any whole
    number up to their entire available category pool on a single course. Endpoint kept
    for API stability; `max_bid` is always null unless a caller supplies an explicit
    override multiplier."""
    return {"credits": credits, "max_bid": max_bid(credits), "rule_id": "AUC.MAX_BID",
            "note": "There is no per-course bid cap. The only real ceiling is your own "
                    "category pool for this course - see POST /api/v1/pools."}


@app.post("/api/v1/settlement", tags=["rules"])
async def settlement(req: SettlementRequest):
    return settle(req.seats, req.bids, req.cap, req.seed)


@app.post("/api/v1/bid-strategy", response_model=BidStrategyResponse, tags=["planning"])
async def bid_strategy(req: BidStrategyRequest):
    """Build a deterministic, reserve-aware plan without inventing rival bids."""
    return build_bid_strategy(req)


# ---------------- simulation jobs ----------------
@app.post("/api/v1/simulations", status_code=202, tags=["simulation"])
async def create_sim(req: SimulationRequest):
    payload = req.model_dump(mode="json")
    h = input_hash(payload)
    try:
        job = MANAGER.submit(payload, h)
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e))
    log.info("simulation submitted", extra={"job_id": job.job_id,
                                            "cache": "hit" if job.cache_hit else "miss"})
    return {"job_id": job.job_id, "state": job.state, "input_hash": h,
            "cache_hit": job.cache_hit,
            "links": {"status": f"/api/v1/simulations/{job.job_id}",
                      "events": f"/api/v1/simulations/{job.job_id}/events",
                      "result": f"/api/v1/simulations/{job.job_id}/result",
                      "cancel": f"/api/v1/simulations/{job.job_id}/cancel"}}


@app.get("/api/v1/simulations/{job_id}", response_model=JobStatus, tags=["simulation"])
async def sim_status(job_id: str):
    j = MANAGER.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job_id")
    return j.status()


@app.post("/api/v1/simulations/{job_id}/cancel", tags=["simulation"])
async def sim_cancel(job_id: str):
    t0 = time.perf_counter()
    ok = MANAGER.cancel(job_id)
    if not ok:
        raise HTTPException(409, "job is not cancellable in its current state")
    log.info("cancel requested", extra={"job_id": job_id,
                                        "runtime_ms": round((time.perf_counter() - t0) * 1000, 2)})
    return {"job_id": job_id, "state": "cancelling",
            "ack_ms": round((time.perf_counter() - t0) * 1000, 2)}


@app.get("/api/v1/simulations/{job_id}/result", tags=["simulation"])
async def sim_result(job_id: str):
    j = MANAGER.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job_id")
    if j.state == "cancelled":
        raise HTTPException(409, "job was cancelled; partial results are never returned as complete")
    if j.state == "failed":
        raise HTTPException(500, j.error or "simulation failed")
    if j.state == "expired":
        raise HTTPException(410, "result has expired")
    if j.state != "completed" or j.result is None:
        raise HTTPException(409, f"job is {j.state}; result not ready")
    out = dict(j.result)
    out["cache_hit"] = j.cache_hit
    store.record_job({"job_id": j.job_id, "input_hash": j.input_hash, "state": j.state,
                      "created_at": j.created_at, "runtime_ms": out.get("runtime_ms"),
                      "courses": j.courses_total, "trials": j.trials_total,
                      "cache_hit": j.cache_hit})
    return out


@app.get("/api/v1/simulations/{job_id}/events", tags=["simulation"])
async def sim_events(job_id: str):
    """Server-Sent Events progress stream."""
    j = MANAGER.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job_id")

    async def gen():
        last = None
        deadline = time.time() + 900
        while time.time() < deadline:
            cur = MANAGER.get(job_id)
            if cur is None:
                break
            s = cur.status()
            packed = json.dumps(s)
            if packed != last:
                yield f"event: progress\ndata: {packed}\n\n"
                last = packed
            if s["state"] in ("completed", "failed", "cancelled", "expired"):
                yield f"event: done\ndata: {packed}\n\n"
                return
            await asyncio.sleep(0.12)
        yield 'event: done\ndata: {"state":"timeout"}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- plans, observations, jobs ----------------
@app.get("/api/v1/plans", tags=["plans"])
async def plans_list():
    return {"plans": store.list_plans()}


@app.post("/api/v1/plans", status_code=201, tags=["plans"])
async def plans_save(body: dict):
    name = str(body.get("name", "Untitled"))[:120]
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(422, "payload must be an object")
    if len(json.dumps(payload)) > 400_000:
        raise HTTPException(413, "plan payload too large (400 KB limit)")
    return store.save_plan(name, payload, body.get("id"))


@app.get("/api/v1/plans/{pid}", tags=["plans"])
async def plans_get(pid: str):
    p = store.get_plan(pid)
    if not p:
        raise HTTPException(404, "unknown plan id")
    return p


@app.delete("/api/v1/plans/{pid}", tags=["plans"])
async def plans_delete(pid: str):
    if not store.delete_plan(pid):
        raise HTTPException(404, "unknown plan id")
    return {"deleted": pid}


@app.post("/api/v1/plans/{pid}/duplicate", status_code=201, tags=["plans"])
async def plans_dup(pid: str, body: dict | None = None):
    name = (body or {}).get("name") or "Copy"
    p = store.duplicate_plan(pid, name[:120])
    if not p:
        raise HTTPException(404, "unknown plan id")
    return p


@app.get("/api/v1/observations", tags=["observations"])
async def obs_list(course_code: str | None = None):
    return {"observations": store.list_observations(course_code)}


@app.post("/api/v1/observations", status_code=201, tags=["observations"])
async def obs_add(body: dict):
    if not body.get("course_code"):
        raise HTTPException(422, "course_code is required")
    return store.add_observation(body)


@app.get("/api/v1/observations/{course_code}/calibration", tags=["observations"])
async def obs_cal(course_code: str):
    return store.calibration(course_code)


@app.post("/api/v1/validate-plan", tags=["rules"])
async def validate_plan(req: SimulationRequest):
    """Cheap server-side plan validation before a job is created.
    Uses the same Pydantic contract, so the frontend cannot drift from it."""
    warnings, blocking = [], []
    total_cr = sum(c.credits for c in req.courses)
    for c in req.courses:
        if c.seats == 0:
            blocking.append(f"{c.code} has zero seats and cannot be won")
        if c.live_bidders is not None and c.live_bidders < c.seats:
            warnings.append(
                f"{c.code}: live count {c.live_bidders} is below {c.seats} seats, so a low bid may "
                f"currently win - but the round may still be open")
        if c.priority == "MUST" and c.live_bidders is None:
            warnings.append(f"{c.code} is a must-have with no live bidder count; the stress default applies")
    for cat in ("ME", "UWE", "CCC"):
        cat_courses = [c for c in req.courses if c.category == cat]
        if cat_courses and req.pools.get(cat, 0) <= 0:
            blocking.append(f"{len(cat_courses)} {cat} course(s) selected but the {cat} pool is zero")
    scenario_count = 3 + len(req.extra_scenarios)
    return {"ok": not blocking, "blocking": blocking, "warnings": warnings,
            "course_count": len(req.courses), "total_credits": total_cr,
            "trials": req.trials, "scenarios": scenario_count,
            "estimated_work_units": len(req.courses) * scenario_count * req.trials,
            "rule_version": RULE_VERSION}


@app.post("/api/v1/simulations/{job_id}/stress-test", tags=["simulation"])
async def stress(job_id: str, body: dict):
    j = MANAGER.get(job_id)
    if not j or j.state != "completed" or j.result is None:
        raise HTTPException(409, "stress test requires a completed simulation")
    credits = body.get("credits") or {}
    res = dict(j.result)
    recs = [dict(r) for r in res["recommendations"]]
    for r in recs:
        r["credits"] = float(credits.get(r["code"], 3))
    res["recommendations"] = recs
    return stress_test_plan(res, float(body.get("credit_cap", 25)),
                            float(body.get("fixed_credits", 0)),
                            int(min(20000, max(200, body.get("cohorts", 4000)))),
                            int(body.get("seed", 0)))


@app.get("/api/v1/jobs", tags=["simulation"])
async def jobs_history(limit: int = 50):
    return {"jobs": store.list_jobs(min(200, max(1, limit)))}


# ---------------- profile & wishlist (scheduler v2: wishlist/CP-SAT phase) ----------------
# See app/services/credit_policy.py and app/services/wishlist.py, and
# docs/research/scheduler_v2_matrix.md for the research this section implements.
@app.get("/api/v1/programmes", tags=["profile"])
async def programmes_list():
    """Compact official catalog; PDFs stay remote and are represented by source URLs."""
    return {**PROGRAMMES.meta, "programs": PROGRAMMES.list()}


@app.get("/api/v1/programmes/{programme_id}", tags=["profile"])
async def programme_detail(programme_id: str):
    programme = PROGRAMMES.get(programme_id)
    if programme is None:
        raise HTTPException(404, "unknown programme")
    return programme


@app.post("/api/v1/degree-audit", tags=["profile"])
async def degree_audit(req: DegreeAuditRequest):
    try:
        return audit_degree(req, PROGRAMMES)
    except KeyError:
        raise HTTPException(422, "unknown programme_id") from None


@app.get("/api/v1/minors", tags=["profile"])
async def minors_list():
    """Undergraduate minor programmes (2024 batch and earlier), transcribed from the University's own circular."""
    return {**MINORS.meta, "minors": MINORS.list()}


@app.get("/api/v1/minors/{minor_id}", tags=["profile"])
async def minor_detail(minor_id: str):
    minor = MINORS.get(minor_id)
    if minor is None:
        raise HTTPException(404, "unknown minor")
    return minor


@app.post("/api/v1/minors/audit", tags=["profile"])
async def minor_audit_route(req: MinorAuditRequest):
    try:
        return audit_minor(req, MINORS)
    except KeyError:
        raise HTTPException(422, "unknown minor_id") from None


@app.post("/api/v1/minors/overview", tags=["profile"])
async def minors_overview_route(req: MinorOverviewRequest):
    return minors_overview(req, MINORS)


@app.post("/api/v1/advisement-report/parse", tags=["profile"])
async def advisement_report_parse(req: AdvisementParseRequest):
    """Parse a private report in memory. The uploaded bytes are never persisted."""
    return await asyncio.to_thread(parse_advisement_report, req.content_base64, req.filename)


@app.post("/api/v1/profiles/validate", tags=["profile"])
async def profiles_validate(req: ProfileValidateRequest):
    cp = req.credit_policy
    result = resolve_ceiling(
        fixed_credits=cp.fixed_credits, personal_target=cp.personal_target,
        min_credits=cp.min_credits, overload_ceiling=cp.overload_ceiling,
        overload_mode=cp.overload_mode, overload_confirmed=cp.overload_confirmed,
        current_year=cp.current_year, eligibility_confirmed=cp.eligibility_confirmed,
        advisor_recommended=cp.advisor_recommended, dean_approved=cp.dean_approved,
    )
    known_reqs = [
        ("remaining_me_credits", req.remaining_me_credits), ("remaining_uwe_credits", req.remaining_uwe_credits),
        ("remaining_ccc_credits", req.remaining_ccc_credits), ("floater_credits", req.floater_credits),
    ]
    unknown = [name for name, v in known_reqs if v is None]
    return {
        "official_ceiling": result.official_ceiling, "active_ceiling": result.active_ceiling,
        "ceiling_mode": result.ceiling_mode, "fixed_credits": result.fixed_credits,
        "personal_target": result.personal_target, "min_credits": result.min_credits,
        "wishlist_room": result.wishlist_room, "is_overload": result.is_overload,
        "overload_confirmed": result.overload_confirmed, "warnings": result.warnings,
        "summary": result.summary, "unknown_fields": unknown,
        "rule_ids": ["CEILING.STANDARD", "CEILING.YEAR4_PLUS2", "CEILING.YEAR4_DEAN_EXTENSION"],
    }


@app.post("/api/v1/wishlists/validate", tags=["profile"])
async def wishlists_validate(req: WishlistValidateRequest):
    codes = [i.code for i in req.items] + [m for g in req.choice_groups for m in g.members]
    unknown = sorted({c for c in codes if catalog.get_course(c) is None})
    if unknown:
        raise HTTPException(422, f"unknown course code(s): {', '.join(unknown)}")
    issues = validate_choice_groups(req.items, req.choice_groups)
    courses = catalog.get_courses([i.code for i in req.items])
    summary = wishlist_summary(req.items, req.choice_groups, req.fixed_credits, courses)
    return {
        "ok": not issues, "issues": issues,
        "count": summary.count, "min_possible_credits": summary.min_possible_credits,
        "max_possible_credits": summary.max_possible_credits,
        "credits_currently_requested": summary.credits_currently_requested,
        "fixed_credits": summary.fixed_credits,
        "total_possible_semester_credits": summary.total_possible_semester_credits,
        "category_composition": summary.category_composition,
        "num_must_have": summary.num_must_have, "num_backup": summary.num_backup,
        "num_impossible": summary.num_impossible, "num_unconfirmed": summary.num_unconfirmed,
        "notes": summary.notes,
        "items": [vars(it) for it in summary.items],
    }


# ---------------- schedule search ----------------
# Backend replacement for the browser's synchronous enumerateSchedules(); see
# app/services/scheduler.py and app/workers/schedule_jobs.py. Measured before
# this existed: the frontend's default 2M-node search budget could block the
# browser main thread for roughly 100 seconds on a realistic shortlist.
@app.post("/api/v1/schedules/search", status_code=202, tags=["scheduling"])
async def schedule_search(req: ScheduleSearchRequest):
    unknown = [c for c in req.shortlist if catalog.get_course(c) is None]
    unknown += [f.code for f in req.fixed if catalog.get_course(f.code) is None]
    unknown += [w.code for w in req.wishlist if catalog.get_course(w.code) is None]
    unknown += [m for g in req.choice_groups for m in g.members if catalog.get_course(m) is None]
    if unknown:
        raise HTTPException(422, f"unknown course code(s): {', '.join(sorted(set(unknown)))}")
    for f in req.fixed:
        n_pkgs = len(catalog.get_course(f.code)["pk"])
        if f.pkg >= n_pkgs:
            raise HTTPException(422, f"{f.code} has no package index {f.pkg} (has {n_pkgs})")
    if req.wishlist:
        issues = validate_choice_groups(req.wishlist, req.choice_groups)
        if issues:
            raise HTTPException(422, "; ".join(issues))

    payload = req.model_dump(mode="json")
    h = input_hash(payload)
    try:
        job = SCHED_MANAGER.submit(payload, h)
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e))
    log.info("schedule search submitted", extra={"job_id": job.job_id,
                                                  "cache": "hit" if job.cache_hit else "miss"})
    return {"job_id": job.job_id, "state": job.state, "input_hash": h,
            "cache_hit": job.cache_hit,
            "links": {"status": f"/api/v1/schedules/{job.job_id}",
                      "events": f"/api/v1/schedules/{job.job_id}/events",
                      "results": f"/api/v1/schedules/{job.job_id}/results",
                      "cancel": f"/api/v1/schedules/{job.job_id}/cancel"}}


@app.get("/api/v1/schedules/{job_id}", response_model=ScheduleJobStatus, tags=["scheduling"])
async def schedule_status(job_id: str):
    j = SCHED_MANAGER.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job_id")
    return j.status()


@app.post("/api/v1/schedules/{job_id}/cancel", tags=["scheduling"])
async def schedule_cancel(job_id: str):
    t0 = time.perf_counter()
    ok = SCHED_MANAGER.cancel(job_id)
    if not ok:
        raise HTTPException(409, "job is not cancellable in its current state")
    log.info("schedule cancel requested", extra={"job_id": job_id,
                                                  "runtime_ms": round((time.perf_counter() - t0) * 1000, 2)})
    return {"job_id": job_id, "state": "cancelling",
            "ack_ms": round((time.perf_counter() - t0) * 1000, 2)}


@app.get("/api/v1/schedules/{job_id}/results", tags=["scheduling"])
async def schedule_results(job_id: str, limit: int = 60, offset: int = 0):
    j = SCHED_MANAGER.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job_id")
    if j.state == "cancelled":
        raise HTTPException(409, "job was cancelled; partial results are never returned as complete")
    if j.state == "failed":
        raise HTTPException(500, j.error or "schedule search failed")
    if j.state == "expired":
        raise HTTPException(410, "result has expired")
    if j.state != "completed" or j.result is None:
        raise HTTPException(409, f"job is {j.state}; result not ready")
    limit = min(500, max(1, limit))
    offset = max(0, offset)
    page = j.result["schedules"][offset:offset + limit]
    out = {
        "schedules": page, "total_found": j.result["total_found"],
        "truncated": j.result["truncated"], "nodes": j.result["nodes"],
        "sort": j.result["sort"], "item_order": j.result["item_order"],
        "mode": j.result.get("mode", "exact"), "clash_count": j.result.get("clash_count", 0),
        "offset": offset, "limit": limit,
        "next_offset": offset + limit if offset + limit < j.result["total_found"] else None,
        "cache_hit": j.cache_hit,
    }
    # wishlist mode (mode == "optimized") carries extra fields the shortlist
    # path never sets - pass them through only when present rather than
    # padding every response with nulls the legacy path never produces.
    for k in ("cp_status", "total_credits", "fixed_credits", "included", "excluded",
              "min_relaxed", "why_not", "credit_min", "credit_target", "credit_max"):
        if k in j.result:
            out[k] = j.result[k]
    return out


@app.post("/api/v1/schedules/{job_id}/explain-exclusion", tags=["scheduling"])
async def schedule_explain_exclusion(job_id: str, body: dict):
    """On-demand why-not for one wishlist course (spec s.12): deliberately not
    computed eagerly for every excluded course in the main search (each check
    can cost up to a couple of extra CP-SAT solves), so this runs one at a
    time, off the event loop since it is CPU-bound."""
    j = SCHED_MANAGER.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job_id")
    if j.req is None or not j.req.get("wishlist"):
        raise HTTPException(409, "explain-exclusion only applies to a wishlist-mode search")
    if j.state != "completed" or j.result is None:
        raise HTTPException(409, f"job is {j.state}; result not ready")
    code = str(body.get("code") or "")
    if not code:
        raise HTTPException(422, "code is required")

    def run():
        from app.domain import catalog
        from app.services.cp_scheduler import SolveResult, WishChoiceGroup, WishItem, explain_omission
        from app.services.scheduler import PlacedMeeting

        req = j.req
        items = []
        for w in req["wishlist"]:
            fc = catalog.get_course(w["code"])
            items.append(WishItem(
                code=w["code"], packages=tuple(fc["pk"]), credits=float(fc.get("cr", 0)),
                intent=w.get("intent", "strong"), priority=int(w.get("priority", 5)),
                forced=w.get("intent") == "must_have", locked_package=w.get("locked_package"),
                excluded_packages=tuple(w.get("excluded_packages", [])),
            ))
        groups = [WishChoiceGroup(kind=g["kind"], members=tuple(g["members"]), min_credits=g.get("min_credits"))
                 for g in req.get("choice_groups", [])]
        fixed_meetings = []
        for f in req.get("fixed", []):
            if f.get("locked", True):
                fc = catalog.get_course(f["code"])
                pkg = fc["pk"][f["pkg"]]
                for m in pkg["m"]:
                    fixed_meetings.append(PlacedMeeting(m=tuple(m), term=pkg["t"], code=f["code"]))
        base = SolveResult(status=j.result.get("cp_status", "optimal"), assign={},
                           included=j.result.get("included", []), excluded=j.result.get("excluded", []),
                           total_credits=j.result.get("total_credits", 0))
        return explain_omission(code, items, fixed_meetings, j.result.get("fixed_credits", 0.0), groups,
                                req.get("credit_min") or 0.0, req.get("credit_target") or req["credit_max"],
                                req["credit_max"], base)

    return await asyncio.get_event_loop().run_in_executor(None, run)


@app.get("/api/v1/schedules/{job_id}/events", tags=["scheduling"])
async def schedule_events(job_id: str):
    """Server-Sent Events progress stream, same pattern as /simulations/{id}/events."""
    j = SCHED_MANAGER.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job_id")

    async def gen():
        last = None
        deadline = time.time() + 900
        while time.time() < deadline:
            cur = SCHED_MANAGER.get(job_id)
            if cur is None:
                break
            s = cur.status()
            packed = json.dumps(s)
            if packed != last:
                yield f"event: progress\ndata: {packed}\n\n"
                last = packed
            if s["state"] in ("completed", "failed", "cancelled", "expired"):
                yield f"event: done\ndata: {packed}\n\n"
                return
            await asyncio.sleep(0.12)
        yield 'event: done\ndata: {"state":"timeout"}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# A production deployment is one same-origin service. This mount must remain
# after every API declaration so it cannot shadow a later POST route.
_frontend_dir = Path(os.getenv(
    "SNU_FRONTEND_DIR",
    str(Path(__file__).resolve().parents[2] / "frontend" / "dist"),
))
if (_frontend_dir / "index.html").is_file():
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
