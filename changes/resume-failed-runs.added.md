`agentl resume <run_id> <pipeline>` resumes a failed run: completed steps, sinks included, are
skipped and their outputs read back from checkpoints under `.agentl/checkpoints/`, and the run
goes on from the first failed step. It refuses when the spec changed or a completed writer would
have to write again; the new run carries `resumed_from` (#339).
