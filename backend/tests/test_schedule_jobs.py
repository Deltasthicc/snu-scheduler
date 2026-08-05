"""Integration tests for ScheduleJobManager - the actual process-isolated
worker, not just the pure algorithm (see test_scheduler.py for that). These
submit real jobs, spawn real child processes, and assert on real timing, the
same standard already applied to the simulation JobManager in CLAUDE.md
(measured: 46ms worker stop time). A synthetic catalog is used so a job can be
made deliberately slow without depending on how compatible today's real
timetable data happens to be."""
from __future__ import annotations
import json
import os
import time

import pytest

from app.services.runner import input_hash
from app.workers.schedule_jobs import ScheduleJobManager


def _synthetic_catalog_path(tmp_path, n_courses=12, n_packages=6):
    """The first n_courses-1 get a globally-unique day+hour slot each, so they
    never conflict with each other and the search branches to full width. The
    last course ("BLOCKER", visited deepest since it has the most packages, per
    most-constrained-first ordering) spans every day all day, so it conflicts
    with everything and every branch fails only at the very last level. This
    forces sustained traversal up to the node budget with a bounded, realistic
    result count (zero), instead of also having to store/score millions of
    matches like an earlier draft of this fixture did (that ate 2GB of RAM)."""
    courses = []
    for c in range(n_courses - 1):
        pk = []
        for k in range(n_packages):
            slot = c * n_packages + k
            day, hour = slot % 6, slot // 6
            pk.append({"t": "Full semester", "l": f"p{k}",
                      "m": [[day, hour * 60, hour * 60 + 50, "LEC", "1", "R1"]]})
        courses.append({"code": f"SYN{c}", "pk": pk})
    blocker_pk = [{"t": "Full semester", "l": "p0" * (n_packages),
                  "m": [[d, 0, 1440, "LEC", "1", "R1"] for d in range(6)]}]
    # give BLOCKER the most packages (all identical, all-day) so it sorts last
    courses.append({"code": "BLOCKER", "pk": blocker_pk * (n_packages + 2)})
    p = tmp_path / "synthetic_catalog.json"
    p.write_text(json.dumps(courses), encoding="utf-8")
    return str(p)


@pytest.fixture
def slow_request(tmp_path, monkeypatch):
    path = _synthetic_catalog_path(tmp_path)
    monkeypatch.setenv("SNU_CATALOG_PATH", path)
    shortlist = [f"SYN{c}" for c in range(11)] + ["BLOCKER"]
    # calibrated directly (see chat/profiling): this shape costs ~69us/node since
    # conflict-checks against BLOCKER's day-spanning meetings happen at maximum
    # placed-meeting depth on almost every node. 50k nodes ~= 3-4s of real work,
    # enough to reliably observe "running" state and cancel mid-flight without
    # making the test suite slow.
    req = {"shortlist": shortlist, "fixed": [], "max_nodes": 50_000,
          "max_results": 300, "sort": "compact"}
    return req


@pytest.fixture
def manager():
    m = ScheduleJobManager()
    yield m
    m.shutdown()


def test_job_completes_with_correct_progress(manager, slow_request):
    h = input_hash(slow_request)
    job = manager.submit(slow_request, h)
    deadline = time.time() + 30
    while manager.get(job.job_id).state not in ("completed", "failed") and time.time() < deadline:
        time.sleep(0.1)
    final = manager.get(job.job_id)
    assert final.state == "completed", final.error
    assert final.result["nodes"] > 0
    assert final.result["truncated"] is True  # 3M budget can't fully explore 20x6 branching


def test_cancellation_is_acknowledged_fast_and_actually_stops_the_worker(manager, slow_request):
    h = input_hash(slow_request)
    job = manager.submit(slow_request, h)
    # give the worker a moment to actually start running (not still spawning)
    deadline = time.time() + 5
    while manager.get(job.job_id).state != "running" and time.time() < deadline:
        time.sleep(0.02)
    t0 = time.perf_counter()
    ok = manager.cancel(job.job_id)
    ack_ms = (time.perf_counter() - t0) * 1000
    assert ok is True
    assert ack_ms < 100, f"cancel() call itself took {ack_ms:.1f}ms - should be near-instant"

    stop_deadline = time.time() + 5
    while manager.get(job.job_id).state not in ("cancelled",) and time.time() < stop_deadline:
        time.sleep(0.02)
    final = manager.get(job.job_id)
    assert final.state == "cancelled"
    # the state flips to "cancelled" the instant the worker's out_q message is
    # drained, which can be a few ms before the OS process object itself
    # reports not-alive (interpreter teardown, queue feeder thread flush) -
    # poll briefly rather than asserting the exact same instant.
    proc_deadline = time.time() + 2
    while final.proc.is_alive() and time.time() < proc_deadline:
        time.sleep(0.01)
    assert not final.proc.is_alive(), "worker process did not exit within 2s of reporting cancelled"


