"""金额边界与舍入策略；测试固定业务结果而非浮点近似。"""

from decimal import ROUND_UP, Inexact, localcontext

import pytest

from testplat_engine.engine.scenario_decimal import CalculationError, calculate


def money(values, operation="add", scale=2, rounding="HALF_UP", path=None):
    return calculate(values, operation=operation, scale=scale, rounding=rounding, path=path or [])


@pytest.mark.parametrize(
    "operands,operation,scale,rounding,expected",
    [
        ([0.1, 0.2], "add", 2, "HALF_UP", "0.30"),
        (["0.335", 3], "multiply", 2, "HALF_UP", "1.01"),
        (["0.335", 3], "multiply", 2, "HALF_EVEN", "1.00"),
        (["1.009", 1], "multiply", 2, "DOWN", "1.00"),
        (["-1.005", 0], "add", 2, "HALF_UP", "-1.01"),
        (["-0.001", 0], "add", 2, "HALF_UP", "0.00"),
        (["2.5", 0], "add", 0, "HALF_EVEN", "2"),
        (["0.123456785", 0], "add", 8, "HALF_UP", "0.12345679"),
        ([], "sum", 2, "HALF_UP", "0.00"),
        (["0.004", "0.004"], "sum", 2, "HALF_UP", "0.01"),
        (["0.00", "0.00"], "sum", 2, "HALF_UP", "0.00"),
        ([9007199254740993, "0.01"], "add", 2, "HALF_UP", "9007199254740993.01"),
    ],
)
def test_exact_arithmetic_and_explicit_rounding(operands, operation, scale, rounding, expected):
    assert money(operands, operation, scale, rounding) == expected


def test_sum_projection_and_ambient_context_isolation():
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert (
            money(
                [{"line": {"amount": "1234.565"}}, {"line": {"amount": "0.005"}}],
                "sum",
                path=["line", "amount"],
            )
            == "1234.57"
        )
        assert context.prec == 2
        assert context.traps[Inexact]


@pytest.mark.parametrize(
    "bad",
    [
        True,
        False,
        None,
        "",
        " ",
        " 1.2",
        "1.2 ",
        "NaN",
        "Infinity",
        float("nan"),
        float("inf"),
        "1e2",
        {},
        [],
        "1,000",
        "1" * 100,
        "0." + "0" * 32 + "1",
        10**33,
    ],
)
def test_invalid_amounts_fail_without_exposing_values(bad):
    with pytest.raises(CalculationError) as caught:
        money([bad, 1])
    assert repr(bad) not in str(caught.value)


@pytest.mark.parametrize(
    "values,operation,path",
    [
        ([1], "multiply", []),
        ([], "add", []),
        ("1.00", "sum", []),
        ([{}], "sum", ["missing"]),
        ([1] * 1025, "sum", []),
        (["999999999999999999999999999999999", 10], "multiply", []),
    ],
)
def test_invalid_arity_projection_and_bounds(values, operation, path):
    with pytest.raises(CalculationError):
        money(values, operation, path=path)
