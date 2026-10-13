"""Airflow operator.

    from agentl.integrations.airflow import AgentlOperator

    AgentlOperator(task_id="sales", pipeline="pipelines/sales.yaml", vars={"DAY": "{{ ds }}"})

`pipeline` and `vars` are templated, so `{{ ds }}` and friends work. The summary is pushed to XCom."""

from __future__ import annotations

from typing import Any

from ..registry import require
from ..runner import run

BaseOperator = require("airflow.models", "airflow").BaseOperator


class AgentlOperator(BaseOperator):  # type: ignore[misc,valid-type]
    template_fields = ("pipeline", "vars")

    def __init__(
        self, *, pipeline: str, vars: dict[str, str] | None = None, db: str | None = None, **kwargs: Any
    ):
        super().__init__(**kwargs)
        self.pipeline = pipeline
        self.vars = vars or {}
        self.db = db

    def execute(self, context: Any) -> dict[str, Any]:
        return run(self.pipeline, vars=self.vars, db=self.db, raise_on_failure=True).summary()
