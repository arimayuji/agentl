"""File connectors.

Every reader takes its files from `path` (a glob), `url` (downloaded first) or the
`path` field of upstream records, and is built on a per-file function so that
`file.read` can dispatch mixed folders and archives to the right one.
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..registry import connector, require
from ._util import is_remote, origin, paths, single, tmpdir, upload

log = logging.getLogger("agentl.files")

Records = list[dict]


# ---------------------------------------------------------------- per-file readers
def read_csv(p: Path, delimiter: str | None = None, encoding: str = "utf-8", **_: Any) -> Records:
    with p.open(newline="", encoding=encoding, errors="replace") as f:
        sample = f.read(64 * 1024)
        f.seek(0)
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = "\t" if p.suffix.lower() == ".tsv" else ","
        return [dict(row) for row in csv.DictReader(f, delimiter=delimiter)]


def read_json(p: Path, key: str | None = None, encoding: str = "utf-8", **_: Any) -> Records:
    text = p.read_text(encoding=encoding)
    if p.suffix.lower() in (".jsonl", ".ndjson"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if key:
        for part in key.split("."):
            data = data[part]
    rows = data if isinstance(data, list) else [data]
    return [r if isinstance(r, dict) else {"value": r} for r in rows]


def read_txt(p: Path, lines: bool = False, encoding: str = "utf-8", **_: Any) -> Records:
    text = p.read_text(encoding=encoding, errors="replace")
    if lines:
        return [
            {"path": str(p), "line": i + 1, "text": t} for i, t in enumerate(text.splitlines()) if t.strip()
        ]
    return [{"path": str(p), "text": text}]


def read_xlsx(p: Path, sheet: str | None = None, header_row: int = 1, **_: Any) -> Records:
    openpyxl = require("openpyxl", "excel")
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.active
        rows = ws.iter_rows(min_row=header_row, values_only=True)
        header = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(next(rows, []))]
        return [dict(zip(header, row, strict=False)) for row in rows if any(v is not None for v in row)]
    finally:
        wb.close()


def read_xls(p: Path, sheet: str | None = None, header_row: int = 1, **_: Any) -> Records:
    xlrd = require("xlrd", "xls")
    book = xlrd.open_workbook(str(p))
    ws = book.sheet_by_name(sheet) if sheet else book.sheet_by_index(0)
    start = header_row - 1
    header = [str(v) if v != "" else f"col_{i}" for i, v in enumerate(ws.row_values(start))]
    out = []
    for r in range(start + 1, ws.nrows):
        row = ws.row_values(r)
        if any(v != "" for v in row):
            out.append(dict(zip(header, row, strict=False)))
    return out


def read_parquet(p: Path, columns: list[str] | None = None, **_: Any) -> Records:
    pq = require("pyarrow.parquet", "parquet")
    return list(pq.read_table(str(p), columns=columns).to_pylist())


def read_pdf(p: Path, **_: Any) -> Records:
    pypdf = require("pypdf", "pdf")
    reader = pypdf.PdfReader(str(p))
    pages = [page.extract_text() or "" for page in reader.pages]
    return [{"path": str(p), "pages": len(pages), "text": "\n".join(pages)}]


def read_docx(p: Path, **_: Any) -> Records:
    docx = require("docx", "office")
    doc = docx.Document(str(p))
    paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
    tables = [[[c.text for c in row.cells] for row in t.rows] for t in doc.tables]
    return [{"path": str(p), "text": "\n".join(paragraphs), "tables": tables}]


def read_doc(p: Path, **opts: Any) -> Records:
    """Legacy Word files: convert with LibreOffice, then read as .docx."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError(f"{p.name}: reading .doc needs LibreOffice ('soffice') on the PATH")
    out_dir = tmpdir("doc-")
    subprocess.run(
        [soffice, "--headless", "--convert-to", "docx", "--outdir", str(out_dir), str(p)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    records = read_docx(out_dir / (p.stem + ".docx"), **opts)
    for r in records:
        r["path"] = str(p)
    return records


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # drop XML namespace


def read_xml(p: Path, items: str | None = None, **_: Any) -> Records:
    """One record per `items` element (default: children of the root).

    Attributes and simple child elements become fields.
    """
    root = ET.parse(p).getroot()  # stdlib parser: no external entities
    nodes = [e for e in root.iter() if _local(e.tag) == items] if items else list(root)
    out = []
    for node in nodes:
        rec: dict[str, Any] = {_local(k): v for k, v in node.attrib.items()}
        for child in node:
            if len(child) == 0:
                rec[_local(child.tag)] = (child.text or "").strip()
        if not rec and (node.text or "").strip():
            rec[_local(node.tag)] = node.text.strip()  # type: ignore[union-attr]
        out.append(rec)
    return out


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def read_html(p: Path, table: int | None = None, encoding: str = "utf-8", **_: Any) -> Records:
    """Rows of the HTML tables; the first row of each table is the header."""
    return html_tables(p.read_text(encoding=encoding, errors="replace"), table)


def html_tables(html: str, table: int | None = None) -> Records:
    """Rows of the tables in an HTML string; the first row of each table is the header."""
    parser = _Tables()
    parser.feed(html)
    out: Records = []
    for i, rows in enumerate(parser.tables):
        if (table is not None and i != table) or len(rows) < 2:
            continue
        header = [h or f"col_{j}" for j, h in enumerate(rows[0])]
        out.extend({"table": i, **dict(zip(header, r, strict=False))} for r in rows[1:])
    return out


READERS: dict[str, Callable[..., Records]] = {
    ".csv": read_csv,
    ".tsv": read_csv,
    ".json": read_json,
    ".jsonl": read_json,
    ".ndjson": read_json,
    ".txt": read_txt,
    ".md": read_txt,
    ".log": read_txt,
    ".xlsx": read_xlsx,
    ".xlsm": read_xlsx,
    ".xls": read_xls,
    ".parquet": read_parquet,
    ".pdf": read_pdf,
    ".docx": read_docx,
    ".doc": read_doc,
    ".xml": read_xml,
    ".html": read_html,
    ".htm": read_html,
}


# ---------------------------------------------------------------- archives
MAX_EXTRACT_BYTES = 1024**3  # 1 GiB per archive, guards against zip bombs


def _extract_zip(p: Path, limit: int) -> list[Path]:
    dest = tmpdir("zip-")
    out = []
    with zipfile.ZipFile(p) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > limit:
            raise ValueError(f"{p.name}: archive expands beyond {limit} bytes")
        for info in infos:
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError(f"{p.name}: unsafe path in archive: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(target)
    return sorted(out)


def _extract_gz(p: Path, limit: int) -> list[Path]:
    target = tmpdir("gz-") / p.stem  # data.csv.gz -> data.csv
    written = 0
    with gzip.open(p, "rb") as src, target.open("wb") as dst:
        while chunk := src.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise ValueError(f"{p.name}: archive expands beyond {limit} bytes")
            dst.write(chunk)
    return [target]


def _expand(p: Path, limit: int, depth: int = 0) -> list[tuple[Path, Path | None]]:
    """(file, archive it came from) pairs, expanding zip/gz up to two levels deep."""
    suffix = p.suffix.lower()
    if suffix in (".zip", ".gz") and depth < 2:
        inner = _extract_zip(p, limit) if suffix == ".zip" else _extract_gz(p, limit)
        return [(f, p) for x in inner for f, _ in _expand(x, limit, depth + 1)]
    return [(p, None)]


# ---------------------------------------------------------------- connectors
def _each(
    reader: Callable[..., Records],
    input: Any,
    path: str | None,
    url: str | None,
    headers: dict | None,
    field: str = "path",
    **opts: Any,
) -> Records:
    out: Records = []
    so = opts.pop("storage_options", None)
    for p in paths(input, path, field, url, headers, so):
        out.extend(reader(p, **opts))
    return out


@connector("file.read")
def file_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    field: str = "path",
    on_unsupported: str = "skip",
    max_extract_bytes: int = MAX_EXTRACT_BYTES,
    storage_options: dict | None = None,
    **opts: Any,
) -> Records:
    """Read any supported file, detecting the format by extension.

    ZIP and GZ archives are expanded and each inner file is read with its own reader.
    Each record gets `_file` (and `_archive` when it came from one).
    """
    if on_unsupported not in ("skip", "error"):
        raise ValueError("on_unsupported must be 'skip' or 'error'")
    out: Records = []
    for p in paths(input, path, field, url, headers, storage_options):
        for f, archive in _expand(p, max_extract_bytes):
            reader = READERS.get(f.suffix.lower())
            if reader is None:
                if on_unsupported == "error":
                    raise ValueError(f"unsupported file type: {f.name}")
                log.warning("file.read: skipping unsupported file %s", f.name)
                continue
            shown = f"{archive.name}!{f.name}" if archive else origin(f)
            for rec in reader(f, **opts):
                rec.setdefault("_file", shown)
                if archive:
                    rec.setdefault("_archive", origin(archive))
                out.append(rec)
    return out


@connector("csv.read")
def csv_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    delimiter: str | None = None,
    encoding: str = "utf-8",
) -> Records:
    return _each(
        read_csv,
        input,
        path,
        url,
        headers,
        delimiter=delimiter,
        encoding=encoding,
        storage_options=storage_options,
    )


@connector("json.read")
def json_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    key: str | None = None,
) -> Records:
    return _each(read_json, input, path, url, headers, key=key, storage_options=storage_options)


