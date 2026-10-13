---
hide:
  - navigation
  - toc
---

<div class="hero" markdown>

# agentl

<p class="tagline">Agentic ETL for Python. Declare pipelines in YAML where each step is a connector,
a function or an LLM agent, and follow every run in a live diagram.</p>

[Get started](getting-started.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/arimayuji/agentl){ .md-button }

</div>

<div class="agt-terminal" role="img" aria-label="Terminal demo: install agentl, validate and run a pipeline, open the dashboard">
  <div class="agt-bar"><span></span><span></span><span></span></div>
  <div class="agt-body"></div>
</div>

## One file, the whole pipeline

```yaml title="pipeline.yaml"
name: invoices
llm: { provider: anthropic, model: claude-opus-5-5 }

steps:
  - id: invoices
    use: csv.read
    with: { path: data/invoices.csv }

  - id: clients
    use: json.read
    with: { path: data/clients.json }

  - id: matched
    use: join
    input: [invoices, clients]
    with: { left: invoices, right: clients, on: tax_id, how: left }

  - id: classify
    agent: Classify the expense category and flag unknown clients for review.
    input: matched
    output: { number: str, client: str?, category: str?, review: bool? }

  - id: save
    use: sqlite.write
    input: classify
    with: { db: out/invoices.db, table: invoices }
```

```bash
agentl run pipeline.yaml
agentl dashboard        # http://127.0.0.1:7070
```

## Why agentl

<div class="grid cards" markdown>

-   :material-file-code-outline: **Pipelines as YAML**

    ---

    No folder of scripts. One declarative file that is easy to review, diff and version.

-   :material-robot-outline: **Agents where judgement is needed**

    ---

    LLM steps run per record with a validated output schema. Bad outputs are rejected, not silently stored.

-   :material-connection: **Connectors included**

    ---

    CSV, JSON, Excel, PDF, Word, HTTP and SQLite out of the box. Add your own in a few lines.

-   :material-graph-outline: **See every run**

    ---

    A live Mermaid diagram, a local dashboard and per-step events for each execution.

-   :material-bell-ring-outline: **Notifications that explain**

    ---

    Slack, webhooks or the console, with rules like `rate > 0.05` and an LLM summary of what failed.

-   :material-cached: **Cheap to re-run**

    ---

    Agent results are cached per record. A crash at record 4,000 resumes from there.

</div>

## Contributors

agentl is built in the open. Thanks to everyone who has contributed code, docs and ideas.

<div class="contributors" markdown>
[![Contributors](https://contrib.rocks/image?repo=arimayuji/agentl)](https://github.com/arimayuji/agentl/graphs/contributors)
</div>

Want to help? Read the [contributing guide](community.md).

## Sponsors

agentl is free and MIT licensed. Sponsorship pays for maintenance time, CI and LLM credits for testing
real providers.

[:octicons-heart-fill-24: Sponsor agentl](https://github.com/sponsors/arimayuji){ .md-button }

Your company's logo could be here. Sponsors at any tier are listed on this page and in the README.
