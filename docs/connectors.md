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
| HTML tables | `html.read` (`table: 0`, `select`, `header_rows`) | one record per row, `table` index | |
| HTML by CSS selector | `html.select` (`items`, `fields`) | one record per matched element | |
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
| `sqlite.write` | `db`, `table`, `mode` (`append`, `replace` or `upsert` with `key`), `on_conflict`, `update_columns` (upsert only) |
| `postgres.read` | `dsn`, `query`, `params` (extra `postgres`) |
| `postgres.write` | `dsn`, `table` (or `schema.table`), `mode` (`append`, `replace`, `upsert`), `key`, `on_conflict`, `update_columns`, `batch_size`, `create` (extra `postgres`) |

Other databases will ship as plugins.

## Transforms

| Connector | Parameters | What it does |
|---|---|---|
| `select` | `fields` (name or list), `rename` (map) | Keep and rename fields |
| `filter` | `where` (map of field: value, all must match), `not_empty` (name or list) | Keep matching records |
| `dedupe` | `on` (name or list), `keep` (`first`, `last`, `{latest: field}`) | One record per key |
| `join` | `left`, `right`, `on` (name or list) or `left_on` / `right_on`, `how` (`inner`, `left`, `right`, `outer`), `suffix` or `suffixes: [_l, _r]` | Join two inputs; a key missing from one side fails naming it |
| `flatten` | `sep` (`.`), `fields` | Nested objects become columns: `end.cidade` |
| `explode` | `field`, `keep_empty` (true) | One record per item of a list |
| `pivot` | `index`, `columns`, `values`, `agg` (`first`, `last`, `sum`, `count`, `avg`, `min`, `max`, `list`) | Long to wide |
| `unpivot` | `columns`, `name` (`variable`), `value` (`value`) | Wide to long |
| `concat` | | Append several inputs |
| `cast` | `fields` (field: type), `locale`, `on_bad`, `strict` | Clean, typed values; bad rows rejected with the reason (below) |
| `compute` | `fields` (field: expression), `on_error` (`reject`, `fail`, `null`) | New or replaced fields from safe expressions (below) |
| `aggregate` | `group_by` (name or list), `metrics` (name: `sum(f)`, `count()`, `avg(f)`, `min(f)`, `max(f)`, `count_distinct(f)`, `first(f)`, `last(f)`, `list(f)`), `window` (`{field, every, end}`) | One record per group, or per time window and group (below) |
| `sort` | `by` (`[region, -total]` or `{total: desc}`) | Order records; empty values last |
| `limit` | `n`, `offset` | The first `n` records |
| `protect` / `unprotect` | `fields`, `key` | Encrypt, hash, mask or drop sensitive fields (below) |
| `fill` | `values` (field: default) | Defaults for missing, null or blank fields |
| `sql` | `query` or `file`, `params`, `allow_files` (false) | SQL over the previous steps, each one a table (below) |

### SQL over previous steps: `sql`

`pip install 'agentl-etl[duckdb]'` (DuckDB is a large wheel, so it is an optional extra). Each
input step is a table named after its step id, queried in an in-process, in-memory DuckDB. Steps on
the Arrow fast path are scanned in place, without a copy; records become an Arrow table first. The
output is records.

Join two sources:

```yaml
- id: report
  input: [orders, customers]
  sql: |
    select c.region, sum(o.total) as total, count(*) as orders
    from orders o join customers c using (customer_id)
    group by c.region
```

Window functions:

```yaml
- id: running
  input: orders
  sql: |
    select order_id, customer_id, total,
           sum(total) over (partition by customer_id order by order_id) as running_total,
           row_number() over (partition by customer_id order by total desc) as rank
    from orders
```

Aggregate from a file, with parameters:

```yaml
- id: daily
  use: sql
  input: orders
  with:
    file: sql/daily.sql          # next to the pipeline
    params: { min_total: 100 }   # $min_total in the query
```

```sql
-- sql/daily.sql
select day, count(*) as orders, sum(total) as total
from orders
where total >= $min_total
group by day
order by day
```

