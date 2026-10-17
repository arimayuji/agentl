"""SQL connectors. v0.1 ships SQLite (stdlib); other databases come as plugins."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from ..registry import connector, stream_reader, stream_writer
from ._util import changeset_options, check_update_columns, single, upsert_options


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
    deletes: str | dict | None = None,
    partition_by: str | list[str] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Writes every chunk in one transaction. Columns that first appear in a later chunk are added
    to the table. `mode`: append, replace (drop the table first) or upsert (rows whose `key`
    columns match an existing row update it; see `sqlite_write` for on_conflict and update_columns).
    Upsert reports inserted, updated and unchanged; it needs no unique index on `key`."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"invalid table name '{table}'")
    if mode not in ("append", "replace", "upsert", "changeset"):
        raise ValueError(f"sqlite.write mode must be append, replace, upsert or changeset, got {mode!r}")
    keys = [key] if isinstance(key, str) else list(key or [])
    drop, parts = changeset_options("sqlite.write", mode, keys, deletes, partition_by, dry_run)
    if mode == "changeset":
        return _changeset(chunks, db, table, keys, drop, parts, dry_run)
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


_STAGE = '"_agentl_changeset"'


def _changeset(
    chunks: Iterable[list[dict]],
    db: str,
    table: str,
    keys: list[str],
    drop: str,
    parts: list[str],
    dry_run: bool,
) -> list[dict]:
    """mode: changeset. Every chunk is staged in a temp table first (deletes need the whole
    source), then the diff with the table runs as a handful of set-based statements. With
    `dry_run` the counts are computed and nothing in the database changes."""
    cols: list[str] = []
    if db and db != ":memory:" and not dry_run:
        Path(db).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(f"DROP TABLE IF EXISTS temp.{_STAGE}")
        for rows in chunks:
            new = [k for k in dict.fromkeys(k for r in rows for k in r) if k not in cols]
            for c in new:
                if not cols:
                    conn.execute(f"CREATE TEMP TABLE {_STAGE} ({_quote(c)}, _hit, _diff)")
                else:
                    conn.execute(f"ALTER TABLE temp.{_STAGE} ADD COLUMN {_quote(c)}")
                cols.append(c)
            if rows:
                names = ", ".join(map(_quote, cols))
                ph = ", ".join("?" for _ in cols)
                conn.executemany(
                    f"INSERT INTO temp.{_STAGE} ({names}) VALUES ({ph})",
                    [tuple(map(r.get, cols)) for r in rows],
                )
        have = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        if not cols:
            if not have:  # no source rows and no table: nothing to compare
                return [_counts(table, 0, 0, 0, 0, 0, dry_run, parts)]
            conn.execute(f"CREATE TEMP TABLE {_STAGE} ({', '.join(map(_quote, keys))}, _hit, _diff)")
            cols = list(keys)
        if missing := [k for k in keys if k not in cols]:
            raise ValueError(f"sqlite.write: key column(s) not in records: {missing}")
        if have and (absent := [k for k in keys if k not in have]):
            raise ValueError(f"sqlite.write: key column(s) not in table {table!r}: {absent}")
        klist = ", ".join(map(_quote, keys))
        conn.execute(  # last record of a key wins, as in upsert
            f"DELETE FROM temp.{_STAGE} WHERE rowid NOT IN "
            f"(SELECT max(rowid) FROM temp.{_STAGE} GROUP BY {klist})"
        )
        conn.execute(f"CREATE INDEX temp._agentl_changeset_k ON {_STAGE} ({klist})")
        if parts:
            conn.execute(
                f"CREATE INDEX temp._agentl_changeset_p ON {_STAGE} ({', '.join(map(_quote, parts))})"
            )
        total = conn.execute(f"SELECT count(*) FROM temp.{_STAGE}").fetchone()[0]
        rest = [c for c in cols if c not in keys]
        match = " AND ".join(f"t.{_quote(k)} IS s.{_quote(k)}" for k in keys)
        inserted = updated = deleted = 0
        gone = ""
        if have:
            # one scan of the table probing the indexed stage: mark hits, and hits whose values differ
            # (a column the table lacks differs when the record has a value for it)
            differs = (
                " OR ".join(
                    f"s.{_quote(c)} IS NOT t.{_quote(c)}" if c in have else f"s.{_quote(c)} IS NOT NULL"
                    for c in rest
                )
                or "0"
            )
            scan = f'SELECT s.rowid FROM "{table}" AS t CROSS JOIN temp.{_STAGE} AS s WHERE {match}'
            conn.execute(f"UPDATE temp.{_STAGE} SET _hit = 1 WHERE rowid IN ({scan})")
            conn.execute(f"UPDATE temp.{_STAGE} SET _diff = 1 WHERE rowid IN ({scan} AND ({differs}))")
            updated = conn.execute(f"SELECT count(*) FROM temp.{_STAGE} WHERE _diff = 1").fetchone()[0]
            gone = _gone(table, keys, drop, parts)
            if gone:
                deleted = conn.execute(f'SELECT count(*) FROM "{table}" AS t WHERE {gone}').fetchone()[0]
        inserted = total - conn.execute(f"SELECT count(*) FROM temp.{_STAGE} WHERE _hit = 1").fetchone()[0]
        changed_parts = total_parts = 0
        if parts:
            plist = ", ".join(f"s.{_quote(p)}" for p in parts)
            pmatch = " AND ".join(f"t.{_quote(p)} IS s.{_quote(p)}" for p in parts)
            lost = f' OR EXISTS (SELECT 1 FROM "{table}" AS t WHERE {pmatch} AND {gone})' if deleted else ""
            total_parts, changed_parts = conn.execute(
                f"SELECT count(*), coalesce(sum(ch), 0) FROM "
                f"(SELECT (max(s._hit IS NULL OR s._diff = 1){lost}) AS ch "
                f"FROM temp.{_STAGE} AS s GROUP BY {plist})"
            ).fetchone()
        if not dry_run and (inserted or updated or deleted or not have):
            names = ", ".join(map(_quote, cols))
            if not have:
                conn.execute(f'CREATE TABLE "{table}" ({names})')
            for c in cols:
                if have and c not in have:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {_quote(c)}')
            if deleted:
                conn.execute(f'DELETE FROM "{table}" AS t WHERE {gone}')
            if updated:
                sets = ", ".join(
                    f"{_quote(c)} = (SELECT s.{_quote(c)} FROM temp.{_STAGE} AS s WHERE {match})"
                    for c in rest
                )
                conn.execute(
                    f'UPDATE "{table}" AS t SET {sets} '
                    f"WHERE EXISTS (SELECT 1 FROM temp.{_STAGE} AS s WHERE s._diff = 1 AND {match})"
                )
            if inserted:
                conn.execute(
                    f'INSERT INTO "{table}" ({names}) SELECT {names} FROM temp.{_STAGE} WHERE _hit IS NULL'
                )
        conn.execute(f"DROP TABLE temp.{_STAGE}")
    out = _counts(table, total, inserted, updated, deleted, total - inserted - updated, dry_run, parts)
    if parts:
        out.update(partitions=total_parts, partitions_unchanged=total_parts - changed_parts)
    return [out]


def _gone(table: str, keys: list[str], drop: str, parts: list[str]) -> str:
    """WHERE clause (on the table aliased `t`) for rows the changeset deletes; empty for none."""
    if drop == "none":
        return ""
    match = " AND ".join(f"t.{_quote(k)} IS s.{_quote(k)}" for k in keys)
    cond = f"NOT EXISTS (SELECT 1 FROM temp.{_STAGE} AS s WHERE {match})"
    if drop == "partition_replaced":
        pmatch = " AND ".join(f"t.{_quote(p)} IS s.{_quote(p)}" for p in parts)
        cond += f" AND EXISTS (SELECT 1 FROM temp.{_STAGE} AS s WHERE {pmatch})"
    return cond


def _counts(
    table: str, n: int, ins: int, upd: int, dele: int, same: int, dry_run: bool, parts: list[str]
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "table": table,
        "written": 0 if dry_run else ins + upd + dele,
        "inserted": ins,
        "updated": upd,
        "deleted": dele,
        "unchanged": same,
        "outcome": "no_change" if not (ins or upd or dele) else "changed",
    }
    if dry_run:
        out["dry_run"] = True
    return out


@connector("sqlite.write")
def sqlite_write(
    input: Any,
    db: str = "",
    table: str = "",
    mode: str = "append",
    key: str | list[str] | None = None,
    on_conflict: str | None = None,
    update_columns: str | list[str] | None = None,
    deletes: str | dict | None = None,
    partition_by: str | list[str] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Write records to a SQLite table, created (and widened) as needed. `mode`: append (default),
    replace, or upsert with `key: id` (a record updates the row with the same key).
    Upsert only: on_conflict update (default; rows whose values did not change are left alone),
    do_nothing (keep the existing row) or error (fail on a duplicate key); update_columns limits
    the update to those columns. The result reports inserted, updated and unchanged.
    mode: changeset compares with the table and writes only what changed; `deletes: missing` (or
    `partition_replaced` with `partition_by`) also removes rows the source no longer has, and
    `dry_run: true` reports the counts without writing."""
    return sqlite_write_chunks(
        [single(input)],
        db=db,
        table=table,
        mode=mode,
        key=key,
        on_conflict=on_conflict,
        update_columns=update_columns,
        deletes=deletes,
        partition_by=partition_by,
        dry_run=dry_run,
    )
