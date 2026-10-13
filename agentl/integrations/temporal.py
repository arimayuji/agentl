"""Run pipelines as Temporal activities.

    from agentl.integrations.temporal import run_pipeline  # an @activity.defn

    worker = Worker(client, task_queue="etl", workflows=[MyWorkflow], activities=[run_pipeline])

    # inside a workflow
    await workflow.execute_activity(
        run_pipeline, RunInput(pipeline="pipelines/sales.yaml"),
        start_to_close_timeout=timedelta(hours=1),
    )

A failed run raises, so Temporal's retry policy applies. Re-runs reuse the per-record
agent cache, so retried activities do not pay twice for records already processed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..registry import require
from ..runner import run

activity = require("temporalio.activity", "temporal")


@dataclass
class RunInput:
    pipeline: str
    vars: dict[str, str] = field(default_factory=dict)
    db: str | None = None


@activity.defn(name="agentl.run")
def run_pipeline(args: RunInput) -> dict[str, Any]:
    activity.heartbeat(args.pipeline)
    return run(args.pipeline, vars=args.vars, db=args.db, raise_on_failure=True).summary()
