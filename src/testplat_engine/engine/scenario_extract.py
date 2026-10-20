"""受限结构化选择器，不执行 JSONPath、表达式或字符串拼接。"""

from collections.abc import Mapping
from typing import Any


class SelectionError(ValueError):
    """只携带结构错误和命中数，不包含源数据或条件值。"""

    def __init__(self, reason: str, count: int | None = None):
        super().__init__(reason)
        self.count = count


def valid_path(path: Any) -> bool:
    return isinstance(path, list) and all(
        (isinstance(part, str) and bool(part) and "${" not in part)
        or (type(part) is int and part >= 0)
        for part in path
    )


def at_path(value: Any, path: list) -> Any:
    for part in path:
        if isinstance(part, str) and isinstance(value, Mapping) and part in value:
            value = value[part]
        elif type(part) is int and isinstance(value, list) and part < len(value):
            value = value[part]
        else:
            raise SelectionError("选择路径缺失或容器类型不符")
    return value


def equal_json(left: Any, right: Any) -> bool:
    # Python 中 True == 1；JSON 精准匹配不能将二者视为同一业务键。
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(equal_json(a, b) for a, b in zip(left, right))
    return left == right


def select(source: Any, selector: dict, cardinality: str) -> tuple[Any, int]:
    value = at_path(source, selector["path"])
    if "where" in selector:
        if not isinstance(value, list):
            raise SelectionError("筛选来源必须是数组")
        matches = []
        for item in value:
            matched = True
            for condition in selector["where"]:
                try:
                    candidate = at_path(item, condition["path"])
                except SelectionError:
                    matched = False
                    break
                if not equal_json(candidate, condition["equals"]):
                    matched = False
                    break
            if matched:
                matches.append(item)
    else:
        # 未筛选时，one 选择路径本身（含数组）；many 要求路径为数组。
        if cardinality == "one":
            return value, 1
        if not isinstance(value, list):
            raise SelectionError("many 来源必须是数组")
        matches = value
    count = len(matches)
    if cardinality == "one":
        if count != 1:
            raise SelectionError("one 提取必须恰好命中一项", count)
        return matches[0], count
    return matches, count
