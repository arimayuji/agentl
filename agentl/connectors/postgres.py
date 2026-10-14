"""PostgreSQL connectors (psycopg 3). Install with `pip install 'agentl-etl[postgres]'`."""

from __future__ import annotations

import datetime as _dt
import decimal
from collections.abc import Iterable, Iterator
from typing import Any

from ..registry import connector, require, stream_reader, stream_writer
from ._util import single

MODES = ("append", "replace", "upsert")


def _pg_type(values: list[Any]) -> str:
    kinds = {type(v) for v in values if v is not None}
    if not kinds:
        return "text"
    if kinds <= {bool}:
        return "boolean"
    if kinds <= {int}:
        return "bigint"
    if kinds <= {int, float, decimal.Decimal}:
        return "double precision" if float in kinds else "numeric"
    if kinds <= {_dt.datetime}:
        return "timestamptz"
    if kinds <= {_dt.date}:
        return "date"
    if kinds <= {dict, list}:
        return "jsonb"
    return "text"


def _ident(name: str) -> Any:
    sql = require("psycopg.sql", "postgres")
    parts = name.split(".")
    if len(parts) > 2 or not all(parts):
        raise ValueError(f"invalid table name '{name}'")
    return sql.Identifier(*parts)


@connector("postgres.read")
def postgres_read(
    input: Any = None, dsn: str = "", query: str = "", params: list | dict | None = None
) -> list[dict]:
    """Run `query` (with `%s` or `%(name)s` placeholders) and return rows as dicts."""
    psycopg = require("psycopg", "postgres")
    rows_mod = require("psycopg.rows", "postgres")
    with psycopg.connect(dsn, row_factory=rows_mod.dict_row) as conn:
        return list(conn.execute(query, params or None).fetchall())


@stream_reader("postgres.read")
def postgres_read_chunks(
    input: Any = None,
    chunk_size: int = 50_000,
    dsn: str = "",
    query: str = "",
    params: list | dict | None = None,
) -> Iterator[list[dict]]:
    """Server-side cursor: the result set stays in Postgres and comes over `chunk_size` rows at a time."""
    psycopg = require("psycopg", "postgres")
    rows_mod = require("psycopg.rows", "postgres")
    with (
        psycopg.connect(dsn, row_factory=rows_mod.dict_row) as conn,
        conn.transaction(),
        conn.cursor(name="agentl_stream") as cur,
    ):
        cur.itersize = chunk_size
        cur.execute(query, params or None)
        while rows := cur.fetchmany(chunk_size):
            yield list(rows)


@stream_writer("postgres.write")
def postgres_write_chunks(
    chunks: Iterable[list[dict]],
    dsn: str = "",
    table: str = "",
    mode: str = "append",
    key: str | list[str] | None = None,
    batch_size: int = 1000,
    create: bool = True,
) -> list[dict]:
    """Writes every chunk in one transaction. The table is created from the first chunk with
    records; columns that first appear later are added, typed from the chunk they appear in."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    keys = [key] if isinstance(key, str) else list(key or [])
    if mode == "upsert" and not keys:
        raise ValueError("mode 'upsert' needs 'key'")
    psycopg = require("psycopg", "postgres")
    sql = require("psycopg.sql", "postgres")
    jsonb = require("psycopg.types.json", "postgres").Jsonb
    tbl = _ident(table)

    def val(v: Any) -> Any:
        return jsonb(v) if isinstance(v, (dict, list)) else v

    cols: list[str] = []
    written = 0
    with psycopg.connect(dsn) as conn, conn.transaction():
        if mode == "replace":
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(tbl))
        for rows in chunks:
            if not rows:
                continue
            new: list[str] = []
            for r in rows:
                new.extend(k for k in r if k not in cols and k not in new)
            missing = [k for k in keys if k not in cols and k not in new]
            if missing:
                raise ValueError(f"key column(s) not in records: {missing}")
            if new and create:
                types = {c: sql.SQL(_pg_type([r.get(c) for r in rows])) for c in new}
                if not cols:
                    defs = sql.SQL(", ").join(
                        sql.SQL("{} {}").format(sql.Identifier(c), types[c]) for c in new
                    )
                    conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(tbl, defs))
                    if mode == "upsert":
                        idx = "uq_" + "_".join([table.replace(".", "_"), *keys])[:55]
                        conn.execute(
                            sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})").format(
                                sql.Identifier(idx), tbl, sql.SQL(", ").join(map(sql.Identifier, keys))
                            )
                        )
                else:
                    for c in new:
                        conn.execute(
                            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                                tbl, sql.Identifier(c), types[c]
                            )
                        )
            cols.extend(new)
            stmt = _insert(sql, tbl, cols, keys if mode == "upsert" else [])
            with conn.cursor() as cur:
                for i in range(0, len(rows), batch_size):
                    cur.executemany(stmt, [[val(r.get(c)) for c in cols] for r in rows[i : i + batch_size]])
            written += len(rows)
    return [{"table": table, "written": written, "mode": mode}]


def _insert(sql: Any, tbl: Any, cols: list[str], keys: list[str]) -> Any:
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        tbl,
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)),
    )
    if not keys:
        return stmt
    rest = [c for c in cols if c not in keys]
    action = (
        sql.SQL("DO UPDATE SET {}").format(
            sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in rest)
        )
        if rest
        else sql.SQL("DO NOTHING")
    )
    return sql.SQL("{} ON CONFLICT ({}) {}").format(
        stmt, sql.SQL(", ").join(map(sql.Identifier, keys)), action
    )


@connector("postgres.write")
def postgres_write(
    input: Any,
    dsn: str = "",
    table: str = "",
    mode: str = "append",
    key: str | list[str] | None = None,
    batch_size: int = 1000,
    create: bool = True,
) -> list[dict]:
    """Write records to `table` (optionally `schema.table`).

    mode: append (insert), replace (drop and recreate), upsert (insert or update on `key`).
    The table is created from the records when missing; upsert adds a unique index on `key`.
    All batches run in one transaction."""
    return postgres_write_chunks(
        [single(input)], dsn=dsn, table=table, mode=mode, key=key, batch_size=batch_size, create=create
    )
