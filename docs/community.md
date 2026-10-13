# Contributing

Bug reports, connector requests and pull requests are welcome.

- **Bugs**: open an issue with the [bug report form](https://github.com/arimayuji/agentl/issues/new?template=bug_report.yml) and a minimal pipeline that reproduces it.
- **New connectors**: use the [connector request form](https://github.com/arimayuji/agentl/issues/new?template=connector_request.yml).
- **Questions and ideas**: [GitHub Discussions](https://github.com/arimayuji/agentl/discussions).
- **Security issues**: report them privately through [Security Advisories](https://github.com/arimayuji/agentl/security/advisories/new), never in a public issue.

## Development setup

```bash
git clone https://github.com/arimayuji/agentl
cd agentl
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pre-commit install
```

Every pull request must pass the same checks CI runs:

```bash
ruff check .
ruff format --check .
mypy
pytest --cov
```

The full guide, including commit conventions and the release process, is in
[CONTRIBUTING.md](https://github.com/arimayuji/agentl/blob/main/CONTRIBUTING.md).

## Contributors

[![Contributors](https://contrib.rocks/image?repo=arimayuji/agentl)](https://github.com/arimayuji/agentl/graphs/contributors)

## Code of conduct

This project follows the [Contributor Covenant 2.1](https://github.com/arimayuji/agentl/blob/main/CODE_OF_CONDUCT.md).
