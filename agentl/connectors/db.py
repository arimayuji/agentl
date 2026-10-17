"""Any SQL database through SQLAlchemy: MySQL/MariaDB, SQL Server, Oracle, Snowflake,
BigQuery, Redshift, DuckDB, SQLite, Postgres... Install `agentl-etl[sql]` plus the driver
(or a ready extra: `mysql`, `mssql`, `oracle`, `snowflake`, `bigquery`)."""

from __future__ import annotations

import datetime as _dt
import decimal
from typing import Any

from ..registry import Rejects, connector, require
from ._util import null_keys, single

MODES = ("append", "replace", "upsert")


def _engine(url: str, engine_options: dict | None) -> Any:
    sa = require("sqlalchemy", "sql")
    if url.startswith("sqlite:///") and ":memory:" not in url:
        # a SQLite file in a folder that does not exist yet: create the folder, as file writers do
        from pathlib import Path

        Path(url.removeprefix("sqlite:///").split("?", 1)[0]).parent.mkdir(parents=True, exist_ok=True)
    return sa.create_engine(url, **(engine_options or {}))


def _type(values: list[Any]) -> Any:
    sa = require("sqlalchemy", "sql")
    kinds = {type(v) for v in values if v is not None}
    if not kinds:
        return sa.Text()
    if kinds <= {bool}:
        return sa.Boolean()
    if kinds <= {int}:
        return sa.BigInteger()
    if kinds <= {int, float}:
        return sa.Float()
    if kinds <= {int, float, decimal.Decimal}:
        return sa.Numeric()
    if kinds <= {_dt.datetime}:
        return sa.DateTime(timezone=True)
    if kinds <= {_dt.date}:
        return sa.Date()
    if kinds <= {dict, list}:
        return sa.JSON()
    return sa.Text()


def _key_type(sa: Any, t: Any) -> Any:
    """Key columns get a bounded string: MySQL and SQL Server cannot index TEXT."""
    return sa.String(255) if isinstance(t, sa.Text) else t


@connector("sql.read")
def sql_read(
    input: Any = None,
    url: str = "",
    query: str = "",
    params: dict | None = None,
    engine_options: dict | None = None,
) -> list[dict]:
    """Run `query` (named parameters: `WHERE id = :id`) and return rows as dicts."""
    sa = require("sqlalchemy", "sql")
    engine = _engine(url, engine_options)
    try:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(sa.text(query), params or {}).mappings()]
    finally:
        engine.dispose()


def _upsert_stmt(sa: Any, conn: Any, table: Any, keys: list[str], cols: list[str]) -> Any | None:
    """Native upsert where the dialect has one; None means delete-then-insert."""
    name = conn.dialect.name
    rest = [c for c in cols if c not in keys]
    if name in ("postgresql", "sqlite"):
        mod = require(f"sqlalchemy.dialects.{name}", "sql")
        stmt = mod.insert(table)
        if not rest:
            return stmt.on_conflict_do_nothing(index_elements=keys)
        return stmt.on_conflict_do_update(index_elements=keys, set_={c: stmt.excluded[c] for c in rest})
    if name in ("mysql", "mariadb"):
        mod = require("sqlalchemy.dialects.mysql", "sql")
        stmt = mod.insert(table)
        return stmt.on_duplicate_key_update({c: stmt.inserted[c] for c in rest or keys})
    return None


@connector("sql.write")
def sql_write(
    input: Any,
    url: str = "",
    table: str = "",
    mode: str = "append",
    key: str | list[str] | None = None,
    schema: str | None = None,
    batch_size: int = 1000,
    create: bool = True,
    engine_options: dict | None = None,
) -> list[dict]:
    """Write records to `table`. `mode`: append, replace (drop and recreate) or upsert on `key`.
    Upsert is native on Postgres, SQLite and MySQL; on other databases rows with the same key
    are deleted and re-inserted in the same transaction. One transaction for the whole write."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    keys = [key] if isinstance(key, str) else list(key or [])
    if mode == "upsert" and not keys:
        raise ValueError("mode 'upsert' needs 'key'")
    sa = require("sqlalchemy", "sql")
    rows = single(input)
    rejected: list[dict] = []
    if mode == "upsert":
        rows, rejected = null_keys("sql.write", rows, keys)
    cols: list[str] = []
    for r in rows:
        cols.extend(k for k in r if k not in cols)
    if missing := [k for k in keys if k not in cols and rows]:
        raise ValueError(f"key column(s) not in records: {missing}")

    engine = _engine(url, engine_options)
    try:
        with engine.begin() as conn:
            meta = sa.MetaData(schema=schema)
            exists = sa.inspect(conn).has_table(table, schema=schema)
            if mode == "replace" and exists:
                sa.Table(table, meta, autoload_with=conn).drop(conn)
                meta = sa.MetaData(schema=schema)
                exists = False
            if not cols:
                return [{"table": table, "written": 0, "mode": mode}]
            if exists:
                tbl = sa.Table(table, meta, autoload_with=conn)
            elif create:
                types = {c: _type([r.get(c) for r in rows]) for c in cols}
                types.update({k: _key_type(sa, types[k]) for k in keys})
                tbl = sa.Table(
                    table,
                    meta,
                    *(sa.Column(c, types[c], primary_key=c in keys and mode == "upsert") for c in cols),
                )
                tbl.create(conn)
            else:
                raise ValueError(f"table '{table}' does not exist (create: false)")

            if mode == "upsert" and _upsert_stmt(sa, conn, tbl, keys, cols) is not None:
                # Native upserts need a unique constraint on the key.
                name = "uq_" + "_".join([table, *keys])[:55]
                sa.Index(name, *(tbl.c[k] for k in keys), unique=True).create(conn, checkfirst=True)

            for i in range(0, len(rows), batch_size):
                batch = [{c: r.get(c) for c in cols} for r in rows[i : i + batch_size]]
                if mode != "upsert":
                    conn.execute(tbl.insert(), batch)
                    continue
                stmt = _upsert_stmt(sa, conn, tbl, keys, cols)
                if stmt is not None:
                    conn.execute(stmt, batch)
                else:
                    cond = sa.and_(*(tbl.c[k] == sa.bindparam(f"_k_{k}") for k in keys))
                    conn.execute(tbl.delete().where(cond), [{f"_k_{k}": r[k] for k in keys} for r in batch])
                    conn.execute(tbl.insert(), batch)
    finally:
        engine.dispose()
    out = [{"table": table, "written": len(rows), "mode": mode}]
    return Rejects(out, rejected) if rejected else out
