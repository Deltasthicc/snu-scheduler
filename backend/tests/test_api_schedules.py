"""HTTP-level integration tests for the schedule-search endpoints, through the
real ASGI app (FastAPI TestClient), not just the JobManager/worker classes
directly. This is the layer test_schedule_jobs.py's tests do NOT cover: they
call ScheduleJobManager directly and read job.result in-process, which means a
route handler that silently drops a field from its response dict (exactly
what happened here: main.py's /results endpoint was never updated to include
mode/clash_count when search_with_fallback added them) passes every one of
those tests while being broken for every real caller. Caught only by actually
going through the HTTP layer end to end."""
from __future__ import annotations
import time

from fastapi.testclient import TestClient

from app.main import app


def _poll_until_done(client, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/v1/schedules/{job_id}").json()
        if s["state"] in ("completed", "failed", "cancelled", "expired"):
            return s
        time.sleep(0.1)
    raise TimeoutError(f"job {job_id} did not finish in time")


def test_results_endpoint_reports_least_conflict_mode_and_clash_count():
    # CCC826/CCC2101 (renamed from CCC2101 in the 2026-08-04 timetable revision -
    # see docs/TIMETABLE_REVISION_DIFF_2026-08-04.md) and CCC2116 both meet Fri
    # 675-765 (First half vs Full semester - terms overlap), a genuinely
    # unavoidable clash, re-verified directly against the revised dataset.
    with TestClient(app) as client:
        req = {"shortlist": ["CCC826/CCC2101", "CCC2116"], "fixed": [], "max_nodes": 2_000_000,
              "max_results": 300, "sort": "compact", "allow_least_conflict": True}
        r = client.post("/api/v1/schedules/search", json=req)
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        status = _poll_until_done(client, job_id)
        assert status["state"] == "completed", status

        results = client.get(f"/api/v1/schedules/{job_id}/results").json()
        assert results["mode"] == "least_conflict", results
        assert results["clash_count"] == 1
        assert results["total_found"] == 1


def test_results_endpoint_reports_exact_mode_when_clash_free():
    # ART202/AMP1001 (Mon+Wed 13:00-14:55) and BIO1001 do not clash - real
    # catalog courses cross-verified in this session's data audit.
    with TestClient(app) as client:
        req = {"shortlist": ["ART202/AMP1001", "BIO1001"], "fixed": [], "max_nodes": 2_000_000,
              "max_results": 300, "sort": "compact", "allow_least_conflict": True}
        r = client.post("/api/v1/schedules/search", json=req)
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        status = _poll_until_done(client, job_id)
        assert status["state"] == "completed", status

        results = client.get(f"/api/v1/schedules/{job_id}/results").json()
        assert results["mode"] == "exact", results
        assert results["clash_count"] == 0
        assert results["total_found"] >= 1


def test_dataset_endpoint_reports_active_version_and_checksum():
    with TestClient(app) as client:
        r = client.get("/api/v1/dataset")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["course_count"] == 325  # see test_catalog.py::test_catalog_loads_325_courses
        assert body["dataset_checksum"]
        assert body["active_version"] in body["known_versions"]


def test_profiles_validate_returns_a_clear_summary():
    with TestClient(app) as client:
        r = client.post("/api/v1/profiles/validate", json={
            "credit_policy": {"fixed_credits": 15, "personal_target": 22, "min_credits": 18}
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ceiling_mode"] == "standard"
        assert body["active_ceiling"] == 25
        assert "15 fixed credits" in body["summary"]


def test_profiles_validate_rejects_ceiling_below_fixed_credits():
    with TestClient(app) as client:
        r = client.post("/api/v1/profiles/validate", json={
            "credit_policy": {"fixed_credits": 28, "personal_target": 20, "min_credits": 10}
        })
        assert r.status_code == 422, r.text


def test_wishlists_validate_rejects_unknown_course_code():
    with TestClient(app) as client:
        r = client.post("/api/v1/wishlists/validate", json={
            "items": [{"code": "NOT-A-REAL-COURSE"}], "fixed_credits": 0
        })
        assert r.status_code == 422, r.text


def test_wishlists_validate_reports_summary_for_real_courses():
    with TestClient(app) as client:
        r = client.post("/api/v1/wishlists/validate", json={
            "items": [{"code": "CCC826/CCC2101", "intent": "must_have"},
                     {"code": "CCC2116", "intent": "strong"}],
            "fixed_credits": 15,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 2
        assert body["num_must_have"] == 1


def test_schedule_search_requires_credit_max_when_wishlist_present():
    with TestClient(app) as client:
        r = client.post("/api/v1/schedules/search", json={
            "shortlist": [], "wishlist": [{"code": "CCC826/CCC2101"}],
        })
        assert r.status_code == 422, r.text


def test_wishlist_search_optimized_mode_and_explain_exclusion():
    with TestClient(app) as client:
        req = {
            "shortlist": [], "fixed": [],
            "wishlist": [{"code": "CCC826/CCC2101", "intent": "must_have"},
                        {"code": "CCC2116", "intent": "strong"}],
            "credit_min": 0, "credit_target": 10, "credit_max": 25,
            "max_nodes": 1000, "max_results": 10,
        }
        r = client.post("/api/v1/schedules/search", json=req)
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        status = _poll_until_done(client, job_id)
        assert status["state"] == "completed", status

        results = client.get(f"/api/v1/schedules/{job_id}/results").json()
        assert results["mode"] == "optimized"
        assert "CCC826/CCC2101" in results["included"]  # must-have, and the genuine clash partner is excluded
        assert "CCC2116" in results["excluded"]
        assert results["why_not"], "why_not should already explain the eagerly-checked exclusion"

        ex = client.post(f"/api/v1/schedules/{job_id}/explain-exclusion", json={"code": "CCC2116"})
        assert ex.status_code == 200, ex.text
        assert ex.json()["blocker"] in ("no_valid_combination", "time_clash_with_fixed")


def test_explain_exclusion_404s_on_unknown_job():
    with TestClient(app) as client:
        r = client.post("/api/v1/schedules/doesnotexist/explain-exclusion", json={"code": "CCC826/CCC2101"})
        assert r.status_code == 404


def test_explain_exclusion_409s_for_a_non_wishlist_job():
    with TestClient(app) as client:
        req = {"shortlist": ["ART202/AMP1001"], "fixed": [], "max_nodes": 2_000_000,
              "max_results": 10, "sort": "compact"}
        r = client.post("/api/v1/schedules/search", json=req)
        job_id = r.json()["job_id"]
        _poll_until_done(client, job_id)
        ex = client.post(f"/api/v1/schedules/{job_id}/explain-exclusion", json={"code": "ART202/AMP1001"})
        assert ex.status_code == 409
