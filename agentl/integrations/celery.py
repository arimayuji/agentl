"""Run pipelines as Celery tasks.

    from celery import Celery
    from agentl.integrations.celery import register

    app = Celery("etl", broker="redis://localhost")
    run_pipeline = register(app)
    run_pipeline.delay("pipelines/sales.yaml", params={"day": "2026-10-13"})

A failed run raises, so Celery's retry options (`autoretry_for`, `max_retries`) apply."""

from __future__ import annotations

from typing import Any

from ..runner import PipelineFailed, run


def run_pipeline(
    pipeline: str,
    vars: dict[str, str] | None = None,
    db: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Task body: run and return a JSON-serializable summary; raise on failure. `params` are
    the pipeline's run parameters; bad ones raise SpecError, which is not retried."""
    return run(pipeline, vars=vars, params=params, db=db, raise_on_failure=True).summary()


def register(app: Any, name: str = "agentl.run", **task_options: Any) -> Any:
    """Register `run_pipeline` on a Celery app. Extra options go to `app.task`."""
    options = {"autoretry_for": (PipelineFailed,), "retry_backoff": True, "max_retries": 3, **task_options}
    return app.task(name=name, **options)(run_pipeline)
