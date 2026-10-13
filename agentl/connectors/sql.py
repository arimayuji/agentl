"""SQL connectors. v0.1 ships SQLite (stdlib); other databases come as plugins."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from ..registry import connector
from ._util import single


@connector("sqlite.read")
def sqlite_read(input: Any = None, db: str = "", query: str = "", params: list | None = None) -> list[dict]:
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(query, params or [])]


@connector("sqlite.write")
def sqlite_write(input: Any, db: str = "", table: str = "", mode: str = "append") -> list[dict]:
    rows = single(input)
    if not table.replace("_", "").isalnum():
        raise ValueError(f"invalid table name '{table}'")
    cols: list[str] = []
    for r in rows:
        cols.extend(k for k in r if k not in cols)
    with closing(sqlite3.connect(db)) as conn, conn:
        if mode == "replace":
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        if cols:
            names = ", ".join('"' + c.replace('"', '""') + '"' for c in cols)
            ph = ", ".join("?" for _ in cols)
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({names})')
            conn.executemany(
                f'INSERT INTO "{table}" ({names}) VALUES ({ph})', [[r.get(c) for c in cols] for r in rows]
            )
    return [{"table": table, "written": len(rows)}]
