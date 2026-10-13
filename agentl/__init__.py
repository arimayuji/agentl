"""agentl: agentic ETL for Python."""

from .llm import register_provider
from .registry import connector
from .runner import Runner, RunResult, run
from .spec import PipelineSpec, SpecError, load, parse

__all__ = [
    "PipelineSpec",
    "RunResult",
    "Runner",
    "SpecError",
    "connector",
    "load",
    "parse",
    "register_provider",
    "run",
]
__version__ = "0.1.1"
