"""`use: resolve`: match messy names to a reference table.

Each value is normalised (case, accents, punctuation, spaces, company suffixes such as Ltda or
Inc), then matched exactly, then by similarity. Only values whose best similarity is below
`threshold` (but at least `min_score`) go to the model, with the closest candidates. Model
decisions are kept per pipeline and step, so the same input never costs twice, and can be exported
for review and imported back (`agentl resolve export|import`)."""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from .registry import Rejects

STOPWORDS = frozenset(
    {"ltda", "ltd", "inc", "llc", "corp", "co", "sa", "s a", "gmbh", "plc", "the", "me", "eireli"}
)
METHODS = ("exact", "fuzzy", "model", "unresolved")

INSTRUCTION = (
    "Pick the candidate that names the same entity as `value`. Answer with that candidate's `id`, "
    "or null when none of them is clearly the same entity. Never invent an id."
)


class Choice(BaseModel):
    match: str | None = None


def decisions_key(pipeline: str, step: str) -> str:
    """Where a resolve step keeps its model decisions in the run store's cache."""
    return f"resolve:{pipeline}:{step}"


def normalise(value: Any, stopwords: frozenset[str] | set[str] = STOPWORDS) -> str:
    """'  São Paulo, Ltda. ' -> 'sao paulo'."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    words = re.sub(r"[^0-9a-z]+", " ", text.lower()).split()
    kept = [w for w in words if w not in stopwords]
    return " ".join(kept or words)


def _scorer() -> Callable[[str, str], float]:
    try:  # optional extra: much faster on large reference tables
        from rapidfuzz import fuzz

        return lambda a, b: fuzz.ratio(a, b) / 100.0
    except ImportError:
        return lambda a, b: difflib.SequenceMatcher(None, a, b).ratio()


def reference_rows(reference: Any, inputs: dict[str, list[dict]], base_dir: Any = None) -> list[dict]:
    """`reference` is a step id among the step's inputs, `{step: id}`, `{file: path}` or
    `{values: [rows]}`."""
    if isinstance(reference, str):
        reference = {"step": reference}
    if not isinstance(reference, dict) or len(reference) != 1:
        raise ValueError("resolve reference must be a step id, {step: id}, {file: path} or {values: [...]}")
    kind, value = next(iter(reference.items()))
    if kind == "step":
        if value not in inputs:
            raise ValueError(f"resolve reference step '{value}' must be one of the step's inputs")
        return list(inputs[value])
    if kind == "values":
        return [v if isinstance(v, dict) else {"id": v, "name": v} for v in value]
    if kind == "file":
        from pathlib import Path

        from .connectors.files import file_read

        path = Path(value)
        if base_dir is not None and not path.is_absolute():
            path = Path(base_dir) / path
        return list(file_read(path=str(path)))
    raise ValueError(f"unknown resolve reference '{kind}'")


def resolve(
    rows: list[dict],
    reference: list[dict],
    field: str,
    *,
    key: str = "id",
    name: str = "name",
    into: str | None = None,
    threshold: float = 0.9,
    min_score: float = 0.5,
    candidates: int = 5,
    on_unresolved: str = "reject",
    stopwords: list[str] | None = None,
    ask: Callable[[dict], tuple[str | None, Any]] | None = None,
    decisions: dict[str, Any] | None = None,
) -> tuple[Rejects, dict[str, int], int]:
    """Returns (records with `<into>`, `<into>_score`, `<into>_method`, rejected ones apart),
    the count per method, and how many model decisions came from `decisions`. `ask(prompt_record)`
    returns (id or None, usage); `decisions` (normalised value -> id or None) is read and updated."""
    if on_unresolved not in ("reject", "keep", "drop"):
        raise ValueError("resolve on_unresolved must be reject, keep or drop")
    into = into or f"{field}_match"
    words = frozenset(stopwords) if stopwords is not None else STOPWORDS
    score = _scorer()
    index: dict[str, str] = {}  # normalised name -> id (the first row wins)
    ids: dict[str, str] = {}
    for ref in reference:
        rid = str(ref.get(key))
        ids[rid] = str(ref.get(name))
        index.setdefault(normalise(ref.get(name), words), rid)
        index.setdefault(normalise(rid, words), rid)  # `SP` also matches the id `SP`
    names = list(index)
    decisions = {} if decisions is None else decisions
    counts = dict.fromkeys(METHODS, 0)
    reused = 0
    seen: dict[str, tuple[str | None, float, str]] = {}  # one decision per distinct value per run
    good, bad = [], []
    for row in rows:
        norm = normalise(row.get(field), words)
        if norm in seen:
            found = seen[norm]
        elif norm in index:
            found = (index[norm], 1.0, "exact")
        else:
            ranked = sorted(((score(norm, n), n) for n in names), reverse=True) if norm else []
            best = ranked[0] if ranked else (0.0, "")
            if best[0] >= threshold:
                found = (index[best[1]], round(best[0], 4), "fuzzy")
            elif norm in decisions and (decisions[norm] is None or decisions[norm] in ids):
                reused += 1
                found = (decisions[norm], round(best[0], 4), "model")
            elif ask is not None and best[0] >= min_score:
                options, picked = [], set()
                for s, n in ranked:
                    if index[n] not in picked:
                        picked.add(index[n])
                        options.append({"id": index[n], "name": ids[index[n]], "score": round(s, 4)})
                    if len(options) >= candidates:
                        break
                choice, _ = ask({"value": row.get(field), "candidates": options})
                choice = choice if choice in picked else None  # never an id outside the candidates
                decisions[norm] = choice
                found = (choice, round(best[0], 4), "model")
            else:
                found = (None, round(best[0], 4), "unresolved")
            if found[2] == "model" and found[0] is None:
                found = (None, found[1], "unresolved")
        seen[norm] = found
        counts[found[2]] += 1
        out = {**row, into: found[0], f"{into}_score": found[1], f"{into}_method": found[2]}
        if found[2] != "unresolved" or on_unresolved == "keep":
            good.append(out)
        elif on_unresolved == "reject":
            bad.append({**out, "_error": f"resolve: no match for {row.get(field)!r}"})
    return Rejects(good, bad), counts, reused
