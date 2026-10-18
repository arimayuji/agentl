# Contributing to agentl

Thanks for your interest. Bug reports, connector ideas and pull requests are welcome.

## Before you start

- For bugs, open an issue with the **Bug report** form and a minimal pipeline that reproduces it.
- For new features or connectors, open an issue first so we can agree on the design.
  Small fixes (typos, docs, obvious bugs) can go straight to a pull request.
- Security problems: do **not** open a public issue. See [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/arimayuji/agentl
cd agentl
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pre-commit install
```

## Checks

Every pull request must pass the same checks CI runs:

```bash
ruff check .
ruff format --check .
mypy
pytest --cov
```

## Guidelines

- **Core stays light.** The core depends only on `pyyaml` and `pydantic`. Connectors that need
  heavy libraries go behind an extra in `pyproject.toml` and import them with `registry.require`.
- **Tests never hit the network or a real LLM.** Use the `echo` provider, a fake provider or `monkeypatch`.
- **Secrets only through environment variables.** Never commit keys, tokens or `.env` files.
- **Public API changes** need docs in the README and an entry in `CHANGELOG.md` under `[Unreleased]`.
- Keep pull requests focused. One feature or fix per PR.

## Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(connectors): add s3.get
fix(runner): keep rejected items when the cache is disabled
docs: explain extract/transform/load sections
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`.
Add `!` after the type (`feat!:`) for breaking changes.

## Writing a connector

A connector is a function `fn(input, **params) -> list[dict]`:

- `input` is `None` for sources, a list of records for one upstream step,
  or `{step_id: records}` when the step has several inputs.
- `params` come from the step's `with:` block.

```python
from agentl import connector


@connector("crm.contacts")
def contacts(input, since=None): ...
```

Connectors that live outside this repo register through the `agentl.connectors`
entry point group. Please name third-party packages `agentl-<name>`.

## Security of contributions

- Pull requests from forks run only the read-only checks: `changes`, `lint`, `test`, `build`,
  docs build, benchmarks perf gate, CodeQL and dependency review. Their `GITHUB_TOKEN` can only
  read, and none of these jobs pushes, publishes or deploys.
- Secrets are never exposed to pull request code. Publishing to PyPI, GitHub releases and the docs
  deploy run only on `main` or a `v*` tag, behind protected environments.
- Changes to `.github/` (workflows, Dependabot), `pyproject.toml`, `requirements/`, `tools/` and the
  release config need the owner's review (see `.github/CODEOWNERS`). New dependencies are checked
  for known vulnerabilities, and third-party actions are pinned to commit SHAs.

## Releases (maintainers)

1. Move the `[Unreleased]` entries in `CHANGELOG.md` to a new version section.
2. Bump `__version__` in `agentl/__init__.py`.
3. Merge to `main`, then tag: `git tag v0.2.0 && git push origin v0.2.0`.
4. The `release` workflow builds, publishes to PyPI and creates the GitHub release.
