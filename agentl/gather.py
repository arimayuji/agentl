"""`gather`: fan in parts that arrive in independent runs, and fire once per group.

    # capture pipeline, one run per part
    - id: mark
      use: gather.add
      with: { group: "batch-${params.date}", item: "${params.part}" }

    # consuming pipeline, under `agentl serve`
    trigger:
      - gather: { group: "batch-{date}", expect: { count: 24 }, ttl: 12h,
                  deadline: { after: 3h, fire: partial } }

Parts live in the run store (the `seen` table, scope `gather:<group>`), added only when the
capture run succeeds; the same item twice counts once. A group fires once per consuming pipeline:
the firing is claimed with a single insert, so concurrent services sharing the store cannot both
fire it. Standard library only."""

from __future__ import annotations

import re
import time
from typing import Any

from .registry import after_run, connector, current_run_id, run_store

PARTS = "gather:"  # parts of a group: seen scope `gather:<group>`, one key per item
FIRED = "gather-fired"  # one key per (group, pipeline) that fired; the run_id column holds how
SEP = "\x1f"


@connector("gather.add")
def gather_add(input: Any, group: str, item: str) -> list[dict]:
    """Add `item` to `group` once the current run succeeds. Passes its input through."""
    if not group or item is None or str(item) == "":
        raise ValueError("gather.add needs `group` and `item`")
    store = run_store()
    run_id = current_run_id() or "direct"

    def add() -> None:
        if store is not None:
            add_part(store, str(group), str(item), run_id)
        else:  # called directly: use the default store
            from .store import open_store

            with open_store() as s:
                add_part(s, str(group), str(item), run_id)

    after_run(add)
    if isinstance(input, list):
        return input
    return [{"group": group, "item": str(item)}]


def add_part(store: Any, group: str, item: str, run_id: str) -> None:
    store.seen_add(PARTS + group, [item], run_id)


def pattern(text: str) -> re.Pattern[str]:
    """`batch-{date}` -> a regex that matches whole group names and captures `date`."""
    out, pos = [], 0
    for m in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", text):
        out.append(re.escape(text[pos : m.start()]))
        out.append(f"(?P<{m.group(1)}>.+?)")
        pos = m.end()
    out.append(re.escape(text[pos:]))
    return re.compile("".join(out) + r"\Z")


def groups(store: Any) -> list[dict]:
    """Every group in the store: `{group, items, first, last, fired: {pipeline: how}}`."""
    fired: dict[str, dict[str, str]] = {}
    for row in store.seen_list(FIRED):
        g, _, pipeline = row["key"].partition(SEP)
        fired.setdefault(g, {})[pipeline] = row["run_id"]
    out = []
    for scope in store.seen_scopes(PARTS):
        rows = store.seen_list(scope["scope"])
        name = scope["scope"][len(PARTS) :]
        out.append(
            {
                "group": name,
                "items": [r["key"] for r in rows],
                "first": scope["first"],
                "last": scope["last"],
                "fired": fired.pop(name, {}),
            }
        )
    return out


def forget(store: Any, group: str) -> int:
    """Drop a group's parts and firing marks, so it can gather and fire again."""
    n = store.seen_forget(PARTS + group)
    for row in store.seen_list(FIRED):
        if row["key"].partition(SEP)[0] == group:
            store.seen_forget(FIRED, row["key"])
    return int(n)


def due(store: Any, pipeline: str, t: Any, now: float | None = None) -> list[tuple[str, dict]]:
    """What the `gather` trigger `t` of `pipeline` must do now: `(how, record)` with `how` one of
    `complete` / `partial` (start a run) or `failed` (notify only). Each group is claimed here,
    so it comes back once. Groups past `ttl` are dropped."""
    from .checks import parse_duration

    now = time.time() if now is None else now
    rx = pattern(t.group)
    ttl = parse_duration(t.ttl)
    deadline = t.deadline
    wait = parse_duration(deadline.after) if deadline else ttl
    late_how = deadline.fire if deadline else "fail"
    out: list[tuple[str, dict]] = []
    for g in groups(store):
        m = rx.match(g["group"])
        if not m:
            continue
        done_by = g["fired"].get(pipeline)
        expired = now >= g["first"] + ttl
        if done_by is None:
            items = sorted(g["items"])
            missing = sorted(set(t.expect.items) - set(items))
            short = max((t.expect.count or 0) - len(items), 0)
            complete = not missing and not short
            how = "complete" if complete else None
            if how is None and (expired or now >= g["first"] + wait):
                how = "partial" if late_how == "partial" else "failed"
            if how and store.seen_claim(FIRED, g["group"] + SEP + pipeline, how):
                record = {
                    "group": g["group"],
                    **m.groupdict(),
                    "items": items,
                    "count": len(items),
                    "missing": missing,
                    "missing_count": max(len(missing), short),
                    "partial": how != "complete",
                }
                out.append((how, record))
        if expired:
            store.seen_forget(PARTS + g["group"])
            store.seen_forget(FIRED, g["group"] + SEP + pipeline)
    return out
