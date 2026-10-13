"""3.3b 内置登录策略 ``generic_flow``（第 1 档单步 / 第 2 档多步），及登录请求公用件。

- 步骤按顺序执行；请求体 json/form/query/raw 与资产共用 ``shared.request_body``；
- 一律 ``follow_redirects=False``（Q3），302 必须可见；
- ``success`` 未写时默认“状态码 < 400”（Q3）；另支持 ``body_not_contains`` 与
  ``reject_location_contains``（probe 常用）；
- ``extract`` 四种来源：``body_json`` / ``response_header`` / ``cookies`` / ``html_meta``（D10）。

失败一律抛 ``LoginStepError(reason, failed_step)``；超时 / 连接失败在这里就转换为
``timeout`` / ``transport``（H3），绝不进入 executor 的 ``_map_exception``。
本模块不调用 ``reveal()``：模板渲染全部经 ``login_render``。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import httpx

from testplat_engine.engine.assert_engine import _extract_value
from testplat_engine.engine.login_render import (
    MissingLoginValueError,
    render_headers,
    render_text,
    render_value,
    secret_template_refs,
)
from testplat_engine.interface import (
    GENERIC_FLOW,
    LOGIN_REF_RE,
    LoginAuth,
    LoginBody,
    LoginContext,
    LoginResult,
    LoginStep,
)
from testplat_engine.ports.http_client import HttpClient, HttpResponse
from testplat_engine.shared.redact import is_sensitive_key
from testplat_engine.shared.request_body import public_body, send_kwargs

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))""")


class LoginStepError(Exception):
    """登录子流程的已分类失败；``values`` 为失败前已产出的命名值（需入替换集合）。"""

    def __init__(
        self, reason: str, failed_step: str | None, values: Mapping[str, str] | None = None
    ) -> None:
        self.reason = reason
        self.failed_step = failed_step
        self.values = dict(values or {})
        super().__init__(f"login step failed reason={reason} step={failed_step}")


def classify_transport(exc: BaseException) -> str | None:
    """超时 → timeout；连接/传输失败 → transport；其它 → None。

    顺序重要：httpx 的超时异常是 RequestError 子类，必须先判。
    """
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, (ConnectionError, httpx.RequestError)):
        return "transport"
    return None


def _named_header(headers: Mapping[str, Any] | None, name: str) -> str | None:
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return str(value)
    return None


def _public_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """登录记录里的 header：敏感名一律 ***（Cookie / Set-Cookie 按名脱敏）。"""
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = str(key)
        out[name] = "***" if is_sensitive_key(name) else str(value)
    return out


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def public_response_record(response: HttpResponse) -> dict[str, Any]:
    """登录响应的原始记录（仅按名脱敏；按值替换由会话池在登录结束后统一做，Q7）。"""
    return {
        "status_code": response.status_code,
        "headers": _public_headers(response.headers),
        "body": _json_safe(response.body),
    }


