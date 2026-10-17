# Notifications

<video class="feature-clip" autoplay muted loop playsinline preload="none" poster="../assets/clips/notify.jpg" aria-label="One event fanned out to several channels">
  <source src="../assets/clips/notify.webm" type="video/webm">
  <source src="../assets/clips/notify.mp4" type="video/mp4">
</video>

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
| `teams` | `webhook` (a Teams Workflows "When a Teams webhook request is received" URL; posts an Adaptive Card) |
| `discord` | `webhook` |
| `telegram` | `token` (bot token), `chat_id` |
| `email` | `host`, `port` (587), `user`, `password`, `from`, `to` (address or list), `starttls` (true), `ssl` (false, port 465) |
| `pagerduty` | `routing_key` (Events API v2), `severity` (default: error for failures, warning otherwise) |
| `opsgenie` | `api_key`, `region` (`eu` for the EU instance), `priority` (default P2 for failures, P4 otherwise) |
| `whatsapp` | `token`, `phone_number_id`, `to` (WhatsApp Cloud API; see [Messaging](connectors.md#messaging) for the 24-hour rule and `template`) |
| `sms` | `account_sid`, `auth_token`, `from`, `to` (Twilio; `to: "whatsapp:+55..."` for WhatsApp through Twilio) |
| `googlechat` | `webhook` |
| `mattermost` | `webhook` (Rocket.Chat too) |
| `ntfy` | `topic` (URL), `token`, `priority`, `tags` (push to your phone) |
| `pushover` | `token`, `user`, `priority` |
| `matrix` | `homeserver`, `access_token`, `room_id` |
| `webhook` | `url`, `headers` (receives `{"text": ..., "event": ...}`) |
| `console` | none |

```yaml
notify:
  channels:
    oncall: { type: pagerduty, routing_key: "${PD_ROUTING_KEY}" }
    team: { type: teams, webhook: "${TEAMS_WEBHOOK}" }
    mail:
      type: email
      host: smtp.gmail.com
      user: "${SMTP_USER}"
      password: "${SMTP_PASSWORD}"
      to: [data-team@example.com]
  rules:
    - on: [run.failed, budget.exceeded]
      to: [oncall, team]
    - on: run.succeeded        # resolves the PagerDuty incident opened by a failure
      to: [oncall]
    - on: item.rejected
      when: "rate > 0.05"
      to: [mail]
```

PagerDuty and Opsgenie alerts are deduplicated per pipeline and step, so a pipeline that keeps
failing updates one incident instead of opening many. Credentials belong in environment variables.

Every messaging channel also exists as a pipeline step (`slack.send`, `whatsapp.send`, ...) to send
records instead of run events: see [Messaging](connectors.md#messaging).

### Custom channels

```python
from agentl import register_channel


def sms(config: dict, text: str, event: dict) -> None:
    my_sms_client.send(config["phone"], text[:160])


register_channel("sms", sms)  # then: channels: { phone: { type: sms, phone: "+55..." } }
```

A failing channel is logged and never fails the pipeline.

A channel's type defaults to its name. Set `type:` to give channels custom names.

## Rules

- `on`: an event type, a list of them, or `"*"`. Use `run.alert` to be told only about the
  [outcomes](concepts.md#run-outcomes) in `outcomes.alert_on` (`failed` and `partial` by default).
- `when`: a simple comparison on the event data, such as `rate > 0.05` or `count >= 10`.
- `summary: agent`: the default LLM writes a three-line explanation of the run.

Each rule sends at most one message per step per run. A failing channel never fails the pipeline.
