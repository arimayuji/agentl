"""Semantic dedupe (#200): the same text rewritten, found by similarity instead of an exact key.

Two backends:

- `shingles` (default): MinHash over character shingles of the normalised text, then the exact Jaccard
  similarity of the candidate pairs. Pure Python, deterministic, no network and no model. It finds
  near-duplicates (copied text with edits, a changed headline, added or removed sentences), not
  paraphrases with other words.
- embeddings: `llm: <name>` asks that llm's provider for one vector per text and compares them by
  cosine similarity. A local model through Ollama keeps it off external services. The runner
  caches each vector by text hash and counts the calls in the step's spend.

Greedy and deterministic: records are visited in `keep` order and each is compared with the
records kept so far; one at or above `threshold` (and inside `window`, when set) makes it a
duplicate of the most similar one. The output keeps input order."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

KEEPS = ("first", "last", "earliest", "latest")
DEFAULT_THRESHOLD = {"shingles": 0.7, "embeddings": 0.92}
_PERMS = 64
_BANDS = 32  # 32 bands of 2 rows: candidates from a Jaccard of about 0.2, verified exactly
_MASK = (1 << 61) - 1
_WORD = re.compile(r"\w+")

Embed = Callable[[list[str]], list[list[float]]]
"""Texts to vectors, one per text, in order."""


def normalise(text: Any) -> list[str]:
    """Lowercase words without accents or punctuation."""
    s = unicodedata.normalize("NFKD", str(text or "")).casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _WORD.findall(s)


def shingles(text: Any, size: int = 5) -> set[str]:
    """Character `size`-grams of the normalised text: one changed word changes only a few."""
    s = " ".join(normalise(text))
    if len(s) <= size:
        return {s} if s else set()
    return {s[i : i + size] for i in range(len(s) - size + 1)}


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")


# fixed coefficients: the same text always gets the same signature, in every run
_COEF = [(_h(f"a{i}") | 1, _h(f"b{i}")) for i in range(_PERMS)]


def minhash(items: set[str]) -> tuple[int, ...]:
    if not items:
        return ()
    hs = [_h(x) for x in items]
    return tuple(min((a * x + b) & _MASK for x in hs) for a, b in _COEF)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb)


def parse_time(v: Any) -> float | None:
    """Epoch seconds from a number, an ISO date or datetime; None when blank or unreadable."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                return float(str(v))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _order(rows: list[dict], keep: str, time: str | None) -> list[int]:
    idx = list(range(len(rows)))
    if keep == "first":
        return idx
    if keep == "last":
        return idx[::-1]
    times = [parse_time(r.get(time)) if time else None for r in rows]
    sign = 1 if keep == "earliest" else -1
    # blank times lose: they come after every dated record, in input order
    return sorted(idx, key=lambda i: (times[i] is None, sign * (times[i] or 0.0), i))


def options(semantic: dict[str, Any]) -> dict[str, Any]:
    """Validated `semantic:` options with defaults filled in."""
    from .checks import parse_duration

    known = {"field", "threshold", "window", "time", "keep", "llm", "id", "mark", "shingle"}
    extra = set(semantic) - known
    if extra:
        raise ValueError(f"dedupe semantic: unknown option(s) {sorted(extra)}; known: {sorted(known)}")
    field = semantic.get("field")
    if not field or not isinstance(field, str):
        raise ValueError("dedupe semantic needs `field`, the text to compare")
    keep = semantic.get("keep", "first")
    if keep not in KEEPS:
        raise ValueError(f"dedupe semantic keep must be one of {', '.join(KEEPS)}, got {keep!r}")
    time = semantic.get("time")
    window = semantic.get("window")
    if (keep in ("earliest", "latest") or window is not None) and not time:
        raise ValueError(f"dedupe semantic: keep {keep} and window need `time`, the record's date field")
    backend = "embeddings" if semantic.get("llm") else "shingles"
    threshold = float(semantic.get("threshold", DEFAULT_THRESHOLD[backend]))
    if not 0 < threshold <= 1:
        raise ValueError(f"dedupe semantic threshold must be in (0, 1], got {threshold}")
    size = int(semantic.get("shingle", 5))
    if size < 1:
        raise ValueError("dedupe semantic shingle must be at least 1")
    return {
        "field": field,
        "keep": keep,
        "time": time,
        "window": None if window is None else parse_duration(str(window)),
        "threshold": threshold,
        "backend": backend,
        "llm": semantic.get("llm"),
        "id": semantic.get("id"),
        "mark": bool(semantic.get("mark", False)),
        "shingle": size,
    }


