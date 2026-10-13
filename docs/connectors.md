# Connectors

Connectors are used with `use:` and configured with `with:`. List what is installed with
`agentl connectors`.

## Files

| Connector | Parameters | Extra |
|---|---|---|
| `csv.read` | `path` (glob), `delimiter`, `encoding` | |
| `csv.write` | `path`, `delimiter` | |
| `json.read` | `path` (glob, `.json` or `.jsonl`), `key` | |
| `json.write` | `path` (`.json` or `.jsonl`) | |
| `excel.read` | `path`, `sheet`, `header_row` | `excel` |
| `pdf.read` | `path` or upstream records with `field` (default `path`) | `pdf` |
| `docx.read` | `path` or upstream records with `field` | `office` |

`pdf.read` returns one record per file with `path`, `pages` and `text`. `docx.read` returns `path`,
`text` and `tables`.

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
