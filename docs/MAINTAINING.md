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
- Require status checks: `lint`, `test (ubuntu-latest, 3.10)`, `test (ubuntu-latest, 3.13)`, `build`, `analyze`.
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

## Labels

`bug`, `enhancement`, `connector`, `docs`, `triage`, `good first issue`, `help wanted`, `breaking`, `dependencies`.
