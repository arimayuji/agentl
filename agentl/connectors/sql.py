"""SQL connectors. v0.1 ships SQLite (stdlib); other databases come as plugins."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from ..registry import Changes, Rejects, connector, stream_reader, stream_writer
from ._util import (
    CHECKSUMS_TABLE,
    SCD2_COLUMNS,
    PartitionSums,
    before,
    changeset_options,
    check_scd2_columns,
    check_update_columns,
    emit_option,
    null_keys,
    scd2_options,
    single,
    upsert_options,
)


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


_AFFINITY = {bool: "INTEGER", int: "INTEGER", float: "REAL", str: "TEXT", bytes: "BLOB"}


def _column(name: str, rows: list[dict]) -> str:
    """Quoted column with a declared type when every non-null value has one Python type."""
    kinds = {type(v) for v in map(lambda r: r.get(name), rows) if v is not None}
    t = _AFFINITY.get(kinds.pop()) if len(kinds) == 1 else None
    return f"{_quote(name)} {t}" if t else _quote(name)


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
    checksums: bool = False,
    emit: str | None = None,
    track: str | list[str] | None = None,
    on_missing: str | None = None,
    as_of: Any = None,
) -> list[dict]:
    """Writes every chunk in one transaction. Columns that first appear in a later chunk are added
    to the table. `mode`: append, replace (drop the table first) or upsert (rows whose `key`
    columns match an existing row update it; see `sqlite_write` for on_conflict and update_columns).
    Upsert reports inserted, updated and unchanged; it needs no unique index on `key` (a table it
    creates gets one). Records with a null key field are rejected, not written (upsert and scd2).
    scd2 keeps every version of a row (see `sqlite_write`); the source is staged in a temp table
    and merged once at the end, so `on_missing: close` sees the whole input."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"invalid table name '{table}'")
    if mode not in ("append", "replace", "upsert", "scd2", "changeset"):
        raise ValueError(
            f"sqlite.write mode must be append, replace, upsert, scd2 or changeset, got {mode!r}"
        )
    keys = [key] if isinstance(key, str) else list(key or [])
    drop, parts = changeset_options("sqlite.write", mode, keys, deletes, partition_by, dry_run, checksums)
    emit_option("sqlite.write", mode, emit)
    if mode == "changeset":
        return _changeset(chunks, db, table, keys, drop, parts, dry_run, checksums, emit)
    if mode == "upsert" and not keys:
        raise ValueError(
            "sqlite.write mode: upsert needs `key` (the column, or columns, that identify a row)"
        )
    conflict, upd = upsert_options("sqlite.write", mode, keys, on_conflict, update_columns)
    tracked, missing_rows, when = scd2_options("sqlite.write", mode, keys, track, on_missing, as_of)
    stage = '"_agentl_scd2"'
    staged = False
    written = inserted = updated = 0
    cols: list[str] = []
    rejected: list[dict] = []
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
            if new and mode == "scd2":
                check_scd2_columns("sqlite.write", None, new)
            if new:
                if not cols:
                    have = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                    if not have:  # a new table: typed columns, and a unique index on an upsert key
                        defs = ", ".join(_column(c, rows) for c in new)
                        conn.execute(f'CREATE TABLE "{table}" ({defs})')
                        have = set(new)
                        if mode == "upsert" and all(k in have for k in keys):
                            idx = _quote("uq_" + "_".join([table, *keys])[:55])
                            klist = ", ".join(map(_quote, keys))
                            conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON "{table}" ({klist})')
                else:
                    have = set(cols)
                for c in new:
                    if c not in have:
                        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {_column(c, rows)}')
                if mode == "scd2":
                    for c in SCD2_COLUMNS:
                        if c not in have and not cols:
                            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {_quote(c)}')
                    if staged:
                        for c in new:
                            conn.execute(f"ALTER TABLE temp.{stage} ADD COLUMN {_quote(c)}")
                cols.extend(new)
            if not rows:
                continue
            names = ", ".join(map(_quote, cols))
            ph = ", ".join("?" for _ in cols)
            insert = f'INSERT INTO "{table}" ({names}) VALUES ({ph})'
            if mode == "scd2":
                rows, bad = null_keys("sqlite.write", rows, keys)
                rejected.extend(bad)
                if not staged:
                    conn.execute(f"DROP TABLE IF EXISTS temp.{stage}")
                    conn.execute(f"CREATE TEMP TABLE {stage} ({names}, _st)")
                    staged = True
                conn.executemany(
                    f"INSERT INTO temp.{stage} ({names}) VALUES ({ph})",
                    [tuple(map(r.get, cols)) for r in rows],
                )
                continue
            if mode != "upsert":
                conn.executemany(insert, [tuple(map(r.get, cols)) for r in rows])
                written += len(rows)
                continue
            missing = [k for k in keys if k not in cols]
            if missing:
                raise ValueError(f"sqlite.write: key column(s) not in records: {missing}")
            check_update_columns("sqlite.write", upd, cols)
            rows, bad = null_keys("sqlite.write", rows, keys)
            rejected.extend(bad)
            if not rows:
                continue
            # Set-based, so a large load stays fast: the chunk goes to a temp table, then one
            # UPDATE for changed rows and one INSERT for new keys.
            tmp = '"_agentl_upsert"'
            conn.execute(f"DROP TABLE IF EXISTS temp.{tmp}")
            # The same declared types as the target, so comparisons on the key can use the indexes.
            decl = {row[1]: row[2] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            tdefs = ", ".join(f"{_quote(c)} {decl.get(c, '')}".rstrip() for c in cols)
            conn.execute(f"CREATE TEMP TABLE {tmp} ({tdefs}, _hit)")
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
        if mode == "scd2":
            if staged:  # an empty input changes nothing, even with on_missing: close
                counts = _sqlite_scd2(conn, table, stage, cols, keys, tracked, missing_rows, when)
                done = [{"table": table, "written": counts.pop("written"), "mode": mode, **counts}]
            else:
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
            return Rejects(done, rejected) if rejected else done
    out: dict[str, Any] = {"table": table, "written": written}
    if mode == "upsert":
        out.update(inserted=inserted, updated=updated, unchanged=written - inserted - updated)
    return Rejects([out], rejected) if rejected else [out]


_STAGE = '"_agentl_changeset"'


def _changeset(
    chunks: Iterable[list[dict]],
    db: str,
    table: str,
    keys: list[str],
    drop: str,
    parts: list[str],
    dry_run: bool,
    checksums: bool = False,
    emit: str | None = None,
) -> list[dict]:
    """mode: changeset. Every chunk is staged in a temp table first (deletes need the whole
    source), then the diff with the table runs as a handful of set-based statements. With
    `dry_run` the counts are computed and nothing in the database changes. With `checksums`,
    staged rows of a partition whose checksum equals the stored one are marked `_skip` (and `_hit`)
    and left out of the comparison and of the deletes."""
    sums = PartitionSums(parts) if checksums else None
    changes: list[dict] | None = [] if emit_option("sqlite.write", "changeset", emit) else None
    skipped = 0
    cols: list[str] = []
    rejected: list[dict] = []  # null keys cannot be matched to a row, as in upsert
    if db and db != ":memory:" and not dry_run:
        Path(db).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(f"DROP TABLE IF EXISTS temp.{_STAGE}")
        for rows in chunks:
            rows, bad = null_keys("sqlite.write", list(rows), keys)
            rejected.extend(bad)
            if sums:
                sums.add(rows)
            new = [k for k in dict.fromkeys(k for r in rows for k in r) if k not in cols]
            for c in new:
                if not cols:
                    conn.execute(f"CREATE TEMP TABLE {_STAGE} ({_quote(c)}, _hit, _diff, _skip)")
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
                empty = [_counts(table, 0, 0, 0, 0, 0, dry_run, parts)]
                if changes is not None:
                    return Changes([], empty, rejected)
                return Rejects(empty, rejected) if rejected else empty
            conn.execute(f"CREATE TEMP TABLE {_STAGE} ({', '.join(map(_quote, keys))}, _hit, _diff, _skip)")
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
        if sums and have:
            equal = sums.equal(_stored_sums(conn, table))
            where = " AND ".join(f"{_quote(p)} IS ?" for p in parts)
            conn.executemany(f"UPDATE temp.{_STAGE} SET _hit = 1, _skip = 1 WHERE {where}", equal)
            skipped = len(equal)
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
            scan = (
                f'SELECT s.rowid FROM "{table}" AS t CROSS JOIN temp.{_STAGE} AS s '
                f"WHERE s._skip IS NULL AND {match}"
            )
            conn.execute(f"UPDATE temp.{_STAGE} SET _hit = 1 WHERE rowid IN ({scan})")
            conn.execute(f"UPDATE temp.{_STAGE} SET _diff = 1 WHERE rowid IN ({scan} AND ({differs}))")
            updated = conn.execute(f"SELECT count(*) FROM temp.{_STAGE} WHERE _diff = 1").fetchone()[0]
            gone = _gone(table, keys, drop, parts, skipped > 0)
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
        if changes is not None:
            changes.extend(_changes(conn, table, cols, keys, have, gone if deleted else "", match))
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
        if sums and not dry_run:
            _save_sums(conn, table, sums.digests(), drop == "missing")
        conn.execute(f"DROP TABLE temp.{_STAGE}")
    out = _counts(table, total, inserted, updated, deleted, total - inserted - updated, dry_run, parts)
    if parts:
        out.update(partitions=total_parts, partitions_unchanged=total_parts - changed_parts)
    if sums:
        out["partitions_skipped"] = skipped
    if changes is not None:
        return Changes(changes, [out], rejected)
    return Rejects([out], rejected) if rejected else [out]


def _changes(
    conn: sqlite3.Connection,
    table: str,
    cols: list[str],
    keys: list[str],
    have: list[str],
    gone: str,
    match: str,
) -> list[dict]:
    """The records a changeset is about to write, read before it writes them: inserted and updated
    ones as the source has them (updates with `_before`, the stored value of each changed column),
    deleted ones as the table has them."""
    names = ", ".join(f"s.{_quote(c)}" for c in cols)
    out: list[dict] = []
    for row in conn.execute(f"SELECT {names} FROM temp.{_STAGE} AS s WHERE s._hit IS NULL ORDER BY s.rowid"):
        out.append({**dict(zip(cols, row, strict=True)), "_change": "inserted"})
    if not have:
        return out
    rest = [c for c in cols if c not in keys]
    old = [c for c in rest if c in have]
    olds = "".join(f", t.{_quote(c)}" for c in old)
    q = (
        f'SELECT {names}{olds} FROM temp.{_STAGE} AS s JOIN "{table}" AS t ON {match} '
        "WHERE s._diff = 1 ORDER BY s.rowid"
    )
    for row in conn.execute(q):
        new = dict(zip(cols, row[: len(cols)], strict=True))
        prev = dict(zip(old, row[len(cols) :], strict=True))
        out.append({**new, "_change": "updated", "_before": before(new, prev, rest)})
    if gone:
        t_names = ", ".join(f"t.{_quote(c)}" for c in have)
        for row in conn.execute(f'SELECT {t_names} FROM "{table}" AS t WHERE {gone} ORDER BY t.rowid'):
            out.append({**dict(zip(have, row, strict=True)), "_change": "deleted"})
    return out


def _gone(table: str, keys: list[str], drop: str, parts: list[str], skip: bool = False) -> str:
    """WHERE clause (on the table aliased `t`) for rows the changeset deletes; empty for none.
    Rows in a partition skipped by checksum are never deleted."""
    if drop == "none":
        return ""
    match = " AND ".join(f"t.{_quote(k)} IS s.{_quote(k)}" for k in keys)
    cond = f"NOT EXISTS (SELECT 1 FROM temp.{_STAGE} AS s WHERE {match})"
    pmatch = " AND ".join(f"t.{_quote(p)} IS s.{_quote(p)}" for p in parts)
    if drop == "partition_replaced":
        cond += f" AND EXISTS (SELECT 1 FROM temp.{_STAGE} AS s WHERE {pmatch})"
    if skip:
        cond += f" AND NOT EXISTS (SELECT 1 FROM temp.{_STAGE} AS s WHERE s._skip = 1 AND {pmatch})"
    return cond


def _stored_sums(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """Partition checksums the last changeset write of `table` stored (empty when none)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", [CHECKSUMS_TABLE]).fetchone():
        return {}
    rows = conn.execute(f"SELECT part, digest FROM {_quote(CHECKSUMS_TABLE)} WHERE tbl = ?", [table])
    return dict(rows.fetchall())


def _save_sums(conn: sqlite3.Connection, table: str, digests: dict[str, str], replace: bool) -> None:
    """Store the source checksums; `replace` (deletes: missing) also forgets partitions the source
    no longer has, since their rows were deleted. Unchanged digests are not rewritten."""
    q = _quote(CHECKSUMS_TABLE)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {q} (tbl TEXT, part TEXT, digest TEXT, PRIMARY KEY (tbl, part))"
    )
    stored = _stored_sums(conn, table)
    if replace and (old := [(table, p) for p in stored if p not in digests]):
        conn.executemany(f"DELETE FROM {q} WHERE tbl = ? AND part = ?", old)
    conn.executemany(
        f"INSERT OR REPLACE INTO {q} (tbl, part, digest) VALUES (?, ?, ?)",
        [(table, p, d) for p, d in digests.items() if stored.get(p) != d],
    )


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


def _sqlite_scd2(
    conn: sqlite3.Connection,
    table: str,
    stage: str,
    cols: list[str],
    keys: list[str],
    track: list[str] | None,
    on_missing: str,
    when: Any,
) -> dict[str, Any]:
    """Merge the staged source into the versioned table, set-based: a handful of statements
    whatever the row count. `_st` is NULL for a new key, 1 for unchanged, 2 for changed."""
    missing = [k for k in keys if k not in cols]
    if missing:
        raise ValueError(f"sqlite.write: key column(s) not in records: {missing}")
    check_scd2_columns("sqlite.write", track, cols)
    tracked = track if track is not None else [c for c in cols if c not in keys]
    ts = when.isoformat(timespec="microseconds")
    tbl = _quote(table)
    s = f"temp.{stage}"
    klist = ", ".join(map(_quote, keys))
    null_key = " OR ".join(f"{_quote(k)} IS NULL" for k in keys)
    if conn.execute(f"SELECT 1 FROM {s} WHERE {null_key} LIMIT 1").fetchone():
        raise ValueError(f"sqlite.write: mode: scd2 needs a value in every key column {keys}")
    conn.execute(f"DELETE FROM {s} WHERE rowid NOT IN (SELECT max(rowid) FROM {s} GROUP BY {klist})")
    conn.execute(f"CREATE INDEX temp._agentl_scd2_k ON {stage} ({klist})")
    match = " AND ".join(f"t.{_quote(k)} = s.{_quote(k)}" for k in keys)
    cur = f"{match} AND t.is_current = 1"
    conn.execute(
        f"UPDATE {s} SET _st = 1 WHERE rowid IN "
        f"(SELECT s.rowid FROM {tbl} AS t CROSS JOIN {s} AS s WHERE {cur})"
    )
    if tracked:
        same = " AND ".join(f"s.{_quote(c)} IS t.{_quote(c)}" for c in tracked)
        conn.execute(
            f"UPDATE {s} SET _st = 2 WHERE rowid IN "
            f"(SELECT s.rowid FROM {tbl} AS t CROSS JOIN {s} AS s WHERE {cur} AND NOT ({same}))"
        )
    late = conn.execute(
        f"SELECT 1 FROM {tbl} AS t CROSS JOIN {s} AS s "
        f"WHERE {cur} AND s._st = 2 AND t.valid_from > ? LIMIT 1",
        (ts,),
    ).fetchone()
    if late:
        raise ValueError(f"sqlite.write: as_of {ts} is before the start of a current version")
    conn.execute(
        f"UPDATE {tbl} AS t SET valid_to = ?, is_current = 0 WHERE is_current = 1 AND EXISTS "
        f"(SELECT 1 FROM {s} AS s WHERE {match} AND s._st = 2)",
        (ts,),
    )
    closed = 0
    if on_missing == "close":
        closed = conn.execute(
            f"UPDATE {tbl} AS t SET valid_to = ?, is_current = 0 WHERE is_current = 1 AND NOT EXISTS "
            f"(SELECT 1 FROM {s} AS s WHERE {match})",
            (ts,),
        ).rowcount
    names = ", ".join(map(_quote, cols))
    conn.execute(
        f"INSERT INTO {tbl} ({names}, valid_from, valid_to, is_current) "
        f"SELECT {names}, ?, NULL, 1 FROM {s} WHERE _st IS NULL OR _st = 2",
        (ts,),
    )
    counts: dict[Any, int] = dict.fromkeys((None, 1, 2), 0)
    counts.update(conn.execute(f"SELECT _st, count(*) FROM {s} GROUP BY _st").fetchall())
    conn.execute(f"DROP TABLE {s}")
    out: dict[str, Any] = {"written": sum(counts.values()), "inserted": counts[None]}
    out.update(versioned=counts[2], unchanged=counts[1])
    if on_missing == "close":
        out["closed"] = closed
    out["as_of"] = ts
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
    checksums: bool = False,
    emit: str | None = None,
    track: str | list[str] | None = None,
    on_missing: str | None = None,
    as_of: Any = None,
) -> list[dict]:
    """Write records to a SQLite table, created (and widened) as needed. `mode`: append (default),
    replace, or upsert with `key: id` (a record updates the row with the same key).
    Upsert only: on_conflict update (default; rows whose values did not change are left alone),
    do_nothing (keep the existing row) or error (fail on a duplicate key); update_columns limits
    the update to those columns. The result reports inserted, updated and unchanged.
    mode: changeset compares with the table and writes only what changed; `deletes: missing` (or
    `partition_replaced` with `partition_by`) also removes rows the source no longer has, and
    `dry_run: true` reports the counts without writing; `checksums: true` (with `partition_by`)
    skips partitions whose records match the checksum stored by the last changeset write;
    `emit: changes` returns the inserted, updated and deleted records (`_change`, `_before`)
    instead of the counts.
    scd2 keeps history (slowly changing dimension type 2) in `valid_from`, `valid_to` (NULL while
    current) and `is_current`: a record whose `track` columns (default: all but the key) changed
    closes the current version and adds a new one; on_missing: close also closes keys absent
    from the input. `as_of` (default now) is the time stamped, stored as UTC ISO 8601 text.
    The result reports inserted, versioned, unchanged (and closed)."""
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
        checksums=checksums,
        emit=emit,
        track=track,
        on_missing=on_missing,
        as_of=as_of,
    )
