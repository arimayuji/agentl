"""agentl: agentic ETL for Python."""

from .llm import register_provider
from .notify import register_channel
from .registry import connector
from .runner import PipelineFailed, Runner, RunResult, run
from .spec import PipelineSpec, SpecError, load, parse

__all__ = [
    "PipelineFailed",
    "PipelineSpec",
    "RunResult",
    "Runner",
    "SpecError",
    "connector",
    "load",
    "parse",
    "register_channel",
    "register_provider",
    "run",
]
__version__ = "0.5.1"
