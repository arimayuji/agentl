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
