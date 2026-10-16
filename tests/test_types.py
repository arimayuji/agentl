"""Semantic types and the cast step (#119). Real-world formats from spreadsheets and ERPs, and the
values that must be rejected rather than "fixed"."""

import datetime as dt

import pytest

from agentl import parse, types
from agentl.connectors.transforms import cast
from agentl.runner import Runner
from agentl.store import Store

T = types


@pytest.mark.parametrize(
    ("raw", "locale", "expected"),
    [
        ("1.234,56", "pt-BR", 1234.56),
        ("1234,56", "pt-BR", 1234.56),
        ("1.050", "pt-BR", 1050.0),
        ("99.9", "pt-BR", 99.9),  # one dot and not three digits after it: a decimal point
        ("(1.234,56)", "pt-BR", -1234.56),
        ("-12", "pt-BR", -12.0),
        ("1,234.56", "en-US", 1234.56),
        ("1 234,5", "pt-BR", 1234.5),
        (7, "pt-BR", 7.0),
        ("", "pt-BR", None),
    ],
)
def test_decimal(raw, locale, expected):
    assert T.to_decimal(raw, locale) == expected


@pytest.mark.parametrize("raw", ["1,2,3", "12.34.5,6", "abc", "1.23,4.5", True])
def test_decimal_rejects(raw):
    with pytest.raises(T.BadValue):
        T.to_decimal(raw, "pt-BR")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R$ 1.234,56", 1234.56),
        ("R$99,9", 99.9),
        ("- R$ 10,00", -10.0),
        ("2.300 BRL", 2300.0),
        (12.345, 12.35),
    ],
)
def test_money(raw, expected):
    assert T.to_money(raw) == expected


def test_int_and_percent():
    assert T.to_int("1.000") == 1000
    with pytest.raises(T.BadValue, match="whole"):
        T.to_int("1,5")
    assert (
        T.to_percent("12,5%") == 0.125 and T.to_percent("0.3", "en-US") == 0.3 and T.to_percent("30") == 0.3
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("sim", True), ("S", True), ("x", True), ("não", False), ("N", False), ("0", False), ("", None)],
)
def test_bool(raw, expected):
    assert T.to_bool(raw) is expected


def test_bool_rejects_unknown():
    with pytest.raises(T.BadValue):
        T.to_bool("talvez")


def test_enum_with_synonyms_case_and_accents():
    values = ["pago", "pendente", "estornado"]
    syn = {"pago": ["paid", "quitado"]}
    assert T.to_enum("PAGO", values, syn) == "pago" and T.to_enum("Quitado", values, syn) == "pago"
    with pytest.raises(T.BadValue, match="not one of"):
        T.to_enum("cancelado", values)


def test_text_name_email():
    assert T.to_text("  ana   lima \n") == "ana lima"
    assert (
        T.to_name(" joão DA silva ") == "João da Silva"
        and T.to_name("maria-clara souza") == "Maria-Clara Souza"
    )
    assert T.to_email(" Ana@Exemplo.COM ") == "ana@exemplo.com"
    for bad in ("ana@", "ana@exemplo", "a..b@x.com"):
        with pytest.raises(T.BadValue):
            T.to_email(bad)


def test_cpf_cnpj_cep_uf():
    assert T.to_cpf("529.982.247-25") == "52998224725"
    for bad in ("529.982.247-24", "111.111.111-11", "123"):
        with pytest.raises(T.BadValue, match="cpf"):
            T.to_cpf(bad)
    assert T.to_cnpj("11.222.333/0001-81") == "11222333000181"
    with pytest.raises(T.BadValue, match="check digits"):
        T.to_cnpj("11.222.333/0001-82")
    assert T.to_cep("01310100") == "01310-100" and T.to_cep(1310100) == "01310-100"
    assert T.to_uf("sp") == "SP"
    with pytest.raises(T.BadValue):
        T.to_uf("XX")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(11) 98765-4321", "+5511987654321"),
        ("11 3333-4444", "+551133334444"),
        ("+55 11 98765-4321", "+5511987654321"),
        ("011 98765-4321", "+5511987654321"),
    ],
)
def test_phone(raw, expected):
    assert T.to_phone(raw) == expected


def test_phone_rejects():
    for bad in ("98765-4321", "(11) 88765-4321"):  # no area code; 11 digits not starting with 9
        with pytest.raises(T.BadValue):
            T.to_phone(bad)


