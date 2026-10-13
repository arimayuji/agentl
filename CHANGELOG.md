# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, minor releases may contain breaking changes.
They are always listed under **Changed** or **Removed**.

## [Unreleased]

### Added

- LLM providers `openai`, `ollama` and `openai-compatible` (vLLM, LM Studio, Groq, ...), with strict
  JSON-schema output, retries and token usage. Standard library only, no new dependency (#16).

## [0.2.0] - 2026-10-13

### Added

- Docs for AI agents: `llms.txt` and `llms-full.txt` built with the site, a "Use with AI agents"
  page with tested prompts, and `AGENTS.md` (#10).
- Guide "Migrate a pandas pipeline" and `examples/sales` (CSV over HTTP to Postgres upsert) (#11).
- `agentl run`/`validate` find `fn:` modules next to the pipeline file and in the working directory.
- `agentl.pandas`: `@dataframe` turns `fn(df) -> DataFrame` into a step, plus `to_df` and
  `to_records`; `agentl-etl[pandas]` extra (#8).
- `postgres.read` and `postgres.write` (`append`, `replace`, `upsert` on `key`), with typed
  auto-created tables and batched writes in one transaction; `agentl-etl[postgres]` extra (#9).
- Readers accept `url:` (HTTP download with `headers` and retries) besides `path:`, and upstream
  records whose `path` is a URL (#7).
- `file.read`: detects the format by extension, expands ZIP and GZ archives safely (path traversal
  rejected, size cap) and reads mixed folders (#22).
- New readers: `txt.read`, `parquet.read`, `doc.read` (via LibreOffice), `xml.read`, `html.read`;
  `excel.read` now also reads legacy `.xls`. New extras: `xls`, `parquet`.

### Changed

- `csv.read` detects the delimiter (`,` `;` tab `|`) when `delimiter` is not set.

### Fixed

- `dedupe`, `filter` and `select` accept a single field name (`on: id`); a string used to be
  split into characters and silently dropped records.
- An agent step with an empty upstream now outputs nothing instead of running once on an empty record.

## [0.1.1] - 2026-10-13

### Changed

- The PyPI distribution is now `agentl-etl` (PyPI treats `agentl` as confusable with an existing
  project). Install with `pip install agentl-etl`; the import (`import agentl`) and the `agentl`
  command are unchanged. 0.1.0 was never published.

## [0.1.0] - 2026-10-13

### Added

- YAML pipeline spec with `${VAR}` interpolation and `extract`/`transform`/`load` sections.
- Runner that executes steps in graph order and emits run, step and item events.
- Agent steps with schema-validated output, per-record cache and rejected items.
- Connectors: CSV, JSON/JSONL, Excel, PDF, Word, HTTP, SQLite, `select`, `filter`, `dedupe`, `join`, `concat`.
- LLM providers: Anthropic and an offline `echo` provider.
- Notifications to Slack, generic webhooks and the console.
- CLI (`run`, `validate`, `diagram`, `runs`, `connectors`, `dashboard`) and a local dashboard.

[Unreleased]: https://github.com/arimayuji/agentl/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/arimayuji/agentl/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/arimayuji/agentl/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/arimayuji/agentl/releases/tag/v0.1.0
