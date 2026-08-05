import base64

import pytest
from fastapi.testclient import TestClient

from app.services import advisement_report as ar
from app import main as main_module


SAMPLE = """
DISCLAIMER: THE ADVISEMENT REPORT IS AN INTERNAL TOOL FOR PLANNING A STUDENT'S COURSE REGISTRATION
Advisement Report-Roll No:123456
For TEST STUDENT prepared on 08/05/2026
Bachelor of Technology Program Monsoon 2023 Not Satisfied
C.G.P.A.: 6.490
Bachelor of Technology in Computer Science and Engineering
Not Satisfied: Requirements
Units: 160.00 required, 35.00 used, 125.00 needed
Major Requirements
BTECH-CSD MAJOR CORE
Units: 61.00 required, 23.00 used, 38.00 needed
Courses Used
MONS 2025 CSD CSD319 Design and Anal. of Algorithms A- 4.00 EN
SPRG 2026 CSD CSD334 Theory of Computation A- 3.00 EN
Courses Available
CSD CSD317
BTECH-CSD MAJOR ELECTIVES
Units: 15.00 required, 0.00 used, 15.00 needed
BTECH-CSD MAJOR PROJECT
Units: 12.00 required, 0.00 used, 12.00 needed
Basic Sciences (BS)
Units: 17.00 required, 4.00 used, 13.00 needed
Courses Used
SPRG 2026 MAT MAT161 Applied Linear Algebra A- 4.00 EN
Engineering Science (ES)
Units: 13.00 required, 0.00 used, 13.00 needed
Core Common Curriculum (CCC)
Units: 18.00 required, 4.00 used, 14.00 needed
Courses Used
SPRG 2026 CCC CCC704 Environmental Studies B 4.00 EN
UWE Requirements
Units: 18.00 required, 4.00 used, 14.00 needed
Courses Used
SPRG 2026 CSD CSD366 Introduction to Reinforcement. B 4.00 EN
CCC and UWE combined requirement
Units: 42.00 required, 8.00 used, 34.00 needed
Course History
MONS 2025 SWE SWE133 Tribal Society F 4.00 EN
MONS 2025 MED MED201 Materials Science & Engg. F* 4.00 EN
SUMR 2026 CSD CSD204 Operating Systems IP 4.00 IP
"""


def _fake_pdf(monkeypatch):
    monkeypatch.setattr(ar, "_extract_text", lambda _raw: (SAMPLE, 6))
    return base64.b64encode(b"%PDF-test").decode()


def test_parser_builds_completed_courses_and_profile(monkeypatch):
    result = ar.parse_advisement_report(_fake_pdf(monkeypatch), "report.pdf")
    assert result["programme_id"] == "b-tech-in-computer-science-and-engineering"
    assert result["totals"] == {"required": 160.0, "used": 35.0, "needed": 125.0}
    assert {c["code"] for c in result["completed_courses"]} == {
        "CSD319", "CSD334", "MAT161", "CCC704", "CSD366"
    }
    assert result["profile_suggestions"]["remaining_floater"] == 6
    assert [c["code"] for c in result["in_progress_courses"]] == ["CSD204"]
    assert {c["code"] for c in result["failed_courses"]} == {"SWE133", "MED201"}


def test_failed_and_ip_are_never_completed(monkeypatch):
    result = ar.parse_advisement_report(_fake_pdf(monkeypatch), "report.pdf")
    completed = {c["code"] for c in result["completed_courses"]}
    assert not completed.intersection({"SWE133", "MED201", "CSD204"})


def test_rejects_non_pdf_payload():
    with pytest.raises(ar.AdvisementParseError, match="not a PDF"):
        ar.parse_advisement_report(base64.b64encode(b"hello").decode(), "report.pdf")


def test_parse_route_uses_in_memory_service(monkeypatch):
    expected = {"format": "snu_advisement_report", "totals": {"used": 35}}
    seen = {}

    def fake_parse(content, filename):
        seen.update(content=content, filename=filename)
        return expected

    monkeypatch.setattr(main_module, "parse_advisement_report", fake_parse)
    client = TestClient(main_module.app)
    encoded = base64.b64encode(b"%PDF-private").decode()
    response = client.post("/api/v1/advisement-report/parse", json={
        "filename": "report.pdf", "content_base64": encoded,
    })
    assert response.status_code == 200
    assert response.json() == expected
    assert seen == {"content": encoded, "filename": "report.pdf"}