@pytest.mark.parametrize(
    ("raw", "order", "expected"),
    [
        ("2026-10-15", "dmy", dt.date(2026, 10, 15)),
        ("15/10/2026", "dmy", dt.date(2026, 10, 15)),
        ("15/10/26", "dmy", dt.date(2026, 10, 15)),
        ("10/15/2026", "mdy", dt.date(2026, 10, 15)),
        ("15.10.2026", "dmy", dt.date(2026, 10, 15)),
        ("2026/10/07", "dmy", dt.date(2026, 10, 7)),
        ("3 out 2026", "dmy", dt.date(2026, 10, 3)),
        ("3 de outubro de 2026", "dmy", dt.date(2026, 10, 3)),
        ("15 Oct 2026", "dmy", dt.date(2026, 10, 15)),
        (46310, "dmy", dt.date(2026, 10, 15)),  # Excel serial
    ],
)
def test_dates(raw, order, expected):
    assert T.to_date(raw, order) == expected


def test_impossible_dates_are_rejected_not_fixed():
    for bad in ("31/02/2026", "2026-13-01", "32 out 2026", "amanhã"):
        with pytest.raises(T.BadValue):
            T.to_date(bad)


def test_date_order_is_decided_per_column():
    assert T.date_order(["01/02/2026", "25/02/2026"]) == "dmy"
    assert T.date_order(["02/25/2026", "01/02/2026"]) == "mdy"
    assert T.date_order(["01/02/2026", "03/04/2026"]) is None
    with pytest.raises(T.BadValue, match="mixes"):
        T.date_order(["25/02/2026", "02/25/2026"])


def test_datetime_with_zone_and_epoch():
    d = T.to_datetime("15/10/2026 14:30", tz="America/Sao_Paulo")
    assert d.isoformat() == "2026-10-15T14:30:00-03:00"
    assert T.to_datetime("2026-10-15T14:30:00Z").isoformat() == "2026-10-15T14:30:00+00:00"
    assert T.to_datetime(1_760_000_000_000).year == 2025


ROWS = [
    {"nome": " joão silva ", "valor": "R$ 1.234,56", "data": "15/10/26", "cpf": "529.982.247-25"},
    {"nome": "MARIA souza", "valor": "1.050", "data": "03/10/2026", "cpf": "111.111.111-11"},
    {"nome": "ana  lima", "valor": "R$ 99,9", "data": "31/02/2026", "cpf": ""},
]
FIELDS = {"nome": "name", "valor": {"money": "BRL"}, "data": "date", "cpf": "cpf"}


def test_cast_cleans_and_rejects_with_reasons():
    out = cast(ROWS, fields=FIELDS)
    assert list(out) == [{"nome": "João Silva", "valor": 1234.56, "data": "2026-10-15", "cpf": "52998224725"}]
    assert [r["_error"] for r in out.rejected] == [
        "cpf: cpf: '111.111.111-11' is not a valid number",
        "data: '31/02/2026' is not a real date",
    ]
    assert out.rejected[1]["_raw"] == {"data": "31/02/2026"}


def test_cast_fail_keep_and_unknown_types():
    with pytest.raises(ValueError, match="field 'cpf'"):
        cast(ROWS, fields=FIELDS, on_bad="fail")
    kept = cast(ROWS, fields=FIELDS, on_bad="keep")
    assert len(kept) == 3 and kept[1]["cpf"] == "111.111.111-11" and not kept.rejected
    with pytest.raises(ValueError, match="did you mean 'date'"):
        cast(ROWS, fields={"data": "dat"})


def test_cast_strict_refuses_an_undecidable_date_column():
    rows = [{"d": "01/02/2026"}, {"d": "03/04/2026"}]
    assert list(cast(rows, fields={"d": "date"})) == [{"d": "2026-02-01"}, {"d": "2026-04-03"}]  # pt-BR
    assert list(cast(rows, fields={"d": "date"}, locale="en-US")) == [
        {"d": "2026-01-02"},
        {"d": "2026-03-04"},
    ]
    with pytest.raises(ValueError, match="day-first or month-first"):
        cast(rows, fields={"d": "date"}, strict=True)


def test_register_type():
    def sku(v):
        if not str(v).startswith("SKU-"):
            raise T.BadValue(f"{v!r} is not a SKU")
        return str(v).upper()

    T.register_type("sku", sku)
    try:
        out = cast([{"s": "SKU-1"}, {"s": "x"}], fields={"s": "sku"})
        assert list(out) == [{"s": "SKU-1"}] and "not a SKU" in out.rejected[0]["_error"]
    finally:
        del T.TYPES["sku"]


def test_runner_keeps_cast_rejections(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("nome,cpf\nAna,529.982.247-25\nBia,123\n")
    spec = parse(
        {
            "name": "c",
            "steps": [
                {"id": "r", "use": "csv.read", "with": {"path": str(src)}},
                {"id": "c", "use": "cast", "input": "r", "with": {"fields": {"cpf": "cpf"}}},
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        res = Runner(spec, store=store).run()
        events = [e for e in store.events(res.run_id) if e["type"] == "item.rejected"]
    assert res.status == "succeeded" and res.outputs["c"] == [{"nome": "Ana", "cpf": "52998224725"}]
    assert len(res.rejected["c"]) == 1 and events[0]["data"]["count"] == 1
