# Recipes

Twenty short pipelines for the jobs people ask for most. Each one is a folder in
[`examples/recipes`](https://github.com/arimayuji/agentl/tree/main/examples/recipes) with its
input data, and each one runs in CI on every change (offline: a local server stands in for the APIs
and a stand-in model for the LLMs), so they keep working as agentl changes.

To try one:

```bash
git clone https://github.com/arimayuji/agentl && cd agentl/examples/recipes/01-classify-tickets
export OPENROUTER_API_KEY=...        # or change the llm block to your provider
agentl lint pipeline.yaml            # every problem, with the fix
agentl plan pipeline.yaml            # LLM calls and cost before running
agentl run pipeline.yaml
```

Recipes that call an API or a site read `${API_URL}` / `${SITE_URL}`: point them at yours.
Recipes that write to a database use `${DATABASE_URL}` and fall back to a local SQLite file.

## Classify tickets

Classify support tickets with an LLM and save them as JSON.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/01-classify-tickets/)

```yaml
--8<-- "examples/recipes/01-classify-tickets/pipeline.yaml"
```

## Cheap model first

A cheap model answers first; records it is unsure about go to a stronger one.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/02-cheap-model-first/)

```yaml
--8<-- "examples/recipes/02-cheap-model-first/pipeline.yaml"
```

## Pdf invoices to excel

Read PDF invoices, extract the fields with an LLM and deliver a spreadsheet.

Needs `pip install "agentl-etl[pdf,excel]"`.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/03-pdf-invoices-to-excel/)

```yaml
--8<-- "examples/recipes/03-pdf-invoices-to-excel/pipeline.yaml"
```

## Contracts summary

Summarize Word contracts: parties, duration and notice period.

Needs `pip install "agentl-etl[office]"`.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/04-contracts-summary/)

```yaml
--8<-- "examples/recipes/04-contracts-summary/pipeline.yaml"
```

## Api pages to database

Read every page of a JSON API and load it into a database. DATABASE_URL: any SQLAlchemy URL (postgresql://..., mysql://...); a local SQLite file by default.

Needs `pip install "agentl-etl[sql]"`.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/05-api-pages-to-database/)

```yaml
--8<-- "examples/recipes/05-api-pages-to-database/pipeline.yaml"
```

## Incremental api load

Load only what changed since the last successful run (the cursor is kept in the run store).

Needs `pip install "agentl-etl[sql]"`.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/06-incremental-api-load/)

```yaml
--8<-- "examples/recipes/06-incremental-api-load/pipeline.yaml"
```

## Web page to markdown

Turn a web page into clean Markdown (navigation and footer removed) and summarize it.

Needs `pip install "agentl-etl[web]"`.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/07-web-page-to-markdown/)

```yaml
--8<-- "examples/recipes/07-web-page-to-markdown/pipeline.yaml"
```

## Web table to csv

Read the tables of a web page as records and save them as CSV.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/08-web-table-to-csv/)

```yaml
--8<-- "examples/recipes/08-web-table-to-csv/pipeline.yaml"
```

## Excel cleanup

Clean a spreadsheet export: keep and rename columns, drop duplicates, write a new spreadsheet.

Needs `pip install "agentl-etl[excel]"`.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/09-excel-cleanup/)

```yaml
--8<-- "examples/recipes/09-excel-cleanup/pipeline.yaml"
```

## Join csv and json

Join records from two sources on a key and save the result.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/10-join-csv-and-json/)

```yaml
--8<-- "examples/recipes/10-join-csv-and-json/pipeline.yaml"
```

## Check before loading

Check the data before it reaches the database: a failed check stops the run before the write.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/11-check-before-loading/)

```yaml
--8<-- "examples/recipes/11-check-before-loading/pipeline.yaml"
```

## Extract from emails

Extract structured fields from free text (emails, chat messages, notes).

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/12-extract-from-emails/)

```yaml
--8<-- "examples/recipes/12-extract-from-emails/pipeline.yaml"
```

## Sentiment in batches

Many short records: send 20 per call to cut cost and time.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/13-sentiment-in-batches/)

```yaml
--8<-- "examples/recipes/13-sentiment-in-batches/pipeline.yaml"
```

## Price before running

Declare prices and a budget, then run `agentl plan pipeline.yaml` to see calls and cost first.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/14-price-before-running/)

```yaml
--8<-- "examples/recipes/14-price-before-running/pipeline.yaml"
```

## Webhook to file

Run on every POST: `agentl serve pipeline.yaml`, then   curl -X POST localhost:7071/hooks/leads -H "Authorization: Bearer $LEADS_HOOK_SECRET" -d '{"name": "Ana"}'

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/15-webhook-to-file/)

```yaml
--8<-- "examples/recipes/15-webhook-to-file/pipeline.yaml"
```

## Daily report

A report every weekday at 07:00 Sao Paulo time: `agentl serve pipeline.yaml`.

Needs `pip install "agentl-etl[excel]"`.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/16-daily-report/)

```yaml
--8<-- "examples/recipes/16-daily-report/pipeline.yaml"
```

## Alert on failure

Tell someone when a run fails. Swap `console` for slack, teams, email, discord, telegram...

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/17-alert-on-failure/)

```yaml
--8<-- "examples/recipes/17-alert-on-failure/pipeline.yaml"
```

## Agent with web tool

An agent step that may look things up on the web, limited to allowed hosts.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/18-agent-with-web-tool/)

```yaml
--8<-- "examples/recipes/18-agent-with-web-tool/pipeline.yaml"
```

## Fallback provider

When the main provider fails (outage, rate limit), the same call goes to the next one.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/19-fallback-provider/)

```yaml
--8<-- "examples/recipes/19-fallback-provider/pipeline.yaml"
```

## Database report

Query a database and publish the result as JSON. Same shape for Postgres (postgres.read) or any SQLAlchemy URL (sql.read).

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/20-database-report/)

```yaml
--8<-- "examples/recipes/20-database-report/pipeline.yaml"
```

## NF-e to Excel

Read a folder of NF-e XML files and deliver a spreadsheet with one row per item, the expense
category picked by an LLM. The XML is read with no LLM (see [NF-e and NFC-e invoices](nfe.md)); drop
the `classify` step to run without one.

Needs `pip install "agentl-etl[excel]"`. Start from it with `agentl new --template nfe-to-excel`.

[Folder](https://github.com/arimayuji/agentl/tree/main/examples/recipes/21-nfe-to-excel/)

```yaml
--8<-- "examples/recipes/21-nfe-to-excel/pipeline.yaml"
```
