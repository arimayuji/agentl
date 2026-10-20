"""Detect personal data in records and suggest `protect` (#239).

`scan` samples the values of each field and runs cheap detectors on them: tax ids with check
digits (CPF, CNPJ), US SSN (format), e-mail, phone (BR and US, written as a phone), payment card
numbers (Luhn), IBAN (mod 97), IP addresses and person names in name-like fields. A field is
suspected when at least `min_share` of its non-empty sampled values match the same detector.

Findings never carry a value in full: examples are masked with `protect`'s `mask_value`."""

from __future__ import annotations

import ipaddress
import re
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from . import types as t
from .connectors.protect import mask_value

Detector = Callable[[str], bool]

# Suggested protect action per kind: identifiers that are joined on are hashed, the rest masked
# or encrypted. Only a suggestion; the pipeline author decides.
ACTIONS = {
    "cpf": "hash",
    "cnpj": "hash",
    "ssn": "hash",
    "email": "hash",
    "phone": "mask",
    "card": "drop",
    "iban": "encrypt",
    "ip": "hash",
    "name": "encrypt",
}

_NAME_FIELD = re.compile(r"(^|_)(name|full_?name|first_?name|last_?name|surname|nome)($|_)", re.I)
_NAME_VALUE = re.compile(r"^[^\W\d_]+(?:['.-][^\W\d_]+)*(?:\s+[^\W\d_]+(?:['.-][^\W\d_]+)*){1,5}$")
_PHONE_CHARS = re.compile(r"^\+?[\d\s().-]{8,20}$")
_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def _ok(parse: Callable[[Any], Any], v: str) -> bool:
    try:
        return parse(v) is not None
    except (t.BadValue, ValueError):
        return False


def _digits(v: str) -> str:
    return re.sub(r"\D", "", v)


def is_cpf(v: str) -> bool:
    return len(_digits(v)) == 11 and re.fullmatch(r"[\d.\s-]+", v) is not None and _ok(t.to_cpf, v)


def is_cnpj(v: str) -> bool:
    return len(_digits(v)) == 14 and re.fullmatch(r"[\d./\s-]+", v) is not None and _ok(t.to_cnpj, v)


def is_ssn(v: str) -> bool:
    # Only the written form 123-45-6789: nine bare digits are too common to call an SSN.
    return re.fullmatch(r"\d{3}-\d{2}-\d{4}", v) is not None and _ok(t.to_ssn, v)


def is_email(v: str) -> bool:
    return "@" in v and _ok(t.to_email, v)


def is_phone(v: str) -> bool:
    if not _PHONE_CHARS.match(v) or not re.search(r"[\s().+-]", v):
        return False  # a bare run of digits is more likely an id than a phone
    return _ok(t.to_phone, v) or _ok(lambda x: t.to_phone(x, "US"), v)


def luhn(d: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(d)):
        n = int(c)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def is_card(v: str) -> bool:
    if not re.fullmatch(r"[\d\s-]+", v):
        return False
    d = _digits(v)
    return 13 <= len(d) <= 19 and d[0] in "3456" and len(set(d)) > 1 and luhn(d)


def is_iban(v: str) -> bool:
    s = v.replace(" ", "").upper()
    if not _IBAN.match(s):
        return False
    moved = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def is_ip(v: str) -> bool:
    if not re.fullmatch(r"[0-9A-Fa-f:.]+", v) or ("." not in v and ":" not in v):
        return False
    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_unspecified)


def is_name(v: str) -> bool:
    return _NAME_VALUE.match(v) is not None


# Order matters: the first detector that matches a value wins.
DETECTORS: dict[str, Detector] = {
    "email": is_email,
    "cnpj": is_cnpj,
    "cpf": is_cpf,
    "ssn": is_ssn,
    "card": is_card,
    "iban": is_iban,
    "ip": is_ip,
    "phone": is_phone,
}


