"""Semantic types for messy real-world values (#119): dates, money, locale numbers, CPF/CNPJ/CEP,
phones, e-mails, booleans, enums and text. Used by the `cast` step.

Each type parses one value and returns the clean value, or raises `BadValue` with a short reason.
Empty values (None or blank text) are None, never an error. Nothing is guessed per row: an
ambiguous date order is decided once per column (see `date_order`), and an impossible value
(31/02, a CPF with wrong check digits) is rejected, never "fixed"."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any


class BadValue(ValueError):
    pass


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# ---------------------------------------------------------------- numbers
def _number_text(v: Any, locale: str) -> str:
    s = str(v).strip().replace(" ", "").replace(" ", "")
    if re.fullmatch(r"-?\(?.*\)?", s) and s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]  # accounting negatives: (1.234,56)
    dec = "," if locale.lower().startswith(("pt", "es", "de", "fr", "it")) else "."
    thou = "." if dec == "," else ","
    if s.count(dec) > 1:
        raise BadValue(f"{v!r} has more than one decimal separator")
    if dec == "," and "," not in s and s.count(".") == 1 and not re.fullmatch(r"-?\d{1,3}\.\d{3}", s):
        dec, thou = ".", ","  # "99.9" in a pt-BR sheet is a decimal point, "1.050" is a thousand
    whole, _, frac = s.partition(dec)
    if thou in frac:
        raise BadValue(f"{v!r}: thousands separator after the decimal one")
    if thou in whole and not re.fullmatch(r"-?\d{1,3}(" + re.escape(thou) + r"\d{3})+", whole):
        raise BadValue(f"{v!r}: misplaced thousands separator")
    out = whole.replace(thou, "") + ("." + frac if frac else "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", out):
        raise BadValue(f"{v!r} is not a number")
    return out


def to_decimal(v: Any, locale: str = "pt-BR") -> float | None:
    if _blank(v):
        return None
    if isinstance(v, bool):
        raise BadValue(f"{v!r} is not a number")
    if isinstance(v, (int, float)):
        return float(v)
    return float(_number_text(v, locale))


def to_int(v: Any, locale: str = "pt-BR") -> int | None:
    f = to_decimal(v, locale)
    if f is None:
        return None
    if f != int(f):
        raise BadValue(f"{v!r} is not a whole number")
    return int(f)


CURRENCY = re.compile(r"^\s*(-?)\s*(R\$|US\$|U\$|\$|€|£|BRL|USD|EUR)\s*", re.I)


def to_money(v: Any, currency: str = "BRL", locale: str = "pt-BR") -> float | None:
    if _blank(v):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v), 2)
    s = str(v).strip()
    m = CURRENCY.match(s)
    if m:
        s = m.group(1) + s[m.end() :]
    s = re.sub(r"\s*(BRL|USD|EUR)\s*$", "", s, flags=re.I)
    return round(float(_number_text(s, locale)), 2)


def to_percent(v: Any, locale: str = "pt-BR") -> float | None:
    """ "12,5%" -> 0.125; a bare number is taken as already a fraction when it is at most 1."""
    if _blank(v):
        return None
    s = str(v).strip()
    if s.endswith("%"):
        return round(float(_number_text(s[:-1], locale)) / 100, 10)
    f = to_decimal(v, locale)
    return f if f is None or abs(f) <= 1 else round(f / 100, 10)


# ---------------------------------------------------------------- booleans and enums
TRUE = {"true", "t", "yes", "y", "sim", "s", "1", "x", "verdadeiro", "v", "on", "ok"}
FALSE = {"false", "f", "no", "n", "não", "nao", "0", "falso", "off", "-"}


def to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if _blank(v):
        return None
    s = str(v).strip().lower()
    if s in TRUE:
        return True
    if s in FALSE:
        return False
    raise BadValue(f"{v!r} is not yes/no")


def _fold(s: str) -> str:
    return (
        "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower().strip()
    )


def to_enum(v: Any, values: list[str], synonyms: dict[str, list[str]] | None = None) -> str | None:
    """One of `values`, matched without case or accents; `synonyms` maps a value to other spellings."""
    if _blank(v):
        return None
    table = {_fold(x): x for x in values}
    for target, alts in (synonyms or {}).items():
        for a in alts:
            table[_fold(a)] = target
    hit = table.get(_fold(str(v)))
    if hit is None:
        raise BadValue(f"{v!r} is not one of {', '.join(values)}")
    return hit


# ---------------------------------------------------------------- text
PARTICLES = {"da", "de", "do", "das", "dos", "e", "di", "du", "del", "van", "von"}


def to_text(v: Any) -> str | None:
    """Trimmed, inner spaces collapsed, Unicode NFC."""
    if _blank(v):
        return None
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(v))).strip()


def to_name(v: Any) -> str | None:
    """A person's name in title case, with Portuguese particles in lower case: "joão DA silva" ->
    "João da Silva"."""
    t = to_text(v)
    if t is None:
        return None
    words = []
    for i, w in enumerate(t.split(" ")):
        low = w.lower()
        words.append(
            low if i and low in PARTICLES else "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-"))
        )
    return " ".join(words)


EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def to_email(v: Any) -> str | None:
    t = to_text(v)
    if t is None:
        return None
    t = t.lower()
    if not EMAIL.match(t) or ".." in t:
        raise BadValue(f"{v!r} is not an e-mail address")
    return t


# ---------------------------------------------------------------- Brazilian documents
def _digits(v: Any) -> str:
    return re.sub(r"\D", "", str(v))


def _check_digit(nums: list[int], weights: list[int]) -> int:
    r = sum(n * w for n, w in zip(nums, weights, strict=True)) % 11
    return 0 if r < 2 else 11 - r


def to_cpf(v: Any) -> str | None:
    if _blank(v):
        return None
    d = _digits(v).zfill(11) if isinstance(v, int) else _digits(v)
    if len(d) != 11:
        raise BadValue(f"cpf: {v!r} does not have 11 digits")
    if d == d[0] * 11:
        raise BadValue(f"cpf: {v!r} is not a valid number")
    n = [int(c) for c in d]
    if (
        _check_digit(n[:9], list(range(10, 1, -1))) != n[9]
        or _check_digit(n[:10], list(range(11, 1, -1))) != n[10]
    ):
        raise BadValue(f"cpf: {v!r} has invalid check digits")
    return d


def to_cnpj(v: Any) -> str | None:
    if _blank(v):
        return None
    d = _digits(v).zfill(14) if isinstance(v, int) else _digits(v)
    if len(d) != 14:
        raise BadValue(f"cnpj: {v!r} does not have 14 digits")
    if d == d[0] * 14:
        raise BadValue(f"cnpj: {v!r} is not a valid number")
    n = [int(c) for c in d]
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    if _check_digit(n[:12], w1) != n[12] or _check_digit(n[:13], [6, *w1]) != n[13]:
        raise BadValue(f"cnpj: {v!r} has invalid check digits")
    return d


def to_cep(v: Any) -> str | None:
    if _blank(v):
        return None
    d = _digits(v).zfill(8) if isinstance(v, int) else _digits(v)
    if len(d) != 8:
        raise BadValue(f"cep: {v!r} does not have 8 digits")
    return f"{d[:5]}-{d[5:]}"


UFS = set(
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()  # noqa: SIM905
)


def to_uf(v: Any) -> str | None:
    t = to_text(v)
    if t is None:
        return None
    if t.upper() not in UFS:
        raise BadValue(f"{v!r} is not a Brazilian state (UF)")
    return t.upper()


def to_phone(v: Any, country: str = "BR") -> str | None:
    """Brazilian phones to E.164: "(11) 98765-4321" -> "+5511987654321". Other countries need the
    number already in +E.164 form."""
    if _blank(v):
        return None
    raw = str(v).strip()
    d = _digits(raw)
    if raw.startswith("+"):
        if not 8 <= len(d) <= 15:
            raise BadValue(f"phone: {v!r} is not a valid international number")
        return "+" + d
    if country.upper() != "BR":
        raise BadValue(f"phone: {v!r} needs a +country code")
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if d.startswith("0") and len(d) in (11, 12):
        d = d[1:]  # trunk prefix
    if len(d) not in (10, 11) or d[0] == "0":
        raise BadValue(f"phone: {v!r} is not a Brazilian number with area code")
    if len(d) == 11 and d[2] != "9":
        raise BadValue(f"phone: {v!r}: an 11-digit mobile starts with 9 after the area code")
    return "+55" + d


# ---------------------------------------------------------------- dates
MONTHS = {
    **dict.fromkeys(["jan", "janeiro", "january"], 1),
    **dict.fromkeys(["fev", "fevereiro", "feb", "february"], 2),
    **dict.fromkeys(["mar", "março", "marco", "march"], 3),
    **dict.fromkeys(["abr", "abril", "apr", "april"], 4),
    **dict.fromkeys(["mai", "maio", "may"], 5),
    **dict.fromkeys(["jun", "junho", "june"], 6),
    **dict.fromkeys(["jul", "julho", "july"], 7),
    **dict.fromkeys(["ago", "agosto", "aug", "august"], 8),
    **dict.fromkeys(["set", "setembro", "sep", "sept", "september"], 9),
    **dict.fromkeys(["out", "outubro", "oct", "october"], 10),
    **dict.fromkeys(["nov", "novembro", "november"], 11),
    **dict.fromkeys(["dez", "dezembro", "dec", "december"], 12),
}
ISO = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ](.+))?$")
NUMERIC = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})(?:[ T](.+))?$")
WORDS = re.compile(r"^(\d{1,2})(?:\s+de)?\s+([A-Za-zçÇ]+)\.?(?:\s+de)?\s+(\d{2}|\d{4})$")


def _year(y: str) -> int:
    n = int(y)
    return n if len(y) == 4 else (2000 + n if n < 70 else 1900 + n)


def _make(y: int, m: int, d: int, v: Any) -> dt.date:
    try:
        return dt.date(y, m, d)
    except ValueError:
        raise BadValue(f"{v!r} is not a real date") from None


def date_order(values: Iterable[Any]) -> str | None:
    """ "dmy" or "mdy" when the column itself says so (a first part over 12 means day first, a
    second part over 12 means month first); None when no value tells; raises when both appear."""
    day_first = month_first = False
    for v in values:
        m = NUMERIC.match(str(v).strip()) if isinstance(v, str) else None
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            day_first |= a > 12
            month_first |= b > 12
    if day_first and month_first:
        raise BadValue("the column mixes day-first and month-first dates")
    return "dmy" if day_first else "mdy" if month_first else None


def to_date(v: Any, order: str = "dmy") -> dt.date | None:
    if _blank(v):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 20000 <= v <= 80000:  # Excel serial day (1954..2119)
            return dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
        raise BadValue(f"{v!r} is not a date")
    s = str(v).strip()
    if m := ISO.match(s):
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)), v)
    if m := NUMERIC.match(s):
        a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
        d, mo = (a, b) if order == "dmy" else (b, a)
        return _make(y, mo, d, v)
    if m := WORDS.match(s):
        month = MONTHS.get(_fold(m.group(2)).rstrip("."))
        if month is None:
            raise BadValue(f"{v!r}: unknown month {m.group(2)!r}")
        return _make(_year(m.group(3)), month, int(m.group(1)), v)
    raise BadValue(f"{v!r} is not a date")


def to_datetime(v: Any, tz: str | None = None, order: str = "dmy") -> dt.datetime | None:
    """A date and time; naive values get `tz` (an IANA zone such as America/Sao_Paulo)."""
    if _blank(v):
        return None
    zone = None
    if tz:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(tz)
    if isinstance(v, dt.datetime):
        out = v
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        secs = v / 1000 if v > 1e11 else v  # epoch milliseconds or seconds
        return dt.datetime.fromtimestamp(secs, zone or dt.timezone.utc)
    else:
        s = str(v).strip()
        try:
            out = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            m = NUMERIC.match(s) or ISO.match(s)
            if not m or not m.group(4):
                day = to_date(s, order)
                assert day is not None
                out = dt.datetime(day.year, day.month, day.day)
            else:
                day = to_date(s[: m.start(4)].strip(), order)
                assert day is not None
                t = re.fullmatch(r"(\d{1,2})[:h](\d{2})(?::(\d{2}))?", m.group(4).strip())
                if not t:
                    raise BadValue(f"{v!r}: unreadable time") from None
                out = dt.datetime(
                    day.year, day.month, day.day, int(t.group(1)), int(t.group(2)), int(t.group(3) or 0)
                )
    if out.tzinfo is None and zone is not None:
        out = out.replace(tzinfo=zone)
    return out


# ---------------------------------------------------------------- registry
Parser = Callable[..., Any]
TYPES: dict[str, Parser] = {
    "int": to_int,
    "decimal": to_decimal,
    "float": to_decimal,
    "money": to_money,
    "percent": to_percent,
    "bool": to_bool,
    "enum": to_enum,
    "text": to_text,
    "name": to_name,
    "email": to_email,
    "cpf": to_cpf,
    "cnpj": to_cnpj,
    "cep": to_cep,
    "uf": to_uf,
    "phone": to_phone,
    "date": to_date,
    "datetime": to_datetime,
}


def register_type(name: str, parse: Parser) -> None:
    """Add a company format: `parse(value, **options)` returns the clean value or raises
    `BadValue` (or ValueError) with the reason."""
    TYPES[name] = parse
