`http.get` and `http.post`: `paginate.style` adds `offset`, `cursor` (token from a JSON path) and
`link` (`Link: rel="next"`) pagination, and `total_key` stops at the page or item count, fetching
known pages `concurrency` at a time. `auth.login` logs in with a form or JSON body (cookies or a
token), logs in again when an answer matches `expired_if` or is a 401, and caches the session for
`token_cache_ttl` under a lock so concurrent requests log in once (`shared: true`: across runs
through the run store) (#212).
