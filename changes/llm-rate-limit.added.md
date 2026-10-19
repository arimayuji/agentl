Client-side rate limits for llms: `llm.<name>.rate_limit: {rpm, tpm}` is a token bucket shared by
every step and thread of a run, with an `llm.throttled` event per wait and the total in
`throttled_s` (run result, step spend, run events, `agentl stats`). The OpenAI-compatible providers
now honour `Retry-After` (seconds or HTTP date, capped at 60 s), and `budget.max_calls` caps the
number of llm calls per run (#336).
