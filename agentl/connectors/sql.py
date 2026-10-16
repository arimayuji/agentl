"""SQL connectors. v0.1 ships SQLite (stdlib); other databases come as plugins."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from ..registry import connector, stream_reader, stream_writer
from ._util import single


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
) -> list[dict]:
    """Writes every chunk in one transaction. Columns that first appear in a later chunk are added
    to the table. `mode`: append, replace (drop the table first) or upsert (rows whose `key`
    columns match an existing row replace it)."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"invalid table name '{table}'")
    if mode not in ("append", "replace", "upsert"):
        raise ValueError(f"sqlite.write mode must be append, replace or upsert, got {mode!r}")
    keys = [key] if isinstance(key, str) else list(key or [])
    if mode == "upsert" and not keys:
        raise ValueError(
            "sqlite.write mode: upsert needs `key` (the column, or columns, that identify a row)"
        )
    written = 0
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
            if rows and mode == "upsert":
                rows = list({tuple(r.get(k) for k in keys): r for r in rows}.values())  # last one wins
                where = " AND ".join(f"{_quote(k)} IS ?" for k in keys)
                conn.executemany(
                    f'DELETE FROM "{table}" WHERE {where}', [[r.get(k) for k in keys] for r in rows]
                )
            if rows:
                names = ", ".join(map(_quote, cols))
                ph = ", ".join("?" for _ in cols)
                conn.executemany(
                    f'INSERT INTO "{table}" ({names}) VALUES ({ph})', [tuple(map(r.get, cols)) for r in rows]
                )
            written += len(rows)
    return [{"table": table, "written": written}]


@connector("sqlite.write")
def sqlite_write(
    input: Any, db: str = "", table: str = "", mode: str = "append", key: str | list[str] | None = None
) -> list[dict]:
    """Write records to a SQLite table, created (and widened) as needed. `mode`: append (default),
    replace, or upsert with `key: id` (a record replaces the row with the same key)."""
    return sqlite_write_chunks([single(input)], db=db, table=table, mode=mode, key=key)
