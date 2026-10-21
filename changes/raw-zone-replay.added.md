`persist: raw` keeps a step's output as an immutable, content-addressed original (sha256) next to
the run store or under `raw.path`, with `raw.keep_days` retention. `agentl replay PIPELINE
--from-raw RUN_ID` reprocesses those originals with the current code without fetching the source,
and `agentl raw ls` / `agentl raw prune` list and prune them (#208).
