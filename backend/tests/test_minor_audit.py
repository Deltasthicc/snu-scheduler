from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models.audit_schemas import AuditCourse
from app.models.minor_schemas import MinorAuditRequest, MinorOverviewRequest
from app.services.minor_audit import MinorCatalog, audit_minor, minors_overview


CATALOG = MinorCatalog(Path(__file__).parents[1] / "app" / "data" / "minors.json")


def course(code, credits, category="university_wide_elective"):
    return AuditCourse(code=code, credits=credits, category=category)


def test_catalog_contains_all_21_published_minors():
    assert len(CATALOG.list()) == 21
    assert CATALOG.get("minor-chemical-engineering") is not None
    assert all(minor.get("sources") for minor in CATALOG.list())
    assert all(minor.get("open_to_note") for minor in CATALOG.list())


def test_every_minor_pathway_requirement_is_well_formed():
    for minor in CATALOG.list():
        assert minor["pathways"], minor["id"]
        for pathway in minor["pathways"]:
            ids = [rule["id"] for rule in pathway["requirements"]]
            assert len(ids) == len(set(ids)), (minor["id"], pathway["id"])
            for rule in pathway["requirements"]:
                assert rule.get("kind") in {"credits", "course_count", "manual_confirmation"}, (minor["id"], rule["id"])
                if rule["kind"] != "manual_confirmation":
                    assert rule.get("course_codes") or rule.get("categories"), (minor["id"], rule["id"], "no matchable codes")


def test_every_minor_audits_cleanly_with_zero_input():
    """The same invariant test_degree_audit.py pins for programmes: nothing is
    ever silently "complete" on a completely empty request, across every
    pathway of every minor."""
    for minor in CATALOG.list():
        for pathway in minor["pathways"]:
            request = MinorAuditRequest(minor_id=minor["id"], pathway_id=pathway["id"])
            result = audit_minor(request, CATALOG)
            assert result["requirements_met"] == 0, (minor["id"], pathway["id"])
            for row in result["requirements"]:
                if row["status"] == "needs_confirmation":
                    continue
                assert row["completed"] == 0, (minor["id"], pathway["id"], row["id"])
                assert row["status"] != "complete", (minor["id"], pathway["id"], row["id"])


def test_a_minor_is_closed_to_students_already_majoring_in_it():
    result = audit_minor(MinorAuditRequest(
        minor_id="minor-computer-science-and-engineering",
        major_programme_id="b-tech-in-computer-science-and-engineering",
    ), CATALOG)
    assert result["eligibility"]["eligible"] is False
    assert "closed" in result["eligibility"]["reason"].lower()


def test_a_minor_is_open_to_a_different_major():
    result = audit_minor(MinorAuditRequest(
        minor_id="minor-computer-science-and-engineering",
        major_programme_id="b-tech-in-mechanical-engineering",
    ), CATALOG)
    assert result["eligibility"]["eligible"] is True


def test_pathway_auto_selects_from_major_when_unambiguous():
    result = audit_minor(MinorAuditRequest(
        minor_id="minor-computer-science-and-engineering",
        major_programme_id="b-tech-in-electrical-and-computer-engineering",
    ), CATALOG)
    assert result["pathway_required"] is False
    assert result["pathway"]["id"] == "pathway-b"


def test_pathway_is_asked_for_rather_than_guessed_without_a_major():
    result = audit_minor(MinorAuditRequest(minor_id="minor-computer-science-and-engineering"), CATALOG)
    assert result["pathway_required"] is True
    assert {opt["id"] for opt in result["pathway_options"]} == {"pathway-a", "pathway-b"}
    assert result["requirements"] == []


def test_cse_minor_pathway_a_needs_the_compulsory_course_and_four_electives():
    result = audit_minor(MinorAuditRequest(
        minor_id="minor-computer-science-and-engineering", pathway_id="pathway-a",
        completed_courses=[course("CSD102", 4), course("CSD204", 4), course("CSD205", 4),
                           course("CSD211", 5), course("CSD213", 4)],
    ), CATALOG)
    rows = {row["id"]: row for row in result["requirements"]}
    assert rows["compulsory"]["status"] == "complete"
    assert rows["basket"]["completed"] == 4
    assert rows["basket"]["status"] == "complete"
    assert result["requirements_met"] == 2