@connector("txt.read")
def txt_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    lines: bool = False,
    encoding: str = "utf-8",
) -> Records:
    return _each(
        read_txt, input, path, url, headers, lines=lines, encoding=encoding, storage_options=storage_options
    )


@connector("excel.read")
def excel_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    sheet: str | None = None,
    header_row: int = 1,
) -> Records:
    def read(p: Path, **kw: Any) -> Records:
        return (read_xls if p.suffix.lower() == ".xls" else read_xlsx)(p, **kw)

    return _each(
        read, input, path, url, headers, sheet=sheet, header_row=header_row, storage_options=storage_options
    )


@connector("parquet.read")
def parquet_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    columns: list[str] | None = None,
) -> Records:
    return _each(read_parquet, input, path, url, headers, columns=columns, storage_options=storage_options)


@connector("pdf.read")
def pdf_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    field: str = "path",
) -> Records:
    return _each(read_pdf, input, path, url, headers, field, storage_options=storage_options)


@connector("docx.read")
def docx_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    field: str = "path",
) -> Records:
    return _each(read_docx, input, path, url, headers, field, storage_options=storage_options)


@connector("doc.read")
def doc_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    field: str = "path",
) -> Records:
    return _each(read_doc, input, path, url, headers, field, storage_options=storage_options)


