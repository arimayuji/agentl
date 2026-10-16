"""join on several keys and outer joins, dedupe keep, flatten / explode, pivot / unpivot (#120)."""

import pytest

from agentl.connectors.transforms import dedupe, explode, flatten, join, pivot, unpivot

L = [{"id": 1, "loja": "a", "v": 10}, {"id": 2, "loja": "a", "v": 20}, {"id": 1, "loja": "b", "v": 5}]
R = [{"id": 1, "loja": "a", "v": 99, "nome": "x"}, {"id": 3, "loja": "a", "v": 7, "nome": "z"}]


def j(**kw):
    return join({"l": L, "r": R}, left="l", right="r", **kw)


def test_join_multi_key_and_how():
    assert j(on=["id", "loja"]) == [{"id": 1, "loja": "a", "v": 10, "v_right": 99, "nome": "x"}]
    assert len(j(on=["id", "loja"], how="left")) == 3
    right = j(on=["id", "loja"], how="right")
    assert right[-1] == {"id": 3, "loja": "a", "v_right": 7, "nome": "z"}
    assert len(j(on=["id", "loja"], how="outer")) == 4


def test_join_left_on_right_on_and_suffixes():
    r = [{"cod": 1, "v": 1}]
    out = join({"l": L, "r": r}, left="l", right="r", left_on="id", right_on="cod", suffixes=["_l", "_r"])
    assert out[0] == {
        "id": 1,
        "loja": "a",
        "v_l": 10,
        "cod": 1,
        "v_r": 1,
    }  # a differently named right key stays


def test_join_single_key_is_unchanged():
    assert j(on="id")[0] == {"id": 1, "loja": "a", "v": 10, "loja_right": "a", "v_right": 99, "nome": "x"}


def test_join_errors_name_the_problem():
    with pytest.raises(ValueError, match="join key cliente is not in the left input"):
        j(on="cliente")
    with pytest.raises(ValueError, match="how must be"):
        j(on="id", how="cross")
    with pytest.raises(ValueError, match="same number"):
        j(left_on=["id", "loja"], right_on="id")


def test_dedupe_keep():
    rows = [{"k": 1, "t": "2026-01-02"}, {"k": 1, "t": "2026-03-01"}, {"k": 1, "t": ""}, {"k": 2, "t": 5}]
    assert [r["t"] for r in dedupe(rows, on="k")] == ["2026-01-02", 5]
    assert [r["t"] for r in dedupe(rows, on="k", keep="last")] == ["", 5]
    assert [r["t"] for r in dedupe(rows, on="k", keep={"latest": "t"})] == ["2026-03-01", 5]
    with pytest.raises(ValueError, match="keep"):
        dedupe(rows, on="k", keep="newest")


def test_flatten_and_explode():
    rows = [{"id": 1, "end": {"cidade": "SP", "geo": {"lat": 1}}, "tags": ["a", "b"]}, {"id": 2, "tags": []}]
    assert flatten(rows)[0] == {"id": 1, "end.cidade": "SP", "end.geo.lat": 1, "tags": ["a", "b"]}
    assert flatten(rows, sep="_")[0]["end_geo_lat"] == 1
    assert [(r["id"], r["tags"]) for r in explode(rows, "tags")] == [(1, "a"), (1, "b"), (2, None)]
    assert len(explode(rows, "tags", keep_empty=False)) == 2


def test_pivot_and_unpivot():
    long = [
        {"loja": "a", "mes": "jan", "v": 1},
        {"loja": "a", "mes": "fev", "v": 2},
        {"loja": "b", "mes": "jan", "v": 3},
        {"loja": "b", "mes": "jan", "v": 4},
    ]
    assert pivot(long, index="loja", columns="mes", values="v") == [
        {"loja": "a", "jan": 1, "fev": 2},
        {"loja": "b", "jan": 3, "fev": None},
    ]
    assert pivot(long, index="loja", columns="mes", values="v", agg="sum")[1]["jan"] == 7
    with pytest.raises(ValueError, match="agg"):
        pivot(long, index="loja", columns="mes", values="v", agg="median")
    wide = [{"loja": "a", "jan": 1, "fev": 2}]
    assert unpivot(wide, columns=["jan", "fev"], name="mes", value="v") == [
        {"loja": "a", "mes": "jan", "v": 1},
        {"loja": "a", "mes": "fev", "v": 2},
    ]
