from decimal import Decimal, InvalidOperation
from typing import Any


class AssertionEvaluationError(ValueError):
    pass


def _strict_json_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(
            _strict_json_equal(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(
            _strict_json_equal(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def evaluate(operation: str, actual: Any, expected: Any = None) -> bool:
    if operation == "non_empty":
        return actual is not None and actual != "" and actual != [] and actual != {}
    if operation == "positive_integer":
        return type(actual) is int and actual > 0
    if operation == "equal":
        return _strict_json_equal(actual, expected)
    if operation == "decimal_equal":
        try:
            return Decimal(str(actual)) == Decimal(str(expected))
        except (InvalidOperation, ValueError, TypeError):
            return False
    raise AssertionEvaluationError("不支持的断言操作")
