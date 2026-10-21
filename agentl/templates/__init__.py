"""Pipeline templates for `agentl new --template`: the tested recipes of examples/recipes and the
ETL patterns of examples/patterns (in patterns/), copied here by tools/sync_templates.py so they
ship in the package. No LLM call: an agent writing a
pipeline is already an LLM, and these are known to validate and run."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent
GROUPS = {"recipes": HERE, "patterns": HERE / "patterns"}


def names(group: str | None = None) -> list[str]:
    """Every template name, or only those of one group (`recipes` or `patterns`)."""
    dirs = [GROUPS[group]] if group else list(GROUPS.values())
    return sorted(p.stem for d in dirs for p in d.glob("*.yaml"))


def group(name: str) -> str:
    """The group a template belongs to: `recipes` or `patterns`."""
    return next((g for g, d in GROUPS.items() if (d / f"{name}.yaml").is_file()), "recipes")


def text(name: str) -> str:
    path = GROUPS[group(name)] / f"{name}.yaml"
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
