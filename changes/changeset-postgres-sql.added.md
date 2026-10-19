`mode: changeset` on `postgres.write` and `sql.write` (#205), with the semantics of
  `sqlite.write`: inserted, updated, deleted and unchanged counts, `deletes: missing` or
  `partition_replaced`, `dry_run` and `outcome: no_change`. `postgres.write` diffs set-based in a
  temp table typed like the destination; `sql.write` compares in Python.
