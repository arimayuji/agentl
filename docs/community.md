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

Everyone who has shipped a change to agentl. Every merged pull request gets its author listed
here with what they did, credited by handle in the [changelog](changelog.md), and a shout-out from
[@useagentl on X](https://x.com/useagentl) and [Instagram](https://www.instagram.com/useagentl/).

<div class="agt-people" markdown="0">
  <a class="agt-person" href="https://github.com/dannyward630">
    <img src="https://github.com/dannyward630.png?size=128" alt="" loading="lazy" width="64" height="64">
    <span class="agt-person-name">@dannyward630</span>
    <span class="agt-person-what">First outside contributor: <code>html.select</code> keeps the page URL on each record (#292)</span>
  </a>
</div>

[![All contributors](https://contrib.rocks/image?repo=arimayuji/agentl)](https://github.com/arimayuji/agentl/graphs/contributors)

### Ways to help that are not code

Docs fixes, a recipe for your use case, a bug report made with `agentl report`, answers in
[Discussions](https://github.com/arimayuji/agentl/discussions) and a post about how you use agentl
all count. Tell us in the pull request or tag @useagentl, and we will add you here.

## Code of conduct

This project follows the [Contributor Covenant 2.1](https://github.com/arimayuji/agentl/blob/main/CODE_OF_CONDUCT.md).
