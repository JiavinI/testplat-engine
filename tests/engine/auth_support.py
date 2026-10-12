"""3.3a 鉴权测试支撑：假秘密、身份表、记录实际请求的回显假 client。

秘密全部是明显的假值，同时作为泄密断言的搜索目标；测试不访问网络。
回显语义与 Agent 4 的本地回显目标一致：
- GET /echo/headers → {echo: {value: <Authorization>, tenant: <X-Tenant-Token>}, seen_header_count}
- GET /echo/error → 500，正文带收到的全部 header
- GET /echo/header-reflect → 响应头 X-Echo 回显 Authorization
- POST /echo/body → 原样回显请求体
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from testplat_engine import AuthTable, SecretStr, StaticHeaderAuth, UnavailableAuth
from testplat_engine.ports.http_client import HttpClient, HttpResponse

AUTH_ASSETS = Path(__file__).parents[1] / "fixtures" / "auth_assets"
ENV = {"api_hosts": {"echo": "http://echo.invalid"}}

ADMIN_SECRET = "tp-fake-secret-9f31c2a7"
PARTNER_SECRET = "tp-fake-partner-4c8e11d0"
ADMIN_HEADER_VALUE = f"Bearer {ADMIN_SECRET}"
ALL_SECRETS = (ADMIN_SECRET, PARTNER_SECRET)


def admin_material() -> StaticHeaderAuth:
    return StaticHeaderAuth(
        headers={"Authorization": SecretStr(ADMIN_HEADER_VALUE)},
        secret_values=(SecretStr(ADMIN_SECRET),),
    )


def partner_material() -> StaticHeaderAuth:
    return StaticHeaderAuth(
        headers={"X-Tenant-Token": SecretStr(PARTNER_SECRET)},
        secret_values=(SecretStr(PARTNER_SECRET),),
    )


def auth_table(default_identity: str | None = "admin", **overrides: Any) -> AuthTable:
    identities: dict[str, Any] = {"admin": admin_material(), "partner": partner_material()}
    identities.update(overrides)
    return AuthTable(identities=identities, default_identity=default_identity)


def unavailable(reason: str = "环境变量 TESTPLAT_PARTNER_KEY 未设置") -> UnavailableAuth:
    return UnavailableAuth(public_reason=reason)


def _lookup(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


@dataclass
class SentRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: Any


@dataclass
class EchoClient(HttpClient):
    """记录**实际发出**的请求（H2 要求断言发出的 header，而不只是结果字段）。"""

    sent: list[SentRequest] = field(default_factory=list)
    fail_on: str | None = None
    fail_with: Exception | None = None

    async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        headers = dict(kwargs.get("headers") or {})
        self.sent.append(SentRequest(method, url, headers, kwargs.get("json")))
        if self.fail_on is not None and url.endswith(self.fail_on) and self.fail_with:
            raise self.fail_with
        auth_value = _lookup(headers, "Authorization")
        tenant = _lookup(headers, "X-Tenant-Token")
        if url.endswith("/echo/headers"):
            body = {
                "echo": {"value": auth_value, "tenant": tenant},
                "seen_header_count": len(headers),
            }
            return HttpResponse(200, {"Content-Type": "application/json"}, body)
        if url.endswith("/echo/error"):
            return HttpResponse(500, {}, {"error": "boom", "received": headers})
        if url.endswith("/echo/header-reflect"):
            return HttpResponse(200, {"X-Echo": auth_value or ""}, {"ok": True})
        if url.endswith("/echo/body"):
            return HttpResponse(200, {}, kwargs.get("json"))
        return HttpResponse(404, {}, None)


def assert_no_secret(*payloads: object) -> None:
    """序列化后搜索全部假秘密。"""
    for payload in payloads:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        for secret in ALL_SECRETS:
            assert secret not in text, f"泄漏秘密: {text}"
