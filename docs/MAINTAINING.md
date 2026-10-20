# Maintaining agentl

Repository settings that are not stored in git. Review them after any change to the repo's visibility or ownership.

## General

- Features: Issues on, Discussions on, Wiki off (docs live in the repo), Projects optional.
- Pull requests: allow **squash merging** only, default message "Pull request title and description".
  Turn on "Always suggest updating pull request branches", "Allow auto-merge" and
  "Automatically delete head branches".
- Releases: enable **release immutability**.

## Branch protection (Rulesets, target `main`)

- Restrict deletions and block force pushes.
- Require a pull request before merging, with conversation resolution.
- Require status checks: `lint`, `test (ubuntu-latest, 3.10)`, `test (ubuntu-latest, 3.13)`, `build`, `Analyze (python)`, `Analyze (actions)`.
  Require branches to be up to date.
- Require signed commits (optional, recommended).
- Solo maintainer: set required approvals to 0 and add yourself to the bypass list only if you need to
  ship hotfixes. Raise approvals to 1 once there is a second maintainer.

Protect tags `v*` with a second ruleset (restrict creation, update and deletion to maintainers) so only you can trigger a release.

## Actions

- Actions permissions: allow GitHub-authored actions and selected verified creators (`pypa/*`).
- Workflow permissions: **read repository contents** by default; leave "Allow GitHub Actions to create and approve
  pull requests" off.
- Fork pull requests: require approval for first-time contributors.

## Security

- Private vulnerability reporting: on.
- Dependency graph, Dependabot alerts and Dependabot security updates: on.
- Secret scanning and **push protection**: on.
- Code scanning: the `CodeQL` workflow in this repo (do not also enable default setup).

## PyPI publishing

1. Create the project on PyPI with a **trusted publisher**: owner `arimayuji`, repo `agentl`,
   workflow `release.yml`, environment `pypi`. No API token is stored anywhere.
2. In GitHub, create the environment `pypi` and add yourself as a required reviewer, restricted to tags `v*`.

## MCP registry

`server.json` lists `agentl mcp` in the [official MCP registry](https://registry.modelcontextprotocol.io)
as `io.github.arimayuji/agentl`, installed with `uvx agentl-etl mcp`. The registry checks the
`<!-- mcp-name: io.github.arimayuji/agentl -->` line in the README (the PyPI description), so a
version can be listed only once it is on PyPI. A test keeps `server.json` on `__version__`.

After each release is on PyPI:

```bash
brew install mcp-publisher        # once
mcp-publisher login github        # once per machine; authorizes io.github.arimayuji/*
mcp-publisher publish             # from the repo root, reads server.json
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.arimayuji/agentl"
```

## Releases

1. Open a release PR. Run `python tools/changelog.py preview`, then
   `python tools/changelog.py build --version X.Y.Z [--date YYYY-MM-DD]`: it writes the
   `## [X.Y.Z] - date` section from the fragments in `changes/`, adds the compare link and deletes
   the fragments. Add the release summary paragraph under the heading by hand if you want one.
2. Bump `__version__` in `agentl/__init__.py` and `server.json`, and `extra.version_label` in
   `mkdocs.yml` on a minor release (a test checks it against `CHANGELOG.md`).
3. Merge, then tag `vX.Y.Z` and push the tag; the `release` workflow publishes.

## Labels

`bug`, `enhancement`, `connector`, `docs`, `triage`, `good first issue`, `help wanted`, `breaking`, `dependencies`.

## Website (agentl.com.br)

The site is built with MkDocs Material from `docs/` and deployed by `.github/workflows/docs.yml`.

1. Settings > Pages > Build and deployment > Source: **GitHub Actions**.
2. Settings > Pages > Custom domain: `agentl.com.br`, then enable **Enforce HTTPS** once the certificate is issued.
3. DNS at Cloudflare (proxy **off**, grey cloud, so GitHub can issue the certificate):
   - `A` records for `agentl.com.br`: `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
   - `CNAME` `www` -> `arimayuji.github.io`
4. Verify the domain in your GitHub account (Settings > Pages > Verified domains) to prevent takeover.

Build locally with `pip install -e '.[docs]' && mkdocs serve`.
