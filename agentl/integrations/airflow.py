"""Airflow operator.

    from agentl.integrations.airflow import AgentlOperator

    AgentlOperator(task_id="sales", pipeline="pipelines/sales.yaml", run_params={"day": "{{ ds }}"})

`pipeline`, `vars` and `run_params` (the pipeline's run parameters) are templated, so
`{{ ds }}` and friends work. It is not called `params` because that name is Airflow's own
DAG-level parameters. The summary is pushed to XCom."""

from __future__ import annotations

from typing import Any

from ..registry import require
from ..runner import run

BaseOperator = require("airflow.models", "airflow").BaseOperator


class AgentlOperator(BaseOperator):  # type: ignore[misc,valid-type]
    template_fields = ("pipeline", "vars", "run_params")

    def __init__(
        self,
        *,
        pipeline: str,
        vars: dict[str, str] | None = None,
        run_params: dict[str, Any] | None = None,
        db: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.pipeline = pipeline
        self.vars = vars or {}
        self.run_params = dict(run_params or {})
        self.db = db

    def execute(self, context: Any) -> dict[str, Any]:
        return run(
            self.pipeline, vars=self.vars, params=self.run_params or None, db=self.db, raise_on_failure=True
        ).summary()
