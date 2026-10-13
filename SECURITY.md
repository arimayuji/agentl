# Security Policy

## Supported versions

Only the latest released minor version receives security fixes while the project is in `0.x`.

## Reporting a vulnerability

Please **do not** open a public issue.

Report it privately through
[GitHub Security Advisories](https://github.com/arimayuji/agentl/security/advisories/new).
Include the affected version, a description, and steps or a pipeline to reproduce.

You can expect an acknowledgement within 7 days. Once a fix is ready, we publish a release
and an advisory, crediting you unless you prefer to stay anonymous.

## Scope notes

agentl runs pipelines you write. Some behaviour is by design and not a vulnerability:

- `fn:` steps import and run Python code named in the YAML. Only run pipelines you trust.
- Connectors read the files, URLs and databases the pipeline points to.
- Agent steps send record contents to the configured LLM provider. Use a local provider
  for data that must not leave your machine.

Issues such as secret leakage in logs, the dashboard or the run store, path or SQL injection
in connectors, or the dashboard being reachable beyond `127.0.0.1` by default are in scope.
