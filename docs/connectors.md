# Connectors

Connectors are used with `use:` and configured with `with:`. List what is installed with
`agentl connectors`.

## Files

Every reader (`csv.read`, `json.read`, `excel.read`, `file.read`, ...) takes its input from one of:

- `path`: a local file or glob (`data/**/*.csv`),
- `path` can also be remote: `s3://`, `gs://`, `az://`/`abfs://`, `sftp://`, `ftp://`, `smb://`,
  `webdav://` and anything else [fsspec](https://filesystem-spec.readthedocs.io) supports, with globs
  (see [Remote storage](#remote-storage)),
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
| `select` | `fields` (name or list), `rename` (map) | Keep and rename fields |
| `filter` | `where` (map of field: value, all must match), `not_empty` (name or list) | Keep matching records |
| `dedupe` | `on` (name or list) | Keep the first record per key, in input order |
| `join` | `left`, `right`, `on`, `how` (`inner`, `left`) | Join two inputs |
| `concat` | | Append several inputs |

## Browser (Playwright)

For pages that only work in a real browser: content rendered by JavaScript, logins, download
buttons. Install with `pip install 'agentl-etl[browser]'` and `playwright install chromium`.

```yaml
- id: report
  use: browser.scrape
  with:
    steps:
      - goto: https://portal.example.com/login
      - fill: "#user"
        value: ${PORTAL_USER}
      - fill: "#password"
        value: ${PORTAL_PASSWORD}
      - click: button[type=submit]
      - goto: https://portal.example.com/reports/monthly
    wait_for: table.results
    selector: table.results      # extract: table (default), text, html or links
```

Click a download button and read the file:

```yaml
- id: export
  use: browser.scrape
  with: { steps: [...login...], download: "a#export-csv" }
- id: rows
  use: file.read
  input: export
```

| Parameter | Notes |
|---|---|
| `url` | page to open; with upstream records it runs once per record and fills `{field}` |
| `steps` | `goto`, `fill` + `value`, `click`, `press` + `key`, `select` + `value`, `check`, `wait_for`, `wait` (ms) |
| `wait_for` | CSS selector to wait for before extracting |
| `selector`, `extract`, `table` | what to extract: `table` rows (header from the first row), `text`, `html`, `links` (`text`, `href`) |
| `download` | selector of a link or button that downloads a file; returns `{path, filename}` |
| `timeout`, `headless`, `executable_path`, `user_agent` | browser settings (`AGENTL_BROWSER_EXECUTABLE` also sets the browser binary) |

Every record gets `_url`. Keep credentials in environment variables, and check the site's terms
before automating it.

## Email (IMAP)

```yaml
- id: invoices
  use: email.read
  with:
    host: imap.gmail.com
    user: ${IMAP_USER}
    password: ${IMAP_PASSWORD}     # an app password for Gmail and Outlook
    folder: INBOX
    search: 'FROM "billing@vendor.com" UNSEEN'
    explode: true                  # one record per attachment, with `path`
    extensions: [csv, xlsx, pdf]

- id: rows
  use: file.read                   # reads each attachment by its type
  input: invoices

# ... transform and load ...

- id: done
  use: email.mark                  # last step: only runs when everything before worked
  input: invoices
  with: { host: imap.gmail.com, user: ${IMAP_USER}, password: ${IMAP_PASSWORD}, move_to: Processed }
```

| Connector | Parameters |
|---|---|
| `email.read` | `host`, `user`, `password`, `port` (993), `ssl` (true), `folder`, `search` (IMAP syntax, default `UNSEEN`), `limit` (newest N), `attachments` (true), `explode`, `extensions`, `mark_seen` (false) |
| `email.mark` | same connection settings; `seen` (true), `flags`, `move_to`. Takes upstream records with `uid` and `folder` |

Each message record has `uid`, `folder`, `message_id`, `from`, `to`, `cc`, `subject`, `date`,
`text`, `html` and `attachments` (`path`, `filename`, `content_type`, `size`). Messages are read
without changing their flags. Attachment names come from the sender, so only a safe base name is
kept and files are saved under the run's temporary folder. Standard library only.

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

## Any SQL database

`sql.read` and `sql.write` work with any database SQLAlchemy supports. Install the extra for yours:

| Database | Install | URL |
|---|---|---|
| MySQL / MariaDB | `agentl-etl[mysql]` | `mysql+pymysql://user:pass@host/db` |
| SQL Server | `agentl-etl[mssql]` | `mssql+pymssql://user:pass@host/db` |
| Oracle | `agentl-etl[oracle]` | `oracle+oracledb://user:pass@host/?service_name=XE` |
| Snowflake | `agentl-etl[snowflake]` | `snowflake://user:pass@account/db/schema?warehouse=WH` |
| BigQuery | `agentl-etl[bigquery]` | `bigquery://project/dataset` |
| DuckDB | `agentl-etl[duckdb]` | `duckdb:///path/file.duckdb` |
| Postgres, SQLite, others | `agentl-etl[sql]` + driver | `postgresql+psycopg://...`, `sqlite:///file.db` |

```yaml
- id: orders
  use: sql.read
  with:
    url: ${WAREHOUSE_URL}
    query: SELECT * FROM orders WHERE created_at >= :since
    params: { since: "2026-10-01" }

- id: save
  use: sql.write
  input: clean
  with: { url: ${MYSQL_URL}, table: orders, mode: upsert, key: id }
```

| Parameter | Notes |
|---|---|
| `mode` | `append`, `replace` (drop and recreate) or `upsert` on `key` |
| `key` | column or list of columns |
| `schema`, `batch_size`, `create` | target schema, rows per batch (default 1000), create the table when missing (default true) |
| `engine_options` | passed to `sqlalchemy.create_engine` (pool size, connect args) |

Upsert is native on Postgres, SQLite and MySQL/MariaDB (a unique index on `key` is created when
missing). On other databases, rows with the same key are deleted and re-inserted inside the same
transaction. Missing tables are created with types inferred from the records; text keys become
`VARCHAR(255)` so they can be indexed. For Postgres-specific features prefer `postgres.write`.

## Remote storage

Every reader, `file.read`, `csv.write` and `json.write` accept remote paths:

```yaml
extract:
  - id: raw
    use: file.read
    with:
      path: s3://my-bucket/exports/2026/**/*.csv

load:
  - id: archive
    use: json.write
    input: clean
    with:
      path: s3://my-bucket/processed/sales.json
```

| Storage | Install | Path |
|---|---|---|
| Amazon S3 (and S3-compatible: R2, MinIO) | `agentl-etl[s3]` | `s3://bucket/key` |
| Google Cloud Storage | `agentl-etl[gcs]` | `gs://bucket/key` |
| Azure Blob / Data Lake | `agentl-etl[azure]` | `az://container/key`, `abfs://...` |
| SFTP | `agentl-etl[sftp]` | `sftp://host/path` |
| FTP, SMB, WebDAV, others | `agentl-etl[remote]` + the fsspec backend | `ftp://...`, `smb://...` |

Credentials come from the usual places for each backend (AWS profile and `AWS_*` variables, Google
application default credentials, Azure environment variables, SSH keys). To pass options explicitly,
use `storage_options`, keeping secrets in environment variables:

```yaml
with:
  path: s3://bucket/data/*.parquet
  storage_options:
    key: ${R2_ACCESS_KEY_ID}
    secret: ${R2_SECRET_ACCESS_KEY}
    client_kwargs: { endpoint_url: https://<account>.r2.cloudflarestorage.com }
```

Files are downloaded to a temporary folder removed at exit; `_file` shows the remote URI. Writes go
to a temporary file and are uploaded when complete.

## Roadmap

S3/R2, email (IMAP), Postgres, Playwright and AWS Lambda are planned.
[Request a connector](https://github.com/arimayuji/agentl/issues/new?template=connector_request.yml).
