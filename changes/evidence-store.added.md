`evidence:` on `http.get`, `http.post`, `web.crawl` and `browser.scrape` keeps what the source
returned in a sha256 content-addressed store (local folder or any fsspec `storage` URL): the
raw body, the request metadata with secrets redacted (`request: true`) and, in the browser, a
full-page screenshot. `when: changed` (default), `always` or `on_failure`; records get
`_evidence`, the run store indexes every artifact, and `agentl evidence ls|show|verify` lists,
prints and re-hashes them (#293).
