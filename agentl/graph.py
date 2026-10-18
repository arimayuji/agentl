"""Step graph: execution order and Mermaid diagrams."""

from __future__ import annotations

from .spec import PipelineSpec, SpecError


def order(spec: PipelineSpec) -> list[str]:
    """Topological order, stable with respect to declaration order."""
    deps = {s.id: set(s.depends) for s in spec.steps}
    done: list[str] = []
    while len(done) < len(deps):
        ready = [i for i in deps if i not in done and deps[i] <= set(done)]
        if not ready:
            cycle = sorted(i for i in deps if i not in done)
            raise SpecError(f"cycle between steps: {cycle}")
        done.append(ready[0])
    return done


_SHAPES = {
    "use": ("[", "]"),
    "fn": ("(", ")"),
    "agent": ("{{", "}}"),
    "check": ("[[", "]]"),
    "wait": ("[/", "/]"),
    "route": ("{", "}"),
}
_STATUS_CLASS = {
    "running": "fill:#dbeafe,stroke:#2563eb",
    "succeeded": "fill:#dcfce7,stroke:#16a34a",
    "failed": "fill:#fee2e2,stroke:#dc2626",
}


def mermaid(spec: PipelineSpec, status: dict[str, str] | None = None) -> str:
    lines = ["flowchart LR"]
    for s in spec.steps:
        router = s.routes is not None
        left, right = _SHAPES["route" if router else s.kind]
        detail = s.use or s.fn or ("check" if s.check else "wait" if s.wait_until else "agent")
        detail = "route" if router else detail
        label = f"{s.id}<br/><small>{detail}</small>".replace('"', "'")
        lines.append(f'  {s.id}{left}"{label}"{right}')
    routed = {r.id: r.targets for r in spec.steps if r.routes is not None}
    for s in spec.steps:
        for ref in s.inputs:
            labels = routed.get(ref, {}).get(s.id)
            # a router's target edge carries the labels it receives (#299)
            lines.append(f"  {ref} -->|{' / '.join(labels)}| {s.id}" if labels else f"  {ref} --> {s.id}")
        for ref in s.after:
            if ref not in s.inputs:
                lines.append(f"  {ref} -.-> {s.id}")
    if status:
        for name, style in _STATUS_CLASS.items():
            lines.append(f"  classDef {name} {style}")
        for step_id, st in status.items():
            if st in _STATUS_CLASS:
                lines.append(f"  class {step_id} {st}")
    return "\n".join(lines)
