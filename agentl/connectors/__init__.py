"""Built-in connectors. Each one is `fn(input, **params) -> list[dict]`.

`input` is None for sources, a list of records for one upstream step, or a
dict of {step_id: records} when the step has several inputs."""

from .. import gather, mapping  # noqa: F401  (registers gather.add, map_schema)
from . import (  # noqa: F401  (registers connectors)
    aws,
    browser,
    chat,
    console,
    db,
    duck,
    files,
    http,
    mail,
    nfe,
    postgres,
    protect,
    queues,
    saas,
    scrape,
    seen,
    sql,
    timeconv,
    transforms,
    webcrawl,
)
