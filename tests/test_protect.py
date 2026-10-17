"""protect / unprotect (#225): field-level encryption, keyed hashing, masking and dropping."""

import json

import pytest

from agentl import connector, lint, parse
from agentl.cli import main
from agentl.connectors.protect import PREFIX, new_key, protect, unprotect
from agentl.runner import Runner
from agentl.store import Store

KEY, OTHER = new_key(), new_key()
ROWS = [
    {"id": 1, "tax_id": "529.982.247-25", "email": "ana@example.com", "phone": "11987654321", "notes": "x"},
    {"id": 2, "tax_id": None, "email": "", "phone": "123", "notes": "y"},
]
FIELDS = {"tax_id": "encrypt", "email": "hash", "phone": "mask", "notes": "drop"}


def test_protect_each_action():
    out = protect(ROWS, fields=FIELDS, key=KEY)
    a, b = out
    assert a["tax_id"].startswith(PREFIX) and "529" not in a["tax_id"]
    assert len(a["email"]) == 64 and a["email"] != "ana@example.com"
    assert a["phone"] == "*******4321" and "notes" not in a
    assert b["tax_id"] is None and b["email"] == "" and b["phone"] == "***"  # empty stays empty
    assert ROWS[0]["tax_id"] == "529.982.247-25"  # input untouched
    assert protect(ROWS, fields={"phone": {"mask": 2}})[0]["phone"] == "*********21"  # no key needed


def test_round_trip_keeps_types_and_nonce_is_random():
    rows = [{"v": 1234.5, "l": [1, "a"], "s": "texto ção"}]
    p1 = protect(rows, fields={"v": "encrypt", "l": "encrypt", "s": "encrypt"}, key=KEY)
    p2 = protect(rows, fields={"v": "encrypt", "l": "encrypt", "s": "encrypt"}, key=KEY)
    assert p1[0]["s"] != p2[0]["s"]
    assert unprotect(p1, fields=["v", "l", "s"], key=KEY) == rows


def test_hash_is_deterministic_per_key():
    h = lambda k: protect([{"e": "ana@example.com"}], fields={"e": "hash"}, key=k)[0]["e"]  # noqa: E731
    assert h(KEY) == h(KEY) and h(KEY) != h(OTHER)


def test_key_rotation_and_wrong_key():
    old = protect(ROWS[:1], fields={"tax_id": "encrypt"}, key=OTHER)
    new = protect(ROWS[:1], fields={"tax_id": "encrypt"}, key=[KEY, OTHER])
    assert unprotect(old + new, fields="tax_id", key=[KEY, OTHER])[1]["tax_id"] == "529.982.247-25"
    with pytest.raises(ValueError, match="was encrypted with key"):
        unprotect(old, fields="tax_id", key=KEY)


def test_tampered_or_moved_value_fails():
    p = protect([{"a": "secret", "b": "x"}], fields={"a": "encrypt"}, key=KEY)[0]
    with pytest.raises(ValueError, match="does not decrypt"):
        unprotect([{"b": p["a"]}], fields="b", key=KEY)
    tampered = p["a"][:-2] + ("A" if p["a"][-2] != "A" else "B") + p["a"][-1]
    with pytest.raises(ValueError, match="does not decrypt"):
        unprotect([{"a": tampered}], fields="a", key=KEY)
    with pytest.raises(ValueError, match="not an encrypted value"):
        unprotect([{"a": "plain"}], fields="a", key=KEY)


def test_bad_options():
    with pytest.raises(ValueError, match="32 random bytes"):
        protect(ROWS, fields={"email": "hash"}, key="short")
    with pytest.raises(ValueError, match="key` is required"):
        protect(ROWS, fields={"email": "hash"})
    with pytest.raises(ValueError, match="not one of"):
        protect(ROWS, fields={"email": "encrypt-ish"}, key=KEY)


def test_keygen(capsys):
    assert main(["keygen"]) == 0
    k = capsys.readouterr().out.strip()
    assert protect([{"e": "x"}], fields={"e": "hash"}, key=k)


@connector("test.protect_rows")
def _rows(input):
    return [dict(r) for r in ROWS]


def test_pipeline_keeps_clear_values_out_of_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTL_DATA_KEY", KEY)
    spec = parse(
        {
            "name": "protect-test",
            "steps": [
                {"id": "src", "use": "test.protect_rows"},
                {
                    "id": "p",
                    "use": "protect",
                    "input": "src",
                    "with": {"fields": FIELDS, "key": "${AGENTL_DATA_KEY}"},
                },
                {
                    "id": "u",
                    "use": "unprotect",
                    "input": "p",
                    "with": {"fields": "tax_id", "key": "${AGENTL_DATA_KEY}"},
                },
            ],
        }
    )
    with Store(tmp_path / "s.db") as store:
        res = Runner(spec, store=store).run()
        assert res.status == "succeeded" and res.outputs["u"][0]["tax_id"] == "529.982.247-25"
        assert store.sample(res.run_id, "u") is None
        stored = json.dumps([store.sample(res.run_id, "p"), store.events(res.run_id)], default=str)
    assert "529.982.247-25" not in stored and "ana@example.com" not in stored and KEY not in stored


def test_lint_warns_when_an_agent_runs_before_protect():
    text = """name: x
llm: { provider: echo }
steps:
  - id: src
    use: csv.read
    with: { path: in.csv }
  - id: label
    agent: label it
    input: src
    output: { label: str }
  - id: p
    use: protect
    input: label
    with: { key: "${K}", fields: { email: hash } }
"""
    r = lint.check_text(text, env={"K": KEY})
    hit = [f for f in r.findings if f.code == "AL008"]
    assert len(hit) == 1 and "label" in hit[0].message and "email" in hit[0].message
