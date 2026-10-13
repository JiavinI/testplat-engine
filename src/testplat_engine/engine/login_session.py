"""3.3b 执行级会话池：按需登录、有效性判断、登录记录与执行级按值替换集合。

每次 ``execute`` 一个池（Q6：经 ``execute_api`` 内部关键字参数与 ``StepExecutionContext``
传递，不进任何公开 DTO），执行结束 ``close()`` 统一关闭 login 身份的专用 client。

- D8：业务步骤解析出 login 身份后调 ``ensure``；池中有效 → 直接复用；否则登录。
- D13/E3：``ttl_seconds`` / ``expires_from`` / Cookie ``expires`` 任一到期（固定提前 30 秒）→ 重登，
  ``logins`` 追加新记录，``login_ref`` 指向新下标。不做跨执行缓存。
- H1：按值替换集合挂在池上，是全部身份的命名值、jar Cookie 值、登录参数秘密、静态 header
  秘密的**并集**；任何快照都用它脱敏。
- Q7：登录过程记录先缓存；登录结束（成功或失败）后**先**把命名值与 jar Cookie 值加入集合，
  **再**统一脱敏落定；失败时额外把登录响应中 ≥ 20 字符的字符串叶子值一律 ``***``。
- E2/H3：登录阶段的超时 / 连接失败 → ``login_failed(reason=timeout|transport)``，绝不抛系统异常。

日志只记身份名、策略名、步骤名、reason 与异常类型名。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from testplat_engine.engine.assert_engine import _extract_value
from testplat_engine.engine.login_flow import (
    GenericFlowStrategy,
    LoginStepError,
    classify_transport,
    login_success,
    send_login_request,
)
from testplat_engine.engine.login_render import (
    MissingLoginValueError,
    render_headers,
    static_login_secrets,
)
from testplat_engine.interface import (
    GENERIC_FLOW,
    LOGIN_REF_RE,
    MIN_SECRET_LENGTH,
    InvalidEnvironmentError,
    JsonObject,
    LoginAuth,
    LoginContext,
    LoginResult,
    LoginStrategy,
)
from testplat_engine.ports.http_client import HttpClient, HttpResponse
from testplat_engine.shared.redact import redact_json, redact_secret_values

logger = logging.getLogger(__name__)

# E3：固定安全余量（秒），不开放配置。
SAFETY_MARGIN_SECONDS = 30.0
# Q7：登录失败时启发式遮蔽的字符串长度下限。
FAILED_LOGIN_MASK_LENGTH = 20
# 时钟可在测试中替换；过期时间统一用 epoch 秒（expires_from 的 epoch 单位与 Cookie expires 同源）。
_now: Callable[[], float] = time.time

BUILTIN_STRATEGY = GenericFlowStrategy()


class LoginFailedError(Exception):
    """一次登录失败（业务结果，不是系统异常）；``login_ref`` 指向失败的那条记录。"""

    def __init__(
        self, identity: str, strategy: str, reason: str, failed_step: str | None, login_ref: int
    ) -> None:
        self.identity = identity
        self.strategy = strategy
        self.reason = reason
        self.failed_step = failed_step
        self.login_ref = login_ref
        super().__init__(f"login failed identity={identity} reason={reason}")

    def error_payload(self) -> JsonObject:
        """``error`` 对象：``kind=login_failed`` + ``login`` 子对象（只含名字与 reason）。"""
        return {
            "kind": "login_failed",
            "message": f"登录失败：身份 {self.identity}",
            "login": {
                "identity": self.identity,
                "strategy": self.strategy,
                "failed_step": self.failed_step,
                "reason": self.reason,
            },
        }


@dataclass(slots=True, repr=False)
class SessionEntry:
    """池中一个身份的鉴权状态；只在本次执行内存活。"""

    identity: str
    client: HttpClient
    owns_client: bool
    values: dict[str, str]
    expires_at: float | None
    login_ref: int
    session_cookie: bool

    def __repr__(self) -> str:
        return f"SessionEntry(identity={self.identity!r}, login_ref={self.login_ref})"


def validate_strategies(strategies: Iterable[object]) -> dict[str, LoginStrategy]:
    """``create_engine(login_strategies=...)`` 的启动期校验；消息只含策略名。"""
    registry: dict[str, LoginStrategy] = {GENERIC_FLOW: BUILTIN_STRATEGY}  # type: ignore[dict-item]
    for strategy in strategies:
        name = getattr(strategy, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("登录策略缺少 name")
        if not callable(getattr(strategy, "validate", None)) or not callable(
            getattr(strategy, "login", None)
        ):
            raise ValueError(f"登录策略 {name[:64]} 未实现 validate/login")
        if name in registry:
            if name == GENERIC_FLOW:
                raise ValueError(f"登录策略名与内置策略冲突: {name}")
            raise ValueError(f"登录策略重名: {name[:64]}")
        registry[name] = strategy  # type: ignore[assignment]
    return registry


def resolve_login_host(identity: str, material: LoginAuth, environment: Mapping[str, Any]) -> str:
    api_hosts = environment.get("api_hosts")
    raw = api_hosts.get(material.host) if isinstance(api_hosts, Mapping) else None
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidEnvironmentError(
            public_message=f"身份 {identity} 的登录 host 不可解析: {material.host}",
            log_reason=f"identity={identity} login host={material.host!r} missing",
        )
    return raw.rstrip("/")


def preflight_login(
    identity: str,
    material: LoginAuth,
    environment: Mapping[str, Any],
    strategies: Mapping[str, LoginStrategy],
) -> str:
    """零副作用预检：策略已注册、``validate()`` 通过、host 可解析、inject 引用有来源。"""
    strategy = strategies.get(material.strategy)
    if strategy is None:
        raise InvalidEnvironmentError(
            public_message=f"身份 {identity} 的登录策略未注册: {material.strategy}",
            log_reason=f"identity={identity} strategy={material.strategy} unregistered",
        )
    try:
        strategy.validate(material)
    except Exception as exc:
        # 只报策略名与异常类型名；插件的异常文本可能夹带参数值。
        raise InvalidEnvironmentError(
            public_message=f"身份 {identity} 的登录配置未通过策略 {material.strategy} 校验",
            log_reason=f"identity={identity} strategy={material.strategy} "
            f"validate failed exc_type={type(exc).__name__}",
        ) from None
    return resolve_login_host(identity, material, environment)


def _mask_long_strings(value: Any) -> Any:
    """Q7 启发式：登录失败时，响应中 ≥ 20 字符的字符串叶子一律 ***。"""
    if isinstance(value, str):
        return "***" if len(value) >= FAILED_LOGIN_MASK_LENGTH else value
    if isinstance(value, list):
        return [_mask_long_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: _mask_long_strings(item) for key, item in value.items()}
    return value


def _expires_from_response(
    material: LoginAuth, responses: Mapping[str, HttpResponse], now: float
) -> float | None:
    spec = material.validity.expires_from if material.validity else None
    if spec is None or not responses:
        return None
    response = responses.get(spec.step) if spec.step else list(responses.values())[-1]
    if response is None:
        return None
    raw = _extract_value(response.body, spec.path)
    try:
        number = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if spec.unit == "seconds":
        return now + number
    if spec.unit == "epoch_ms":
        return number / 1000.0
    return number


class SessionPool:
    """一次执行的会话池；不可跨执行复用（D13）。"""

    def __init__(
        self,
        strategies: Mapping[str, LoginStrategy],
        client_factory: Callable[[], tuple[HttpClient, bool]],
    ) -> None:
        self._strategies = strategies
        self._client_factory = client_factory
        self._entries: dict[str, SessionEntry] = {}
        self._secrets: set[str] = set()
        self._logins: list[JsonObject] = []
        self._closed = False

    def __repr__(self) -> str:
        return f"SessionPool(identities={sorted(self._entries)!r}, logins={len(self._logins)})"

    # ------------------------------------------------------------ 按值替换集合（H1）

    def add_secrets(self, values: Iterable[str]) -> None:
        for value in values:
            if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
                self._secrets.add(value)

    def snapshot_secrets(self) -> tuple[str, ...]:
        """执行级替换集合的当前快照：长度降序（先替换长串，避免部分替换残留）。"""
        return tuple(sorted(self._secrets, key=len, reverse=True))

    def absorb_cookies(self, client: HttpClient) -> None:
        """D12：Cookie 身份的任何响应都会更新其 jar；新 Cookie 值立即进入替换集合。"""
        jar = client.cookie_jar() if hasattr(client, "cookie_jar") else []
        self.add_secrets(cookie.value for cookie in jar)

    def public_logins(self) -> list[JsonObject]:
        return [dict(item) for item in self._logins]

    # ------------------------------------------------------------ 有效性（D13/E3）

    def _is_valid(self, entry: SessionEntry) -> bool:
        now = _now()
        if entry.expires_at is not None and now >= entry.expires_at - SAFETY_MARGIN_SECONDS:
            return False
        if entry.session_cookie and hasattr(entry.client, "cookie_jar"):
            for cookie in entry.client.cookie_jar():
                if cookie.expires is not None and now >= cookie.expires - SAFETY_MARGIN_SECONDS:
                    return False
        return True

    @staticmethod
    def cookie_names(entry: SessionEntry) -> list[str]:
        if not entry.session_cookie or not hasattr(entry.client, "cookie_jar"):
            return []
        return sorted({cookie.name for cookie in entry.client.cookie_jar()})

    # ------------------------------------------------------------ 按需登录（D8）

    async def ensure(
        self,
        identity: str,
        material: LoginAuth,
        host_url: str,
        *,
        timeout_seconds: float,
        connections: Mapping[str, Mapping[str, object]],
    ) -> SessionEntry:
        """返回可用的会话；需要时登录。登录失败抛 ``LoginFailedError``（业务结果）。"""
        entry = self._entries.get(identity)
        if entry is not None and self._is_valid(entry):
            return entry
        if entry is not None:
            # 失效：丢弃旧会话（含 jar），用全新 client 重新登录。
            del self._entries[identity]
            await self._close_client(entry.client, entry.owns_client, identity)
        return await self._login(identity, material, host_url, timeout_seconds, connections)

    async def _login(
        self,
        identity: str,
        material: LoginAuth,
        host_url: str,
        timeout_seconds: float,
        connections: Mapping[str, Mapping[str, object]],
    ) -> SessionEntry:
        strategy = self._strategies[material.strategy]
        # 登录前已知的秘密（密码、SecretStr 参数、连接密码）先入集合。
        self.add_secrets(static_login_secrets(material, connections))
        client, owns = self._client_factory()
        records: list[dict[str, Any]] = []
        probe_records: list[dict[str, Any]] = []
        target = records

        def record(
            request: Mapping[str, object],
            response: Mapping[str, object] | None,
            *,
            name: str | None = None,
        ) -> None:
            # Q7：只缓存，不在此处构造公开快照；JSON 化防止插件传入不可序列化对象。
            target.append(
                {
                    "name": name,
                    "request": _jsonable(request),
                    "response": None if response is None else _jsonable(response),
                }
            )

        ctx = LoginContext(
            identity=identity,
            http=client,
            host_url=host_url,
            params=material.params,  # type: ignore[arg-type]
            connections=connections,  # type: ignore[arg-type]
            record=record,
            flow=material.flow,
            timeout_seconds=timeout_seconds,
        )
        started = time.perf_counter()
        values: dict[str, str] = {}
        responses: dict[str, HttpResponse] = {}
        expires_at: float | None = None
        failure: tuple[str, str | None] | None = None
        exc_type: str | None = None
        try:
            try:
                if material.strategy == GENERIC_FLOW:
                    values, responses = await BUILTIN_STRATEGY.run(ctx)
                else:
                    result = await asyncio.wait_for(strategy.login(ctx), timeout=timeout_seconds)
                    if not isinstance(result, LoginResult):
                        raise TypeError("strategy must return LoginResult")
                    values = dict(result.values)
                    expires_at = result.expires_at
            except LoginStepError as exc:
                values = exc.values
                failure = (exc.reason, exc.failed_step)
                exc_type = type(exc.__cause__ or exc).__name__
            except Exception as exc:
                reason = classify_transport(exc) or "strategy_error"
                failure = (reason, None)
                exc_type = type(exc).__name__

            # Q7 顺序：命名值与 jar Cookie 值先入集合，之后才构造任何快照。
            self.add_secrets(values.values())
            self.absorb_cookies(client)

            if failure is None:
                failure = self._check_values(material, values, host_url)
            if failure is None and material.probe is not None:
                target = probe_records
                failure, exc_type = await self._probe(ctx, material, values)
                self.absorb_cookies(client)
        except BaseException:
            # CancelledError 等：不吞，但不能遗留专用 client。
            await self._close_client(client, owns, identity)
            raise

        now = _now()
        if failure is None:
            from_response = _expires_from_response(material, responses, now)
            ttl = material.validity.ttl_seconds if material.validity else None
            if from_response is not None:
                expires_at = from_response
            elif expires_at is None and ttl is not None:
                expires_at = now + ttl

        login_ref = len(self._logins)
        self._logins.append(
            self._public_record(
                identity, material, records, probe_records, failure, time.perf_counter() - started
            )
        )
        if failure is not None:
            await self._close_client(client, owns, identity)
            reason, failed_step = failure
            logger.warning(
                "login failed identity=%s strategy=%s step=%s reason=%s exc_type=%s",
                identity,
                material.strategy,
                failed_step,
                reason,
                exc_type,
            )
            raise LoginFailedError(identity, material.strategy, reason, failed_step, login_ref)

        entry = SessionEntry(
            identity=identity,
            client=client,
            owns_client=owns,
            values=values,
            expires_at=expires_at,
            login_ref=login_ref,
            session_cookie=material.session_cookie,
        )
        self._entries[identity] = entry
        return entry

    @staticmethod
    def _check_values(
        material: LoginAuth, values: Mapping[str, str], host_url: str
    ) -> tuple[str, str | None] | None:
        """token 长度下限与 inject 引用覆盖（插件可能没产出声明的名字）。"""
        token = values.get("token")
        if token is not None and len(token) < MIN_SECRET_LENGTH:
            return ("token_too_short", None)
        missing = material.inject_refs() - set(values)
        if missing:
            return ("extract_missing", None)
        try:
            render_headers(material.inject_headers, values, host_url)
        except MissingLoginValueError:
            return ("extract_missing", None)
        return None

    async def _probe(
        self, ctx: LoginContext, material: LoginAuth, values: Mapping[str, str]
    ) -> tuple[tuple[str, str | None] | None, str | None]:
        probe = material.probe
        assert probe is not None
        try:
            inject = render_headers(material.inject_headers, values, ctx.host_url)
            response = await send_login_request(
                ctx,
                name="probe",
                method=probe.method,
                path=probe.path,
                headers=probe.headers,
                body=None,
                values=values,
                extra_headers=inject,
            )
        except LoginStepError as exc:
            reason = exc.reason if exc.reason in ("timeout", "transport") else "probe_failed"
            return (reason, "probe"), type(exc.__cause__ or exc).__name__
        except Exception as exc:
            return (classify_transport(exc) or "probe_failed", "probe"), type(exc).__name__
        if not login_success(response, probe.success):
            return ("probe_failed", "probe"), None
        return None, None

    def _public_record(
        self,
        identity: str,
        material: LoginAuth,
        records: list[dict[str, Any]],
        probe_records: list[dict[str, Any]],
        failure: tuple[str, str | None] | None,
        duration: float,
    ) -> JsonObject:
        """统一脱敏落定一条登录记录：按键 → 按值（执行级并集）→ 失败启发式（Q7）。"""
        secrets = self.snapshot_secrets()

        def finalize(item: dict[str, Any]) -> JsonObject:
            request = redact_secret_values(redact_json(item["request"]), secrets)
            response = item["response"]
            if response is not None:
                response = redact_secret_values(redact_json(response), secrets)
                if failure is not None:
                    response = {
                        **response,
                        "headers": _mask_long_strings(response.get("headers")),
                        "body": _mask_long_strings(response.get("body")),
                    }
            return {"name": item["name"], "request": request, "response": response}

        steps = [finalize(item) for item in records]
        probe = finalize(probe_records[-1]) if probe_records else None
        error: JsonObject | None = None
        if failure is not None:
            error = {"reason": failure[0], "failed_step": failure[1]}
        return {
            "identity": identity,
            "strategy": material.strategy,
            "tier": material.tier,
            "status": "failed" if failure is not None else "success",
            "duration_seconds": max(0.0, duration),
            "steps": steps,  # type: ignore[dict-item]
            "probe": probe,  # type: ignore[dict-item]
            "error": error,
        }

    # ------------------------------------------------------------ 生命周期

    async def _close_client(self, client: HttpClient, owns: bool, identity: str) -> None:
        if not owns or not hasattr(client, "close"):
            return
        try:
            await client.close()  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning(
                "login client close failed identity=%s exc_type=%s", identity, type(exc).__name__
            )

    async def close(self) -> None:
        """执行结束：关闭全部专用 client；可重复调用。"""
        if self._closed:
            return
        self._closed = True
        entries, self._entries = self._entries, {}
        for entry in entries.values():
            await self._close_client(entry.client, entry.owns_client, entry.identity)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    # SecretStr 的 str() 是 ***；其它对象一律字符串化。
    return str(value)


__all__ = [
    "LOGIN_REF_RE",
    "LoginFailedError",
    "SessionEntry",
    "SessionPool",
    "preflight_login",
    "validate_strategies",
]
