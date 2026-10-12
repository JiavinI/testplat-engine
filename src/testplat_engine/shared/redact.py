"""公开结果与 alias 绑定用的递归脱敏。

脱敏后的值不得再供后续步骤引用；凭证传播须等待独立身份/秘密通道。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from testplat_engine.interface import JsonObject, JsonValue

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


def redact_json(value: Any) -> JsonValue:
    """递归脱敏 JSON 值；敏感键的值替换为占位符，结构保留。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for key, item in value.items():
            name = str(key)
            if is_sensitive_key(name):
                result[name] = _REDACTED
            else:
                result[name] = redact_json(item)
        return result
    return str(value)


def safe_response_for_alias(response: Mapping[str, Any] | None) -> JsonObject | None:
    """构造可绑定到 alias 的安全响应对象。

    只保留 status_code / headers / body；headers 与 body 均递归脱敏。
    不包含 request、error 或完整 StepOutcome。
    """
    if response is None:
        return None
    status_code = response.get("status_code")
    headers = response.get("headers", {})
    body = response.get("body")
    safe_headers: JsonObject = {}
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            name = str(key)
            if is_sensitive_key(name):
                safe_headers[name] = _REDACTED
            else:
                if isinstance(value, (dict, list)):
                    safe_headers[name] = redact_json(value)
                else:
                    safe_headers[name] = str(value)
    return {
        "status_code": status_code if isinstance(status_code, int) else status_code,
        "headers": safe_headers,
        "body": redact_json(body),
    }
