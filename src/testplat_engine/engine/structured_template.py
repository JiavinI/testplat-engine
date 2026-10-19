"""显式结构化 API 模板；只实现迁移契约使用的表达式，不执行任意代码。"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from testplat_engine.interface import InvalidInputError

_EXPR = re.compile(r"\$\{([^{}]+)\}")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_MISSING = object()


def _lookup(path: str, params: Mapping[str, Any]) -> Any:
    if not _PATH.fullmatch(path):
        raise InvalidInputError("模板变量路径无效")
    node: Any = params
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _evaluate(expr: str, params: Mapping[str, Any]) -> Any:
    if expr == "now()":
        return datetime.now(tz=UTC).isoformat(timespec="seconds")
    if expr.startswith("date_add_days(") and expr.endswith(")"):
        value = _lookup(expr[len("date_add_days(") : -1], params)
        if value is _MISSING:
            return _MISSING
        try:
            days = Decimal(str(value))
            if not days.is_finite() or days != days.to_integral_value():
                raise ValueError
            return (datetime.now().astimezone().date() + timedelta(days=int(days))).isoformat()
        except (InvalidOperation, ValueError, OverflowError):
            raise InvalidInputError("date_add_days 的天数必须是有效整数") from None
    path, separator, default = expr.partition("|")
    required = path.endswith("!")
    path = path.removesuffix("!")
    value = _lookup(path, params)
    if separator and value is _MISSING:
        return default
    if required and (value is _MISSING or value is None):
        raise InvalidInputError(f"必填变量缺失或为空: {path}")
    return value


def render_template(node: Any, params: Mapping[str, Any], *, text: bool = False) -> Any:
    """缺失可选字段整项省略；完整占位符保留类型；Header 总是文本。"""
    if isinstance(node, Mapping):
        result = {}
        for key, value in node.items():
            rendered = render_template(value, params, text=text)
            if rendered is not _MISSING:
                result[key] = rendered
        return result
    if isinstance(node, list):
        result_list = []
        for value in node:
            rendered = render_template(value, params, text=text)
            if rendered is not _MISSING:
                result_list.append(rendered)
        return result_list
    if not isinstance(node, str):
        return node
    matches = list(_EXPR.finditer(node))
    if not matches:
        return node
    whole = _EXPR.fullmatch(node)
    if whole:
        value = _evaluate(whole.group(1), params)
        if value is _MISSING:
            return value
        return str(value) if text else copy.deepcopy(value)
    output = node
    for match in reversed(matches):
        value = _evaluate(match.group(1), params)
        if value is _MISSING:
            return _MISSING
        output = output[: match.start()] + str(value) + output[match.end() :]
    return output


def structured_params(inputs: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """默认值由资产维护；请求值覆盖，显式 null 不回退。"""
    defaults = {
        name: copy.deepcopy(spec["default"])
        for name, spec in (inputs or {}).items()
        if isinstance(spec, Mapping) and "default" in spec
    }
    defaults.update(params)
    return defaults
