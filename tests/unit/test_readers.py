from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest

from dashdashgo.config.models import ReaderOptions, ReportFormat
from dashdashgo.errors import ReportFormatError
from dashdashgo.ingestion.readers import READERS, get_reader


def test_every_required_format_has_a_reader() -> None:
    assert set(READERS) == {ReportFormat.CSV, ReportFormat.XLSX, ReportFormat.JSON}


def test_csv_strips_bom_keeps_raw_strings_and_nulls(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_bytes("﻿ID,Name,Amount\n007,  Alice ,1.50\n008,,\n".encode())
    frame = get_reader(ReportFormat.CSV).read(path, ReaderOptions())
    assert list(frame.columns) == ["ID", "Name", "Amount"]
    assert frame.iloc[0].tolist() == [
        "007",
        "  Alice ",
        "1.50",
    ]  # leading zeros kept, no type guessing
    assert frame.iloc[1].tolist() == ["008", None, None]


def test_csv_detects_semicolon_delimiter(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_text("a;b\n1;2\n3;4\n")
    frame = get_reader(ReportFormat.CSV).read(path, ReaderOptions())
    assert list(frame.columns) == ["a", "b"]
    assert len(frame) == 2


def test_csv_wrong_encoding_is_a_format_error(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_bytes(b"a,b\n\xff\xfe,1\n")
    with pytest.raises(ReportFormatError, match="encoding"):
        get_reader(ReportFormat.CSV).read(path, ReaderOptions(encoding="utf-8"))


def test_empty_csv_is_a_format_error(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_text("")
    with pytest.raises(ReportFormatError):
        get_reader(ReportFormat.CSV).read(path, ReaderOptions())


def _workbook(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Summary"
    ws.append(["ignore me"])
    data = wb.create_sheet("Query result")
    data.append(["Usage Date", "Value"])
    data.append([datetime(2026, 9, 14), 12.5])
    wb.save(path)


def test_xlsx_selects_named_sheet_and_keeps_python_values(tmp_path: Path) -> None:
    path = tmp_path / "r.xlsx"
    _workbook(path)
    frame = get_reader(ReportFormat.XLSX).read(path, ReaderOptions(sheet="Query result"))
    assert list(frame.columns) == ["Usage Date", "Value"]
    assert frame.iloc[0, 0] == datetime(2026, 9, 14)
    assert frame.iloc[0, 1] == 12.5


def test_xlsx_missing_sheet_lists_available_sheets(tmp_path: Path) -> None:
    path = tmp_path / "r.xlsx"
    _workbook(path)
    with pytest.raises(ReportFormatError, match="Summary"):
        get_reader(ReportFormat.XLSX).read(path, ReaderOptions(sheet="Nope"))


def test_xlsx_rejects_non_workbook(tmp_path: Path) -> None:
    path = tmp_path / "r.xlsx"
    path.write_text("a,b\n")
    with pytest.raises(ReportFormatError, match="not an XLSX"):
        get_reader(ReportFormat.XLSX).read(path, ReaderOptions())


def test_json_reads_nested_records_under_path(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"data": {"items": [{"id": 1, "owner": {"name": "A"}}, {"id": 2}]}}))
    frame = get_reader(ReportFormat.JSON).read(path, ReaderOptions(records_path="data.items"))
    assert list(frame.columns) == ["id", "owner"]
    assert frame.iloc[0]["owner"] == {"name": "A"}  # nested objects preserved for `flatten`
    assert frame.iloc[1]["owner"] is None


def test_json_object_without_records_path_gives_a_hint(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"rows": []}))
    with pytest.raises(ReportFormatError, match="records_path"):
        get_reader(ReportFormat.JSON).read(path, ReaderOptions())


def test_malformed_json_reports_line(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text('[{"a": 1},\n{"a": }]')
    with pytest.raises(ReportFormatError, match="line 2"):
        get_reader(ReportFormat.JSON).read(path, ReaderOptions())
