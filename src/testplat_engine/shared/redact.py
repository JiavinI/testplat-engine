"""公开结果脱敏与 alias 敏感键剔除。

- ``redact_json``：公开结果用，敏感键保留并替换为 ``***``。
- ``strip_sensitive_json``：alias 绑定用，敏感键直接**移除**（唯一的 alias 脱敏语义）。

脱敏后的值不得再供后续步骤引用；凭证传播须等待独立身份/秘密通道。
本模块不依赖 interface，避免与 DTO 构造形成循环导入。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 裁决稿要求的最小集合；大小写不敏感匹配键名。
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "api_key",
        # 与单 API 执行器既有头脱敏对齐，避免公开结果出现盲区。
        "proxy-authorization",
        "x-api-key",
        "api-key",
    }
)

_REDACTED = "***"


def is_sensitive_key(key: str) -> bool:
    """键名是否属于必须脱敏的秘密字段（大小写不敏感）。"""
    return key.lower() in _SENSITIVE_KEYS


def redact_json(value: Any) -> Any:
    """递归脱敏 JSON 值；敏感键的值替换为占位符，结构保留。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if is_sensitive_key(name):
                result[name] = _REDACTED
            else:
                result[name] = redact_json(item)
        return result
    return str(value)


def strip_sensitive_json(value: Any) -> Any:
    """递归**移除**敏感键（而非替换为占位符），供 alias 绑定使用。

    为什么不复用 ``redact_json``：占位符 ``***`` 会作为"合法值"流入后续步骤，
    把"秘密被隐藏"伪装成"下游拿到了一个字符串"。移除后，对应路径在下游引用时
    表现为缺失，由 CaseRunner 报告稳定的 ``variable_resolution`` 失败。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [strip_sensitive_json(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): strip_sensitive_json(item)
            for key, item in value.items()
            if not is_sensitive_key(str(key))
        }
    return str(value)