def test_cancelled_job_never_later_reports_a_result(manager, slow_request):
    h = input_hash(slow_request)
    job = manager.submit(slow_request, h)
    manager.cancel(job.job_id)
    deadline = time.time() + 5
    while manager.get(job.job_id).state not in ("cancelled", "completed") and time.time() < deadline:
        time.sleep(0.02)
    final = manager.get(job.job_id)
    if final.state == "cancelled":
        assert final.result is None


def test_identical_request_is_a_cache_hit(manager, slow_request):
    slow_request["max_nodes"] = 5000  # small enough to actually finish (may still truncate)
    h = input_hash(slow_request)
    job1 = manager.submit(slow_request, h)
    deadline = time.time() + 10
    while manager.get(job1.job_id).state not in ("completed", "failed") and time.time() < deadline:
        time.sleep(0.05)
    assert manager.get(job1.job_id).state == "completed"

    job2 = manager.submit(slow_request, h)
    assert job2.cache_hit is True
    assert job2.state == "completed"


def test_unknown_job_id_returns_none(manager):
    assert manager.get("does-not-exist") is None


def test_cancel_after_completion_returns_false(manager, slow_request):
    slow_request["max_nodes"] = 5000
    h = input_hash(slow_request)
    job = manager.submit(slow_request, h)
    deadline = time.time() + 10
    while manager.get(job.job_id).state not in ("completed", "failed") and time.time() < deadline:
        time.sleep(0.05)
    assert manager.cancel(job.job_id) is False


def _tiny_conflicting_catalog_path(tmp_path):
    """A and B always clash on their only package; C has one package that
    clashes with A and one that's free - real, but small enough to resolve
    almost instantly, for tests that don't need the slow-search fixture."""
    courses = [
        {"code": "A", "pk": [{"t": "Full semester", "l": "a0", "m": [[0, 0, 50, "LEC", "1", "R1"]]}]},
        {"code": "B", "pk": [{"t": "Full semester", "l": "b0", "m": [[0, 0, 50, "LEC", "1", "R1"]]}]},
        {"code": "C", "pk": [{"t": "Full semester", "l": "c0", "m": [[0, 0, 50, "LEC", "1", "R1"]]},
                             {"t": "Full semester", "l": "c1", "m": [[2, 0, 50, "LEC", "1", "R1"]]}]},
    ]
    p = tmp_path / "tiny_catalog.json"
    p.write_text(json.dumps(courses), encoding="utf-8")
    return str(p)


def test_worker_falls_back_to_least_conflict_when_nothing_is_clash_free(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("SNU_CATALOG_PATH", _tiny_conflicting_catalog_path(tmp_path))
    req = {"shortlist": ["A", "B"], "fixed": [], "max_nodes": 1000, "max_results": 10,
          "sort": "compact", "allow_least_conflict": True}
    h = input_hash(req)
    job = manager.submit(req, h)
    deadline = time.time() + 10
    while manager.get(job.job_id).state not in ("completed", "failed") and time.time() < deadline:
        time.sleep(0.05)
    final = manager.get(job.job_id)
    assert final.state == "completed", final.error
    assert final.result["mode"] == "least_conflict"
    assert final.result["clash_count"] == 1
    assert final.result["total_found"] >= 1


def test_worker_treats_unlocked_fixed_course_as_a_search_item(manager, tmp_path, monkeypatch):
    # C is "fixed" but unlocked at its clashing package (c0); with A also in the
    # shortlist, a real solver must be free to swap C onto c1 to clear the clash -
    # a locked fixed course could never be moved this way.
    monkeypatch.setenv("SNU_CATALOG_PATH", _tiny_conflicting_catalog_path(tmp_path))
    req = {"shortlist": ["A"], "fixed": [{"code": "C", "pkg": 0, "locked": False}],
          "max_nodes": 1000, "max_results": 10, "sort": "compact", "allow_least_conflict": True}
    h = input_hash(req)
    job = manager.submit(req, h)
    deadline = time.time() + 10
    while manager.get(job.job_id).state not in ("completed", "failed") and time.time() < deadline:
        time.sleep(0.05)
    final = manager.get(job.job_id)
    assert final.state == "completed", final.error
    assert final.result["mode"] == "exact"
    assert final.result["total_found"] >= 1
    assert all(s["assign"]["C"] == 1 for s in final.result["schedules"])
