`emit: changes` on changeset writes (`sqlite.write`, `postgres.write`, `sql.write`, #289): the
step outputs the inserted, updated and deleted rows, each with `_change` and, for updates,
`_before` (the stored values of the changed columns), so a downstream step consumes the diff.
Works with `dry_run` as a preview; the counts stay in events and the run summary.