def dedupe(
    rows: list[dict], semantic: dict[str, Any], embed: Embed | None = None
) -> tuple[list[dict], list[dict], dict]:
    """Kept records (input order), dropped records and a report. A dropped record carries
    `_duplicate_of` (the kept record's `id` field, or its input position), `_similarity` and
    `_kept_because`; with `mark: true` dropped records stay in the output instead of being left
    out, and kept ones get `_duplicate_of: null`. Dropped records are never in `rejected`: a
    duplicate is not an error."""
    o = options(semantic)
    if o["backend"] == "embeddings" and embed is None:
        raise ValueError("dedupe semantic with `llm` runs inside a pipeline (it needs the llm provider)")
    field, threshold, window = o["field"], o["threshold"], o["window"]
    times = [parse_time(r.get(o["time"])) for r in rows] if o["time"] else [None] * len(rows)
    texts = [str(r.get(field) or "") for r in rows]

    sets: list[set[str]] = []
    vectors: list[list[float]] = []
    if o["backend"] == "shingles":
        sets = [shingles(t, o["shingle"]) for t in texts]
        sigs = [minhash(s) for s in sets]
        rows_per_band = _PERMS // _BANDS
    else:
        assert embed is not None
        filled = [i for i, t in enumerate(texts) if t.strip()]
        got = embed([texts[i] for i in filled]) if filled else []
        if len(got) != len(filled):
            raise ValueError(f"dedupe semantic: the llm returned {len(got)} vectors for {len(filled)} texts")
        vectors = [[] for _ in texts]
        for i, v in zip(filled, got, strict=True):
            vectors[i] = list(v)

    buckets: dict[tuple, list[int]] = {}  # shingles: LSH band -> kept records
    kept: list[int] = []
    dup_of: dict[int, tuple[int, float]] = {}
    for i in _order(rows, o["keep"], o["time"]):
        best, score = -1, 0.0
        if o["backend"] == "shingles":
            cands: set[int] = set()
            keys = (
                [(b, sigs[i][b * rows_per_band : (b + 1) * rows_per_band]) for b in range(_BANDS)]
                if sigs[i]
                else []
            )
            for k in keys:
                cands.update(buckets.get(k, ()))
            for j in sorted(cands):
                if _in_window(times[i], times[j], window):
                    s = jaccard(sets[i], sets[j])
                    if s > score:
                        best, score = j, s
        elif vectors[i]:
            for j in kept:
                if vectors[j] and _in_window(times[i], times[j], window):
                    s = cosine(vectors[i], vectors[j])
                    if s > score:
                        best, score = j, s
        if best >= 0 and score >= threshold:
            dup_of[i] = (best, score)
            continue
        kept.append(i)
        if o["backend"] == "shingles" and sigs[i]:
            for k in keys:
                buckets.setdefault(k, []).append(i)

    def ref(j: int) -> Any:
        return rows[j].get(o["id"]) if o["id"] else j

    reason = {
        "first": "first in input order",
        "last": "last in input order",
        "earliest": f"earliest {o['time']}",
        "latest": f"latest {o['time']}",
    }[o["keep"]]
    out: list[dict] = []
    groups: dict[int, list[dict]] = {}
    for i, r in enumerate(rows):
        if i in dup_of:
            j, s = dup_of[i]
            groups.setdefault(j, []).append({"dropped": ref(i), "similarity": round(s, 4)})
            if o["mark"]:
                out.append(
                    {**r, "_duplicate_of": ref(j), "_similarity": round(s, 4), "_kept_because": reason}
                )
        else:
            out.append({**r, "_duplicate_of": None} if o["mark"] else r)
    dropped = [
        {**rows[i], "_duplicate_of": ref(j), "_similarity": round(s, 4), "_kept_because": reason}
        for i, (j, s) in sorted(dup_of.items())
    ]
    report = {
        "backend": o["backend"],
        "threshold": threshold,
        "keep": o["keep"],
        "records": len(rows),
        "kept": len(rows) - len(dup_of),
        "dropped": len(dup_of),
        "groups": [
            {"kept": ref(j), "because": reason, "duplicates": d} for j, d in sorted(groups.items())[:20]
        ],
    }
    return out, dropped, report


def _in_window(a: float | None, b: float | None, window: float | None) -> bool:
    """Records without a date are compared with every record."""
    return window is None or a is None or b is None or abs(a - b) <= window


def text_key(provider: str, model: str, text: str) -> str:
    """Cache key of one embedding: provider, model and the text's hash."""
    digest = hashlib.sha256(text.encode()).hexdigest()
    return hashlib.sha256(f"embedding|{provider}|{model}|{digest}".encode()).hexdigest()
