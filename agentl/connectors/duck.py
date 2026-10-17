"""`sql` step: query the outputs of previous steps as tables, in an in-process DuckDB."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from ..registry import connector, require

_EXTRA = "duckdb"


def _is_table(value: Any) -> bool:
    return hasattr(value, "num_rows") and hasattr(value, "to_pylist")


def connect(allow_files: bool = False) -> Any:
    """An in-memory DuckDB. Without `allow_files` it cannot touch files or the network
    (`read_csv`, `COPY ... TO`, `ATTACH` a file, `INSTALL`), nor load extensions, and the SQL cannot
    change these settings back."""
    duckdb = require("duckdb", _EXTRA)
    config = {
        "enable_external_access": allow_files,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": allow_files,
    }
    con = duckdb.connect(":memory:", config=config)
    con.execute("SET lock_configuration = true")
    return con


def _arrow(name: str, rows: Any) -> Any:
    if _is_table(rows):
        return rows  # registered as is: DuckDB scans the Arrow buffers without a copy
    pa = require("pyarrow", _EXTRA)
    try:
        return pa.Table.from_pylist(list(rows))
    except (pa.ArrowInvalid, pa.ArrowTypeError) as e:
        raise ValueError(
            f"input '{name}' has fields with mixed types and cannot become a table ({e}); "
            "add a `cast` step first"
        ) from e


def tables(query: str) -> set[str] | None:
    """Names of the tables a query reads, without the CTEs it defines. None when DuckDB is not
    installed or the query does not parse."""
    try:
        duckdb = require("duckdb", _EXTRA)
    except ImportError:
        return None
    try:
        tree = json.loads(
            duckdb.connect(":memory:").execute("select json_serialize_sql($q)", {"q": query}).fetchone()[0]
        )
    except Exception:
        return None
    if tree.get("error"):
        return None
    used: set[str] = set()
    ctes: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if (
                node.get("type") == "BASE_TABLE"
                and not node.get("schema_name")
                and not node.get("catalog_name")
            ):
                used.add(str(node.get("table_name")))
            for item in (
                (node.get("cte_map") or {}).get("map", []) if isinstance(node.get("cte_map"), dict) else []
            ):
                ctes.add(str(item.get("key")))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(tree)
    return used - ctes


def read_query(query: str | None, file: str | None, base_dir: str | None) -> str:
    if bool(query) == bool(file):
        raise ValueError("sql needs exactly one of query (inline SQL) or file (a .sql path)")
    if file:
        path = Path(file)
        if not path.is_absolute() and base_dir:
            path = Path(base_dir) / path
        return path.read_text(encoding="utf-8")
    return str(query)


def _error(step: str, e: Exception, names: list[str]) -> ValueError:
    msg = str(e)
    m = re.search(r"Table with name (\S+) does not exist", msg)
    if m:
        missing = m.group(1)
        hint = next(iter(difflib.get_close_matches(missing, names, n=1)), None)
        msg = f"table '{missing}' is not an input of this step. Inputs: {', '.join(names) or '(none)'}"
        if hint:
            msg += f". Did you mean '{hint}'?"
    else:
        msg = msg.replace("\n\n", "\n")
    return ValueError(f"sql step '{step}': {msg}")


def run(
    step: str,
    inputs: dict[str, Any],
    query: str | None = None,
    file: str | None = None,
    params: dict[str, Any] | None = None,
    allow_files: bool = False,
    base_dir: str | None = None,
) -> list[dict]:
    """Runs `query` with each input registered as a table named after its step id."""
    sql = read_query(query, file, base_dir)
    duckdb = require("duckdb", _EXTRA)
    con = connect(allow_files)
    empty = []
    try:
        for name, rows in inputs.items():
            if not _is_table(rows) and not rows:
                # No records, so no columns: a zero-row placeholder keeps `count(*)` and `select *`
                # working; a query naming its columns returns no records (below).
                con.execute(
                    f'CREATE VIEW "{name.replace(chr(34), chr(34) * 2)}" AS SELECT NULL AS _empty WHERE false'
                )
                empty.append(name)
                continue
            con.register(name, _arrow(name, rows))
        try:
            res = con.execute(sql, params or None)
        except duckdb.BinderException:
            if empty:
                return []
            raise
        out = res.to_arrow_table() if hasattr(res, "to_arrow_table") else res.fetch_arrow_table()
        return out.to_pylist()
    except duckdb.Error as e:
        raise _error(step, e, list(inputs)) from e
    finally:
        con.close()


@connector("sql")
def sql(
    input: Any,
    query: str | None = None,
    file: str | None = None,
    params: dict[str, Any] | None = None,
    allow_files: bool = False,
) -> list[dict]:
    """SQL over the previous steps (`agentl-etl[duckdb]`). Each input step is a table named after
    its step id; with several inputs, join them as usual:

        - id: report
          input: [orders, customers]
          sql: |
            select c.region, sum(o.total) as total, count(*) as orders
            from orders o join customers c using (customer_id)
            group by c.region

    `sql:` is short for `use: sql` with `with: {query: ...}`; `file: report.sql` reads the query
    from a file next to the pipeline. `params: {min_total: 100}` binds `$min_total` as a value,
    never as SQL text. The query runs in an in-memory DuckDB that cannot read files or the network,
    attach databases or install extensions; `allow_files: true` lifts that for this step. Output:
    records. Called directly with a plain list, the table is named `input`."""
    inputs = input if isinstance(input, dict) else {"input": [] if input is None else input}
    return run("sql", inputs, query=query, file=file, params=params, allow_files=allow_files)