- `sql:` on a step is short for `use: sql` with `with: {query: ...}`.
- `params` are bound as values (`$name`), never pasted into the SQL text.
- **Sandbox.** By default the query cannot read or write files, reach the network, `ATTACH` a
  database, `COPY ... TO`, `INSTALL` or load extensions, and cannot change those settings. Set
  `allow_files: true` on the step only when the SQL itself must read files such as
  `read_parquet('data/*.parquet')`.
- Errors name the step and keep DuckDB's `LINE n` pointer; a table that is not an input suggests
  the closest step id. `agentl validate` reports the same (AE009) when DuckDB is installed.
- An input with no records has no columns: `count(*)` gives 0 and a query that names its columns
  returns no records.
- With `chunk_size`, a `sql` step is a boundary: it collects its inputs before it runs.

When to use which: `compute`, `aggregate`, `cast` and the other transforms above need no extra and
stream with `chunk_size`; use them for per-record expressions and simple group-bys. Use `sql` when
the transform is naturally a join, several group-bys or window functions over data that fits in
memory. When the data already lives in a warehouse, run the SQL there (a `sql.read` query) instead
of moving it out to transform it.

### Match names to a reference table: `resolve`

Maps messy values ("SÃO PAULO", "S. Paulo", "SP") to one id of a reference table. Each value is
normalised (lower case, accents and punctuation removed, suffixes such as Ltda, Inc or LLC
dropped), then matched exactly against the reference names and ids, then by similarity. Only
values whose best similarity is below `threshold` and at least `min_score` go to the model, with
the closest `candidates`; the model can only answer one of those ids or null.

```yaml
- id: regions
  use: resolve
  input: [rows, region_table]       # the reference can be one of the inputs
  with:
    field: region
    reference: region_table         # or {file: regions.csv} or {values: [{id: SP, name: Sao Paulo}]}
    key: id                         # reference column with the id (default id)
    name: name                      # reference column with the name (default name)
    threshold: 0.9                  # similarity accepted without the model (0 to 1)
    min_score: 0.5                  # below this, unresolved without asking the model
    on_unresolved: reject           # reject (default), keep (match null) or drop
```

Each record gets `<field>_match` (the id; rename with `into`), `<field>_match_score` and
`<field>_match_method` (`exact`, `fuzzy`, `model` or `unresolved`). Unresolved records are
rejected, so they never reach the destination; list and retry them with `agentl rejected`.
`model: false` turns the model off; `llm:` on the step picks which one to use.

Similarity uses the standard library's `difflib`; install `agentl-etl[fuzzy]` to use
`rapidfuzz`, which is much faster on large reference tables (scores differ slightly).

The model's decisions are stored per pipeline and step, so a value never costs twice. A
`resolve.counts` event reports how many rows took each path, plus how many model decisions came
from the store (`cached`). Export them for review, fix them, and import them back:

```bash
agentl resolve export my-pipeline regions decisions.json
agentl resolve import my-pipeline regions decisions.json
```

### Time windows: `aggregate` with `window`

`window: {field: ts, every: 1m}` puts each record in a tumbling window of `every` (`30s`, `5m`,
`1h`, `1d`) and aggregates per window and `group_by` group:

```yaml
- id: per_minute
  use: aggregate
  input: readings
  with:
    group_by: device
    window: { field: ts, every: 1m, end: true }
    metrics: { avg_temp: "avg(temperature)", max_temp: "max(temperature)", readings: "count()" }
```

- Windows are aligned to the Unix epoch in UTC: `1m` windows start at :00 seconds, `1d` windows
  at 00:00 UTC (21:00 the day before in `-03:00`).
- The timestamp can be ISO text, a datetime or epoch seconds or milliseconds (also as text).
- Each output record has `window_start` in ISO 8601, keeping the input's offset (`+00:00` for
  `Z` and epoch numbers); naive input gives naive output. `end: true` adds `window_end`.
- Output is sorted by window, then group.
- A record whose timestamp is empty or cannot be read goes to the step's rejected records with
  `_error`; the rest are aggregated.
- `every` is checked before any record is read: `1x` or `0s` fail with a clear error.

Each run aggregates only the records it got. When a window spans two micro-batches, both runs
produce a row for it; load with an upsert on `window_start` and the group, and use metrics that
can be recomputed (or read the whole window again) if partial rows matter.

### Expressions: `compute`

