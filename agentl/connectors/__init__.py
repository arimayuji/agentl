"""Built-in connectors. Each one is `fn(input, **params) -> list[dict]`.

`input` is None for sources, a list of records for one upstream step, or a
dict of {step_id: records} when the step has several inputs."""

from . import (  # noqa: F401  (registers connectors)
    aws,
    browser,
    chat,
    db,
    duck,
    files,
    http,
    mail,
    postgres,
    protect,
    queues,
    saas,
    scrape,
    sql,
    transforms,
)
