"""PostgreSQL connectors (psycopg 3). Install with `pip install 'agentl-etl[postgres]'`."""

from __future__ import annotations

import datetime as _dt
import decimal
from collections.abc import Iterable, Iterator
from typing import Any

from ..registry import Rejects, arrow_writer, connector, require, stream_reader, stream_writer
from ._util import (
    check_scd2_columns,
    check_update_columns,
    null_keys,
    scd2_options,
    single,
    upsert_options,
)

MODES = ("append", "replace", "upsert", "scd2")


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
    on_conflict: str | None = None,
    update_columns: str | list[str] | None = None,
    track: str | list[str] | None = None,
    on_missing: str | None = None,
    as_of: Any = None,
) -> list[dict]:
    """Writes every chunk in one transaction. The table is created from the first chunk with
    records; columns that first appear later are added, typed from the chunk they appear in.
    scd2 stages the input in a temp table and merges it once at the end (see `postgres_write`)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    keys = [key] if isinstance(key, str) else list(key or [])
    if mode == "upsert" and not keys:
        raise ValueError("mode 'upsert' needs 'key'")
    conflict, upd = upsert_options("postgres.write", mode, keys, on_conflict, update_columns)
    tracked, missing_rows, when = scd2_options("postgres.write", mode, keys, track, on_missing, as_of)
    psycopg = require("psycopg", "postgres")
    sql = require("psycopg.sql", "postgres")
    stage = sql.Identifier("_agentl_scd2")
    staged = False
    jsonb = require("psycopg.types.json", "postgres").Jsonb
    tbl = _ident(table)

    def val(v: Any) -> Any:
        return jsonb(v) if isinstance(v, (dict, list)) else v

    cols: list[str] = []
    written = inserted = updated = 0
    rejected: list[dict] = []
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
            if new and mode == "scd2":
                check_scd2_columns("postgres.write", None, new)
            if new and create:
                types = {c: sql.SQL(_pg_type([r.get(c) for r in rows])) for c in new}
                if not cols:
                    defs = sql.SQL(", ").join(
                        sql.SQL("{} {}").format(sql.Identifier(c), types[c]) for c in new
                    )
                    if mode == "scd2":
                        defs = sql.SQL(
                            "{}, valid_from timestamptz NOT NULL, valid_to timestamptz, "
                            "is_current boolean NOT NULL"
                        ).format(defs)
                    conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(tbl, defs))
                    if mode == "scd2":  # one current version per key
                        idx = "uq_" + "_".join([table.replace(".", "_"), *keys])[:46] + "_current"
                        conn.execute(
                            sql.SQL(
                                "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({}) WHERE is_current"
                            ).format(sql.Identifier(idx), tbl, sql.SQL(", ").join(map(sql.Identifier, keys)))
                        )
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
            if mode == "scd2":
                if not staged:
                    conn.execute(
                        sql.SQL(
                            "CREATE TEMP TABLE {} (LIKE {}, _ord bigint GENERATED ALWAYS AS IDENTITY, "
                            "_st smallint) ON COMMIT DROP"
                        ).format(stage, tbl)
                    )
                    conn.execute(  # LIKE copies NOT NULL; the stage never holds these columns
                        sql.SQL(
                            "ALTER TABLE {} ALTER valid_from DROP NOT NULL, ALTER is_current DROP NOT NULL"
                        ).format(stage)
                    )
                    staged = True
                else:
                    for c in new:
                        conn.execute(
                            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                                stage, sql.Identifier(c), sql.SQL(_pg_type([r.get(c) for r in rows]))
                            )
                        )
            cols.extend(new)
            if mode == "scd2":
                rows, bad = null_keys("postgres.write", rows, keys)
                rejected.extend(bad)
                stmt = _insert(sql, stage, cols, [], conflict, None)
                with conn.cursor() as cur:
                    for i in range(0, len(rows), batch_size):
                        cur.executemany(
                            stmt, [[val(r.get(c)) for c in cols] for r in rows[i : i + batch_size]]
                        )
                continue
            if mode != "upsert":
                stmt = _insert(sql, tbl, cols, [], conflict, None)
                with conn.cursor() as cur:
                    for i in range(0, len(rows), batch_size):
                        cur.executemany(
                            stmt, [[val(r.get(c)) for c in cols] for r in rows[i : i + batch_size]]
                        )
                written += len(rows)
                continue
            check_update_columns("postgres.write", upd, cols)
            rows, bad = null_keys("postgres.write", rows, keys)
            rejected.extend(bad)
            stmt = _insert(sql, tbl, cols, keys, conflict, upd)
            with conn.cursor() as cur:
                for i in range(0, len(rows), batch_size):
                    params = [[val(r.get(c)) for c in cols] for r in rows[i : i + batch_size]]
                    if conflict == "error":
                        cur.executemany(stmt, params)
                        inserted += len(params)
                        continue
                    cur.executemany(stmt, params, returning=True)
                    while True:  # one result per row that was inserted or changed
                        for (was_insert,) in cur.fetchall():
                            if was_insert:
                                inserted += 1
                            else:
                                updated += 1
                        if not cur.nextset():
                            break
            written += len(rows)
        if mode == "scd2":
            if not staged:  # an empty input changes nothing, even with on_missing: close
                done = [
                    {
                        "table": table,
                        "written": 0,
                        "mode": mode,
                        "inserted": 0,
                        "versioned": 0,
                        "unchanged": 0,
                    }
                ]
            else:
                counts = _pg_scd2(conn, sql, tbl, stage, cols, keys, tracked, missing_rows, when)
                done = [{"table": table, "written": counts.pop("written"), "mode": mode, **counts}]
            return Rejects(done, rejected) if rejected else done
    out: dict[str, Any] = {"table": table, "written": written, "mode": mode}
    if mode == "upsert":
        out.update(inserted=inserted, updated=updated, unchanged=written - inserted - updated)
    return Rejects([out], rejected) if rejected else [out]


def _pg_scd2(
    conn: Any,
    sql: Any,
    tbl: Any,
    stage: Any,
    cols: list[str],
    keys: list[str],
    track: list[str] | None,
    on_missing: str,
    when: Any,
) -> dict[str, Any]:
    """Merge the staged source into the versioned table with a fixed number of statements.
    `_st` is NULL for a new key, 1 for unchanged, 2 for changed."""
    check_scd2_columns("postgres.write", track, cols)
    tracked = track if track is not None else [c for c in cols if c not in keys]
    ident = sql.Identifier
    k_ids = [ident(k) for k in keys]
    null_key = sql.SQL(" OR ").join(sql.SQL("{} IS NULL").format(k) for k in k_ids)
    if conn.execute(sql.SQL("SELECT 1 FROM {} WHERE {} LIMIT 1").format(stage, null_key)).fetchone():
        raise ValueError(f"postgres.write: mode: scd2 needs a value in every key column {keys}")

    def match(a: str, b: str) -> Any:
        return sql.SQL(" AND ").join(sql.SQL("{}.{} = {}.{}").format(ident(a), k, ident(b), k) for k in k_ids)

    conn.execute(  # last record per key wins
        sql.SQL("DELETE FROM {0} AS a USING {0} AS b WHERE {1} AND a._ord < b._ord").format(
            stage, match("a", "b")
        )
    )
    differ = (
        sql.SQL("({}) IS DISTINCT FROM ({})").format(
            sql.SQL(", ").join(sql.SQL("t.{}").format(ident(c)) for c in tracked),
            sql.SQL(", ").join(sql.SQL("s.{}").format(ident(c)) for c in tracked),
        )
        if tracked
        else sql.SQL("false")
    )
    conn.execute(
        sql.SQL(
            "UPDATE {} AS s SET _st = CASE WHEN {} THEN 2 ELSE 1 END FROM {} AS t WHERE t.is_current AND {}"
        ).format(stage, differ, tbl, match("t", "s"))
    )
    late = conn.execute(
        sql.SQL(
            "SELECT 1 FROM {} AS t JOIN {} AS s ON {} "
            "WHERE t.is_current AND s._st = 2 AND t.valid_from > %s LIMIT 1"
        ).format(tbl, stage, match("t", "s")),
        [when],
    ).fetchone()
    if late:
        raise ValueError(f"postgres.write: as_of {when.isoformat()} is before the start of a current version")
    conn.execute(
        sql.SQL(
            "UPDATE {} AS t SET valid_to = %s, is_current = false FROM {} AS s "
            "WHERE t.is_current AND s._st = 2 AND {}"
        ).format(tbl, stage, match("t", "s")),
        [when],
    )
    out: dict[str, Any] = {}
    if on_missing == "close":
        out["closed"] = conn.execute(
            sql.SQL(
                "UPDATE {} AS t SET valid_to = %s, is_current = false WHERE t.is_current "
                "AND NOT EXISTS (SELECT 1 FROM {} AS s WHERE {})"
            ).format(tbl, stage, match("t", "s")),
            [when],
        ).rowcount
    names = sql.SQL(", ").join(map(ident, cols))
    conn.execute(
        sql.SQL(
            "INSERT INTO {} ({}, valid_from, valid_to, is_current) "
            "SELECT {}, %s, NULL, true FROM {} WHERE _st IS NULL OR _st = 2 ORDER BY _ord"
        ).format(tbl, names, names, stage),
        [when],
    )
    counts: dict[Any, int] = dict.fromkeys((None, 1, 2), 0)
    counts.update(conn.execute(sql.SQL("SELECT _st, count(*) FROM {} GROUP BY _st").format(stage)).fetchall())
    conn.execute(sql.SQL("DROP TABLE {}").format(stage))
    return {
        "written": sum(counts.values()),
        "inserted": counts[None],
        "versioned": counts[2],
        "unchanged": counts[1],
        **out,
        "as_of": when.isoformat(),
    }


def _insert(
    sql: Any, tbl: Any, cols: list[str], keys: list[str], conflict: str, upd: list[str] | None
) -> Any:
    """INSERT for append/replace; for upsert, an ON CONFLICT clause that only touches rows whose
    values change and RETURNING `(xmax = 0)`, true for an inserted row and false for an updated one."""
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        tbl,
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)),
    )
    if not keys or conflict == "error":
        return stmt
    rest = upd if upd is not None else [c for c in cols if c not in keys]
    target = sql.SQL(", ").join(map(sql.Identifier, keys))
    if conflict == "do_nothing" or not rest:
        return sql.SQL("{} ON CONFLICT ({}) DO NOTHING RETURNING true").format(stmt, target)
    ids = [sql.Identifier(c) for c in rest]
    return sql.SQL(
        "{} ON CONFLICT ({}) DO UPDATE SET {} WHERE ({}) IS DISTINCT FROM ({}) RETURNING (xmax = 0)"
    ).format(
        stmt,
        target,
        sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(i) for i in ids),
        sql.SQL(", ").join(sql.SQL("{}.{}").format(tbl, i) for i in ids),
        sql.SQL(", ").join(sql.SQL("EXCLUDED.{}").format(i) for i in ids),
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
    on_conflict: str | None = None,
    update_columns: str | list[str] | None = None,
    track: str | list[str] | None = None,
    on_missing: str | None = None,
    as_of: Any = None,
) -> list[dict]:
    """Write records to `table` (optionally `schema.table`).

    mode: append (insert), replace (drop and recreate), upsert (insert or update on `key`).
    The table is created from the records when missing; upsert adds a unique index on `key`.
    Upsert only: on_conflict update (default; rows whose values did not change are left alone),
    do_nothing (keep the existing row) or error (fail on a duplicate key); update_columns limits
    the update to those columns. The result reports inserted, updated and unchanged.
    scd2 keeps history (slowly changing dimension type 2) in `valid_from`, `valid_to` (NULL while
    current) and `is_current`, with a unique index on `key` among current rows: a record whose
    `track` columns (default: all but the key) changed closes the current version and adds a new
    one; on_missing: close also closes keys absent from the input. `as_of` (default now, needs a
    time zone) is stamped as timestamptz. The result reports inserted, versioned, unchanged
    (and closed). All batches run in one transaction."""
    return postgres_write_chunks(
        [single(input)],
        dsn=dsn,
        table=table,
        mode=mode,
        key=key,
        batch_size=batch_size,
        create=create,
        on_conflict=on_conflict,
        update_columns=update_columns,
        track=track,
        on_missing=on_missing,
        as_of=as_of,
    )


def _arrow_pg_type(t: Any) -> str | None:
    """Postgres type for an Arrow type, matching what `_pg_type` picks for the same Python values;
    None when COPY cannot take it as is (nested or binary types)."""
    pa = require("pyarrow", "arrow")
    types = pa.types
    if types.is_null(t) or types.is_string(t) or types.is_large_string(t):
        return "text"
    if types.is_boolean(t):
        return "boolean"
    if types.is_integer(t):
        return "bigint"
    if types.is_floating(t):
        return "double precision"
    if types.is_decimal(t):
        return "numeric"
    if types.is_timestamp(t):
        return "timestamptz"
    if types.is_date(t):
        return "date"
    return None


@arrow_writer(
    "postgres.write",
    accepts=lambda w: (
        w.get("mode", "append") in ("append", "replace")
        and w.get("on_conflict") is None
        and w.get("update_columns") is None
    ),
)
def postgres_write_arrow(
    data: Any,
    dsn: str = "",
    table: str = "",
    mode: str = "append",
    key: str | list[str] | None = None,
    batch_size: int = 1000,
    create: bool = True,
) -> list[dict]:
    """append/replace from an Arrow table with COPY, one transaction. Nested or binary columns fall
    back to the normal writer."""
    from ..polars import from_polars, untag

    if from_polars(data):
        data = untag(data)
    name = table
    pg_types = {f.name: _arrow_pg_type(f.type) for f in data.schema}
    if None in pg_types.values() or not data.num_columns:
        return postgres_write_chunks(
            [data.to_pylist()], dsn=dsn, table=name, mode=mode, key=key, batch_size=batch_size, create=create
        )
    psycopg = require("psycopg", "postgres")
    sql = require("psycopg.sql", "postgres")
    tbl = _ident(name)
    cols = list(data.column_names)
    with psycopg.connect(dsn) as conn, conn.transaction():
        if mode == "replace":
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(tbl))
        if data.num_rows == 0:
            return [{"table": name, "written": 0, "mode": mode}]
        if create:
            defs = sql.SQL(", ").join(
                sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(pg_types[c] or "text")) for c in cols
            )
            conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(tbl, defs))
        stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(tbl, sql.SQL(", ").join(map(sql.Identifier, cols)))
        with conn.cursor() as cur, cur.copy(stmt) as copy:
            for batch in data.to_batches(max_chunksize=50_000):
                columns = [batch.column(i).to_pylist() for i in range(batch.num_columns)]
                for row in zip(*columns, strict=True):
                    copy.write_row(row)
    return [{"table": name, "written": data.num_rows, "mode": mode}]
