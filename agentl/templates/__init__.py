"""Pipeline templates for `agentl new --template`: the tested recipes of examples/recipes, copied
here by tools/sync_templates.py so they ship in the package. No LLM call: an agent writing a
pipeline is already an LLM, and these are known to validate and run."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent


def names() -> list[str]:
    return sorted(p.stem for p in HERE.glob("*.yaml"))


def text(name: str) -> str:
    path = HERE / f"{name}.yaml"
    if not path.is_file():
        import difflib

        close = difflib.get_close_matches(name, names(), n=1)
        hint = f" Did you mean '{close[0]}'?" if close else ""
        raise ValueError(f"no template '{name}'.{hint} List them with: agentl new --list")
    return path.read_text(encoding="utf-8")


def summary(name: str) -> str:
    """What the template does: its leading comment, up to the `Needs:` line or a blank one."""
    parts = []
    for line in text(name).splitlines():
        body = line.lstrip("#").strip()
        if not line.startswith("#") or not body or body.startswith("Needs:"):
            break
        parts.append(body)
    return " ".join(parts)
