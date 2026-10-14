"""Guard the core footprint (#68): no new runtime dependencies, no optional library at import time."""

import subprocess
import sys
from importlib.metadata import requires

CORE = {"pyyaml", "pydantic"}
OPTIONAL = [
    "pandas",
    "numpy",
    "requests",
    "sqlalchemy",
    "boto3",
    "fsspec",
    "anthropic",
    "psycopg",
    "playwright",
    "smtplib",
    "urllib.request",
]


def test_core_dependencies_are_unchanged():
    deps = {
        r.split(";")[0].split(">")[0].split("=")[0].split("[")[0].strip().lower()
        for r in requires("agentl-etl") or []
        if "extra ==" not in r
    }
    assert deps == CORE, f"core dependencies changed: {sorted(deps)}; add new libraries as extras"


def test_import_does_not_load_optional_libraries():
    probe = f"import agentl,sys;print([m for m in {OPTIONAL!r} if m in sys.modules])"
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "[]", f"`import agentl` loaded {out}; import them lazily where they are used"
