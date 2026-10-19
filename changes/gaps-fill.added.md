`agentl gaps --fill` backfills only the missing partitions through the `agentl backfill` path
(resumable, `--max` run budget, `--concurrency`, composite keys passed as params), and
`agentl gaps --notify CHANNEL` sends the gaps to a `notify:` channel for periodic checks (#219).
