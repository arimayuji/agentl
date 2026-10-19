`sql` step: an input with no records keeps its columns when they are known (a `sqlite.read` of an
empty table, or the step's `schema:` or `output:`), so a `left join` against it returns every left
row instead of nothing. When the columns are unknown and other inputs have records, a query that
names them now fails with a clear error instead of returning no records (#288).