def test_cse_minor_pathway_b_needs_both_course_count_and_credit_floor():
    """"Any five courses ... to earn a minimum of 18 credits" is two
    independent predicates over the same basket, not one - five 3-credit
    electives would satisfy the count but not the credit floor."""
    five_light_electives = [course("CSD-ELECTIVE-A", 3), course("CSD-ELECTIVE-B", 3),
                            course("CSD-ELECTIVE-C", 3), course("CSD-ELECTIVE-D", 3),
                            course("CSD-ELECTIVE-E", 3)]
    # None of these match the real basket codes, so this should show 0 progress
    # on both predicates - confirms the matcher isn't accidentally permissive.
    empty = audit_minor(MinorAuditRequest(
        minor_id="minor-computer-science-and-engineering", pathway_id="pathway-b",
        completed_courses=five_light_electives), CATALOG)
    rows = {row["id"]: row for row in empty["requirements"]}
    assert rows["basket"]["completed"] == 0
    assert rows["basket-credits"]["completed"] == 0

    five_real = [course("CSD204", 4), course("CSD205", 4), course("CSD213", 4),
                course("CSD317", 4), course("CSD319", 4)]
    full = audit_minor(MinorAuditRequest(
        minor_id="minor-computer-science-and-engineering", pathway_id="pathway-b",
        completed_courses=five_real), CATALOG)
    rows = {row["id"]: row for row in full["requirements"]}
    assert rows["basket"]["completed"] == 5 and rows["basket"]["status"] == "complete"
    assert rows["basket-credits"]["completed"] == 20 and rows["basket-credits"]["status"] == "complete"


def test_ece_minor_mandatory_ece101_only_counts_for_pathways_where_it_is_mandatory():
    """ECE101 is a mandatory, credit-counting course for Civil/Chemical and
    Non-Engineering pathways, but only a pre-requisite (not counted) for
    Mechanical and CSE. A Mechanical-pathway audit must not silently credit it."""
    mech = audit_minor(MinorAuditRequest(
        minor_id="minor-electrical-and-computer-engineering", pathway_id="mechanical",
        completed_courses=[course("ECE101", 5)]), CATALOG)
    assert mech["requirements"][0]["completed"] == 0, "ECE101 is a pre-requisite here, not a counted course"

    civil_chem = audit_minor(MinorAuditRequest(
        minor_id="minor-electrical-and-computer-engineering", pathway_id="civil-chemical",
        completed_courses=[course("ECE101", 5)]), CATALOG)
    rows = {row["id"]: row for row in civil_chem["requirements"]}
    assert rows["mandatory"]["completed"] == 5 and rows["mandatory"]["status"] == "complete"


def test_open_ended_baskets_are_disclosed_not_fabricated():
    """Sociology, pre-2025 English, IR electives, Biotechnology's optional
    basket, Chemistry's Category C, and Mathematics' "other MAT200+" bucket
    all publish a shape but not an enumerable course list. These must report
    needs_confirmation, never a computed - and therefore invented - number."""
    for minor_id, pathway_id in [
        ("minor-sociology", "default"), ("minor-english", "default"),
        ("minor-international-relations", "default"), ("minor-biotechnology", "default"),
        ("minor-chemistry", "default"), ("minor-mathematics", "default"),
    ]:
        result = audit_minor(MinorAuditRequest(minor_id=minor_id, pathway_id=pathway_id), CATALOG)
        manual_rows = [row for row in result["requirements"] if row["kind"] == "manual_confirmation"]
        assert manual_rows, (minor_id, "expected at least one manual_confirmation row")
        for row in manual_rows:
            assert row["status"] == "needs_confirmation"
            assert row["completed"] is None
            assert row["note"], (minor_id, row["id"], "manual_confirmation row must explain why")


def test_http_contract_rejects_unknown_minor_id():
    with TestClient(app) as client:
        response = client.post("/api/v1/minors/audit", json={"minor_id": "not-a-real-minor"})
        assert response.status_code == 422

        ok = client.post("/api/v1/minors/audit", json={"minor_id": "minor-chemical-engineering"})
        assert ok.status_code == 200, ok.text


def test_http_contract_lists_and_serves_the_full_catalogue():
    with TestClient(app) as client:
        listing = client.get("/api/v1/minors")
        assert listing.status_code == 200
        assert len(listing.json()["minors"]) == 21

        detail = client.get("/api/v1/minors/minor-physics")
        assert detail.status_code == 200
        assert detail.json()["title"] == "Minor in Physics"

        missing = client.get("/api/v1/minors/does-not-exist")
        assert missing.status_code == 404


def test_overview_sweeps_every_minor_for_a_given_major():
    result = minors_overview(MinorOverviewRequest(major_programme_id="b-tech-in-computer-science-and-engineering"), CATALOG)
    assert result["total_count"] == 21
    by_id = {row["id"]: row for row in result["minors"]}
    assert by_id["minor-computer-science-and-engineering"]["eligibility"]["eligible"] is False
    assert by_id["minor-mechanical-engineering"]["eligibility"]["eligible"] is True
    # ECE minor's CSE basket should auto-resolve without asking for a pathway.
    assert by_id["minor-electrical-and-computer-engineering"]["pathway_required"] is False
    assert by_id["minor-electrical-and-computer-engineering"]["pathway"]["id"] == "cse"


def test_overview_flags_ambiguous_pathways_rather_than_guessing():
    result = minors_overview(MinorOverviewRequest(major_programme_id=None), CATALOG)
    by_id = {row["id"]: row for row in result["minors"]}
    assert by_id["minor-computer-science-and-engineering"]["pathway_required"] is True
