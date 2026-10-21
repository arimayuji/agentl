Fixed: `agentl eval` scored every record of a step with `rules:` as having no answer, because the `_path` tag that hybrid steps add to their answers hid them from the scorer.
