# Security Policy

## Supported versions

Only the latest released minor version receives security fixes while the project is in `0.x`.

## Reporting a vulnerability

Please **do not** open a public issue.

Report it privately through
[GitHub Security Advisories](https://github.com/arimayuji/agentl/security/advisories/new).
Include the affected version, a description, and steps or a pipeline to reproduce.

You can expect an acknowledgement within 7 days and a fix or a plan within 30 days. We aim to
publish within 90 days of the report, or earlier when a fix ships; we agree the date with you.
Once a fix is ready, we publish a release and an advisory, crediting you unless you prefer to
stay anonymous.

## Scope notes

agentl runs pipelines you write. Some behaviour is by design and not a vulnerability:

- `fn:` steps import and run Python code named in the YAML. Only run pipelines you trust.
- `agentl mcp` lets an MCP client (Claude Code, Cursor) validate and run pipelines. Its
  `run_pipeline` only runs files under the folder it was started in (`--root`), and refuses
  pipelines that run code (`fn:` steps, Python tools, local MCP server commands) unless started
  with `--allow-code`. `validate` and `diagram` never read environment variables or secrets.
- `agentl serve` webhooks without `secret:` accept any request that reaches them. The server
  listens on `127.0.0.1` by default and warns when an open webhook is served on another address.
- Connectors read the files, URLs and databases the pipeline points to. URL fetches (`url:` on
  readers, `http.get`, `http.post`) refuse loopback, private, link-local (cloud metadata) and
  reserved addresses, also after redirects and at connect time (the connection goes to the
  address that was checked, so DNS rebinding is refused), unless allowed with `AGENTL_ALLOW_HOSTS` or
  `AGENTL_ALLOW_PRIVATE_NETWORK=1`.
- Agent steps send record contents to the configured LLM provider. Use a local provider
  for data that must not leave your machine.

Bypasses of the URL guard, and record data breaking out of the `<record>` block in agent prompts,
are in scope. Issues such as secret leakage in logs, the dashboard or the run store, path or SQL injection
in connectors, or the dashboard being reachable beyond `127.0.0.1` by default are in scope.

## Verifying releases

Releases are built in GitHub Actions and published to PyPI with trusted publishing (no stored
token). Each release has a build provenance attestation and a CycloneDX SBOM attached to the
GitHub release:

```bash
pip download agentl-etl==<version> --no-deps -d dist
gh attestation verify dist/agentl_etl-<version>-py3-none-any.whl --repo arimayuji/agentl
```

The signed Sigstore bundle is also attached to each GitHub release as
`agentl-etl-<version>.sigstore.json`, for verifying offline (`--bundle`).

All GitHub Actions are pinned by commit SHA, and the repository is scored by OpenSSF Scorecard.
