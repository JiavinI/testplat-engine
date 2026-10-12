"""变量解析器。

处理 API 契约中已实现字段（body / headers / success）的 ``${name}`` 渲染。
不静默扩展未实现的 DSL（如 query、auth）；格式能力以现有 YAML 行为为准。
"""

import copy
import re
from typing import Any

from testplat_engine.domain.api import Api

# 整个字符串恰好是一个 ``${name}`` 占位符。
_WHOLE_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def resolve_variables(
    api: Api, params: dict[str, Any], *, preserve_types: bool = False
) -> dict[str, Any]:
    """解析 API 中的变量

    将 API 契约中的变量占位符 (如 ${username}) 替换为 params 中的实际值。

    Args:
        api: API 契约
        params: 参数字典
        preserve_types: 默认 False，保持 0.1 单 API 行为（一律字符串插值）。
            Case 路径传 True：``body`` / ``success`` 中"整个字符串恰好是一个占位符"
            且参数存在时，原样保留参数的 JSON 类型（数字、布尔、对象），不被字符串化。
            ``headers`` 永远是字符串，不受影响；嵌入普通字符串的占位符仍按字符串插值。

    Returns:
        解析后的 API 定义,包含 method, url, body, headers 等

    Examples:
        >>> api = Api(
        ...     id="api.user.login",
        ...     method="POST",
        ...     url="/login",
        ...     request={"body": {"user": "${username}"}},
        ...     ...
        ... )
        >>> resolved = resolve_variables(api, {"username": "admin"})
        >>> resolved["body"]["user"]
        'admin'
    """
    result = {
        "method": api.method,
        "url": api.url,
    }

    # 处理请求体
    if api.request and "body" in api.request:
        result["body"] = _resolve_dict(api.request["body"], params, preserve_types)

    # 处理请求头
    if api.request and "headers" in api.request:
        result["headers"] = _resolve_dict(api.request["headers"], params, False)

    # 成功条件里的占位符也要渲染（如 value: "${username}"）
    if api.success:
        result["success"] = _resolve_dict(api.success, params, preserve_types)

    return result


def _resolve_dict(data: Any, params: dict[str, Any], preserve_types: bool = False) -> Any:
    """递归解析字典中的变量

    Args:
        data: 待解析的数据
        params: 参数字典

    Returns:
        解析后的数据
    """
    if isinstance(data, dict):
        return {k: _resolve_dict(v, params, preserve_types) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_dict(item, params, preserve_types) for item in data]
    elif isinstance(data, str):
        if preserve_types:
            whole = _WHOLE_PLACEHOLDER_RE.fullmatch(data)
            if whole is not None and whole.group(1) in params:
                # 深拷贝：同一参数被多处引用时互不共享可变结构。
                return copy.deepcopy(params[whole.group(1)])
        return _resolve_string(data, params)
    else:
        return data


def _resolve_string(value: str, params: dict[str, Any]) -> str:
    """解析字符串中的变量占位符

    支持 ${variable_name} 语法。

    Args:
        value: 待解析的字符串
        params: 参数字典

    Returns:
        解析后的字符串
    """
    # 匹配 ${variable_name} 模式
    pattern = r'\$\{([^}]+)\}'

    def replace_match(match):
        var_name = match.group(1)
        return str(params.get(var_name, match.group(0)))

    return re.sub(pattern, replace_match, value)
