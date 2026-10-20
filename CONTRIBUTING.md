# Contributing to agentl

Thanks for your interest. Bug reports, connector ideas and pull requests are welcome.

## Before you start

- For bugs, open an issue with the **Bug report** form and a minimal pipeline that reproduces it.
- For new features or connectors, open an issue first so we can agree on the design.
  Small fixes (typos, docs, obvious bugs) can go straight to a pull request.
- Security problems: do **not** open a public issue. See [SECURITY.md](SECURITY.md).

## Contributing with an AI agent

Hit an error in your own project? `agentl report --pipeline pipeline.yaml` prints a ready-to-paste
bug report and a prefilled issue URL (it sends nothing). To go from there to a fix with Claude
Code, Cursor or another coding agent, point it at
[.github/CONTRIBUTING_AI.md](.github/CONTRIBUTING_AI.md).

## Credit

Every merged pull request counts: you show up in the README's Contributors section (built from the
repository's commit history) and the changelog entry for your change credits you by GitHub handle.

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

### Locked CI dependencies

CI installs its tools from hash-locked files in `requirements/` (`pip install --require-hashes`),
then agentl itself with `pip install --no-deps --no-build-isolation -e .`. Each `.txt` is generated
from the `.in` next to it; the command is in the `.in` header. After changing the `dev` extra in
`pyproject.toml`, update `requirements/ci.in` to match and regenerate:

```bash
for f in fuzz ci build browser; do
  uv pip compile --universal --python-version 3.10 --generate-hashes -o requirements/$f.txt requirements/$f.in
done
```

`--universal` writes one lock for Python 3.10 to 3.13 on Linux, Windows and macOS. Dependabot
proposes updates weekly.

### Fuzzing

`fuzz/` holds [Atheris](https://github.com/google/atheris) targets for the parsers that read
untrusted input (pipeline YAML, CSV, JSON, XML and HTML, `${VAR}` and `{field}` templates).
ClusterFuzzLite (`.clusterfuzzlite/`, `.github/workflows/cflite.yml`) runs them briefly on pull
requests and longer every week. To run one locally:

```bash
pip install atheris -e .
python fuzz/fuzz_csv.py -max_total_time=30
```

## Guidelines

- **Core stays light.** The core depends only on `pyyaml` and `pydantic`. Connectors that need
  heavy libraries go behind an extra in `pyproject.toml` and import them with `registry.require`.
- **Tests never hit the network or a real LLM.** Use the `echo` provider, a fake provider or `monkeypatch`.
- **Secrets only through environment variables.** Never commit keys, tokens or `.env` files.
- **User-facing changes** need docs and a changelog fragment: add `changes/<short-slug>.<section>.md`
  (section: `added`, `changed`, `fixed`, `removed`, `security` or `docs`) holding the bullet text.
  Do not edit `CHANGELOG.md`; see [`changes/README.md`](changes/README.md).
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

## Bots

- **Welcome**: a short comment on your first issue or pull request.
- **Labeler**: adds labels such as `connectors`, `cli` or `docs` from the files a pull request
  changes (`.github/labeler.yml`).
- **Release Drafter**: after each merge, updates a draft release grouped by label and title prefix
  (`feat`, `fix`, `docs`, `ci`, `deps`). It never publishes; a maintainer does.
- **Quality**: `typos` checks spelling (add real words to `_typos.toml`) and `zizmor` checks the
  workflows for security issues. Run them locally with `pip install typos zizmor`, then
  `typos` and `zizmor --offline .`.

The welcome and labeler workflows use `pull_request_target` but never check out or run pull
request code.

## Releases (maintainers)

1. In the release PR, run `python tools/changelog.py build --version X.Y.Z` (check first with
   `python tools/changelog.py preview`). It writes the fragments into `CHANGELOG.md` and deletes them.
2. Bump `__version__` in `agentl/__init__.py`.
3. Merge to `main`, then tag: `git tag v0.2.0 && git push origin v0.2.0`.
4. The `release` workflow builds, publishes to PyPI and creates the GitHub release.
