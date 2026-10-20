`vs_runs` and `vs_last_run` checks take `nulls: { column: 5% }`, which fails when a column's
null rate moves more than that many percentage points from the median of the last runs (or from
the last run). It works with `severity`, so `vs_runs: warn` only reports the drift (#185).
