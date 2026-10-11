"""断言引擎

验证 HTTP 响应是否符合预期条件。
"""

from typing import Any

from testplat_engine.ports.http_client import HttpResponse


def check_success(response: HttpResponse, success_condition: dict[str, Any]) -> bool:
    """检查响应是否符合成功条件

    根据 API 契约中定义的成功条件,验证 HTTP 响应。

    支持的条件类型:
    - path + value: 使用 JSONPath 提取值并比较
    - status_code: 检查 HTTP 状态码

    Args:
        response: HTTP 响应对象
        success_condition: 成功条件定义,如 {"path": "$.code", "value": "0"}

    Returns:
        True 表示响应符合成功条件,False 表示不符合

    Examples:
        >>> response = HttpResponse(
        ...     status_code=200,
        ...     headers={},
        ...     body={"code": "0", "message": "success"}
        ... )
        >>> check_success(response, {"path": "$.code", "value": "0"})
        True
    """
    # 1. 检查状态码 (如果指定)
    if "status_code" in success_condition:
        expected_code = success_condition["status_code"]
        if response.status_code != expected_code:
            return False

    # 2. 检查 JSONPath 条件 (如果指定)
    if "path" in success_condition:
        path = success_condition["path"]
        expected_value = success_condition.get("value")

        actual_value = _extract_value(response.body, path)

        # 如果 expected_value 为空字符串,只检查路径是否存在
        if expected_value == "":
            return True

        return str(actual_value) == str(expected_value)

    # 默认:如果没有指定条件,检查状态码 2xx
    return 200 <= response.status_code < 300


def _extract_value(data: Any, path: str) -> Any:
    """从数据中提取指定路径的值

    简化的 JSONPath 实现,支持:
    - $ : 根节点
    - $.field : 字段访问
    - $.field.nested : 嵌套字段

    Args:
        data: 数据对象 (通常是 dict)
        path: JSONPath 表达式

    Returns:
        提取的值,如果路径不存在返回 None

    Examples:
        >>> data = {"user": {"name": "Alice"}}
        >>> _extract_value(data, "$.user.name")
        'Alice'
    """
    if path == "$":
        return data

    # 移除开头的 $. 或 $
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$"):
        path = path[1:]
        if path.startswith("."):
            path = path[1:]

    # 按 . 分割路径
    parts = path.split(".")

    current = data
    for part in parts:
        if not part:  # 跳过空部分
            continue

        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None

        if current is None:
            return None

    return current
