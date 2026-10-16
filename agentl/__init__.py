"""agentl: agentic ETL for Python."""

from .llm import register_provider
from .notify import register_channel
from .quick import http_get_all, read, write
from .registry import connector
from .runner import PipelineFailed, Runner, RunResult, run
from .spec import PipelineSpec, SpecError, load, parse
from .tools import tool

__all__ = [
    "PipelineFailed",
    "PipelineSpec",
    "RunResult",
    "Runner",
    "SpecError",
    "connector",
    "http_get_all",
    "load",
    "parse",
    "read",
    "register_channel",
    "register_provider",
    "run",
    "tool",
    "write",
]
__version__ = "0.10.0"