```yaml
- id: totals
  use: compute
  input: orders
  with:
    fields:
      total: "price * qty"
      nome: "upper(trim(nome))"
      faixa: "if(total > 1000, 'alta', 'baixa')"
      cep: "regex(endereco, '\\d{5}-?\\d{3}')"
      dias: "date_diff(today(), criado_em)"
```

Fields are computed in order, so a later one can use an earlier one. An expression has field
names, numbers, quoted text, `+ - * / // %`, comparisons, `in`, `and` / `or` / `not`,
`a if cond else b`, and these functions: `upper`, `lower`, `trim`, `title`, `unaccent`, `replace`,
`regex` (first match, or a group), `regex_replace`, `substr`, `concat`, `split`, `len`, `coalesce`,
`if`, `round`, `abs`, `floor`, `ceil`, `min`, `max`, `int`, `float`, `str`, `is_null`, `today`,
`year`, `month`, `day`, `date_diff` (days, weeks, months or years), and `field("a name with
spaces")`. Nothing else is accepted: no attribute access, indexing, imports or other calls, so a
pipeline file cannot run code through an expression. A missing field is null, arithmetic with null
gives null, division by zero gives null, and a value that is not a number where one is needed sends
the record to the rejected records with the reason. Text in quotes is taken as written, so a regex
keeps its backslashes. For more than this, use a `fn:` step.

### Clean types: `cast`

Most transform work is dirty data in known formats. `cast` turns it into clean values and moves
the rows it cannot read to the step's rejected records, with the reason and the original value,
instead of letting them crash a later step or reach the database.

```yaml
- id: clean
  use: cast
  input: sheet
  with:
    locale: pt-BR                       # numbers 1.234,56; dates day first when nothing tells
    on_bad: reject                      # or fail (stop the run), keep (leave the original value)
    fields:
      nome: name                        # " joão DA silva " -> "João da Silva"
      valor: { money: BRL }             # "R$ 1.234,56" -> 1234.56
      data: date                        # "15/10/26", "3 out 2026", 46310 (Excel) -> "2026-10-15"
      criado_em: { datetime: America/Sao_Paulo }
      cpf: cpf                          # digits only; check digits verified
      cep: cep                          # "01310100" -> "01310-100"
      telefone: phone                   # "(11) 98765-4321" -> "+5511987654321"
      ativo: bool                       # sim, s, x, true, 1 / não, n, false, 0
      status: { enum: [pago, pendente], synonyms: { pago: [paid, quitado] } }
```

| Type | Reads | Gives |
|---|---|---|
| `int`, `decimal` | `1.234,56` (pt-BR), `1,234.56` (en-US), `(10,00)` negative | number |
| `money` | `R$ 1.234,56`, `US$ 10`, `2.300 BRL` (`{money: BRL}`) | number, 2 decimals |
| `percent` | `12,5%`, `0.3`, `30` | fraction (0.125) |
| `date` | ISO, `15/10/2026`, `15/10/26`, `15.10.2026`, `3 out 2026`, `3 de outubro de 2026`, `Oct 15, 2026`, `15th October 2026`, Excel serials | `2026-10-15` |
| `datetime` | the above with a time, ISO with zone, epoch seconds or ms (`{datetime: <IANA zone>}` for naive values) | ISO with offset |
| `bool` | `sim`/`não`, `s`/`n`, `x`, `true`/`false`, `1`/`0` | true / false |
| `enum` | the values and their synonyms, without case or accents | the value |
| `text`, `name` | anything: trimmed, spaces collapsed, NFC; `name` also title case (da, de, do in lower case) | text |
| `email` | addresses, lower-cased | text |
| `cpf`, `cnpj` | with or without punctuation; check digits verified | digits |
| `cep`, `uf` | `01310100`, `sp` | `01310-100`, `SP` |
| `phone` | Brazilian numbers with area code; US and Canada with `{phone: US}`; or `+` international | E.164 |
| `state`, `zip` | `ca`, `California`; `94103`, `941031234` | `CA`; `94103`, `94103-1234` |
| `ein`, `ssn` | `12-3456789`; `123 45 6789` (format only, see below) | `12-3456789`; `123-45-6789` |

Rules, so nothing is guessed silently:

