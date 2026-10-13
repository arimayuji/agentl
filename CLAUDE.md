# CLAUDE.md

agentl: agentic ETL library in Python. Public repo, English for code and docs.

- `agentl/spec.py` YAML -> pydantic models (`${VAR}` interpolation, extract/transform/load sugar).
- `agentl/runner.py` executes the graph, per-record agent cache, emits events.
- `agentl/connectors/` built-in connectors, `fn(input, **params) -> list[dict]`, registered with `@connector`.
- `agentl/llm.py` providers (`anthropic`, `echo` for offline tests). `agentl/notify.py`, `agentl/dashboard.py` (stdlib only).

Rules
1. Core depends only on `pyyaml` and `pydantic`. Heavy libraries go behind extras and `registry.require`.
2. Tests never call a real LLM or network: use the `echo` provider or monkeypatch.
3. Secrets only through environment variables.

Commands: `python -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q`
4. Commits, PRs, issues and comments carry no AI attribution: no `Co-Authored-By`, no session links, no "Generated with" footers.
