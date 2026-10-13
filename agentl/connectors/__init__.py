"""Built-in connectors. Each one is `fn(input, **params) -> list[dict]`.

`input` is None for sources, a list of records for one upstream step, or a
dict of {step_id: records} when the step has several inputs."""

from . import db, files, http, mail, postgres, sql, transforms  # noqa: F401  (registers connectors)
