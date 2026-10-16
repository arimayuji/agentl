"""compute, aggregate, sort, limit, fill (#120), and the safe expression language behind compute."""

import pytest

from agentl import expr, parse
from agentl.connectors.transforms import aggregate, compute, fill, limit, sort
from agentl.runner import Runner
from agentl.store import Store

ROWS = [
    {"nome": "  ana  lima ", "price": "10.5", "qty": 2, "regiao": "SP", "data": "2026-10-01"},
    {"nome": "BIA", "price": 3, "qty": 4, "regiao": "RJ", "data": "2026-10-15"},
    {"nome": "caio", "price": None, "qty": 1, "regiao": "SP", "data": ""},
]


@pytest.mark.parametrize(
    ("text", "rec", "expected"),
    [
        ("price * qty", {"price": "10.5", "qty": 2}, 21.0),
        ("price * qty", {"price": None, "qty": 2}, None),  # null spreads, no error
        ("a / b", {"a": 1, "b": 0}, None),
        ("upper(trim(nome))", {"nome": "  ana  lima "}, "ANA LIMA"),
        ("if(total > 1000, 'alta', 'baixa')", {"total": 1500}, "alta"),
        ("'alta' if total > 1000 else 'baixa'", {"total": 10}, "baixa"),
        ("coalesce(apelido, nome, 'sem nome')", {"nome": "Ana", "apelido": ""}, "Ana"),
        ("regex(end, '\\d{5}-?\\d{3}')", {"end": "Rua X, 01310-100 SP"}, "01310-100"),
        ("field('nome do campo')", {"nome do campo": 7}, 7),
        ("concat(a, '-', b)", {"a": "x", "b": None}, "x-"),
        ("round(v, 2)", {"v": 2.345}, 2.35),
        ("v >= 1 and v < 5", {"v": "3"}, True),
        ("regiao in ['SP', 'RJ']", {"regiao": "SP"}, True),
        ("not ativo", {"ativo": False}, True),
        ("year(data)", {"data": "15/10/2026"}, 2026),
        ("date_diff(b, a)", {"a": "2026-10-01", "b": "2026-10-15"}, 14),
        ("split(email, '@', 1)", {"email": "ana@exemplo.com"}, "exemplo.com"),
        ("missing == null", {}, True),
        ("len(unaccent('ação'))", {}, 4),
    ],
)
def test_expressions(text, rec, expected):
    assert expr.evaluate(text, rec) == expected


@pytest.mark.parametrize(
    "text",
    [
        "().__class__",
        "nome.upper()",
        "__import__('os').system('id')",
        "open('/etc/passwd')",
        "[x for x in range(3)]",
        "(lambda: 1)()",
        "rows[0]",
        "eval('1')",
        "a := 1",
    ],
)
def test_expressions_cannot_run_code(text):
    with pytest.raises(expr.ExprError):
        expr.compile_expr(text)


def test_unknown_function_suggests():
    with pytest.raises(expr.ExprError, match="did you mean upper"):
        expr.compile_expr("uper(nome)")


def test_compute_in_order_and_rejects_with_reason():
    out = compute(ROWS, fields={"total": "price * qty", "dobro": "total * 2", "nome": "title(trim(nome))"})
    assert [(r["nome"], r["total"], r["dobro"]) for r in out] == [
        ("Ana Lima", 21.0, 42.0),
        ("Bia", 12, 24),
        ("Caio", None, None),
    ]
    bad = compute([{"v": "abc"}], fields={"n": "v * 2"})
    assert list(bad) == [] and bad.rejected[0]["_error"] == "n: 'abc' is not a number"
    assert compute([{"v": "abc"}], fields={"n": "v * 2"}, on_error="null")[0]["n"] is None
    with pytest.raises(ValueError, match="field 'n'"):
        compute([{"v": "abc"}], fields={"n": "v * 2"}, on_error="fail")
    with pytest.raises(expr.ExprError):  # a bad expression fails before any record
        compute([], fields={"n": "x.y"})


def test_aggregate():
    rows = [
        {"regiao": "SP", "cliente": "a", "valor": 10},
        {"regiao": "RJ", "cliente": "b", "valor": "5"},
        {"regiao": "SP", "cliente": "a", "valor": 30},
        {"regiao": "SP", "cliente": "c", "valor": None},
    ]
    out = aggregate(
        rows,
        group_by="regiao",
        metrics={
            "total": "sum(valor)",
            "n": "count()",
            "com_valor": "count(valor)",
            "clientes": "count_distinct(cliente)",
            "media": "avg(valor)",
            "maior": "max(valor)",
        },
    )
    assert out == [
        {"regiao": "SP", "total": 40.0, "n": 3, "com_valor": 2, "clientes": 2, "media": 20.0, "maior": 30},
        {"regiao": "RJ", "total": 5.0, "n": 1, "com_valor": 1, "clientes": 1, "media": 5.0, "maior": "5"},
    ]
    assert aggregate(rows, metrics={"n": "count()"}) == [{"n": 4}]
    with pytest.raises(ValueError, match="cannot read"):
        aggregate(rows, metrics={"x": "median(valor)"})
    with pytest.raises(ValueError, match="needs numbers"):
        aggregate([{"v": "abc"}], metrics={"s": "sum(v)"})


def test_sort_limit_fill():
    rows = [{"r": "SP", "t": 5}, {"r": "RJ", "t": None}, {"r": "SP", "t": 30}, {"r": "AM", "t": 7}]
    assert [(r["r"], r["t"]) for r in sort(rows, by=["r", "-t"])] == [
        ("AM", 7),
        ("RJ", None),
        ("SP", 30),
        ("SP", 5),
    ]
    assert [r["t"] for r in sort(rows, by={"t": "desc"})] == [30, 7, 5, None]  # empty last either way
    assert [r["t"] for r in sort(rows, by="t")] == [5, 7, 30, None]
    assert limit(rows, n=2, offset=1) == rows[1:3]
    assert fill([{"a": " ", "b": 0}, {}], values={"a": "x", "b": 9}) == [
        {"a": "x", "b": 0},
        {"a": "x", "b": 9},
    ]


def test_pipeline_compute_aggregate_sort(tmp_path):
    src = tmp_path / "vendas.csv"
    src.write_text("regiao,preco,qtd\nSP,10,2\nRJ,5,1\nSP,abc,1\nSP,3,3\n")
    spec = parse(
        {
            "name": "v",
            "steps": [
                {"id": "r", "use": "csv.read", "with": {"path": str(src)}},
                {"id": "c", "use": "compute", "input": "r", "with": {"fields": {"total": "preco * qtd"}}},
                {
                    "id": "a",
                    "use": "aggregate",
                    "input": "c",
                    "with": {"group_by": "regiao", "metrics": {"total": "sum(total)"}},
                },
                {"id": "s", "use": "sort", "input": "a", "with": {"by": "-total"}},
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        res = Runner(spec, store=store).run()
    assert res.status == "succeeded" and res.outputs["s"] == [
        {"regiao": "SP", "total": 29.0},
        {"regiao": "RJ", "total": 5.0},
    ]
    assert len(res.rejected["c"]) == 1
