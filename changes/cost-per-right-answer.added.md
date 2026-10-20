`agentl eval` reports the cost per right answer on a golden set (`right` and `cost_per_right` in
`--json`); `agentl stats` reports, on agent steps with human review, the approved answers and their
cost, counting what people accepted, fixed and rejected; and `agentl plan` estimates agent steps
from the tokens per record that past runs of the step and model really took, falling back to the
character-based guess when there is no history (#156).