- **Empty is null**, not an error (a blank CPF is null, not a rejection).
- **Dates are read in one order per column.** A value like `25/02` means the column is day first,
  `02/25` month first; a column with both fails. When nothing tells (`01/02`), the locale decides
  (pt-BR day first, en-US month first), or `strict: true` stops instead.
- **Impossible values are rejected, never fixed**: `31/02/2026`, a CPF with wrong check digits.
- **Numbers in pt-BR**: a dot followed by exactly three digits is a thousands separator
  (`1.050` is 1050); any other single dot is a decimal point (`99.9`).
- Dates and times come out as ISO text, so every writer stores the same thing.
- **US data**: set `locale: en-US` (`1,234.56`, month-first dates when nothing tells). `ssn` and
  `ein` check the format only (an SSN with a never-issued range such as 000 or 666 is refused);
  they do not say a number belongs to anyone, and a rejection reason never repeats an SSN. These
  are personal data: the run store keeps the first records of each step for `show_output`, so set
  `AGENTL_SAMPLES=0` for such pipelines.
- Your own formats: `agentl.types.register_type("sku", parse)`, where `parse(value)` returns the
  clean value or raises `ValueError` with the reason.

### Protect sensitive fields: `protect`

Encrypt, hash, mask or drop fields before records reach a destination, for data protection laws
(GDPR, LGPD) or plain privacy. Needs `pip install 'agentl-etl[crypto]'` for `encrypt`.

```bash
agentl keygen          # 32 random bytes in base64; keep it in a secret manager
export AGENTL_DATA_KEY=...
```

```yaml
- id: protect
  use: protect
  input: clean
  with:
    key: ${AGENTL_DATA_KEY}          # or a secret reference; never the key itself
    fields:
      tax_id: encrypt                # reversible with `unprotect` and the key
      email: hash                    # same input, same output: joins and dedupe still work
      phone: mask                    # "*******4321" ({mask: 2} keeps 2 characters)
      notes: drop
```

| Action | What is stored | Reversible |
|---|---|---|
| `encrypt` | `enc:v1:<key id>:<base64>`, AES-256-GCM with a random nonce; the field name is bound to the value, so it does not decrypt in another field | yes, with `unprotect` |
| `hash` | HMAC-SHA256 in hex, keyed: a plain hash of a tax id or phone number is reversed by trying every value | no |
| `mask` | the last characters, the rest as `*` | no |
| `drop` | nothing | no |

To read encrypted fields back: `use: unprotect` with `fields: [tax_id]` and the same `key`. Its output
is never kept in the run store's samples. To rotate keys, pass a list: `key: [${NEW_KEY}, ${OLD_KEY}]`
protects with the first and unprotects with any of them; hashes change with the key, so rehash
before relying on joins across keys. An empty value stays empty.

- Put `protect` before any agent step that does not need those fields: `agentl lint` warns (AL008)
  when an agent step sees them first, since its records go to the LLM provider.
- Encrypted or hashed data is still personal data under most privacy laws, and whoever holds the
  key can read it. Losing the key loses the data.
- Steps before `protect` still hold the clear values: set `AGENTL_SAMPLES=0` and
  `AGENTL_QUARANTINE=0` if the run store must never see them.
- In transit, encryption is set in the connection URL, for example `?sslmode=require` on Postgres.

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

## Extracting data from web pages

`html.select` turns HTML into records with CSS selectors; `crawl` walks a list of links (with
pagination) and runs extraction steps on every page it links to. Both use the standard library
only.

```yaml
- id: reports
  use: crawl
  with:
    start: https://example.org/reports
    list: { select: "div.item a", attr: href, next_page: "a.next", max_pages: 5 }
    detail:
      - use: html.select
        with:
          fields:
            title: h2.title
            price: { select: .price, type: money }
            pdf: { select: a.pdf, attr: href, absolute: true }
      - use: html.read
        with: { select: table.data, header_rows: 2 }
    limit: 20
    on_selector_miss: fail_with_snapshot   # the default
```

Every record from a detail step gets `url` (the detail page) and `source_url` (the list page
that linked to it). Without `detail`, `crawl` gives one record per page with its `html`, ready
for a later `html.select`, `html.read` or agent step. Without `list`, the `start` URLs (or the
`url` field of upstream records) are the pages to read.

