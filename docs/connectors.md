# Connectors

Connectors are used with `use:` and configured with `with:`. List what is installed with
`agentl connectors`.

## Files

<video class="feature-clip" autoplay muted loop playsinline preload="none" poster="../assets/clips/files.jpg" aria-label="Reading a folder of mixed files into records">
  <source src="../assets/clips/files.webm" type="video/webm">
  <source src="../assets/clips/files.mp4" type="video/mp4">
</video>

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
| HTML as Markdown | `html.read` (`format: markdown`) | one record per page: `title`, `markdown` | `web` |
| ZIP, GZ | `file.read` | records of the inner files | |

Writers: `csv.write`, `json.write` (`.json` or `.jsonl`), `parquet.write` (`compression`, needs
`agentl-etl[parquet]`) and `excel.write` (needs `agentl-etl[excel]`). All of them take
[versioned names](#versioned-outputs).

### Excel for the people who use the data

```yaml
- id: report
  use: excel.write
  input: [summary, detail]          # one sheet per input step
  with:
    path: "smb://fileserver/finance/sales_{date}.xlsx"
    latest: "smb://fileserver/finance/sales.xlsx"
    keep: 30
```

`excel.write` gives a file a person can open as is: bold header, frozen first row, a filter on
the header and column widths that fit. Numeric text (from CSV, say) becomes numbers, except values
with a leading zero such as `00123`, which stay text; `numbers: false` keeps everything as written.
Dicts and lists are written as JSON, and dates with a time zone are converted to UTC, since Excel
has no time zones. `sheet:` names the sheet when there is a single input.

### Versioned outputs

Every file writer (`csv.write`, `json.write`, `parquet.write`, `excel.write`) takes:

| Setting | Example | Effect |
| --- | --- | --- |
| `{date}` / `{datetime}` in `path` | `reports/sales_{date}.xlsx` | one file per day (`2026-10-14`) or per run (`2026-10-14T050007`), local time |
| `latest` | `reports/sales.xlsx` | a copy of the new file under a fixed name, for people and links |
| `keep` | `30` | delete all but the 30 newest versions (needs `{date}` or `{datetime}`) |

`keep` only counts files whose name matches the dated pattern, so the `latest` copy and other
files in the folder are never deleted. It works on local paths and on any remote path (S3, SMB,
SFTP, ...). If the storage keeps versions itself (S3 bucket versioning, SharePoint), writing to a
fixed name is enough.

### Example: CSV straight from a URL

```yaml
- id: sales
  use: csv.read
  with:
    url: https://example.com/exports/sales.csv
    headers: { Authorization: "Bearer ${API_TOKEN}" }
```

## HTTP

<video class="feature-clip" autoplay muted loop playsinline preload="none" poster="../assets/clips/http.jpg" aria-label="Paginated API requests with retries">
  <source src="../assets/clips/http.webm" type="video/webm">
  <source src="../assets/clips/http.mp4" type="video/mp4">
</video>

| Connector | Parameters |
|---|---|
| `http.get` | `url`, `headers`, `params`, `paginate`, `items`, `concurrency`, `timeout`, `retries` |
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

Query parameters go in `params` (also filled per record). To follow pages:

```yaml
- id: invoices
  use: http.get
  with:
    url: https://api.example.com/invoices
    params: { per_page: 500, updated_since: "{cursor}" }   # see Concepts: cursor
    paginate: { param: page, next: next_page }   # also: start (1), max_pages (1000)
    items: items
  cursor: { field: updated_at, initial: "2026-01-01" }
```

URL fetches refuse loopback, private, link-local (for example cloud metadata at
`169.254.169.254`) and reserved addresses, including after redirects, so URLs that come from data
or from an agent cannot reach internal services. For intranet pipelines set
`AGENTL_ALLOW_HOSTS=host1,10.0.0.0/8` (host names or CIDRs), or `AGENTL_ALLOW_PRIVATE_NETWORK=1`.

Pages are requested until the `next` field of the response is empty (or, without `next`, until a page
returns no items). Requests are retried with exponential backoff on network errors, 429 and 5xx
responses.

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

## AWS Lambda

Call a Lambda function as a step: once, or once per upstream record. Install with
`pip install 'agentl-etl[aws]'`; credentials come from the standard AWS chain (environment
variables, profile, instance or task role).

```yaml
- id: export
  use: aws.lambda
  with: { function: billing-export, payload: { month: "2026-10" }, items: data.rows }

- id: enrich
  use: aws.lambda
  input: customers
  with:
    function: score-customer
    payload: { customer_id: "{id}" }   # filled from each record
    keep_input: true                   # output records keep the customer's fields
    concurrency: 8
```

| Parameter | Notes |
|---|---|
| `function`, `qualifier` | function name or ARN; version or alias |
| `payload` | JSON payload; with upstream records `{field}` is filled per record, and without `payload` the record itself is sent |
| `items` | dotted path to the list inside the response |
| `keep_input` | merge the input record into each output record |
| `invocation_type` | `RequestResponse` (default) or `Event` (fire and forget, returns `status_code`) |
| `region`, `endpoint_url`, `concurrency` | client settings; parallel invocations (default 4) |

The JSON response becomes records: a list as is, an object as one record, anything else as
`{"value": ...}`. A function error fails the step with the function's error message.

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
| `selector`, `extract`, `table` | what to extract: `table` rows (header from the first row), `text`, `html`, `links` (`text`, `href`), `markdown` (see below) |
| `evidence`, `evidence_keep_days` | keep the HTML, Markdown and a full-page screenshot of every page (see below) |
| `retry_if_empty` | load the page again up to N times when the extraction gave nothing |
| `download` | selector of a link or button that downloads a file; returns `{path, filename}` |
| `timeout`, `headless`, `executable_path`, `user_agent` | browser settings (`AGENTL_BROWSER_EXECUTABLE` also sets the browser binary) |

Every record gets `_url`. Keep credentials in environment variables, and check the site's terms
before automating it.

## Web pages as Markdown

Raw HTML is a poor input for an agent step: menus, footers, cookie banners and scripts cost tokens
and confuse the model. `format: markdown` on `html.read` (and `extract: markdown` on
`browser.scrape`) gives one record per page with `title` and `markdown`: the main content with
headings, lists, tables and links (made absolute), and nothing else. It uses
[trafilatura](https://trafilatura.readthedocs.io): `pip install 'agentl-etl[web]'`.

```yaml
- id: pages
  use: html.read
  input: urls                     # records with a `path` field holding URLs
  with:
    format: markdown
    retry_if_empty: 2             # read again when a page gives no content
    evidence: true                # keep what the run saw
    evidence_keep_days: 30
- id: summary
  agent: Summarize the price changes on this page.
  input: pages
  output: { summary: str, prices_up: bool }
```

**Evidence.** Scraping breaks quietly: a late render, a layout change, a login wall. With
`evidence: true` (or a folder path) every page read is kept under
`.agentl/evidence/<date>/<time>-<page>.html`, with the `.md` next to it and, for
`browser.scrape`, a full-page `.png` screenshot. The paths are in the record
(`_evidence_html`, `_evidence_markdown`, `_evidence_screenshot`), so the step output says where
to look. `evidence_keep_days` deletes older date folders at the start of the step.
A page that is still empty after `retry_if_empty` comes back as a record with an empty
`markdown` (`_empty: true` in the browser) and its evidence, instead of disappearing.

Pages that need clicks or forms still go through `browser.scrape` with `steps`. For an agent that
navigates on its own, give an agent step a browser MCP server as a tool (for example
[Playwright MCP](https://github.com/microsoft/playwright-mcp), see
[Tools and MCP servers](agents.md#tools-and-mcp-servers)) rather than scripting every click.

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

<video class="feature-clip" autoplay muted loop playsinline preload="none" poster="../assets/clips/upsert.jpg" aria-label="Upsert keeps one row per key across runs">
  <source src="../assets/clips/upsert.webm" type="video/webm">
  <source src="../assets/clips/upsert.mp4" type="video/mp4">
</video>

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

## GraphQL

```yaml
- id: issues
  use: graphql.query
  with:
    url: https://api.github.com/graphql
    headers: { Authorization: "Bearer ${GITHUB_TOKEN}" }
    query: |
      query($owner: String!, $after: String) {
        organization(login: $owner) { repositories(first: 100, after: $after) {
          nodes { name stargazerCount } pageInfo { hasNextPage endCursor } } }
      }
    variables: { owner: arimayuji }
    items: organization.repositories.nodes
    page_info: organization.repositories.pageInfo   # follow endCursor while hasNextPage
```

`items` is the path to the list inside `data`. With `page_info`, the query must take `$after`
(or the name in `cursor_var`). With upstream records it runs once per record, filling `{field}` in
`variables`. A response with `errors` fails the step with their messages.

## Google Sheets and Drive

```yaml
- id: targets
  use: gsheets.read
  with: { spreadsheet_id: 1AbC..., range: "Targets!A:D" }   # first row is the header
- id: publish
  use: gsheets.write
  input: report
  with: { spreadsheet_id: 1AbC..., range: Report, mode: replace }   # or append
- id: drops
  use: gdrive.files
  with: { folder_id: 1XyZ..., pattern: "*.csv" }   # downloads; records have `path`
- id: rows
  use: file.read
  input: drops
```

`gdrive.files` exports Google Sheets as `.xlsx`, Docs as `.docx` and Slides as `.pdf`.
`gdrive.upload` sends the files named in each record's `path` to a folder (for example the
output of `excel.write`), replacing a file with the same name as a new version.

Credentials: `GOOGLE_ACCESS_TOKEN`, or a service account JSON in `GOOGLE_APPLICATION_CREDENTIALS`
(needs `pip install 'agentl-etl[google]'` to sign the token request). Share the sheet or folder
with the service account's email.

## SharePoint and OneDrive

The shared-folder workflow: read the files people drop in a document library, and deliver the
spreadsheet back there.

```yaml
- id: inbox
  use: sharepoint.files
  with: { site: "contoso.sharepoint.com:/sites/Finance", folder: /Inbox, pattern: "*.xlsx" }
- id: rows
  use: file.read
  input: inbox
# ... transform, check ...
- id: xlsx
  use: excel.write
  input: verified
  with: { path: "out/sales_{date}.xlsx" }
- id: deliver
  use: sharepoint.upload
  input: xlsx
  with: { site: "contoso.sharepoint.com:/sites/Finance", folder: /Reports }
```

Choose the drive with `site` (hostname and site path), `drive_id`, or `user` (a OneDrive, by
user principal name). Uploads replace a file with the same name; files over 4 MB go in 5 MiB
chunks through an upload session.

Credentials: an app registration with the Graph application permission `Sites.ReadWrite.All` (or
`Sites.Selected` granted on the site), through `MS_TENANT_ID`, `MS_CLIENT_ID` and
`MS_CLIENT_SECRET`; or a ready token in `MS_ACCESS_TOKEN`. Standard library only.

## Discord

`discord.send` posts records to a channel through an
[incoming webhook](https://support.discord.com/hc/en-us/articles/228383668) (channel settings,
Integrations, Webhooks). Put the webhook URL in an environment variable: it is a secret.

```yaml
- id: post
  use: discord.send
  input: new_orders
  with:
    webhook: ${DISCORD_WEBHOOK}
    content: "New order {id} from {customer}: R$ {total}"
```

- `content` fills `{field}` placeholders from each record (fields only: no format specs). Without
  `content` or `embed`, each record is shown as `key: value` lines.
- `mode: each` (default) posts one message per record; `mode: batch` packs the records into as
  few messages as fit Discord's 2000 characters.
- `embed: { title, description, url, color, footer }` (templates too, `color` as `"#0070c9"`) posts
  a card per record. `username` and `avatar_url` change how the webhook shows up.
- `max_messages` (default 50) stops the step before posting when the input would flood the
  channel. Rate limits (429) are waited out and 5xx answers retried (`retries`, default 3).
- Mentions are off: a record containing `@everyone` is posted as text and pings nobody.
- Each output record has the message number, Discord's message `id` and `channel_id`.

For alerts when a run fails, use the `discord` channel of [`notify:`](notifications.md) instead.

## Queues and streams

Kafka, Amazon SQS, RabbitMQ and Google Pub/Sub, as micro-batches:

```yaml
name: invoice-events
trigger:
  - every: 30s                      # with agentl serve: run again, never two at once
steps:
  - id: events
    use: kafka.consume
    with: { brokers: "${KAFKA_BROKERS}", topic: invoices, group: agentl, max_records: 5000, max_seconds: 10 }
  - id: classify
    agent: Classify the cost center of this invoice line.
    input: events
    output: { cost_center: str }
  - id: save
    use: postgres.write
    input: classify
    with: { dsn: "${PG_DSN}", table: invoice_lines, mode: upsert, key: [topic, partition, offset] }
```

A consumer reads up to `max_records` messages or for `max_seconds`, whichever comes first. It
acknowledges them only when the whole run succeeded: Kafka offsets are committed, SQS messages
deleted, RabbitMQ and Pub/Sub messages acked. If any step fails, nothing is acknowledged and the
broker delivers the messages again (at-least-once). Make the load idempotent, as the upsert on
`topic, partition, offset` above does. A failed acknowledgement fails the run (`ack.failed`).

| Read | Write | Install | Records read |
| --- | --- | --- | --- |
| `kafka.consume` (`brokers`, `topic`, `group`, `offset_reset`, `config`) | `kafka.produce` (`key` field, waits for delivery) | `[kafka]` | `value`, `key`, `topic`, `partition`, `offset`, `timestamp`, `headers` |
| `sqs.receive` (`queue_url`, `release_on_failure`) | `sqs.send` (10 per request; FIFO `group_id`, `dedup_field`) | `[aws]` | `body`, `message_id`, `attributes` |
| `rabbitmq.consume` (`url`, `queue`; requeued on failure) | `rabbitmq.publish` (persistent, publisher confirms) | `[rabbitmq]` | `body`, `routing_key`, `headers` |
| `pubsub.pull` (`subscription`; deadline reset on failure) | `pubsub.publish` (waits for message ids) | `[pubsub]` | `data`, `attributes`, `message_id`, `publish_time` |

Message bodies are parsed as JSON when they are JSON (`format: auto`); `format: json` fails on
anything else, `format: text` keeps the text. Writers send each record as JSON, or one field of
it (`value`, `body`, `data`). MQTT is not covered: its brokers acknowledge on receipt, which does
not fit the ack-after-success model.

## Remote storage

Every reader, `file.read`, `csv.write`, `json.write` and `parquet.write` accept remote paths:

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
