# AGENTS.md

Instructions for coding agents working on the agentl repository. Users of the library should read
https://agentl.com.br/llms-full.txt instead.

## Project

agentl is an agentic ETL library for Python. Pipelines are YAML; each step is a connector, a Python
function or an LLM agent. Distribution name `agentl-etl`, import and CLI `agentl`. Public repo:
English for code, docs, commits and issues.

- `agentl/spec.py`: YAML to pydantic models (`${VAR}` and `${VAR:-default}`, extract/transform/load sugar).
- `agentl/runner.py`: runs the graph, per-record agent cache, emits events.
- `agentl/connectors/`: built-in connectors, `fn(input, **params) -> list[dict]`, registered with `@connector`.
- `agentl/llm.py`: providers (`anthropic`, `echo` for offline tests).
- `agentl/pandas.py`: `@dataframe` bridge. `agentl/notify.py`, `agentl/dashboard.py`: stdlib only.
- `docs/`: MkDocs Material site at agentl.com.br. `tools/llms_txt.py` builds `llms.txt`.

## Setup and checks

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest -q
.venv/bin/pip install -e '.[docs]' && .venv/bin/mkdocs build --strict
```

Postgres tests run only with `AGENTL_TEST_PG_DSN=postgresql://...` set.

## Rules

1. The core depends only on `pyyaml` and `pydantic`. Other libraries go behind an extra in
   `pyproject.toml` and are imported with `registry.require(module, extra)`.
2. Tests never call a real LLM or the network: use the `echo` provider, a local HTTP server or
   monkeypatch.
3. Secrets only through environment variables. Never commit `.env` files or tokens.
4. SQL: quote identifiers, always pass values as parameters.
5. Files and archives from users are untrusted: keep the zip-slip and size checks.
6. Every user-facing change gets docs and a CHANGELOG entry under `[Unreleased]`.
7. Commits, PRs, issues and comments carry no AI attribution: no `Co-Authored-By`, no session
   links, no "Generated with" footers.
8. Everything in the repository is in English: code, identifiers, examples, sample data, docs,
   commits and PRs. No Portuguese pipeline names, step ids, field names or data values.
9. Brand colors: blue `#0070c9` (main), yellow `#ffd400` (accent, the dot after the name) and white
   (surfaces), with navy `#0b1b2b` for text. The logo is a white `a` with a yellow dot on blue.
   Blue surfaces carry the brand shade: a deep blue (`#005ea8`) circle from the top right corner.
   Green and red are status colors only (success, failure), never brand accents. Brand images come
   from `media/brand/build.py`.
