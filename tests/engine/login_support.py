"""3.3b 登录鉴权测试支撑：假目标服务（httpx.MockTransport，零网络）、身份材料、假插件。

假目标端点与 Agent 0 fixture 提案一致：
- POST /login/json → {code:0, data:{token, userId, expiresIn}}；密码错误 → code=1
- POST /login/form、GET /login/query（token 另在 $.data.hash）
- GET /login/query-csrf（另给 csrfToken）
- GET /csrf/login → <meta name="csrf-token"> + 两条 Set-Cookie（验证 H2）
- POST /csrf/login → 302 + Set-Cookie SESSION；csrf 错误 → 302 到 /login
- GET /csrf/protected → 有会话 200 + x-uid；无会话 200 + 登录表单
- POST /login/short → token 只有 3 字符；GET /login/slow → 超时；/login/down → 连接失败
- POST /login/fail → code!=0，响应带一段长串（验证 Q7 启发式）
- GET /echo/token → 在非敏感键下回显 X-TOKEN / Cookie / X-CSRF-Token / Authorization
- GET /protected/401、POST /echo/raw、/echo/form、/echo/query、/echo/json、/business/slow
秘密全部是明显假值（TESTPLAT_ 前缀变量的“渲染结果”），同时是泄密搜索目标。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from testplat_engine import (
    AuthTable,
    ExtractRule,
    LoginAuth,
    LoginBody,
    LoginContext,
    LoginFlow,
    LoginResult,
    LoginStep,
    ProbeSpec,
    SecretStr,
    SingleApiEngine,
    StaticHeaderAuth,
)
from testplat_engine.plugins.httpx_client import HttpxClient

LOGIN_ASSETS = Path(__file__).parents[1] / "fixtures" / "login_assets"
HOST = "http://target.test"
ENV = {"api_hosts": {"target": HOST}}

BUYER_PASSWORD = "tp-fake-buyer-pass-71c2"
SELLER_PASSWORD = "tp-fake-seller-pass-93ab"
REDIS_PASSWORD = "tp-fake-redis-pass-5e0d"
PLUGIN_KEY = "tp-fake-plugin-key-11aa"
PLUGIN_TOKEN = "plug-token-5f5f5f5f5f"
ADMIN_SECRET = "tp-fake-admin-static-0042"
CSRF_TOKEN = "csrf-ff00ee11dd22"
CSRF_SEED = "seed-0a0b0c0d0e"
FAIL_DETAIL = "detail-0123456789abcdefghij"  # ≥ 20 字符，验证 Q7 启发式
STATIC_SECRETS = (
    BUYER_PASSWORD,
    SELLER_PASSWORD,
    REDIS_PASSWORD,
    PLUGIN_KEY,
    ADMIN_SECRET,
    PLUGIN_TOKEN,
    CSRF_TOKEN,
    CSRF_SEED,
    FAIL_DETAIL,
)


def _lookup(headers: httpx.Headers, name: str) -> str | None:
    return headers.get(name)


@dataclass
class FakeTarget:
    """有状态的假目标服务；记录每个实际发出的请求，签发确定性的 token / 会话。"""

    requests: list[httpx.Request] = field(default_factory=list)
    issued: list[str] = field(default_factory=list)
    sessions: set[str] = field(default_factory=set)
    revoked: set[str] = field(default_factory=set)
    expires_in: int = 3600
    session_max_age: int | None = None
    counter: int = 0
    # 3.3d：业务端点 — 吊销 token 后回 401；always_401 强制每次 401。
    always_401: bool = False
    revoke_after_business: bool = False
    first_401_body_secret: str = "revoked-body-secret-9f9f"

    def secrets(self) -> tuple[str, ...]:
        return (*STATIC_SECRETS, *self.issued)

    def revoke_all(self) -> None:
        """吊销所有已签发 token（设计稿 §6-38）。"""
        self.revoked.update(self.issued)

    def _token(self, user: str) -> str:
        self.counter += 1
        token = f"tok-{user}-{self.counter:04d}-a8f3e2c1d0"
        self.issued.append(token)
        return token

    def _login_ok(self, user: str, password: str | None, *, csrf: bool = False) -> httpx.Response:
        if password not in (BUYER_PASSWORD, SELLER_PASSWORD):
            return httpx.Response(200, json={"code": 1, "msg": "bad password"})
        token = self._token(user)
        data: dict[str, Any] = {
            "token": token,
            "hash": token,
            "userId": "10001",
            "expiresIn": self.expires_in,
        }
        if csrf:
            data["csrfToken"] = CSRF_TOKEN
        return httpx.Response(200, json={"code": 0, "data": data})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        body = request.content.decode() if request.content else ""
        if path == "/login/json":
            data = json.loads(body or "{}")
            return self._login_ok(data.get("username", "?"), data.get("password"))
        if path == "/login/form":
            form = {k: v[0] for k, v in parse_qs(body).items()}
            return self._login_ok(form.get("username", "?"), form.get("password"))
        if path in ("/login/query", "/login/query-csrf"):
            q = dict(request.url.params)
            return self._login_ok(
                q.get("username", "?"), q.get("password"), csrf=path.endswith("csrf")
            )
        if path == "/login/short":
            return httpx.Response(200, json={"code": 0, "data": {"token": "abc"}})
        if path == "/login/fail":
            return httpx.Response(200, json={"code": 2, "msg": "locked", "trace": FAIL_DETAIL})
        if path == "/login/slow":
            raise httpx.ReadTimeout("slow", request=request)
        if path == "/login/down":
            raise httpx.ConnectError("down", request=request)
        if path == "/business/slow":
            raise httpx.ReadTimeout("slow", request=request)
        if path == "/csrf/login" and request.method == "GET":
            html = f'<html><head><meta name="csrf-token" content="{CSRF_TOKEN}"></head></html>'
            return httpx.Response(
                200,
                text=html,
                headers=[
                    ("Set-Cookie", f"_csrf={CSRF_SEED}; Path=/"),
                    ("Set-Cookie", "lang=zh-CN; Path=/"),
                ],
            )
        if path == "/csrf/login":
            form = {k: v[0] for k, v in parse_qs(body).items()}
            csrf = form.get("_csrf")
            password = form.get("password") or form.get("LoginForm[password]")
            if (
                csrf != CSRF_TOKEN
                or password != SELLER_PASSWORD
                or CSRF_SEED not in (request.headers.get("Cookie") or "")
            ):
                return httpx.Response(302, headers={"Location": "/login"})
            self.counter += 1
            session = f"sess-{self.counter:04d}-7b7b7b7b"
            self.issued.append(session)
            self.sessions.add(session)
            cookie = f"SESSION={session}; Path=/"
            if self.session_max_age is not None:
                cookie += f"; Max-Age={self.session_max_age}"
            return httpx.Response(302, headers=[("Location", "/home"), ("Set-Cookie", cookie)])
        if path == "/csrf/protected":
            cookie = request.headers.get("Cookie") or ""
            if any(f"SESSION={s}" in cookie for s in self.sessions):
                return httpx.Response(200, text="<html>ok</html>", headers={"x-uid": "777"})
            return httpx.Response(200, text='<form id="loginFrom"></form>')
        if path in ("/echo/token", "/echo/token-post"):
            token = _lookup(request.headers, "X-TOKEN") or ""
            if self.always_401 or token in self.revoked:
                return httpx.Response(
                    401,
                    json={"msg": "unauthorized", "detail": self.first_401_body_secret},
                )
            posted = None
            if path == "/echo/token-post":
                try:
                    posted = json.loads(body or "null")
                except json.JSONDecodeError:
                    posted = body
            payload: dict[str, Any] = {
                "echo": {
                    "x_token": token or None,
                    "cookie": _lookup(request.headers, "Cookie"),
                    "csrf": _lookup(request.headers, "X-CSRF-Token"),
                    "authz": _lookup(request.headers, "Authorization"),
                }
            }
            if path == "/echo/token-post":
                payload["posted"] = posted
            if self.revoke_after_business and token:
                self.revoked.add(token)
            return httpx.Response(200, json=payload)
        if path == "/echo/secrets":
            # §6-32 专用：回显 token 与登录密码，验证缓存命中路径的按值替换。
            token = _lookup(request.headers, "X-TOKEN") or ""
            if self.always_401 or token in self.revoked:
                return httpx.Response(
                    401,
                    json={"msg": "unauthorized", "detail": self.first_401_body_secret},
                )
            return httpx.Response(
                200,
                json={
                    "echo": {
                        "x_token": token or None,
                        "password_echo": BUYER_PASSWORD,
                    }
                },
            )
        if path == "/protected/401":
            return httpx.Response(
                401, json={"msg": "unauthorized", "detail": self.first_401_body_secret}
            )
        if path == "/echo/raw":
            return httpx.Response(
                200,
                json={"raw": body, "content_type": request.headers.get("Content-Type")},
            )
        if path == "/echo/form":
            form = {k: v[0] for k, v in parse_qs(body).items()}
            return httpx.Response(
                200, json={"form": form, "content_type": request.headers.get("Content-Type")}
            )
        if path == "/echo/query":
            return httpx.Response(200, json={"query": dict(request.url.params)})
        if path == "/echo/json":
            return httpx.Response(200, json=json.loads(body or "null"))
        return httpx.Response(404, json={"path": path})

    def factory(self) -> HttpxClient:
        return HttpxClient(transport=httpx.MockTransport(self.handler))

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def engine_for(
    target: FakeTarget,
    strategies: tuple[Any, ...] = (),
    *,
    login_cache_max_entries: int = 256,
) -> SingleApiEngine:
    return SingleApiEngine(
        login_strategies=strategies,
        client_factory=target.factory,
        login_cache_max_entries=login_cache_max_entries,
    )


# ------------------------------------------------------------------ 身份材料


def buyer_material(**overrides: Any) -> LoginAuth:
    """第 1 档通用：单步 JSON 登录 + 一条 body_json 提取 + 注入 ${login:token}。"""
    step = overrides.pop("step", None) or LoginStep(
        name="login",
        method="POST",
        path=overrides.pop("path", "/login/json"),
        body=LoginBody(
            type="json",
            fields={"username": "buyer001", "password": SecretStr(BUYER_PASSWORD)},
        ),
        success=overrides.pop("success", {"path": "$.code", "value": 0}),
        extract=(ExtractRule(source="body_json", path="$.data.token", as_name="token"),),
    )
    kwargs: dict[str, Any] = {
        "strategy": "generic_flow",
        "host": "target",
        "tier": "generic",
        "flow": LoginFlow(steps=(step,)),
        "secret_values": (SecretStr(BUYER_PASSWORD),),
        "inject_headers": {"X-TOKEN": "${login:token}"},
    }
    kwargs.update(overrides)
    return LoginAuth(**kwargs)


def seller_material(**overrides: Any) -> LoginAuth:
    """第 2 档配置型：CSRF 预取（html_meta）+ 表单登录 302 + Cookie 会话 + probe。"""
    steps = (
        LoginStep(
            name="bootstrap",
            method="GET",
            path="/csrf/login",
            extract=(ExtractRule(source="html_meta", name="csrf-token", as_name="csrf"),),
        ),
        LoginStep(
            name="submit",
            method="POST",
            path="/csrf/login",
            headers={"Referer": "${login_host}/csrf/login"},
            body=LoginBody(
                type="form",
                fields={
                    "_csrf": "${login:csrf}",
                    "username": "seller001",
                    "password": SecretStr(SELLER_PASSWORD),
                },
            ),
            success={"status_code": 302, "reject_location_contains": "/login"},
        ),
    )
    kwargs: dict[str, Any] = {
        "strategy": "generic_flow",
        "host": "target",
        "tier": "configured",
        "flow": LoginFlow(steps=steps),
        "secret_values": (SecretStr(SELLER_PASSWORD),),
        "session_cookie": True,
        "probe": ProbeSpec(
            method="GET",
            path="/csrf/protected",
            success={"status_code": 200, "body_not_contains": '<form id="loginFrom"'},
        ),
        "inject_headers": {"X-CSRF-Token": "${login:csrf}"},
    }
    kwargs.update(overrides)
    return LoginAuth(**kwargs)


def plugin_material(strategy: str = "fake_plugin", behavior: str = "values") -> LoginAuth:
    """第 3 档插件：参数含 SecretStr，inject 引用插件产出的命名值。"""
    return LoginAuth(
        strategy=strategy,
        host="target",
        tier="plugin",
        params={"behavior": behavior, "api_key": SecretStr(PLUGIN_KEY), "redis": "redis_main"},
        secret_values=(SecretStr(PLUGIN_KEY),),
        inject_headers={"X-TOKEN": "${login:token}", "Authorization": "Key ${login:token}"},
    )


def admin_static() -> StaticHeaderAuth:
    return StaticHeaderAuth(
        headers={"Authorization": SecretStr(f"Bearer {ADMIN_SECRET}")},
        secret_values=(SecretStr(ADMIN_SECRET),),
    )


def table(**identities: Any) -> AuthTable:
    """构造测试身份表；可用 ``cache_scope=...`` 关键字开启跨执行缓存作用域。"""
    cache_scope = identities.pop("cache_scope", None)
    base: dict[str, Any] = {
        "buyer": buyer_material(),
        "seller": seller_material(),
        "plugged": plugin_material(),
        "admin": admin_static(),
    }
    base.update(identities)
    return AuthTable(
        identities=base,
        default_identity=None,
        connections={
            "redis_main": {"host": "10.0.0.5", "port": 6379, "password": SecretStr(REDIS_PASSWORD)}
        },
        cache_scope=cache_scope,
    )


# ------------------------------------------------------------------ 假插件


class FakePlugin:
    """测试用插件：values → 产出命名值（并经 record 记录含密码的请求）；raise → 抛异常；
    sleep → 超过超时预算。"""

    def __init__(self, name: str = "fake_plugin") -> None:
        self.name = name
        self.contexts: list[LoginContext] = []
        self.validated: list[str] = []

    def validate(self, auth: LoginAuth) -> None:
        self.validated.append(auth.strategy)
        if "behavior" not in (auth.params or {}):
            raise ValueError(f"missing behavior {PLUGIN_KEY}")  # 异常文本含秘密：不得外泄

    async def login(self, ctx: LoginContext) -> LoginResult:
        self.contexts.append(ctx)
        behavior = ctx.params["behavior"]
        api_key = ctx.params["api_key"]
        ctx.record(
            {
                "method": "POST",
                "url": f"{ctx.host_url}/plugin/login?k={api_key.reveal()}",
                "headers": {},
                "body": {"key": api_key.reveal()},
            },
            {"status_code": 200, "headers": {}, "body": {"issued": PLUGIN_TOKEN}},
            name="plugin_login",
        )
        if behavior == "raise":
            raise RuntimeError(f"plugin exploded with {api_key.reveal()}")
        if behavior == "sleep":
            await asyncio.sleep(5)
        return LoginResult(values={"token": PLUGIN_TOKEN})


def dump(payload: Any) -> str:
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)


def assert_no_secrets(target: FakeTarget, *payloads: Any) -> None:
    for payload in payloads:
        text = dump(payload)
        for secret in target.secrets():
            assert secret not in text, f"泄漏秘密 {secret[:6]}…"
