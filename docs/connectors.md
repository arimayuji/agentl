# Connectors

Connectors are used with `use:` and configured with `with:`. List what is installed with
`agentl connectors`.

## Files

Every reader (`csv.read`, `json.read`, `excel.read`, `file.read`, ...) takes its input from one of:

- `path`: a local file or glob (`data/**/*.csv`),
- `url`: an address downloaded first (`http(s)` with `headers` for authentication; `file://` also works),
- upstream records with a `path` field (local paths or URLs).

### Any file: `file.read`

```yaml
- id: inbox
  use: file.read
  with: { path: "inbox/**/*" }
```

`file.read` picks the reader by extension, expands **ZIP** and **GZ** archives (up to two levels)
and reads each inner file with its own reader. Records get `_file` (and `_archive` when they came
from one). Unsupported files are skipped with a warning, or fail the step with
`on_unsupported: error`. Archives are extracted safely: paths escaping the archive are rejected and
the expanded size is capped by `max_extract_bytes` (1 GiB by default).

### Support matrix

| Format | Connector | Record shape | Extra |
|---|---|---|---|
| CSV, TSV | `csv.read` | one record per row (delimiter detected) | |
| JSON, JSONL, NDJSON | `json.read` (`key: data.items`) | one record per item | |
| TXT, MD, LOG | `txt.read` (`lines: true` for one per line) | `path`, `text` | |
| XLSX, XLSM | `excel.read` (`sheet`, `header_row`) | one record per row | `excel` |
| XLS (legacy Excel) | `excel.read` | one record per row | `xls` |
| Parquet | `parquet.read` (`columns`) | one record per row | `parquet` |
| PDF | `pdf.read` | `path`, `pages`, `text` | `pdf` |
| DOCX | `docx.read` | `path`, `text`, `tables` | `office` |
| DOC (legacy Word) | `doc.read` | `path`, `text`, `tables` | `office` + LibreOffice installed |
| XML | `xml.read` (`items: item`) | attributes and simple children of each item | |
| HTML tables | `html.read` (`table: 0`) | one record per row, `table` index | |
| ZIP, GZ | `file.read` | records of the inner files | |

Writers: `csv.write` and `json.write` (`.json` or `.jsonl`).

### Example: CSV straight from a URL

```yaml
- id: sales
  use: csv.read
  with:
    url: https://example.com/exports/sales.csv
    headers: { Authorization: "Bearer ${API_TOKEN}" }
```

## HTTP

| Connector | Parameters |
|---|---|
| `http.get` | `url`, `headers`, `items`, `concurrency`, `timeout`, `retries` |
| `http.post` | same as `http.get`, plus `json` |

With an upstream step, `{field}` placeholders in the URL (and in string values of the JSON body) are
filled from each record, so one request is made per record:

```yaml
- id: details
  use: http.get
  input: customers
  with:
    url: https://api.example.com/customers/{id}
    headers: { Authorization: "Bearer ${API_TOKEN}" }
    items: data.results
    concurrency: 5
```

Requests are retried with exponential backoff on network errors, 429 and 5xx responses.

## SQL

| Connector | Parameters |
|---|---|
| `sqlite.read` | `db`, `query`, `params` |
| `sqlite.write` | `db`, `table`, `mode` (`append` or `replace`) |
| `postgres.read` | `dsn`, `query`, `params` (extra `postgres`) |
| `postgres.write` | `dsn`, `table` (or `schema.table`), `mode` (`append`, `replace`, `upsert`), `key`, `batch_size`, `create` (extra `postgres`) |

Other databases will ship as plugins.

## Transforms

| Connector | Parameters | What it does |
|---|---|---|
| `select` | `fields`, `rename` | Keep and rename fields |
| `filter` | `where`, `not_empty` | Keep records matching values |
| `dedupe` | `on` | Drop duplicates by key |
| `join` | `left`, `right`, `on`, `how` (`inner`, `left`) | Join two inputs |
| `concat` | | Append several inputs |

## Roadmap

S3/R2, email (IMAP), Postgres, Playwright and AWS Lambda are planned.
[Request a connector](https://github.com/arimayuji/agentl/issues/new?template=connector_request.yml).

## PostgreSQL

```yaml
load:
  - id: save
    use: postgres.write
    input: clean
    with:
      dsn: ${DATABASE_URL}
      table: public.sales
      mode: upsert
      key: id
```

Install with `pip install 'agentl-etl[postgres]'`. Missing tables are created with types inferred
from the records (`bigint`, `double precision`, `boolean`, `jsonb`, `timestamptz`, `text`).
`upsert` creates a unique index on `key` and uses `ON CONFLICT ... DO UPDATE`. Identifiers are
quoted and values are always sent as parameters. Keep the DSN in an environment variable.
