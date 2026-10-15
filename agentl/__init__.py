"""agentl: agentic ETL for Python."""

from .llm import register_provider
from .notify import register_channel
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
    "load",
    "parse",
    "register_channel",
    "register_provider",
    "run",
    "tool",
]
__version__ = "0.8.1"
