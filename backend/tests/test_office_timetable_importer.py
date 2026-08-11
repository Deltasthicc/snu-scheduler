from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools import import_office_timetable_xlsx as office_importer  # noqa: E402


def test_office_import_rejects_missing_authoritative_scoping_column(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "missing-scoping.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    headers = [c for c in office_importer.REQUIRED_COLUMNS if c != office_importer.COL_ME_FOR]
    sheet.append(headers)
    sheet.append(["x"] * len(headers))
    book.save(path)
    book.close()

    with pytest.raises(office_importer.WorkbookError, match="Major Elective for Programmes"):
        office_importer.read_workbook(path)


def test_office_import_required_schema_includes_batch_and_programme_scoping():
    required = set(office_importer.REQUIRED_COLUMNS)
    assert office_importer.COL_MAJOR_FOR in required
    assert office_importer.COL_ME_FOR in required
    assert office_importer.COL_BLOCK in required