@connector("xml.read")
def xml_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    items: str | None = None,
) -> Records:
    return _each(read_xml, input, path, url, headers, items=items, storage_options=storage_options)


@connector("html.read")
def html_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    storage_options: dict | None = None,
    table: int | None = None,
) -> Records:
    return _each(read_html, input, path, url, headers, table=table, storage_options=storage_options)


# ---------------------------------------------------------------- writers
def _target(path: str) -> Path:
    """Local file to write: the path itself, or a temp file uploaded afterwards."""
    local = tmpdir("out-") / Path(path).name if is_remote(path) else Path(path)
    local.parent.mkdir(parents=True, exist_ok=True)
    return local


@connector("csv.write")
def csv_write(input: Any, path: str, delimiter: str = ",", storage_options: dict | None = None) -> Records:
    rows = single(input)
    local = _target(path)
    fields: list[str] = []
    for r in rows:
        fields.extend(k for k in r if k not in fields)
    with open(local, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=delimiter)
        w.writeheader()
        w.writerows(rows)
    if is_remote(path):
        upload(local, path, storage_options)
    return [{"path": path, "written": len(rows)}]


@connector("json.write")
def json_write(input: Any, path: str, storage_options: dict | None = None) -> Records:
    rows = single(input)
    local = _target(path)
    with open(local, "w", encoding="utf-8") as f:
        if path.endswith(".jsonl"):
            f.writelines(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)
        else:
            json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    if is_remote(path):
        upload(local, path, storage_options)
    return [{"path": path, "written": len(rows)}]
