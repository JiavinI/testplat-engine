"""变量解析器

处理 API 契约中的变量渲染,将占位符替换为实际值。
"""

from typing import Any

from testplat_engine.domain.api import Api


def resolve_variables(api: Api, params: dict[str, Any]) -> dict[str, Any]:
    """解析 API 中的变量

    将 API 契约中的变量占位符 (如 ${username}) 替换为 params 中的实际值。

    Args:
        api: API 契约
        params: 参数字典

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
        result["body"] = _resolve_dict(api.request["body"], params)

    # 处理请求头
    if api.request and "headers" in api.request:
        result["headers"] = _resolve_dict(api.request["headers"], params)

    # 成功条件里的占位符也要渲染（如 value: "${username}"）
    if api.success:
        result["success"] = _resolve_dict(api.success, params)

    return result


def _resolve_dict(data: Any, params: dict[str, Any]) -> Any:
    """递归解析字典中的变量

    Args:
        data: 待解析的数据
        params: 参数字典

    Returns:
        解析后的数据
    """
    if isinstance(data, dict):
        return {k: _resolve_dict(v, params) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_dict(item, params) for item in data]
    elif isinstance(data, str):
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
    import re

    # 匹配 ${variable_name} 模式
    pattern = r'\$\{([^}]+)\}'

    def replace_match(match):
        var_name = match.group(1)
        return str(params.get(var_name, match.group(0)))

    return re.sub(pattern, replace_match, value)
