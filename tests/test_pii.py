import pytest

from agentl import pii
from agentl.checks import run_expectations

CPF, CNPJ = "529.982.247-25", "11.222.333/0001-81"
CARD, IBAN = "4111 1111 1111 1111", "GB82 WEST 1234 5698 7654 32"

TRUE = [
    ("cpf", CPF),
    ("cpf", "52998224725"),
    ("cnpj", CNPJ),
    ("ssn", "123-45-6789"),
    ("email", "ana.silva@example.com"),
    ("phone", "(11) 98765-4321"),
    ("phone", "+1 415 555 0123"),
    ("card", CARD),
    ("iban", IBAN),
    ("ip", "203.0.113.7"),
    ("ip", "2001:db8::1"),
]
FALSE = [
    ("cpf", "529.982.247-26"),  # wrong check digit
    ("cpf", "111.111.111-11"),
    ("cnpj", "11.222.333/0001-80"),
    ("ssn", "666-45-6789"),
    ("ssn", "123456789"),
    ("email", "not an e-mail@"),
    ("phone", "12345"),
    ("phone", "1987654321"),  # bare digits: an id, not a phone
    ("card", "4111 1111 1111 1112"),  # Luhn fails
    ("card", "0000 0000 0000 0000"),
    ("iban", "GB82 WEST 1234 5698 7654 33"),
    ("ip", "999.1.1.1"),
    ("ip", "127.0.0.1"),
    ("ip", "1.2"),
]


@pytest.mark.parametrize("kind,value", TRUE)
def test_detects(kind, value):
    assert pii.classify(value) == kind


@pytest.mark.parametrize("kind,value", FALSE)
def test_false_positives(kind, value):
    assert pii.classify(value) != kind


def test_names_only_in_name_fields():
    assert pii.classify("Ana Maria Silva", "full_name") == "name"
    assert pii.classify("Ana Maria Silva", "product") is None
    assert pii.classify("12 Main St", "name") is None
    assert pii.classify("Ana", "name") is None


def test_other_types_are_ignored():
    assert pii.classify(None) is None
    assert pii.classify(True) is None
    assert pii.classify(3.5) is None
    assert pii.classify({"a": 1}) is None
    assert pii.classify(52998224725) == "cpf"


def test_scan_masks_and_suggests():
    rows = [
        {"id": i, "tax_id": CPF, "email": f"u{i}@example.com", "card": CARD, "note": "ok"} for i in range(5)
    ]
    found = {f["field"]: f for f in pii.scan(rows)}
    assert set(found) == {"tax_id", "email", "card"}
    assert found["tax_id"]["kind"] == "cpf"
    assert found["tax_id"]["matched"] == found["tax_id"]["checked"] == 5
    assert found["card"]["example"] == "*" * 15 + "1111"
    assert pii.suggest_protect(list(found.values())) == {"tax_id": "hash", "email": "hash", "card": "drop"}
    text = " ".join(pii.describe(f) for f in found.values())
    for value in (CPF, CARD, "u0@example.com"):
        assert value not in text


def test_scan_threshold_and_sample():
    rows = [{"x": CPF}] + [{"x": "free text"}] * 3
    assert pii.scan(rows) == []
    assert pii.scan(rows, min_share=0.25)[0]["kind"] == "cpf"
    assert pii.scan([{"x": "a"}] * 5 + [{"x": CPF}] * 5, sample=5) == []


def test_protected_values_do_not_match():
    from agentl.connectors.protect import protect

    rows = [{"tax_id": CPF, "phone": "(11) 98765-4321", "email": "a@example.com"}]
    out = protect(rows, fields={"tax_id": "mask", "phone": "mask", "email": "drop"})
    assert pii.scan(out) == []


def test_pii_expectation():
    rows = [{"tax_id": CPF, "email": "a@example.com"}]
    fails = run_expectations(rows, {"pii": True}, None)
    assert {f["check"] for f in fails} == {"pii"}
    assert len(fails) == 2
    assert all(CPF not in f["detail"] and "a@example.com" not in f["detail"] for f in fails)
    assert len(run_expectations(rows, {"pii": {"allow": ["email"]}}, None)) == 1
    assert run_expectations(rows, {"pii": False}, None) == []
    assert run_expectations([{"n": 1}], {"pii": True}, None) == []
