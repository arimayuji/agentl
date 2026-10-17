"""Any SQL database through SQLAlchemy: MySQL/MariaDB, SQL Server, Oracle, Snowflake,
BigQuery, Redshift, DuckDB, SQLite, Postgres... Install `agentl-etl[sql]` plus the driver
(or a ready extra: `mysql`, `mssql`, `oracle`, `snowflake`, `bigquery`)."""

from __future__ import annotations

import datetime as _dt
import decimal
from typing import Any

from ..registry import connector, require
from ._util import SCD2_COLUMNS, check_scd2_columns, scd2_options, single

MODES = ("append", "replace", "upsert", "scd2")


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
    track: str | list[str] | None = None,
    on_missing: str | None = None,
    as_of: Any = None,
) -> list[dict]:
    """Write records to `table`. `mode`: append, replace (drop and recreate), upsert on `key`, or
    scd2 (history in `valid_from`, `valid_to`, `is_current`; see `postgres.write`).
    Upsert is native on Postgres, SQLite and MySQL; on other databases rows with the same key
    are deleted and re-inserted in the same transaction. scd2 reads the current versions' key
    and tracked columns in one query and compares them in Python, so they must fit in memory.
    One transaction for the whole write."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    keys = [key] if isinstance(key, str) else list(key or [])
    if mode == "upsert" and not keys:
        raise ValueError("mode 'upsert' needs 'key'")
    tracked, missing_rows, when = scd2_options("sql.write", mode, keys, track, on_missing, as_of)
    sa = require("sqlalchemy", "sql")
    rows = single(input)
    cols: list[str] = []
    for r in rows:
        cols.extend(k for k in r if k not in cols)
    if missing := [k for k in keys if k not in cols and rows]:
        raise ValueError(f"key column(s) not in records: {missing}")
    if mode == "scd2":
        check_scd2_columns("sql.write", tracked, cols if rows else [])

    engine = _engine(url, engine_options)
    try:
        with engine.begin() as conn:
            meta = sa.MetaData(schema=schema)
            exists = sa.inspect(conn).has_table(table, schema=schema)
            if mode == "replace" and exists:
                sa.Table(table, meta, autoload_with=conn).drop(conn)
                meta = sa.MetaData(schema=schema)
                exists = False
            if not cols:  # an empty input changes nothing, even with on_missing: close
                out: dict[str, Any] = {"table": table, "written": 0, "mode": mode}
                if mode == "scd2":
                    out.update(inserted=0, versioned=0, unchanged=0)
                return [out]
            if exists:
                tbl = sa.Table(table, meta, autoload_with=conn)
            elif create:
                types = {c: _type([r.get(c) for r in rows]) for c in cols}
                types.update({k: _key_type(sa, types[k]) for k in keys})
                extra = []
                if mode == "scd2":
                    extra = [
                        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
                        sa.Column("valid_to", sa.DateTime(timezone=True)),
                        sa.Column("is_current", sa.Boolean(), nullable=False),
                    ]
                tbl = sa.Table(
                    table,
                    meta,
                    *(sa.Column(c, types[c], primary_key=c in keys and mode == "upsert") for c in cols),
                    *extra,
                )
                tbl.create(conn)
            else:
                raise ValueError(f"table '{table}' does not exist (create: false)")

            if mode == "upsert" and _upsert_stmt(sa, conn, tbl, keys, cols) is not None:
                # Native upserts need a unique constraint on the key.
                name = "uq_" + "_".join([table, *keys])[:55]
                sa.Index(name, *(tbl.c[k] for k in keys), unique=True).create(conn, checkfirst=True)

            if mode == "scd2":
                counts = _scd2(sa, conn, tbl, rows, cols, keys, tracked, missing_rows, when, batch_size)
                return [{"table": table, "mode": mode, **counts}]
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
    return [{"table": table, "written": len(rows), "mode": mode}]


def _utc(v: Any) -> Any:
    if isinstance(v, str):
        v = _dt.datetime.fromisoformat(v)
    return v.replace(tzinfo=_dt.timezone.utc) if v.tzinfo is None else v


def _scd2(
    sa: Any,
    conn: Any,
    tbl: Any,
    rows: list[dict],
    cols: list[str],
    keys: list[str],
    track: list[str] | None,
    on_missing: str,
    when: Any,
    batch_size: int,
) -> dict[str, Any]:
    """SCD type 2 on any SQLAlchemy database: one SELECT of the current versions, then batched
    UPDATEs (close) and INSERTs (new versions)."""
    if missing := [c for c in SCD2_COLUMNS if c not in tbl.c]:
        raise ValueError(f"sql.write: mode: scd2 needs column(s) {missing} in table '{tbl.name}'")
    tracked = track if track is not None else [c for c in cols if c not in keys]
    source: dict[tuple, dict] = {}
    for r in rows:  # last record per key wins
        k = tuple(r.get(c) for c in keys)
        if None in k:
            raise ValueError(f"sql.write: mode: scd2 needs a value in every key column {keys}")
        source[k] = r
    sel = sa.select(*(tbl.c[c] for c in [*keys, *tracked, "valid_from"])).where(tbl.c.is_current == sa.true())
    current = {tuple(row[: len(keys)]): row[len(keys) :] for row in conn.execute(sel)}
    new, changed, same = [], [], 0
    for k, r in source.items():
        cur = current.get(k)
        if cur is None:
            new.append(r)
        elif tuple(cur[:-1]) != tuple(r.get(c) for c in tracked):
            if _utc(cur[-1]) > when:
                raise ValueError(
                    f"sql.write: as_of {when.isoformat()} is before the start of a current version"
                )
            changed.append(r)
        else:
            same += 1
    close = [k for k in current if k not in source] if on_missing == "close" else []
    ending = [tuple(r.get(c) for c in keys) for r in changed] + close
    cond = sa.and_(tbl.c.is_current == sa.true(), *(tbl.c[c] == sa.bindparam(f"_k_{c}") for c in keys))
    stmt = tbl.update().where(cond).values(valid_to=when, is_current=False)
    for i in range(0, len(ending), batch_size):
        conn.execute(
            stmt, [{f"_k_{c}": v for c, v in zip(keys, k, strict=True)} for k in ending[i : i + batch_size]]
        )
    adds = new + changed
    for i in range(0, len(adds), batch_size):
        batch = [
            {**{c: r.get(c) for c in cols}, "valid_from": when, "valid_to": None, "is_current": True}
            for r in adds[i : i + batch_size]
        ]
        conn.execute(tbl.insert(), batch)
    out: dict[str, Any] = {"written": len(source), "inserted": len(new), "versioned": len(changed)}
    out["unchanged"] = same
    if on_missing == "close":
        out["closed"] = len(close)
    out["as_of"] = when.isoformat()
    return out
