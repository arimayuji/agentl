Agent steps look for personal data in the records they send: `pii: {before_llm: warn|mask|block|off}`
emits a `pii.detected` event (the default), replaces detected values with placeholders like
`[EMAIL_1]` and restores them in the answer, or rejects the record to the quarantine. The cache
keeps masked answers. `agentl lint` warns (AL012) when sampled CSV fields that look like personal
data reach an agent step without `protect` or `pii`, and the docs say what is sent to providers
(#337).
