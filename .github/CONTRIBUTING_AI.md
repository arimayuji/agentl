# Contributing to agentl with an AI coding agent

This guide is for AI coding agents (Claude Code, Cursor, Copilot, Codex and others) helping a user
who hit a problem with agentl in their own project. It goes from the error to an issue, and from
the issue to a fix pull request. Humans can follow it too.

Always show the user what you are about to file or push, and let them decide. Never file an issue
or open a pull request on their behalf without asking.

## 1. From error to issue

In the user's project, where the run store (`.agentl/agentl.db`) is:

```bash
agentl report --pipeline pipeline.yaml          # the latest failed run
agentl report RUN_ID --pipeline pipeline.yaml -o bug.md
agentl report --crash                           # the last CLI crash (a Python traceback)
agentl report --json                            # the same, as data
```

With the agentl MCP server (`agentl mcp`), call the read-only `report` tool instead.

The report holds the agentl version, Python and OS, the failing step, the error and its traceback,
what `agentl doctor` sees, the pipeline YAML with secret values and `${VAR:-default}` defaults
masked, and a minimal repro suggestion. It ends with a prefilled GitHub issue URL (or, when that is
too long, asks to paste the markdown into the Bug report form). `agentl report` sends nothing over
the network.

Before the user files it:

1. Read the whole report and remove anything private the masking missed (hostnames, customer
   names, file paths, data values in the traceback).
2. Replace the pipeline with the smallest one that still fails, with 2 or 3 made-up records and
   local files instead of real services. Run it once to confirm the same error.
3. Search the [open issues](https://github.com/arimayuji/agentl/issues) for the same error first.

Not a bug? A missing connector goes in the **Connector request** form, a new idea in **Feature
request**. A confusing error message is a bug: say what you expected it to tell you.

## 2. From issue to fix

```bash
git clone https://github.com/arimayuji/agentl
cd agentl
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Read [AGENTS.md](../AGENTS.md) first: it is the map and the rules. Where things live:

| What | Where |
| --- | --- |
| YAML to models, `${VAR}` | `agentl/spec.py` |
| Running steps, events, cache | `agentl/runner.py` |
| Built-in connectors (`csv.read`, `http.get`...) | `agentl/connectors/`, registered with `@connector` |
| LLM providers (`echo` for tests) | `agentl/llm.py` |
| Error messages for YAML problems | `agentl/lint.py`, `agentl/spec.py` |
| CLI commands | `agentl/cli.py` |
| MCP tools | `agentl/mcp_server.py` |
| Docs (MkDocs) | `docs/` |
| Tests | `tests/`, one `test_<area>.py` per area |

A connector is `fn(input, **params) -> list[dict]`; `input` is `None` for sources. Libraries other
than `pyyaml` and `pydantic` go behind an extra in `pyproject.toml` and are imported with
`registry.require(module, extra)`.

### Write the test first

Turn the repro from the issue into a failing test, then fix the code until it passes. A test
builds a small pipeline in `tmp_path` and runs it:

```python
from agentl.runner import Runner
from agentl.spec import load
from agentl.store import Store


def test_csv_read_keeps_empty_columns(tmp_path):
    (tmp_path / "in.csv").write_text("id,note\n1,\n")
    (tmp_path / "p.yaml").write_text(
        f"name: t\nsteps:\n  - id: rows\n    use: csv.read\n    with: {{path: {tmp_path / 'in.csv'}}}\n"
    )
    with Store(tmp_path / "s.db") as store:
        result = Runner(load(tmp_path / "p.yaml"), store=store).run()
    assert result.outputs["rows"] == [{"id": "1", "note": ""}]
```

### Checks

All of these must pass before you open the pull request:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest -q
```

Docs changes: `.venv/bin/pip install -e '.[docs]' && .venv/bin/mkdocs build --strict`.

## 3. The pull request

Follow [the pull request template](pull_request_template.md): a summary that links the issue
(`Closes #123`), the list of changes and the checklist. Every user-facing change gets docs and a
changelog fragment in `changes/` (`<slug>.<section>.md`, see `changes/README.md`); do not edit
`CHANGELOG.md` directly. One fix per pull request. Commit messages use
Conventional Commits (`fix(connectors): ...`).

## Rules

1. **No secrets.** Nothing from the user's `.env`, environment, tokens, DSNs or real data goes into
   an issue, a test, an example or a commit.
2. **No network in tests.** Use local files, a local HTTP server (see `tests/helpers.py`) or
   `monkeypatch`.
3. **Tests never call a real LLM.** Use the `echo` provider or a fake provider registered with
   `register_provider`.
4. **Everything in English**: code, identifiers, sample data, docs, commits and pull requests.
5. **No AI attribution** in commits, pull requests or issues: no `Co-Authored-By` lines for agents,
   no session links, no "Generated with" footers.
6. Security problems never go in a public issue: see [SECURITY.md](../SECURITY.md).