def response_text(response: HttpResponse) -> str:
    if response.text is not None:
        return response.text
    if isinstance(response.body, str):
        return response.body
    try:
        return json.dumps(response.body, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(response.body)


def login_success(response: HttpResponse, condition: Mapping[str, Any] | None) -> bool:
    """登录步骤 / probe 的成功判定（Q3）。

    未写 ``status_code`` 时要求状态码 < 400；``path``/``value`` 语义与资产断言一致；
    ``body_not_contains`` / ``reject_location_contains`` 命中即失败。
    """
    cond = dict(condition or {})
    if "status_code" in cond:
        if response.status_code != cond["status_code"]:
            return False
    elif response.status_code >= 400:
        return False
    if "path" in cond:
        expected = cond.get("value")
        actual = _extract_value(response.body, str(cond["path"]))
        if expected != "" and str(actual) != str(expected):
            return False
    needle = cond.get("body_not_contains")
    if isinstance(needle, str) and needle and needle in response_text(response):
        return False
    reject = cond.get("reject_location_contains")
    if isinstance(reject, str) and reject:
        location = _named_header(response.headers, "Location") or ""
        if reject in location:
            return False
    return True


def _html_meta(text: str, name: str) -> str | None:
    for tag in _META_TAG_RE.findall(text):
        attrs: dict[str, str] = {}
        for match in _ATTR_RE.finditer(tag):
            value = next(g for g in match.groups()[2:] if g is not None)
            attrs[match.group(1).lower()] = value
        if attrs.get("name") == name and "content" in attrs:
            return attrs["content"]
    return None


def extract_value(rule: Any, response: HttpResponse, http: HttpClient) -> str | None:
    """按一条 ``ExtractRule`` 取值；取不到返回 None。"""
    if rule.source == "body_json":
        value = _extract_value(response.body, rule.path)
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    if rule.source == "response_header":
        return _named_header(response.headers, rule.name)
    if rule.source == "cookies":
        jar = http.cookie_jar() if hasattr(http, "cookie_jar") else []
        if rule.format == "cookie_header":
            cookies = jar or list(response.cookies)
            if not cookies:
                return None
            return "; ".join(f"{c.name}={c.value}" for c in cookies)
        for cookie in [*response.cookies, *jar]:
            if cookie.name == rule.name:
                return cookie.value
        return None
    if rule.source == "html_meta":
        return _html_meta(response_text(response), rule.name)
    return None


async def send_login_request(
    ctx: LoginContext,
    *,
    name: str,
    method: str,
    path: str,
    headers: Mapping[str, object] | None,
    body: LoginBody | None,
    values: Mapping[str, str],
    extra_headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    """渲染并发送一个登录请求（步骤或 probe），把原始请求/响应交给 ``ctx.record``。

    渲染缺值 → ``extract_missing``；超时/连接失败 → ``timeout``/``transport``（H3）。
    """
    try:
        url = ctx.host_url + render_text(path, values, ctx.host_url)
        send_headers = dict(extra_headers or {})
        send_headers.update(render_headers(headers, values, ctx.host_url))
        body_type = "json" if body is None else body.type
        if body is None:
            rendered_body: Any = None
        elif body.type == "raw":
            rendered_body = render_text(body.content, values, ctx.host_url)
        else:
            rendered_body = render_value(body.fields, values, ctx.host_url)
    except MissingLoginValueError as exc:
        raise LoginStepError("extract_missing", name, values) from exc
    kwargs, final_headers = send_kwargs(
        body_type, rendered_body, send_headers or None, None if body is None else body.content_type
    )
    if body is None:
        kwargs = {}
    request_record = {
        "method": method,
        "url": url,
        "headers": _public_headers(final_headers),
        **public_body(body_type, rendered_body),
    }
    try:
        response = await ctx.http.request(  # type: ignore[attr-defined]
            method,
            url,
            headers=final_headers,
            timeout=ctx.timeout_seconds,
            follow_redirects=False,
            **kwargs,
        )
    except Exception as exc:
        ctx.record(request_record, None, name=name)
        reason = classify_transport(exc)
        if reason is None:
            raise
        raise LoginStepError(reason, name, values) from exc
    ctx.record(request_record, public_response_record(response), name=name)
    return response


class GenericFlowStrategy:
    """内置 ``generic_flow``：按 ``LoginFlow`` 顺序执行步骤、判定成功、提取命名值。"""

    name = GENERIC_FLOW

    def validate(self, auth: LoginAuth) -> None:
        """预检：步骤引用的命名值必须由**前序**步骤提取；inject/probe 引用必须有来源。"""
        if auth.flow is None:
            raise ValueError("generic_flow 缺少 flow")
        available: set[str] = set()
        for step in auth.flow.steps:
            refs = step.referenced_names() | secret_template_refs(step.headers)
            missing = refs - available
            if missing:
                raise ValueError(f"步骤 {step.name} 引用了未提取的命名值: {sorted(missing)}")
            available |= {rule.as_name for rule in step.extract}
        refs = auth.inject_refs() | secret_template_refs(auth.inject_headers)
        if auth.probe is not None:
            for item in auth.probe.headers.values():  # type: ignore[union-attr]
                if isinstance(item, str):
                    refs |= set(LOGIN_REF_RE.findall(item))
            refs |= secret_template_refs(auth.probe.headers)
        missing = refs - available
        if missing:
            raise ValueError(f"inject/probe 引用了未提取的命名值: {sorted(missing)}")

    async def run(self, ctx: LoginContext) -> tuple[dict[str, str], dict[str, HttpResponse]]:
        """执行流程，返回 (命名值, 步骤名 → 响应)；失败抛 ``LoginStepError``。"""
        assert ctx.flow is not None
        values: dict[str, str] = {}
        responses: dict[str, HttpResponse] = {}
        for step in ctx.flow.steps:
            response = await self._run_step(ctx, step, values)
            responses[step.name] = response
            if not login_success(response, step.success):
                raise LoginStepError("assertion", step.name, values)
            for rule in step.extract:
                value = extract_value(rule, response, ctx.http)  # type: ignore[arg-type]
                if value is None or value == "":
                    if rule.optional:
                        continue
                    raise LoginStepError("extract_missing", step.name, values)
                values[rule.as_name] = value
        return values, responses

    async def _run_step(
        self, ctx: LoginContext, step: LoginStep, values: Mapping[str, str]
    ) -> HttpResponse:
        return await send_login_request(
            ctx,
            name=step.name,
            method=step.method,
            path=step.path,
            headers=step.headers,
            body=step.body,
            values=values,
        )

    async def login(self, ctx: LoginContext) -> LoginResult:
        """协议入口（供外部直接复用）；会话池内部走 ``run`` 以拿到响应计算过期时间。"""
        values, _ = await self.run(ctx)
        return LoginResult(values=values)