def classify(value: Any, field: str = "") -> str | None:
    """The kind of personal data `value` looks like, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value) if isinstance(value, int) else ""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > 254:
        return None
    for kind, detect in DETECTORS.items():
        if detect(v):
            return kind
    if _NAME_FIELD.search(field) and is_name(v):
        return "name"
    return None


def scan(
    records: Iterable[dict[str, Any]], sample: int = 200, min_share: float = 0.5
) -> list[dict[str, Any]]:
    """Suspected personal-data fields in the first `sample` records, as
    [{field, kind, matched, checked, example, suggest}]. `example` is masked."""
    seen: dict[str, Counter[str]] = {}
    checked: Counter[str] = Counter()
    example: dict[tuple[str, str], str] = {}
    for i, r in enumerate(records):
        if i >= sample:
            break
        for field, value in r.items():
            if value is None or value == "":
                continue
            checked[field] += 1
            kind = classify(value, str(field))
            if kind:
                seen.setdefault(field, Counter())[kind] += 1
                example.setdefault((field, kind), mask_value(str(value).strip()))
    out = []
    for field, kinds in seen.items():
        kind, n = kinds.most_common(1)[0]
        if n / checked[field] >= min_share:
            out.append(
                {
                    "field": field,
                    "kind": kind,
                    "matched": n,
                    "checked": checked[field],
                    "example": example[(field, kind)],
                    "suggest": ACTIONS[kind],
                }
            )
    return out


def suggest_protect(findings: list[dict[str, Any]]) -> dict[str, str]:
    """`fields` for a `protect` step that covers `findings`."""
    return {f["field"]: f["suggest"] for f in findings}


def describe(finding: dict[str, Any]) -> str:
    """One line for a finding; carries only the masked example."""
    f = finding
    return (
        f"{f['field']}: looks like {f['kind']} ({f['matched']} of {f['checked']} sampled values, "
        f"e.g. {f['example']}); protect it with {f['suggest']}"
    )


# ---------------------------------------------------------------- before an llm call (#337)
# Personal data inside free text: candidates found by these patterns are confirmed with the
# detector of their kind (check digits, Luhn) before they count. Order matters: each pass
# replaces what it found, so a later, looser pattern (phone) never sees an e-mail or a CPF.
_EMBEDDED: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"(?<![\w.%+-])[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("cnpj", re.compile(r"(?<![\w/.-])\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?![\w/-])")),
    ("cpf", re.compile(r"(?<![\w.-])\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?![\w-])")),
    ("ssn", re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")),
    ("card", re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")),
    ("phone", re.compile(r"(?<![\w+])\+?(?:\(\d{2,3}\)\s?|\d)[\d\s().-]{6,18}\d(?!\w)")),
]
_PLACEHOLDER = re.compile(r"\[([A-Z]+)_(\d+)\]")


class Masker:
    """Replaces the personal data of one record with placeholders like `[EMAIL_1]`: the same
    value gets the same placeholder everywhere in the record, and the numbering depends only
    on the record, so a masked record (and the cache key made from it) is stable across runs.
    `fields` and `kinds` narrow what is looked at (default: every field and kind)."""

    def __init__(self, fields: Iterable[str] | None = None, kinds: Iterable[str] | None = None):
        self.fields = set(fields) if fields else None
        self.kinds = set(kinds) if kinds else None
        self.originals: dict[str, Any] = {}  # placeholder -> original value
        self.found: Counter[tuple[str, str]] = Counter()  # (field, kind) -> occurrences
        self._by_value: dict[tuple[str, str, str], str] = {}
        self._count: Counter[str] = Counter()

    def _token(self, kind: str, value: Any, field: str) -> str:
        self.found[(field, kind)] += 1
        key = (kind, type(value).__name__, str(value))
        if key not in self._by_value:
            self._count[kind] += 1
            token = f"[{kind.upper()}_{self._count[kind]}]"
            self._by_value[key] = token
            self.originals[token] = value
        return self._by_value[key]

    def _text(self, v: str, field: str) -> str:
        if (kind := classify(v, field)) and (self.kinds is None or kind in self.kinds):
            return self._token(kind, v, field)
        for kind, pattern in _EMBEDDED:
            if self.kinds is not None and kind not in self.kinds:
                continue

            def sub(m: re.Match[str], kind: str = kind) -> str:
                s = m.group(0)
                if not DETECTORS[kind](s.strip()):
                    return s
                return self._token(kind, s, field)

            v = pattern.sub(sub, v)
        return v

    def _value(self, v: Any, field: str) -> Any:
        if isinstance(v, str):
            return self._text(v, field)
        if isinstance(v, int) and not isinstance(v, bool):
            kind = classify(v, field)  # e.g. a CPF or card number stored as a number
            if kind and (self.kinds is None or kind in self.kinds):
                return self._token(kind, v, field)
            return v
        if isinstance(v, dict):
            return {k: self._value(x, field) for k, x in v.items()}
        if isinstance(v, list):
            return [self._value(x, field) for x in v]
        return v

    def mask(self, record: dict[str, Any]) -> dict[str, Any]:
        """The record with its personal data replaced; `originals` maps placeholders back."""
        return {
            k: self._value(v, str(k)) if self.fields is None or k in self.fields else v
            for k, v in record.items()
        }

    def restore(self, value: Any) -> Any:
        """`value` with the placeholders of this record put back, in strings at any depth.
        Placeholders the model invented (not from this record) stay as they are."""
        if not self.originals:
            return value
        if isinstance(value, str):
            if value in self.originals:  # a whole value comes back with its type (an int stays one)
                return self.originals[value]
            return _PLACEHOLDER.sub(lambda m: str(self.originals.get(m.group(0), m.group(0))), value)
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        return value
