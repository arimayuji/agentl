`on_drift: remap` on a check (#178) no longer stops the run: it hands the new layout to the
`map_schema` steps that read the check, which reuse a stored or pinned mapping or infer a new
one, and stop for review when they have `review: required`. The new layout is recorded only
once it has been mapped. A check with `on_drift: remap` now needs such a step.
