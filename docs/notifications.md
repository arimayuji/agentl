# Notifications

Rules subscribe to runner [events](concepts.md#events) and post to channels.

```yaml
notify:
  channels:
    slack: { webhook: "${SLACK_WEBHOOK}" }
    ops: { type: webhook, url: https://ops.example.com/hooks/agentl }
    console: {}
  rules:
    - on: run.failed
      to: [slack]
      summary: agent
    - on: item.rejected
      when: "rate > 0.05"
      to: [slack, ops]
```

## Channels

| Type | Settings |
|---|---|
| `slack` | `webhook` |
| `webhook` | `url` (receives `{"text": ..., "event": ...}`) |
| `console` | none |

A channel's type defaults to its name. Set `type:` to give channels custom names.

## Rules

- `on`: an event type, a list of them, or `"*"`.
- `when`: a simple comparison on the event data, such as `rate > 0.05` or `count >= 10`.
- `summary: agent`: the default LLM writes a three-line explanation of the run.

Each rule sends at most one message per step per run. A failing channel never fails the pipeline.
