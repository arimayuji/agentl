"""The short way for one-off jobs: read, write and page through APIs from plain Python, with the
same readers and writers pipelines use.

    import agentl as al

    rows = al.read("invoices/*.pdf")                      # a list of dicts
    rows = [r for r in rows if "Total" in r["text"]]
    al.write(rows, "out/invoices.xlsx")

    items = al.http_get_all("https://api.example.com/orders", items="data", next="next_page")

When the job has to run again, call an LLM, survive a failure halfway, run on a schedule or check
its data before delivering, write a pipeline instead: it adds the cache, retries, checks and run
history these functions leave out."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

Records = list[dict[str, Any]]

_READ_BY_EXT = {
    ".csv": "csv.read",
    ".tsv": "csv.read",
    ".json": "json.read",
    ".jsonl": "json.read",
    ".ndjson": "json.read",
    ".txt": "txt.read",
    ".md": "txt.read",
    ".log": "txt.read",
    ".xlsx": "excel.read",
    ".xlsm": "excel.read",
    ".parquet": "parquet.read",
    ".pdf": "pdf.read",
    ".docx": "docx.read",
    ".doc": "doc.read",
    ".xml": "xml.read",
    ".html": "html.read",
    ".htm": "html.read",
}
_WRITE_BY_EXT = {
    ".csv": "csv.write",
    ".tsv": "csv.write",
    ".json": "json.write",
    ".jsonl": "json.write",
    ".xlsx": "excel.write",
    ".parquet": "parquet.write",
}
_DATABASES = (
    "postgres",
    "postgresql",
    "mysql",
    "mariadb",
    "sqlite",
    "mssql",
    "oracle",
    "snowflake",
    "bigquery",
    "duckdb",
)


def _scheme(target: str) -> str:
    return urlparse(target).scheme.split("+", 1)[0].lower() if "://" in target else ""


def _ext(target: str) -> str:
    path = urlparse(target).path if "://" in target else target
    return PurePosixPath(path.replace("\\", "/")).suffix.lower()


def _sqlite_path(url: str) -> str:
    return url.split("://", 1)[1].removeprefix("/") or ":memory:"


def _connector(name: str) -> Any:
    from . import registry

    return registry.get(name)


def read(source: str, **options: Any) -> Records:
    """Records from a file, a glob, a URL or a database.

    - Files and globs (`data/*.csv`), local or remote (`s3://`, `gs://`, ...), by extension: csv, tsv,
      json, jsonl, txt, md, xlsx, parquet, pdf, docx, doc, xml, html; zip, gz and others through
      `file.read`. `options` go to that reader, e.g. `sheet="export"` for Excel.
    - `http(s)://` URLs: a file reader when the URL ends in a known extension, otherwise the JSON
      response (`items="data"` picks the list inside it). For every page of an API use
      `http_get_all`.
    - Database URLs (`postgresql://`, `sqlite:///`, `mysql://`, ...) with `query="select ..."`.
    """
    scheme = _scheme(source)
    if scheme in _DATABASES:
        if "query" not in options:
            raise ValueError(f"reading {scheme} needs query='select ...'")
        if scheme == "sqlite":  # standard library, no SQLAlchemy needed
            return list(_connector("sqlite.read")(None, db=_sqlite_path(source), **options))
        if scheme in ("postgres", "postgresql") and "+" not in source.split("://", 1)[0]:
            return list(_connector("postgres.read")(None, dsn=source, **options))
        return list(_connector("sql.read")(None, url=source, **options))
    ext = _ext(source)
    if scheme in ("http", "https"):
        name = _READ_BY_EXT.get(ext)
        if name is None:
            return list(_connector("http.get")(None, url=source, **options))
        return list(_connector(name)(None, url=source, **options))
    name = _READ_BY_EXT.get(ext, "file.read")
    if ext == ".tsv":
        options.setdefault("delimiter", "\t")
    return list(_connector(name)(None, path=source, **options))


def write(rows: Records, target: str, **options: Any) -> dict[str, Any]:
    """Write records to a file (by extension: csv, tsv, json, jsonl, xlsx, parquet; local or remote)
    or a database table (`postgresql://...`, `sqlite:///...` with `table="name"`). Folders are
    created. Returns what was written: path or table, and the number of records."""
    rows = list(rows)
    scheme = _scheme(target)
    if scheme in _DATABASES:
        if "table" not in options:
            raise ValueError(f"writing to {scheme} needs table='name'")
        if scheme == "sqlite":
            out = _connector("sqlite.write")(rows, db=_sqlite_path(target), **options)
            return {**(dict(out[0]) if out else {}), "written": len(rows)}
        if scheme in ("postgres", "postgresql") and "+" not in target.split("://", 1)[0]:
            out = _connector("postgres.write")(rows, dsn=target, **options)
        else:
            out = _connector("sql.write")(rows, url=target, **options)
    else:
        ext = _ext(target)
        name = _WRITE_BY_EXT.get(ext)
        if name is None:
            raise ValueError(
                f"no writer for '{ext or target}'. Use one of {', '.join(sorted(_WRITE_BY_EXT))}"
            )
        if ext == ".tsv":
            options.setdefault("delimiter", "\t")
        out = _connector(name)(rows, path=target, **options)
    summary = dict(out[0]) if out else {}
    summary.setdefault("written", len(rows))
    return summary


def http_get_all(
    url: str,
    *,
    items: str | None = None,
    next: str | None = None,
    page_param: str = "page",
    start: int = 1,
    params: dict | None = None,
    headers: dict | None = None,
    max_pages: int = 1000,
    **options: Any,
) -> Records:
    """Every record of a paged JSON API. `items` is the key of the list in each response
    (`"data"`); `next` the key that is empty on the last page (`"next_page"`). Without `next`, pages
    are requested until one comes back empty. Same SSRF guard, retries and timeouts as `http.get`."""
    paginate: dict[str, Any] = {"param": page_param, "start": start, "max_pages": max_pages}
    if next:
        paginate["next"] = next
    return list(
        _connector("http.get")(
            None, url=url, items=items, params=params, headers=headers, paginate=paginate, **options
        )
    )
