from agentl.registry import get


def test_join_keeps_right_columns_with_the_same_name():
    orders = [{"id": 1, "customer": 7, "name": "order 1"}, {"id": 2, "customer": 9, "name": "order 2"}]
    customers = [{"customer": 7, "name": "Ana"}]
    out = get("join")({"o": orders, "c": customers}, left="o", right="c", on="customer")
    assert out == [{"id": 1, "customer": 7, "name": "order 1", "name_right": "Ana"}]
    left = get("join")(
        {"o": orders, "c": customers}, left="o", right="c", on="customer", how="left", suffix="_c"
    )
    assert left[0]["name_c"] == "Ana" and left[1] == orders[1]


def test_select_fields_take_output_or_input_names():
    from agentl.registry import get

    sel = get("select")
    rows = [{"A": 1, "b": 2}]
    assert sel(rows, rename={"A": "a"}, fields=["a"]) == [{"a": 1}]
    assert sel(rows, rename={"A": "a"}, fields=["A", "b"]) == [{"a": 1, "b": 2}]
    assert sel([{"x": 1}, {"y": 2}], fields=["x", "y"]) == [{"x": 1, "y": None}, {"x": None, "y": 2}]
    assert sel([], fields=["nope"]) == []


def test_select_fails_on_a_field_no_record_has():
    import pytest

    from agentl.registry import get

    with pytest.raises(ValueError, match=r"'nope'.*fields seen: A"):
        get("select")([{"A": 1}], fields=["nope"])


def test_cast_fail_stops_on_missing_or_null_unless_optional():
    import pytest

    from agentl.registry import get

    cast = get("cast")
    with pytest.raises(ValueError, match="'x' is missing"):
        cast([{"y": 1}], fields={"x": "int"}, on_bad="fail")
    with pytest.raises(ValueError, match="'x' is null"):
        cast([{"x": None}], fields={"x": "int"}, on_bad="fail")
    assert list(cast([{"y": 1}], fields={"x": "int?"}, on_bad="fail")) == [{"y": 1, "x": None}]
    for flag in ("optional", "nullable"):
        spec = {"x": {"type": "int", flag: True}}
        assert list(cast([{"x": None}], fields=spec, on_bad="fail")) == [{"x": None}]
    assert list(cast([{"y": 1}], fields={"x": "int"})) == [{"y": 1, "x": None}]
