"""请求体类型（D11 / 设计稿 §5.7）：资产与登录步骤共用的发送方式与公开快照形状。

- ``json``（默认）→ ``json=body``：与 0.3.0 的调用参数逐字一致（M4 零影响）；
- ``form`` → ``data=body``（一层标量键值，Content-Type 由 httpx 自动设置）；
- ``query`` → ``params=body``（一层标量键值，与 URL 自带 query 合并）；公开快照中
  ``url`` 不含这些参数，单独放 ``request.query``（Q8）；
- ``raw`` → ``content=body``（字符串），必须有 ``content_type`` 或显式 ``Content-Type``（Q4）；
- ``multipart`` / 未知值 → 资产加载即 ``invalid_asset``（§9 本批不做）。
Q4：显式写的 ``Content-Type`` 头永远优先，``content_type`` 只在未显式声明时补上。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

BODY_TYPES = frozenset({"json", "form", "query", "raw"})
# Q5：资产里出现这些 auth 配置专用占位符即 invalid_asset，避免三套占位符互相误吞。
FORBIDDEN_ASSET_PLACEHOLDERS = ("${secret:", "${login:", "${login_host}")


def has_header(headers: Mapping[str, Any] | None, name: str) -> bool:
    """不区分大小写判断 header 是否显式声明。"""
    if not isinstance(headers, Mapping):
        return False
    return any(str(key).lower() == name.lower() for key in headers)


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, str):
        return any(token in value for token in FORBIDDEN_ASSET_PLACEHOLDERS)
    if isinstance(value, Mapping):
        return any(_contains_forbidden(k) or _contains_forbidden(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item) for item in value)
    return False


def validate_asset_document(data: Mapping[str, Any]) -> None:
    """资产加载期校验：占位符作用域（Q5）+ ``request.type``（D11/Q4）。

    只抛 ``ValueError``（由 executor 映射为 ``invalid_asset``）；消息不回显字段值。
    """
    if _contains_forbidden(data):
        raise ValueError("资产不得包含 ${secret:} / ${login:} / ${login_host} 占位符")
    request = data.get("request")
    if not isinstance(request, Mapping):
        return
    body_type = request.get("type", "json")
    if body_type not in BODY_TYPES:
        # multipart 与任何未知类型都在这里拒绝；类型名截断后才进消息。
        raise ValueError(f"request.type 不受支持: {str(body_type)[:32]}")
    body = request.get("body")
    if body_type in ("form", "query") and body is not None:
        if not isinstance(body, Mapping):
            raise ValueError(f"request.type={body_type} 的 body 必须是一层键值")
        for key, item in body.items():
            if not isinstance(item, (str, int, float, bool)) and item is not None:
                raise ValueError(f"request.type={body_type} 的 body[{key}] 必须是标量")
    if body_type == "raw":
        if body is not None and not isinstance(body, str):
            raise ValueError("request.type=raw 的 body 必须是字符串")
        content_type = request.get("content_type")
        if not (isinstance(content_type, str) and content_type) and not has_header(
            request.get("headers"), "Content-Type"
        ):
            raise ValueError("request.type=raw 需要 content_type 或显式 Content-Type 头")
    elif "content_type" in request and request.get("content_type") is not None:
        if not isinstance(request.get("content_type"), str):
            raise ValueError("request.content_type 必须是字符串")


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def send_kwargs(
    body_type: str,
    body: Any,
    headers: dict[str, str] | None,
    content_type: str | None,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """返回 (client.request 的请求体 kwargs, 最终 headers)。

    json 只产出 ``{"json": body}``，与 0.3.0 完全一致（现有资产零影响）。
    """
    if body_type == "json":
        return {"json": body}, headers
    if body_type == "form":
        data = {str(k): _scalar_text(v) for k, v in (body or {}).items()}
        return {"data": data}, headers
    if body_type == "query":
        params = {str(k): _scalar_text(v) for k, v in (body or {}).items()}
        return {"params": params}, headers
    # raw
    final = dict(headers or {})
    if content_type and not has_header(final, "Content-Type"):
        final["Content-Type"] = content_type
    content = "" if body is None else str(body)
    return {"content": content}, final


def public_body(body_type: str, body: Any) -> dict[str, Any]:
    """公开快照中与请求体相关的字段：``body_type`` + ``body``（query 类型另给 ``query``）。"""
    if body_type == "query":
        query = {str(k): _scalar_text(v) for k, v in (body or {}).items()}
        return {"body_type": body_type, "body": None, "query": query}
    if body_type == "form" and isinstance(body, Mapping):
        return {"body_type": body_type, "body": {str(k): _scalar_text(v) for k, v in body.items()}}
    return {"body_type": body_type, "body": body}
