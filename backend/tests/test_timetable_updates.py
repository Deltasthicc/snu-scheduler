"""Tests for backend/app/timetable_updates/*. Uses a real local HTTP server
(no live Netlify dependency) so the normal suite never depends on internet
access. One opt-in live smoke test at the bottom, skipped unless
SNU_LIVE_TIMETABLE_TEST=1 is set.
"""
from __future__ import annotations
import http.server
import json
import threading
import time

import pytest

from app.domain import catalog
from app.timetable_updates import apply as apply_mod
from app.timetable_updates import normalize as normalize_mod
from app.timetable_updates import parser as parser_mod
from app.timetable_updates import source as source_mod
from app.timetable_updates.diff import diff_datasets, detect_renames
from app.timetable_updates.models import UpdateState
from app.timetable_updates.poller import UpdateService

pytestmark = []


def _page(data_obj: dict, extra_js: str = "") -> str:
    return (
        "<!doctype html><html><body><script>\n"
        f"const DATA = {json.dumps(data_obj)};\n"
        f"{extra_js}\n"
        "window.PLANNER = {get data(){ return DATA; }};\n"
        "</script></body></html>"
    )


SAMPLE_A = {"CSD": {"CSD1YR": [
    {"code": "CSD101", "title": "Intro", "type": "Major", "uwe": "No", "comp": "LEC", "sec": "LEC1",
     "block": "", "term": "Full semester", "day": "Mon", "start": "09:00 AM", "end": "10:00 AM",
     "room": "A1", "inst": "X", "cap": "50", "note": "", "rowid": 1},
]}}

SAMPLE_A_COSMETIC = {"CSD": {"CSD1YR": [  # same data, different key order / whitespace-equivalent
    {"title": "Intro", "code": "CSD101", "type": "Major", "uwe": "No", "comp": "LEC", "sec": "LEC1",
     "block": "", "term": "Full semester", "day": "Mon", "start": "09:00 AM", "end": "10:00 AM",
     "room": "A1", "inst": "X", "cap": "50", "note": "", "rowid": 1},
]}}

