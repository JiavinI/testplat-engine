"""列表投影和数量计算的边界；建单资产集成测试位于 Assets。"""

import pytest

from testplat_engine.engine.scenario_extract import SelectionError
from testplat_engine.engine.scenario_list import transform


def test_project_preserves_order_and_json_values():
    rows = [{"value": value} for value in [None, False, 0, [], {"a": 1}]]
    assert transform(rows, operation="project", path=["value"]) == [None, False, 0, [], {"a": 1}]
    assert transform([], operation="project", path=[]) == []


def test_sum_produces_json_integer():
    total = transform([{"qty": 2}, {"qty": 3}], operation="positive_integer_sum", path=["qty"])
    assert total == 5 and type(total) is int


@pytest.mark.parametrize("value", [None, False, 0, -1, 1.5, "2", 2**53])
def test_bad_quantity_is_not_coerced_or_rounded(value):
    with pytest.raises(SelectionError):
        transform([{"qty": 1}, {"qty": value}], operation="positive_integer_sum", path=["qty"])


@pytest.mark.parametrize(
    "source,operation,path",
    [
        ([], "positive_integer_sum", []),
        (None, "project", []),
        ([{}], "project", ["missing"]),
        ([1] * 1025, "project", []),
    ],
)
def test_missing_projection_and_invalid_source_fail(source, operation, path):
    with pytest.raises(SelectionError):
        transform(source, operation=operation, path=path)
