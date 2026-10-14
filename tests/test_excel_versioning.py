"""excel.write and versioned outputs (#86)."""

import datetime as dt
import os
import time

import pytest

openpyxl = pytest.importorskip("openpyxl")

from agentl.connectors.files import csv_write, excel_write, json_write, versioned  # noqa: E402

NOW = dt.datetime(2026, 10, 14, 5, 0, 7)


def test_excel_write_is_ready_to_open(tmp_path):
    rows = [
        {
            "id": "00123",
            "region": "south",
            "total": "1500.50",
            "qty": "3",
            "when": dt.datetime(2026, 10, 14, 5, tzinfo=dt.timezone.utc),
        },
        {"id": "00124", "region": "north", "total": "80", "qty": "1", "tags": ["a", "b"]},
    ]
    out = excel_write(rows, str(tmp_path / "report.xlsx"))
    assert out == [{"path": str(tmp_path / "report.xlsx"), "written": 2}]
    ws = openpyxl.load_workbook(tmp_path / "report.xlsx")["data"]
    assert [c.value for c in ws[1]] == ["id", "region", "total", "qty", "when", "tags"]
    assert ws["A1"].font.bold and ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:F3"
    assert ws["A2"].value == "00123"  # leading zero: stays text
    assert ws["C2"].value == 1500.5 and ws["D2"].value == 3  # numeric text becomes numbers
    assert ws["E2"].value == dt.datetime(2026, 10, 14, 5)  # Excel has no time zones
    assert ws["F3"].value == '["a", "b"]'
    assert ws.column_dimensions["C"].width >= len("1500.50")


def test_excel_write_one_sheet_per_input(tmp_path):
    excel_write({"summary": [{"n": 1}], "detail": [{"a": 1}, {"a": 2}]}, str(tmp_path / "r.xlsx"))
    wb = openpyxl.load_workbook(tmp_path / "r.xlsx")
    assert wb.sheetnames == ["summary", "detail"] and wb["detail"].max_row == 3


def test_excel_numbers_can_stay_text(tmp_path):
    excel_write([{"v": "42"}], str(tmp_path / "r.xlsx"), numbers=False)
    assert openpyxl.load_workbook(tmp_path / "r.xlsx")["data"]["A2"].value == "42"


def test_versioned_paths():
    assert versioned("out/sales_{date}.xlsx", NOW) == "out/sales_2026-10-14.xlsx"
    assert versioned("out/sales_{datetime}.csv", NOW) == "out/sales_2026-10-14T050007.csv"
    assert versioned("out/plain.csv", NOW) == "out/plain.csv"
    with pytest.raises(ValueError, match=r"only \{date\} and \{datetime\}"):
        versioned("out/{user}.csv")


def test_latest_copy_and_keep(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    for day in ("2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13"):
        (folder / f"sales_{day}.csv").write_text("old\n")
    out = csv_write(
        [{"a": 1}],
        str(folder / "sales_{date}.csv"),
        latest=str(folder / "sales_latest.csv"),
        keep=3,
    )[0]
    today = dt.date.today().isoformat()
    assert out["path"] == str(folder / f"sales_{today}.csv") and out["deleted"] == 2
    assert (
        (folder / "sales_latest.csv").read_text() == (folder / f"sales_{today}.csv").read_text() == "a\n1\n"
    )
    left = sorted(p.name for p in folder.iterdir())
    assert left == ["sales_2026-10-12.csv", "sales_2026-10-13.csv", f"sales_{today}.csv", "sales_latest.csv"]


def test_keep_needs_a_dated_name(tmp_path):
    with pytest.raises(ValueError, match="keep needs"):
        json_write([{"a": 1}], str(tmp_path / "x.json"), keep=3)


def test_versioning_on_excel_and_jsonl(tmp_path):
    out = excel_write([{"a": 1}], str(tmp_path / "r_{date}.xlsx"), latest=str(tmp_path / "r.xlsx"))[0]
    assert os.path.exists(out["path"]) and out["latest"] == str(tmp_path / "r.xlsx")
    first = json_write([{"a": 1}], str(tmp_path / "e_{datetime}.jsonl"), keep=1)[0]["path"]
    time.sleep(1.1)
    second = json_write([{"a": 2}], str(tmp_path / "e_{datetime}.jsonl"), keep=1)[0]
    assert second["deleted"] == 1 and not os.path.exists(first) and os.path.exists(second["path"])


def test_remote_latest_and_keep_with_fsspec(tmp_path):
    fsspec = pytest.importorskip("fsspec")
    mem = fsspec.filesystem("memory")
    for day in ("2026-10-01", "2026-10-02"):
        mem.pipe(f"/reports/s_{day}.csv", b"old")
    out = csv_write(
        [{"a": 1}], "memory://reports/s_{date}.csv", latest="memory://reports/s_latest.csv", keep=2
    )[0]
    assert out["deleted"] == 1
    names = sorted(p.rsplit("/", 1)[-1] for p in mem.ls("/reports", detail=False))
    assert names == ["s_2026-10-02.csv", f"s_{dt.date.today().isoformat()}.csv", "s_latest.csv"]
    assert mem.cat("/reports/s_latest.csv") == b"a\r\n1\r\n"  # csv module default, what Excel expects
