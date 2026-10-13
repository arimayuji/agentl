"""File connectors: CSV, JSON/JSONL, Excel, PDF, Word."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..registry import connector, require
from ._util import paths, single


@connector("csv.read")
def csv_read(input: Any = None, path: str | None = None, delimiter: str = ",", encoding: str = "utf-8") -> list[dict]:
    out: list[dict] = []
    for p in paths(input, path):
        with p.open(newline="", encoding=encoding) as f:
            out.extend(dict(row) for row in csv.DictReader(f, delimiter=delimiter))
    return out


@connector("csv.write")
def csv_write(input: Any, path: str, delimiter: str = ",") -> list[dict]:
    rows = single(input)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for r in rows:
        fields.extend(k for k in r if k not in fields)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=delimiter)
        w.writeheader()
        w.writerows(rows)
    return [{"path": path, "written": len(rows)}]


@connector("json.read")
def json_read(input: Any = None, path: str | None = None, key: str | None = None) -> list[dict]:
    out: list[dict] = []
    for p in paths(input, path):
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".jsonl":
            out.extend(json.loads(line) for line in text.splitlines() if line.strip())
            continue
        data = json.loads(text)
        if key:
            data = data[key]
        out.extend(data if isinstance(data, list) else [data])
    return out


@connector("json.write")
def json_write(input: Any, path: str) -> list[dict]:
    rows = single(input)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if path.endswith(".jsonl"):
            f.writelines(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)
        else:
            json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    return [{"path": path, "written": len(rows)}]


@connector("excel.read")
def excel_read(input: Any = None, path: str | None = None, sheet: str | None = None, header_row: int = 1) -> list[dict]:
    openpyxl = require("openpyxl", "excel")
    out: list[dict] = []
    for p in paths(input, path):
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.active
        rows = ws.iter_rows(min_row=header_row, values_only=True)
        header = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(next(rows, []))]
        for row in rows:
            if any(v is not None for v in row):
                out.append(dict(zip(header, row)))
        wb.close()
    return out


@connector("pdf.read")
def pdf_read(input: Any = None, path: str | None = None, field: str = "path") -> list[dict]:
    pypdf = require("pypdf", "pdf")
    out = []
    for p in paths(input, path, field):
        reader = pypdf.PdfReader(str(p))
        pages = [page.extract_text() or "" for page in reader.pages]
        out.append({"path": str(p), "pages": len(pages), "text": "\n".join(pages)})
    return out


@connector("docx.read")
def docx_read(input: Any = None, path: str | None = None, field: str = "path") -> list[dict]:
    docx = require("docx", "office")
    out = []
    for p in paths(input, path, field):
        doc = docx.Document(str(p))
        paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
        tables = [[[c.text for c in row.cells] for row in t.rows] for t in doc.tables]
        out.append({"path": str(p), "text": "\n".join(paragraphs), "tables": tables})
    return out
