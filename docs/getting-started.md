# Getting started

## Install

```bash
pip install agentl-etl                 # core: CSV, JSON, HTTP, SQLite, runner, dashboard
pip install 'agentl-etl[office,pdf]'   # Excel, Word, PDF
pip install 'agentl-etl[anthropic]'    # Claude as the LLM provider
pip install 'agentl-etl[all]'
```

agentl needs Python 3.10 or newer. The package is published as `agentl-etl`; you still `import agentl` and run the `agentl` command.

## A one-off job: Python

For a job you run once (convert a spreadsheet, combine files, read an API), a short script is
enough. agentl reads and writes the formats for you:

```python
import agentl as al

rows = al.read("invoices/*.pdf")  # csv, tsv, json, xlsx, parquet, pdf, docx, txt, xml, html, URLs
rows = al.read("customers.xlsx", sheet="export")
rows = al.read("sqlite:///shop.db", query="select * from orders")
rows = al.http_get_all("https://api.example.com/items", items="data", next="next_page")

al.write(rows, "out/report.xlsx")  # csv, json, jsonl, xlsx, parquet, or a DB URL with table=
```

Records are plain dicts, so the rest is ordinary Python. When the job has to run again, call an
LLM on many records, survive a failure halfway, run on a schedule or have its data checked before
delivery, turn it into a pipeline: that adds the per-record cache, retries, checks, a cost budget
and run history.

## Your first pipeline

The repository ships an example that runs offline with the `echo` provider, so you can try it
without an API key:

```bash
git clone https://github.com/arimayuji/agentl
cd agentl
pip install -e .
agentl validate examples/quickstart.yaml
agentl run examples/quickstart.yaml --show classify
```

## Use a real model

Switch the provider and export your key:

```yaml
llm:
  provider: anthropic
  model: claude-opus-5-5
```

```bash
export ANTHROPIC_API_KEY=...
agentl run pipeline.yaml
```

!!! warning "Secrets"
    Never write keys in the YAML. Use `${VAR}` and environment variables.

## Next steps

- Learn the building blocks in [Concepts](concepts.md).
- Browse the [connectors](connectors.md).
- Watch your runs in the [dashboard](cli.md).
