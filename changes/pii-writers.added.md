`agentl preview` lists fields that look like personal data in a step's input and output (masked
examples only) and suggests the `protect` step for them. `agentl lint` warns (AL014) when such a
field of a local CSV sample reaches a writer step without `protect` or a `pii` check before it;
`sensitive: false` on the writer step silences it on purpose (#239).