**html.select.** `items` is the selector of the repeated element (a card, a row); without it the
whole page is one record. Each entry in `fields` is a selector (its trimmed text) or a mapping:

| Key | Notes |
|---|---|
| `select` | CSS selector inside the item; empty means the item itself |
| `attr` | read this attribute instead of the text (`href`, `src`, `data-id`) |
| `absolute` | resolve a relative link against the page URL (or its `<base href>`) |
| `type` | convert with the [`cast`](#clean-types-cast) types: `money`, `int`, `decimal`, `date`, `{money: USD}`... (`locale` on the step, `pt-BR` by default) |
| `all` | every match as a list instead of the first |
| `required` | `false` gives `null` when nothing matches instead of a selector miss |

The HTML comes from `url`, `path`, or the `html` field of upstream records (`browser.scrape` with
`extract: html`, `crawl`), so it chains after a login done in the browser. Supported selectors:
tag, `#id`, `.class`, `[attr]`, `[attr=v]` (and `^=`, `$=`, `*=`, `~=`), `:first-child`,
`:last-child`, `:nth-child(n)`, `:nth-of-type(n)`, descendant and `>` combinators, and groups
with commas. XPath is not supported.

**Tables.** `html.read` takes `select` (a CSS selector for the tables to read), `header_rows: N`
(several header rows are joined: `Sales` over `Q1` becomes `Sales Q1`) and repeats `colspan` and
`rowspan` cells into every row and column they cover, so merged cells come out denormalised.

**Good conduct.** `crawl` makes one request at a time, `delay` seconds apart (1 by default), with
a `User-Agent` that names agentl (`user_agent` changes it), follows only links on the start
host (`same_host: false` to leave it), and goes through the same private-network guard as every
other connector (see [HTTP](#http)). It does not read `robots.txt` yet: check the
site's terms before crawling it.

**When the site changes.** A selector that matches nothing is how a scraper breaks. With
`on_selector_miss: fail_with_snapshot` (default) the step fails with an error that names the
selector and the URL, saves the HTML it received under `.agentl/evidence/<date>/` (or
`snapshot_dir`), puts that path in the error and emits a `selector.missed` event with
`selector`, `url` and `snapshot`. Open the snapshot, fix the selector, run again. `skip` drops the
page (or the item, for a field) and logs a warning; `empty` keeps the item with `null` in the
field. A missing `next_page` link is the end of the list, not a miss. A future mode will hand the
snapshot to an agent step as the fallback when a selector fails (#195).

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

Upsert options, the same on `postgres.write` and `sqlite.write`:

| Option | Meaning |
|---|---|
| `on_conflict: update` | Default. A record whose key exists updates the row, only when a value differs (`... DO UPDATE ... WHERE (cols) IS DISTINCT FROM (EXCLUDED cols)`). |
| `on_conflict: do_nothing` | Keep the existing row; only new keys are inserted. |
| `on_conflict: error` | Fail the step (and roll back the transaction) on a key that already exists. |
| `update_columns: [status, amount]` | With `update`, change only these columns; the others keep their stored values. |

```yaml
    with:
      dsn: ${DATABASE_URL}
      table: public.sales
      mode: upsert
      key: id
      update_columns: [status, amount]
```

An upsert reports `inserted`, `updated` and `unchanged` (key existed, nothing changed, or skipped
by `do_nothing`) in its result, in the `step.succeeded` event next to `written`, and per step under
`writes` in the run summary. Loading the same data twice reports `inserted: 0` and
`unchanged` equal to the number of records, and leaves the table as it was. The options are
checked before connecting: `on_conflict` or `update_columns` without `mode: upsert`, an unknown
value, a key column in `update_columns` or a column the records do not have is an error.
`sqlite.write` needs no unique index on `key`.

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

## Messaging

Every messaging service is a load step `<service>.send` and a [`notify:`](notifications.md)
channel of the same name, from one implementation. The steps share these settings:

- `content`: template with `{field}` placeholders (fields only, no format specs). Without it, each
  record is sent as `key: value` lines.
- `title`: template too, for services with a title (email subject, ntfy, Pushover, Teams).
- `mode: each` (default) sends one message per record; `mode: batch` packs records into as few
  messages as the service allows.
- `max_messages` (default 50) stops the step before sending anything when the input would flood
  the destination. Rate limits (429) are waited out and 5xx answers retried (`retries`, default 3).

| Step | Settings | Get them from |
|---|---|---|
| `slack.send` | `webhook`, `username`, `icon_emoji`, `channel` | Slack app, Incoming Webhooks |
| `mattermost.send` | `webhook` | Integrations, Incoming Webhooks (Rocket.Chat works too) |
| `teams.send` | `webhook` | Teams Workflows, "When a Teams webhook request is received" |
| `discord.send` | `webhook`, `embed`, `username` | Channel settings, Integrations, Webhooks ([details](#discord)) |
| `googlechat.send` | `webhook` | Space, Apps and integrations, Webhooks |
| `telegram.send` | `token`, `chat_id` | @BotFather; the chat id from `getUpdates` |
| `whatsapp.send` | `token`, `phone_number_id`, `to`, `template` | Meta for Developers, WhatsApp, API Setup |
| `sms.send` | `account_sid`, `auth_token`, `from`, `to` | Twilio console |
| `ntfy.send` | `topic`, `token`, `priority`, `tags` | ntfy.sh (or your own server); install the ntfy app |
| `pushover.send` | `token`, `user`, `priority` | pushover.net |
| `matrix.send` | `homeserver`, `access_token`, `room_id` | Element, Settings, Help and About |
| `email.send` | `host`, `port`, `user`, `password`, `from`, `to` | your SMTP provider |

```yaml
- id: alert_sales
  use: whatsapp.send
  input: big_orders
  with:
    token: ${WHATSAPP_TOKEN}
    phone_number_id: ${WHATSAPP_PHONE_ID}
    to: "5511999990000"
    content: "Order {id}: R$ {total} from {customer}"
- id: push
  use: ntfy.send
  input: big_orders
  with: { topic: "https://ntfy.sh/${NTFY_TOPIC}", title: "Order {id}", mode: batch }
```

**WhatsApp** has rules of its own. The Cloud API delivers free text only within 24 hours of the
person's last message to your business number. Outside that window, send an approved template:
`template: { name: order_update, language: pt_BR }`, and `content` fills its first body
parameter. Meta charges per conversation. Without a Meta business account, Twilio reaches WhatsApp
too: `sms.send` with `from: "whatsapp:+1..."` and `to: "whatsapp:+55..."`.

Records are untrusted data: Slack `<!channel>`, Google Chat `<users/all>` and Discord `@everyone`
in a record are sent as text and ping nobody; Telegram gets plain text (no Markdown or HTML).

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

Kafka, Amazon SQS, RabbitMQ, Google Pub/Sub, MQTT, Amazon Kinesis and Azure Event Hubs, as
micro-batches:

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
| `mqtt.subscribe` (`url`, `topic`, `qos`, `client_id`, `clean_session`) | `mqtt.publish` (`topic` or `topic_field`, `qos`, `retain`; waits for confirmation) | `[mqtt]` | `value`, `topic`, `qos`, `retain`, `timestamp` |
| `kinesis.read` (`stream` name or ARN, `region`, `start`) | | `[aws]` | `value`, `partition_key`, `sequence_number`, `shard_id`, `arrival_timestamp` |
| `eventhubs.consume` (`connection_string` or `namespace`, `eventhub`, `consumer_group`, `start`) | | `[eventhubs]` | `value`, `partition_id`, `sequence_number`, `offset`, `enqueued_time`, `partition_key`, `properties` |

Message bodies are parsed as JSON when they are JSON (`format: auto`); `format: json` fails on
anything else, `format: text` keeps the text. Writers send each record as JSON, or one field of
it (`value`, `body`, `data`).

### MQTT

`mqtt.subscribe` connects to `mqtt://host:1883` or `mqtts://host:8883` (TLS), with credentials
in the URL (`mqtt://user:pass@host`) or in `username` / `password`. `topic` is one filter or a
list, with the `+` (one level) and `#` (the rest) wildcards. `timestamp` is when agentl received
the message (MQTT 3.1.1 messages carry no time of their own); readings usually carry the
device's time in the payload.

MQTT has no consumer offsets. What survives between runs is the broker's session:

- With a `client_id`, the session is persistent (`clean_session: false` by default). QoS 1 and 2
  messages are acked only when the run succeeds; if it fails, or a message arrives after the
  batch is full, it stays with the broker and is delivered again on the next run with that
  `client_id` (at-least-once). Use one `client_id` per pipeline.
- While no run is connected, the broker keeps QoS 1/2 messages for the session up to its own
  queue limits and session expiry. Check them before running every few minutes.
- Without a `client_id`, or with `clean_session: true`, the session ends on disconnect and what
  was not read is gone.
- QoS 0 messages are never stored or redelivered: they can be lost.
- Retained messages arrive right after subscribing, with `retain: true`.

`mqtt.publish` sends each record (or its `value` field) as JSON to `topic`, or to the topic in
each record's `topic_field`, with `qos` and `retain`, and fails if any message is not confirmed
within `timeout`.

Sensor readings to Postgres, one row per device and minute:

```yaml
name: sensor-minutes
trigger:
  - every: 1m
steps:
  - id: readings
    use: mqtt.subscribe
    with:
      url: "mqtts://${MQTT_USER}:${MQTT_PASSWORD}@${MQTT_HOST}:8883"
      topic: sensors/+/telemetry
      qos: 1
      client_id: agentl-sensor-minutes
      max_records: 20000
      max_seconds: 20
  - id: fields                      # payload {"device": "d1", "ts": "...", "temperature": 21.5}
    use: flatten
    input: readings
    with: { fields: value }
  - id: pick
    use: select
    input: fields
    with:
      fields: [value.device, value.ts, value.temperature]
      rename: { value.device: device, value.ts: ts, value.temperature: temperature }
  - id: typed
    use: cast
    input: pick
    with: { fields: { ts: datetime, temperature: float } }
  - id: per_minute
    use: aggregate
    input: typed
    with:
      group_by: device
      window: { field: ts, every: 1m, end: true }
      metrics: { avg_temp: "avg(temperature)", max_temp: "max(temperature)", readings: "count()" }
  - id: save
    use: postgres.write
    input: per_minute
    with: { dsn: "${PG_DSN}", table: sensor_minutes, mode: upsert, key: [device, window_start] }
```

Devices on a cloud IoT service can use the same recipe: AWS IoT Core rules can route messages
to a Kinesis data stream (read it with `kinesis.read`), and Azure IoT Hub exposes an Event
Hubs-compatible endpoint (read it with `eventhubs.consume`). The payload is in `value` either way.

!!! note "Batches, not millisecond streaming"
    agentl runs batches and micro-batches, such as `agentl serve` running a pipeline every
    minute. It is not a stream processor: latency is the trigger interval plus the run. Real-time
    alarms (a temperature over a limit right now) belong in the broker's rules or a stream
    processor; agentl fits the aggregation, enrichment and loading around them.

### Kinesis and Event Hubs

Neither keeps consumer offsets for agentl, so the checkpoints live in the run store, under the
pipeline and step id, like incremental cursors: `agentl state` lists them and resetting the step
reads from `start` again. They advance only when the whole run succeeds; a failed run reads the
same records again (at-least-once).

- `kinesis.read` reads every shard of a data stream (`stream`: name or ARN, `region`). The first
  run starts at `start: TRIM_HORIZON` (the oldest record kept, the default) or `LATEST`; later
  runs continue after the last sequence number read per shard (`AFTER_SEQUENCE_NUMBER`). After a
  reshard, a closed shard is read to its end before its child shards.
- `eventhubs.consume` receives from every partition for `max_seconds` (or until `max_records`)
  with the sync `EventHubConsumerClient`. Connect with `connection_string` (plus `eventhub` when
  it has no `EntityPath`), or with `namespace` (`<name>.servicebus.windows.net`) and `eventhub`
  through `DefaultAzureCredential` (install `azure-identity`). `consumer_group` defaults to
  `$Default`, `start` to `earliest` (or `latest`). No blob checkpoint store is needed; use one
  consumer group per pipeline.

Azure IoT Hub's built-in endpoint is Event Hubs-compatible: give `eventhubs.consume` the
"Event Hub-compatible endpoint" connection string from the hub's built-in endpoints page.

```yaml
  - id: readings
    use: kinesis.read
    with: { stream: sensor-telemetry, region: us-east-1, max_records: 20000, max_seconds: 20 }
```

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
