"""SQL connectors. v0.1 ships SQLite (stdlib); other databases come as plugins."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from ..registry import connector, stream_reader, stream_writer
from ._util import check_update_columns, single, upsert_options


@connector("sqlite.read")
def sqlite_read(input: Any = None, db: str = "", query: str = "", params: list | None = None) -> list[dict]:
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(query, params or [])]


@stream_reader("sqlite.read")
def sqlite_read_chunks(
    input: Any = None, chunk_size: int = 50_000, db: str = "", query: str = "", params: list | None = None
) -> Iterator[list[dict]]:
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, params or [])
        while rows := cur.fetchmany(chunk_size):
            yield [dict(r) for r in rows]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@stream_writer("sqlite.write")
def sqlite_write_chunks(
    chunks: Iterable[list[dict]],
    db: str = "",
    table: str = "",
    mode: str = "append",
    key: str | list[str] | None = None,
    on_conflict: str | None = None,
    update_columns: str | list[str] | None = None,
) -> list[dict]:
    """Writes every chunk in one transaction. Columns that first appear in a later chunk are added
    to the table. `mode`: append, replace (drop the table first) or upsert (rows whose `key`
    columns match an existing row update it; see `sqlite_write` for on_conflict and update_columns).
    Upsert reports inserted, updated and unchanged; it needs no unique index on `key`."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"invalid table name '{table}'")
    if mode not in ("append", "replace", "upsert"):
        raise ValueError(f"sqlite.write mode must be append, replace or upsert, got {mode!r}")
    keys = [key] if isinstance(key, str) else list(key or [])
    if mode == "upsert" and not keys:
        raise ValueError(
            "sqlite.write mode: upsert needs `key` (the column, or columns, that identify a row)"
        )
    conflict, upd = upsert_options("sqlite.write", mode, keys, on_conflict, update_columns)
    written = inserted = updated = 0
    cols: list[str] = []
    if db and db != ":memory:":
        Path(db).parent.mkdir(parents=True, exist_ok=True)  # as file writers do
    with closing(sqlite3.connect(db)) as conn, conn:
        if mode == "replace":
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        for rows in chunks:
            new: list[str] = []
            known = set(cols)
            last: Any = None
            for r in rows:
                fields = r.keys()
                if fields == last:  # records of one source share their keys: one C-level compare
                    continue
                last = fields
                for k in fields:
                    if k not in known:
                        known.add(k)
                        new.append(k)
            if new:
                if not cols:
                    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(map(_quote, new))})')
                    have = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                else:
                    have = set(cols)
                for c in new:
                    if c not in have:
                        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {_quote(c)}')
                cols.extend(new)
            if not rows:
                continue
            names = ", ".join(map(_quote, cols))
            ph = ", ".join("?" for _ in cols)
            insert = f'INSERT INTO "{table}" ({names}) VALUES ({ph})'
            if mode != "upsert":
                conn.executemany(insert, [tuple(map(r.get, cols)) for r in rows])
                written += len(rows)
                continue
            missing = [k for k in keys if k not in cols]
            if missing:
                raise ValueError(f"sqlite.write: key column(s) not in records: {missing}")
            check_update_columns("sqlite.write", upd, cols)
            # Set-based, so a large load stays fast: the chunk goes to a temp table, then one
            # UPDATE for changed rows and one INSERT for new keys.
            tmp = '"_agentl_upsert"'
            conn.execute(f"DROP TABLE IF EXISTS temp.{tmp}")
            conn.execute(f"CREATE TEMP TABLE {tmp} ({names}, _hit)")
            conn.executemany(
                f"INSERT INTO temp.{tmp} ({names}) VALUES ({ph})", [tuple(map(r.get, cols)) for r in rows]
            )
            klist = ", ".join(map(_quote, keys))
            conn.execute(f"CREATE INDEX temp._agentl_upsert_k ON {tmp} ({klist})")
            if conflict == "error":
                dup = conn.execute(
                    f"SELECT {klist} FROM temp.{tmp} GROUP BY {klist} HAVING count(*) > 1"
                ).fetchone()
                if dup is not None:
                    found = dict(zip(keys, dup, strict=True))
                    raise ValueError(f"sqlite.write: on_conflict: error and key {found} repeats in the input")
            else:  # last one wins
                conn.execute(
                    f"DELETE FROM temp.{tmp} WHERE rowid NOT IN "
                    f"(SELECT max(rowid) FROM temp.{tmp} GROUP BY {klist})"
                )
            match = " AND ".join(f"t.{_quote(k)} IS s.{_quote(k)}" for k in keys)
            # CROSS JOIN keeps the target table outside: one scan of it, probing the indexed temp table
            # (the target may have no index on the key).
            conn.execute(
                f"UPDATE temp.{tmp} SET _hit = 1 WHERE rowid IN "
                f'(SELECT s.rowid FROM "{table}" AS t CROSS JOIN temp.{tmp} AS s WHERE {match})'
            )
            if conflict == "error":
                hit = conn.execute(f"SELECT {klist} FROM temp.{tmp} WHERE _hit = 1 LIMIT 1").fetchone()
                if hit is not None:
                    found = dict(zip(keys, hit, strict=True))
                    raise ValueError(f"sqlite.write: on_conflict: error and a row with key {found} exists")
            rest = upd if upd is not None else [c for c in cols if c not in keys]
            if conflict == "update" and rest:
                sets = ", ".join(
                    f"{_quote(c)} = (SELECT s.{_quote(c)} FROM temp.{tmp} AS s WHERE {match})" for c in rest
                )
                same = " AND ".join(f"s.{_quote(c)} IS t.{_quote(c)}" for c in rest)
                cur = conn.execute(
                    f'UPDATE "{table}" AS t SET {sets} '
                    f"WHERE EXISTS (SELECT 1 FROM temp.{tmp} AS s WHERE {match} AND NOT ({same}))"
                )
                updated += cur.rowcount
            cur = conn.execute(
                f'INSERT INTO "{table}" ({names}) SELECT {names} FROM temp.{tmp} WHERE _hit IS NULL'
            )
            inserted += cur.rowcount
            n = conn.execute(f"SELECT count(*) FROM temp.{tmp}").fetchone()[0]
            conn.execute(f"DROP TABLE temp.{tmp}")
            written += n
    out: dict[str, Any] = {"table": table, "written": written}
    if mode == "upsert":
        out.update(inserted=inserted, updated=updated, unchanged=written - inserted - updated)
    return [out]


@connector("sqlite.write")
def sqlite_write(
    input: Any,
    db: str = "",
    table: str = "",
    mode: str = "append",
    key: str | list[str] | None = None,
    on_conflict: str | None = None,
    update_columns: str | list[str] | None = None,
) -> list[dict]:
    """Write records to a SQLite table, created (and widened) as needed. `mode`: append (default),
    replace, or upsert with `key: id` (a record updates the row with the same key).
    Upsert only: on_conflict update (default; rows whose values did not change are left alone),
    do_nothing (keep the existing row) or error (fail on a duplicate key); update_columns limits
    the update to those columns. The result reports inserted, updated and unchanged."""
    return sqlite_write_chunks(
        [single(input)],
        db=db,
        table=table,
        mode=mode,
        key=key,
        on_conflict=on_conflict,
        update_columns=update_columns,
    )
