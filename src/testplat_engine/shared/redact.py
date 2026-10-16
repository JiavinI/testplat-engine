"""公开结果脱敏与 alias 敏感键剔除。

- ``redact_json``：公开结果用，敏感键保留并替换为 ``***``。
- ``strip_sensitive_json``：alias 绑定用，敏感键直接**移除**（按键名语义）。
- ``redact_secret_values``：3.3a 按值替换。把本次执行注入的秘密值在任意嵌套字符串
  中按**子串**替换为 ``***``，键保留。

两种 alias 语义的区别（Q4）：``strip_sensitive_json`` 针对“键名敏感”的字段，整键移除，
下游引用表现为缺失；``redact_secret_values`` 针对“值是本次凭证”的字符串（例如目标服务
把 Authorization 回显到 ``$.echo.value`` 这种非敏感键下），只替换子串、保留键，下游拿到
的是 ``***`` 而不是原值。alias 绑定的输入是已经过两层处理的公开响应，两者叠加生效。

``_SENSITIVE_KEYS`` 是 Engine 唯一的敏感名单：公开 JSON 键与 HTTP 头名都以它为准（H1）。
本模块不依赖 interface，避免与 DTO 构造形成循环导入。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 裁决稿要求的最小集合；大小写不敏感匹配键名。也是 HTTP 头脱敏的唯一来源，
# executor 不再维护独立的头名单（H1：两份名单曾经不一致）。
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


def redact_secret_values(value: Any, secrets: tuple[str, ...]) -> Any:
    """递归把字符串中出现的秘密子串替换为 ``***``；键与结构保留。

    ``secrets`` 由调用方按长度降序传入（先替换完整 header 值，再替换原始变量值），
    空元组时原样返回副本。键名不替换：键承载结构语义，秘密作为键名不属于本批场景。
    """
    if not secrets:
        return value
    if isinstance(value, str):
        return redact_secret_text(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [redact_secret_values(item, secrets) for item in value]
    if isinstance(value, Mapping):
        return {str(key): redact_secret_values(item, secrets) for key, item in value.items()}
    return redact_secret_text(str(value), secrets)


def redact_secret_text(text: str, secrets: tuple[str, ...]) -> str:
    """长秘密按子串替换；短凭据只匹配完整值，避免误改 status、URL 等正常文本。"""
    if text in secrets:
        return _REDACTED
    for secret in secrets:
        if len(secret) >= 8 and secret in text:
            text = text.replace(secret, _REDACTED)
    return text
