# Changelog fragments

Every user-facing pull request adds one file here instead of editing `CHANGELOG.md`, so pull
requests do not conflict on it.

- Name: `<short-slug>.<section>.md`, where section is `added`, `changed`, `fixed`, `removed`,
  `security` or `docs`. Example: `http-retry-after.fixed.md`.
- Content: the text of one bullet, without the leading `- `. Markdown, may wrap lines. Reference
  issues as usual, e.g. `(#290)`, and credit outside contributors by handle.
- `python tools/changelog.py preview` shows the next release's entries. At release time the
  maintainer runs `python tools/changelog.py build --version X.Y.Z`, which writes them into
  `CHANGELOG.md` and deletes the fragments.