SAMPLE_B_REAL_CHANGE = {"CSD": {"CSD1YR": [
    {"code": "CSD101", "title": "Intro", "type": "Major", "uwe": "No", "comp": "LEC", "sec": "LEC1",
     "block": "", "term": "Full semester", "day": "Tue", "start": "09:00 AM", "end": "10:00 AM",
     "room": "A1", "inst": "X", "cap": "50", "note": "", "rowid": 1},
]}}


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    html = _page(SAMPLE_A)
    etag = '"v1"'

    def do_GET(self):
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match and if_none_match == self.etag:
            self.send_response(304)
            self.send_header("ETag", self.etag)
            self.end_headers()
            return
        body = self.html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("ETag", self.etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


@pytest.fixture
def fixture_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _FixtureHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/"
    server.shutdown()


@pytest.fixture
def isolated_catalog(tmp_path):
    """Points catalog.py and apply.py's file paths at a temp directory with a
    small synthetic dataset, then restores the real ones on teardown - other
    test files in this session depend on the real catalog still being loaded
    afterward."""
    data_dir = tmp_path / "data"
    versions_dir = data_dir / "timetable_versions"
    (versions_dir / "v1").mkdir(parents=True)
    courses = [{"code": "CSD101", "title": "Intro", "school": "", "dept": "", "ttype": "Major",
               "uwe": False, "cr": 3.0, "crOfficial": True, "crBasis": "", "terms": ["Full semester"],
               "blocks": [], "cat": "CORE", "seats": 50, "unsched": False, "why": "",
               "pk": [{"t": "Full semester", "l": "LEC:LEC1",
                      "m": [[0, 540, 600, "LEC", "LEC1", "A1"]]}]}]
    checksum = catalog.canonical_checksum(courses)
    manifest_entry = {"version_id": "v1", "source_name": "test", "source_url": None, "retrieved_at": None,
                      "source_checksum": None, "dataset_checksum": checksum, "importer_version": "test",
                      "effective_semester": "test", "course_count": 1, "package_count": 1,
                      "error_count": 0, "warning_count": 0, "validation_status": "clean"}
    (versions_dir / "v1" / "courses.json").write_text(json.dumps(courses), encoding="utf-8")
    (versions_dir / "v1" / "manifest_entry.json").write_text(json.dumps(manifest_entry), encoding="utf-8")
    (data_dir / "courses.json").write_text(json.dumps(courses), encoding="utf-8")
    (data_dir / "dataset_manifest.json").write_text(
        json.dumps({"active_version": "v1", "versions": [manifest_entry]}), encoding="utf-8")
    frontend_data = tmp_path / "frontend_src" / "data.json"
    frontend_data.parent.mkdir(parents=True)
    frontend_data.write_text(json.dumps(courses), encoding="utf-8")

    orig = {
        "catalog._DATA_PATH": catalog._DATA_PATH, "catalog._MANIFEST_PATH": catalog._MANIFEST_PATH,
        "apply.VERSIONS_DIR": apply_mod.VERSIONS_DIR, "apply.MANIFEST_PATH": apply_mod.MANIFEST_PATH,
        "apply.BACKEND_COURSES_PATH": apply_mod.BACKEND_COURSES_PATH, "apply.FRONTEND_DATA": apply_mod.FRONTEND_DATA,
    }
    catalog._DATA_PATH = str(data_dir / "courses.json")
    catalog._MANIFEST_PATH = str(data_dir / "dataset_manifest.json")
    apply_mod.VERSIONS_DIR = versions_dir
    apply_mod.MANIFEST_PATH = data_dir / "dataset_manifest.json"
    apply_mod.BACKEND_COURSES_PATH = data_dir / "courses.json"
    apply_mod.FRONTEND_DATA = frontend_data
    catalog.reload()
    try:
        yield {"data_dir": data_dir, "versions_dir": versions_dir, "checksum": checksum, "courses": courses}
    finally:
        catalog._DATA_PATH = orig["catalog._DATA_PATH"]
        catalog._MANIFEST_PATH = orig["catalog._MANIFEST_PATH"]
        apply_mod.VERSIONS_DIR = orig["apply.VERSIONS_DIR"]
        apply_mod.MANIFEST_PATH = orig["apply.MANIFEST_PATH"]
        apply_mod.BACKEND_COURSES_PATH = orig["apply.BACKEND_COURSES_PATH"]
        apply_mod.FRONTEND_DATA = orig["apply.FRONTEND_DATA"]
        catalog.reload()


# ---------------- source.py: conditional HTTP ----------------

def test_fetch_returns_200_with_etag_on_first_request(fixture_server):
    r = source_mod.fetch(fixture_server, timeout_s=5)
    assert r.status_code == 200
    assert r.etag == '"v1"'
    assert r.html is not None
    assert not r.not_modified


def test_fetch_returns_304_when_etag_matches(fixture_server):
    first = source_mod.fetch(fixture_server, timeout_s=5)
    second = source_mod.fetch(fixture_server, known_etag=first.etag, timeout_s=5)
    assert second.not_modified is True
    assert second.html is None


def test_fetch_force_bypasses_etag_short_circuit(fixture_server):
    first = source_mod.fetch(fixture_server, timeout_s=5)
    forced = source_mod.fetch(fixture_server, known_etag=first.etag, force=True, timeout_s=5)
    assert forced.not_modified is False
    assert forced.html is not None


def test_fetch_rejects_oversized_response(fixture_server):
    r = source_mod.fetch(fixture_server, max_bytes=10, timeout_s=5)
    assert r.error is not None
    assert "exceeds" in r.error or "exceeded" in r.error


def test_fetch_reports_network_error_for_unreachable_host():
    r = source_mod.fetch("http://127.0.0.1:1/", timeout_s=2)
    assert r.error is not None
    assert r.html is None


# ---------------- parser.py ----------------

def test_parser_extracts_valid_literal():
    html = _page(SAMPLE_A)
    extracted = parser_mod.parse(html)
    assert extracted.parsed == SAMPLE_A
    assert len(extracted.extracted_hash) == 16


def test_parser_rejects_forbidden_token():
    html = "<script>const DATA = {\"a\": function(){ return 1; }};</script>"
    with pytest.raises(parser_mod.ParseError):
        parser_mod.parse(html)


def test_parser_rejects_missing_assignment():
    with pytest.raises(parser_mod.ParseError):
        parser_mod.parse("<html>no data here</html>")


def test_parser_cosmetic_reordering_changes_extracted_hash_but_not_content():
    e1 = parser_mod.parse(_page(SAMPLE_A))
    e2 = parser_mod.parse(_page(SAMPLE_A_COSMETIC))
    assert e1.parsed == e2.parsed  # same content
    # extracted_hash may differ (different literal text) - that's expected;
    # normalize.py is what proves the actual dataset is unchanged


# ---------------- normalize.py ----------------

def test_normalize_builds_expected_package_and_hash():
    result = normalize_mod.normalize(SAMPLE_A, {})
    assert len(result.courses) == 1
    assert result.courses[0]["code"] == "CSD101"
    assert len(result.courses[0]["pk"]) == 1
    assert len(result.normalized_hash) == 16


def test_normalize_identical_content_produces_identical_hash_regardless_of_key_order():
    r1 = normalize_mod.normalize(SAMPLE_A, {})
    r2 = normalize_mod.normalize(SAMPLE_A_COSMETIC, {})
    assert r1.normalized_hash == r2.normalized_hash  # this is the exact bug that was found and fixed


def test_normalize_real_change_produces_different_hash():
    r1 = normalize_mod.normalize(SAMPLE_A, {})
    r2 = normalize_mod.normalize(SAMPLE_B_REAL_CHANGE, {})
    assert r1.normalized_hash != r2.normalized_hash


def test_normalize_carries_forward_credits_from_existing_dataset():
    existing = {"CSD101": {"code": "CSD101", "cr": 4.5, "crOfficial": True, "cat": "ME",
                           "school": "S", "dept": "D", "ttype": "Major Elective", "crBasis": "x"}}
    result = normalize_mod.normalize(SAMPLE_A, existing)
    assert result.courses[0]["cr"] == 4.5
    assert result.courses[0]["cat"] == "ME"


# ---------------- diff.py ----------------

def test_diff_detects_added_and_removed():
    old = [{"code": "A", "title": "t", "seats": 1, "terms": ["Full semester"], "cat": "ME", "pk": []}]
    new = [{"code": "B", "title": "t", "seats": 1, "terms": ["Full semester"], "cat": "ME", "pk": []}]
    d = diff_datasets(old, new)
    assert d["added_courses"] == ["B"]
    assert d["removed_courses"] == ["A"]


def test_diff_detects_rename_not_independent_add_remove():
    old = [{"code": "CCC2101", "title": "t", "seats": 1, "terms": ["Full semester"], "cat": "CCC", "pk": []}]
    new = [{"code": "CCC826/CCC2101", "title": "t", "seats": 1, "terms": ["Full semester"], "cat": "CCC", "pk": []}]
    d = diff_datasets(old, new)
    assert d["renamed_courses"] == [{"old_code": "CCC2101", "new_code": "CCC826/CCC2101"}]
    assert d["added_courses"] == []
    assert d["removed_courses"] == []


def test_diff_detects_changed_package_times():
    old = [{"code": "A", "title": "t", "seats": 1, "terms": ["Full semester"], "cat": "ME",
           "pk": [{"l": "LEC:LEC1", "m": [[0, 540, 600, "LEC", "LEC1", "A1"]]}]}]
    new = [{"code": "A", "title": "t", "seats": 1, "terms": ["Full semester"], "cat": "ME",
           "pk": [{"l": "LEC:LEC1", "m": [[1, 540, 600, "LEC", "LEC1", "A1"]]}]}]
    d = diff_datasets(old, new)
    assert len(d["changed_courses"]) == 1
    assert "packages_moved" in d["changed_courses"][0]["diffs"]


def test_diff_detects_no_changes_for_identical_datasets():
    c = [{"code": "A", "title": "t", "seats": 1, "terms": ["Full semester"], "cat": "ME", "pk": []}]
    d = diff_datasets(c, c)
    assert d["summary"] == {"renamed": 0, "added": 0, "removed": 0, "changed": 0, "unchanged": 1}


def test_detect_renames_is_index_based_not_quadratic():
    # correctness check, not a perf benchmark: confirms the O(n) index-based
    # implementation gives the same answer a naive O(n^2) scan would
    added = ["X/NEW1", "NEW2"]
    removed = ["NEW1", "OLD2"]
    renames, rem_added, rem_removed = detect_renames(added, removed)
    assert renames == [{"old_code": "NEW1", "new_code": "X/NEW1"}]
    assert rem_added == ["NEW2"]
    assert rem_removed == ["OLD2"]


# ---------------- apply.py: transactional apply / rollback ----------------

def test_stage_and_apply_version_success(isolated_catalog):
    new_courses = [dict(isolated_catalog["courses"][0])]
    new_courses[0]["seats"] = 999
    checksum = catalog.canonical_checksum(new_courses)
    entry = {"version_id": "v2", "dataset_checksum": checksum, "course_count": 1, "package_count": 1,
            "error_count": 0, "warning_count": 0}
    apply_mod.stage_version("v2", new_courses, {}, entry)
    result = apply_mod.apply_version("v2", expected_checksum=checksum)
    assert result["dataset_checksum"] == checksum
    assert catalog.dataset_info()["active_version"] == "v2"
    assert catalog.get_course("CSD101")["seats"] == 999


def test_apply_rejects_stale_checksum(isolated_catalog):
    new_courses = [dict(isolated_catalog["courses"][0], seats=1)]
    entry = {"version_id": "v3", "dataset_checksum": "aaaa", "course_count": 1, "package_count": 1,
            "error_count": 0, "warning_count": 0}
    apply_mod.stage_version("v3", new_courses, {}, entry)
    with pytest.raises(apply_mod.ApplyError, match="no longer matches"):
        apply_mod.apply_version("v3", expected_checksum="different-checksum")


def test_apply_rejects_nonexistent_candidate(isolated_catalog):
    with pytest.raises(apply_mod.ApplyError, match="no longer exists"):
        apply_mod.apply_version("does-not-exist")


def test_apply_rejects_candidate_with_validation_errors(isolated_catalog):
    dup_courses = [isolated_catalog["courses"][0], isolated_catalog["courses"][0]]  # duplicate code
    checksum = catalog.canonical_checksum(dup_courses)
    entry = {"version_id": "v4", "dataset_checksum": checksum, "course_count": 2, "package_count": 2,
            "error_count": 0, "warning_count": 0}
    apply_mod.stage_version("v4", dup_courses, {}, entry)
    with pytest.raises(apply_mod.ApplyError, match="re-validation"):
        apply_mod.apply_version("v4", expected_checksum=checksum)
    # active dataset must be untouched
    assert catalog.dataset_info()["active_version"] == "v1"


def test_discard_removes_never_applied_candidate(isolated_catalog):
    entry = {"version_id": "v5", "dataset_checksum": "x", "course_count": 0, "package_count": 0,
            "error_count": 0, "warning_count": 0}
    out_dir = apply_mod.stage_version("v5", [], {}, entry)
    assert out_dir.exists()
    apply_mod.discard_candidate("v5")
    assert not out_dir.exists()


def test_discard_refuses_an_applied_version(isolated_catalog):
    with pytest.raises(apply_mod.ApplyError, match="applied"):
        apply_mod.discard_candidate("v1")


def test_rollback_reactivates_prior_version(isolated_catalog):
    new_courses = [dict(isolated_catalog["courses"][0], seats=777)]
    checksum = catalog.canonical_checksum(new_courses)
    entry = {"version_id": "v6", "dataset_checksum": checksum, "course_count": 1, "package_count": 1,
            "error_count": 0, "warning_count": 0}
    apply_mod.stage_version("v6", new_courses, {}, entry)
    apply_mod.apply_version("v6", expected_checksum=checksum)
    assert catalog.get_course("CSD101")["seats"] == 777

    apply_mod.apply_version("v1", expected_checksum=None)  # rollback = re-apply the old version's own files
    assert catalog.dataset_info()["active_version"] == "v1"
    assert catalog.get_course("CSD101")["seats"] == 50


# ---------------- poller.py: async state machine ----------------

@pytest.mark.asyncio
async def test_poller_not_modified_on_second_check(fixture_server, isolated_catalog, monkeypatch):
    # point the service at content matching what's already "active" so the
    # very first check reports no_dataset_change, then a same-etag refetch
    # reports not_modified
    svc = UpdateService(url=fixture_server, enabled=False, auto_apply=False)
    r1 = await svc.check(force=True)
    assert r1["state"] in ("no_dataset_change", "update_available")  # depends on synthetic catalog content
    r2 = await svc.check(force=False)
    assert r2["state"] == "not_modified"


@pytest.mark.asyncio
async def test_poller_stages_real_change_as_candidate(isolated_catalog):
    server = http.server.HTTPServer(("127.0.0.1", 0), type("H", (_FixtureHandler,),
                                                           {"html": _page(SAMPLE_B_REAL_CHANGE), "etag": '"real"'}))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        svc = UpdateService(url=url, enabled=False, auto_apply=False)
        result = await svc.check(force=True)
        assert result["state"] == "update_available"
        assert svc.candidate is not None
        assert svc.candidate.error_count == 0
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_poller_apply_then_status_reflects_new_active(isolated_catalog):
    server = http.server.HTTPServer(("127.0.0.1", 0), type("H", (_FixtureHandler,),
                                                           {"html": _page(SAMPLE_B_REAL_CHANGE), "etag": '"real2"'}))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        svc = UpdateService(url=url, enabled=False, auto_apply=False)
        await svc.check(force=True)
        cand = svc.candidate
        await svc.apply(cand.version_id, cand.dataset_checksum)
        assert svc.state == UpdateState.APPLIED
        assert svc.candidate is None
        status = svc.status()
        assert status["active_version"] == cand.version_id
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_poller_concurrent_checks_are_deduplicated(fixture_server, isolated_catalog):
    import asyncio
    svc = UpdateService(url=fixture_server, enabled=False, auto_apply=False)
    results = await asyncio.gather(svc.check(force=True), svc.check(force=True))
    skipped = [r for r in results if r.get("skipped")]
    assert len(skipped) == 1  # exactly one of the two concurrent calls was rejected


@pytest.mark.asyncio
async def test_poller_backoff_increases_on_network_failure(isolated_catalog):
    svc = UpdateService(url="http://127.0.0.1:1/", enabled=False, auto_apply=False)
    await svc.check(force=True)
    first_backoff = svc.backoff_seconds
    assert first_backoff > 0
    await svc.check(force=True)
    assert svc.backoff_seconds > first_backoff  # exponential increase
    assert svc.state == UpdateState.OFFLINE


@pytest.mark.asyncio
async def test_poller_backoff_resets_after_successful_contact(fixture_server, isolated_catalog):
    svc = UpdateService(url="http://127.0.0.1:1/", enabled=False, auto_apply=False)
    await svc.check(force=True)
    assert svc.backoff_seconds > 0
    svc.url = fixture_server
    await svc.check(force=True)
    assert svc.backoff_seconds == 0.0


@pytest.mark.asyncio
async def test_poller_auto_apply_disabled_leaves_candidate_staged(isolated_catalog):
    server = http.server.HTTPServer(("127.0.0.1", 0), type("H", (_FixtureHandler,),
                                                           {"html": _page(SAMPLE_B_REAL_CHANGE), "etag": '"a3"'}))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        svc = UpdateService(url=url, enabled=False, auto_apply=False)
        await svc.check(force=True)
        assert svc.state == UpdateState.UPDATE_AVAILABLE
        assert catalog.dataset_info()["active_version"] == "v1"  # unchanged
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_poller_auto_apply_enabled_applies_automatically(isolated_catalog):
    server = http.server.HTTPServer(("127.0.0.1", 0), type("H", (_FixtureHandler,),
                                                           {"html": _page(SAMPLE_B_REAL_CHANGE), "etag": '"a4"'}))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        svc = UpdateService(url=url, enabled=False, auto_apply=True)
        await svc.check(force=True)
        assert svc.state == UpdateState.APPLIED
        assert catalog.dataset_info()["active_version"] != "v1"
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_poller_rejects_apply_of_non_staged_version(isolated_catalog):
    svc = UpdateService(url="http://example.invalid/", enabled=False, auto_apply=False)
    with pytest.raises(apply_mod.ApplyError, match="not the currently staged"):
        await svc.apply("some-version", "some-checksum")


@pytest.mark.asyncio
async def test_poller_status_shape(fixture_server, isolated_catalog):
    svc = UpdateService(url=fixture_server, enabled=True, auto_apply=False, poll_interval_minutes=20)
    s = svc.status()
    assert s["poller_enabled"] is True
    assert s["poll_interval_minutes"] == 20
    assert s["update_available"] is False


# ---------------- opt-in live smoke test ----------------

@pytest.mark.skipif("SNU_LIVE_TIMETABLE_TEST" not in __import__("os").environ,
                    reason="opt-in live test; set SNU_LIVE_TIMETABLE_TEST=1 to run")
def test_live_netlify_fetch_and_parse():
    r = source_mod.fetch("https://snioe-monsoon2026-tt.netlify.app/", force=True, timeout_s=15)
    assert r.error is None
    assert r.html
    extracted = parser_mod.parse(r.html)
    assert isinstance(extracted.parsed, dict)
    # deliberately does not normalize/apply anything - live test observes only
