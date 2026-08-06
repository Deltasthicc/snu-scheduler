from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models.audit_schemas import AuditCourse, DegreeAuditRequest
from app.services.degree_audit import ProgrammeCatalog, audit_degree


CATALOG = ProgrammeCatalog(Path(__file__).parents[1] / "app" / "data" / "programs.json")


def course(code, credits, category):
    return AuditCourse(code=code, credits=credits, category=category)


def test_catalog_contains_every_current_official_program_entry():
    assert len(CATALOG.list()) == 44
    assert CATALOG.get("b-tech-in-computer-science-and-engineering") is not None
    assert all(p["official_page"].startswith("https://snu.edu.in/") for p in CATALOG.list())
    assert all(p.get("pathways", {}).get("kind") for p in CATALOG.list())
    assert all(p["pathways"].get("sources") for p in CATALOG.list())
    assert all(p["pathways"].get("options") for p in CATALOG.list())


def test_every_programme_pathway_choice_has_a_unique_stable_id():
    for programme in CATALOG.list():
        options = programme["pathways"]["options"]
        ids = [option["id"] for option in options]
        assert len(ids) == len(set(ids)), programme["id"]
        assert all(option.get("title") and option.get("type") for option in options), programme["id"]


def test_published_pathway_types_and_cse_options_are_not_conflated():
    cse = CATALOG.get("b-tech-in-computer-science-and-engineering")["pathways"]
    assert cse["kind"] == "formal_specialisation"
    assert {option["title"] for option in cse["options"]} == {
        "Artificial Intelligence and Machine Learning",
        "Data Science and Big Data Analytics",
        "Cyber Security and Privacy",
    }
    assert all(option["minimum_credits"] == 12 for option in cse["options"])

    design = CATALOG.get("bachelor-of-design")["pathways"]
    assert design["kind"] == "programme_stream"
    assert all(option["type"] == "stream" for option in design["options"])

    doctorate = CATALOG.get("ph-d-in-civil-engineering")["pathways"]
    assert doctorate["kind"] == "research_areas"
    assert all(option["type"] == "research_area" for option in doctorate["options"])


def test_every_catalogued_programme_can_run_an_audit():
    """A partial public curriculum may return no requirement rows, but selecting
    any listed programme must always produce a valid, honest response."""
    for program in CATALOG.list():
        result = audit_degree(DegreeAuditRequest(programme_id=program["id"]), CATALOG)
        assert result["programme"]["id"] == program["id"]
        assert result["coverage"] == program["verification"]
        assert result["requirements_total"] == len(program["requirements"])
        assert result["remaining_credits"] >= 0


def test_every_catalogued_programme_can_run_through_public_api():
    """The HTTP schema and route must accept every programme exposed by the UI."""
    with TestClient(app) as client:
        for program in CATALOG.list():
            response = client.post(
                "/api/v1/degree-audit",
                json={"programme_id": program["id"]},
            )
            assert response.status_code == 200, (program["id"], response.text)
            payload = response.json()
            assert payload["programme"]["id"] == program["id"]
            assert payload["coverage"] == program["verification"]


def test_catalog_requirement_ids_are_unique_and_numerically_valid():
    for program in CATALOG.list():
        requirements = program["requirements"]
        ids = [rule["id"] for rule in requirements]
        assert len(ids) == len(set(ids)), program["id"]
        for rule in requirements:
            assert rule.get("kind", "credits") in {"credits", "milestone"}, program["id"]
            if rule.get("kind", "credits") == "credits":
                assert rule["required"] > 0, (program["id"], rule["id"])


def test_overlapping_ccc_uwe_rules_count_without_double_allocation_bug():
    req = DegreeAuditRequest(
        programme_id="b-tech-in-computer-science-and-engineering",
        completed_courses=[
            course("CCC1", 18, "CCC"), course("UWE1", 18, "UWE"),
            course("FLEX", 6, "UWE"), course("CORE1", 20, "major_core"),
            course("CORE2", 20, "major_core"), course("CORE3", 21, "major_core"),
            course("ME", 15, "major_elective"), course("BS", 17, "basic_science"),
            course("ES", 13, "engineering_science"), course("PROJ", 12, "project"),
        ],
    )
    result = audit_degree(req, CATALOG)
    rows = {row["id"]: row for row in result["requirements"]}
    assert rows["ccc"]["completed"] == 18
    assert rows["uwe"]["completed"] == 24
    assert rows["ccc_uwe"]["completed"] == 42
    assert rows["total"]["completed"] == 160
    assert result["remaining_credits"] == 0


def test_private_aggregate_is_lower_bound_not_double_counted():
    req = DegreeAuditRequest(
        programme_id="b-tech-in-computer-science-and-engineering",
        completed_courses=[course("CCC1", 4, "CCC")],
        completed_requirement_credits={"total": 105, "ccc": 7},
    )
    result = audit_degree(req, CATALOG)
    rows = {row["id"]: row for row in result["requirements"]}
    assert rows["total"]["completed"] == 105
    assert rows["ccc"]["completed"] == 7
    assert result["aggregate_progress_applied"] is True
    assert any("aggregate/transfer" in warning for warning in result["warnings"])


def test_planned_courses_are_projected_but_not_marked_completed():
    req = DegreeAuditRequest(
        programme_id="b-tech-in-mechanical-engineering",
        completed_courses=[course(f"DONE{i}", 20, "major_core") for i in range(7)]
                          + [course("DONE8", 11, "major_core")],
        planned_courses=[course("NEXT", 9, "major_elective")],
    )
    result = audit_degree(req, CATALOG)
    total = next(row for row in result["requirements"] if row["id"] == "total")
    assert total["completed"] == 151
    assert total["planned"] == 9
    assert total["remaining_after_plan"] == 0


def test_partial_public_program_is_honest_and_accepts_private_override():
    empty = audit_degree(DegreeAuditRequest(programme_id="b-sc-research-in-economics"), CATALOG)
    assert empty["requirements_total"] == 0
    assert empty["warnings"]

    req = DegreeAuditRequest.model_validate({
        "programme_id": "b-sc-research-in-economics",
        "completed_milestones": ["coursework"],
        "custom_requirements": [
            {"id": "coursework", "label": "Required coursework", "kind": "milestone"},
            {"id": "thesis", "label": "Thesis submitted", "kind": "milestone"},
        ],
    })
    result = audit_degree(req, CATALOG)
    assert result["coverage"] == "profile_overrides_applied"
    assert result["requirements_met"] == 1
    assert result["requirements_total"] == 2
