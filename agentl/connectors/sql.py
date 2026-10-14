"""SQL connectors. v0.1 ships SQLite (stdlib); other databases come as plugins."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
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
    chunks: Iterable[list[dict]], db: str = "", table: str = "", mode: str = "append"
) -> list[dict]:
    """Writes every chunk in one transaction. Columns that first appear in a later chunk are added
    to the table."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"invalid table name '{table}'")
    written = 0
    cols: list[str] = []
    with closing(sqlite3.connect(db)) as conn, conn:
        if mode == "replace":
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        for rows in chunks:
            new: list[str] = []
            for r in rows:
                new.extend(k for k in r if k not in cols and k not in new)
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
            if rows:
                names = ", ".join(map(_quote, cols))
                ph = ", ".join("?" for _ in cols)
                conn.executemany(
                    f'INSERT INTO "{table}" ({names}) VALUES ({ph})', [[r.get(c) for c in cols] for r in rows]
                )
            written += len(rows)
    return [{"table": table, "written": written}]


@connector("sqlite.write")
def sqlite_write(input: Any, db: str = "", table: str = "", mode: str = "append") -> list[dict]:
    return sqlite_write_chunks([single(input)], db=db, table=table, mode=mode)
