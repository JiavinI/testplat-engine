"""Engine 执行入口：单 API 执行 Module + Case 串行执行接线。

本模块实现公开 ``EnginePort``：消费 Control 已解析的环境配置，加载资产、
渲染变量、发送 HTTP 请求并做断言。不负责读取 ``config_root``、落库或 HTTP 映射。

``execute(request)`` 是唯一公开入口，按请求类型判别：
``ApiExecutionRequest`` → 单 API（0.1 行为不变）；
``CaseExecutionRequest`` → ``CaseRunner`` 串行执行，``api`` 步骤委托回本模块的单 API 路径。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, overload

import httpx
import yaml

from testplat_engine.adapters.filesystem.case_loader import validate_case_request_params
from testplat_engine.adapters.filesystem.registry import Registry
from testplat_engine.domain.api import Api
from testplat_engine.domain.result import ApiResult
from testplat_engine.engine.api_step_executor import ApiStepExecutor
from testplat_engine.engine.assert_engine import check_success
from testplat_engine.engine.auth_injection import (
    ResolvedAuth,
    merge_headers,
    public_auth,
    resolve_identity,
    secret_values,
)
from testplat_engine.engine.case_runner import CaseRunner
from testplat_engine.engine.login_cache import LoginCache
from testplat_engine.engine.login_render import render_headers
from testplat_engine.engine.login_session import (
    LoginFailedError,
    SessionEntry,
    SessionPool,
    preflight_login,
    validate_strategies,
)
from testplat_engine.engine.resolver import resolve_variables
from testplat_engine.interface import (
    ApiExecutionOutcome,
    ApiExecutionRequest,
    AssetNotFoundError,
    AuthTable,
    CaseExecutionOutcome,
    CaseExecutionRequest,
    EngineError,
    EngineInternalError,
    EngineTimeoutError,
    EngineTransportError,
    ErrorStage,
    InvalidAssetError,
    InvalidEnvironmentError,
    JsonObject,
    JsonValue,
    LoginAuth,
    LoginStrategy,
    _thaw_json,
)
from testplat_engine.plugins.httpx_client import HttpxClient
from testplat_engine.ports.http_client import HttpClient, HttpResponse
from testplat_engine.ports.step_executor import StepExecutorRegistry
from testplat_engine.shared.redact import (
    is_sensitive_key,
    redact_json,
    redact_secret_text,
    redact_secret_values,
)
from testplat_engine.shared.request_body import has_header, public_body, send_kwargs

logger = logging.getLogger(__name__)

# 请求/响应头的敏感名单只有一个来源：shared.redact._SENSITIVE_KEYS（H1）。
# 敏感头的值不进入公开结果，也**不进入日志**：本模块任何日志都不记录 header 值，
# 只记 kind、stage、异常类型名（旧注释声称完整值可进入日志，与日志硬约束矛盾，已改正）。


class SingleApiEngine:
    """实现 ``EnginePort`` 的单 API 执行 Module。

    配置所有权：
    - ``assets_root`` / ``api_id``：由本引擎通过 Registry 加载 YAML 资产。
    - ``environment``：由 Control 从 ``project.config_root`` 解析后传入；
      本引擎只消费其中的 ``api_hosts``，绝不从 ``assets_root.parent`` 猜测配置目录。
    - ``timeout_seconds``：必须原样传到 HTTP client，确保超时真正生效。

    可选注入 ``HttpClient`` 便于单元测试；未注入时按次创建 ``HttpxClient``，
    并在 ``finally`` 中关闭，避免连接池泄漏。Case 的每个 ``api`` 步骤各自走一次该
    生命周期，所以任何步骤失败都不会遗留本引擎创建的连接。
    """

    def __init__(
        self,
        http_client: HttpClient | None = None,
        *,
        login_strategies: Sequence[LoginStrategy] = (),
        client_factory: Callable[[], HttpClient] | None = None,
        login_cache_max_entries: int = 256,
    ) -> None:
        self._injected_client = http_client
        # 测试 seam：每次需要新 client 时调用（例如包了 httpx.MockTransport 的 HttpxClient）；
        # 生产路径为 None → 每次新建 HttpxClient。注入的 http_client 优先于工厂。
        self._client_factory = client_factory
        # 内置 generic_flow 总是注册；插件重名 / 与内置冲突 → 启动期 ValueError（只含策略名）。
        self._strategies = validate_strategies(login_strategies)
        # 3.3d：Engine 实例持有一份跨执行登录缓存；0 = 关闭（行为等同 0.4.0）。
        self._login_cache = LoginCache(login_cache_max_entries)
        # 第一批 registry 只注册 api；其他 type 已由 Case 契约层在 preflight 拒绝。
        registry = StepExecutorRegistry()
        registry.register("api", ApiStepExecutor(self))
        self._case_runner = CaseRunner(registry, session_factory=self.new_session_pool)

    @overload
    async def execute(self, request: ApiExecutionRequest) -> ApiExecutionOutcome: ...

    @overload
    async def execute(self, request: CaseExecutionRequest) -> CaseExecutionOutcome: ...

    async def execute(
        self, request: ApiExecutionRequest | CaseExecutionRequest
    ) -> ApiExecutionOutcome | CaseExecutionOutcome:
        """唯一公开入口：按请求类型执行单 API 或串行 Case。"""
        if isinstance(request, CaseExecutionRequest):
            return await self._execute_case(request)
        if isinstance(request, ApiExecutionRequest):
            return await self.execute_api(request)
        raise EngineInternalError(
            "preflight",
            log_reason=f"unsupported request type: {type(request).__name__}",
        )

    async def _execute_case(self, request: CaseExecutionRequest) -> CaseExecutionOutcome:
        """加载并校验 Case（preflight，不产生执行结果），再交给 CaseRunner。"""
        try:
            registry = Registry(assets_root=request.assets_root)
            case = registry.load_case(request.case_id)
            case_params = validate_case_request_params(
                case,
                _thaw_json(request.params),  # type: ignore[arg-type]
            )
        except EngineError:
            raise
        except Exception as exc:
            raise EngineInternalError(
                "preflight", log_reason=f"{type(exc).__name__}: {exc}"
            ) from exc
        return await self._case_runner.run(
            case,
            assets_root=request.assets_root,
            environment=_thaw_json(request.environment),  # type: ignore[arg-type]
            case_params=case_params,
            timeout_seconds=request.timeout_seconds,
            auth=request.auth,  # H2：身份表逐层下传，CaseRunner 本身不解析身份
        )

    def preflight_api(
        self,
        api_id: str,
        assets_root: Path,
        environment: Mapping[str, JsonValue],
        *,
        auth: AuthTable | None = None,
    ) -> None:
        """只做资产加载、host 与身份解析，不发请求；供 Case 在执行前整体预检。

        3.3b：login 身份另做零副作用预检（策略已注册、validate()、登录 host、inject 引用来源）。
        """
        api = self._load_api(api_id, assets_root)
        self._resolve_base_url(api, environment)
        resolved_auth = resolve_identity(api, auth)
        if resolved_auth is not None and isinstance(resolved_auth.material, LoginAuth):
            preflight_login(
                resolved_auth.identity, resolved_auth.material, environment, self._strategies
            )

    def new_session_pool(self) -> SessionPool:
        """每次执行一个会话池（Q6：只在 Engine 内部流转，不进公开 DTO）。"""
        return SessionPool(self._strategies, self._acquire_login_client, cache=self._login_cache)

    async def execute_api(
        self,
        request: ApiExecutionRequest,
        *,
        preserve_param_types: bool = False,
        session_pool: SessionPool | None = None,
    ) -> ApiExecutionOutcome:
        """执行单 API，返回业务结果或抛出已分类系统异常。

        ``preserve_param_types`` 仅供 Case 的 api 步骤使用（不属于 ``EnginePort``）：
        默认 False 保持 0.1 的字符串插值行为。
        ``session_pool`` 仅供 Case 路径传入执行级会话池（Q6）；单 API 调用时本方法自建并在
        结束时关闭，``logins`` 随单 API 结果返回。
        """
        own_pool = session_pool is None
        pool = self.new_session_pool() if session_pool is None else session_pool
        try:
            return await self._execute_api(request, preserve_param_types, pool, own_pool)
        finally:
            if own_pool:
                await pool.close()

    async def _execute_api(
        self,
        request: ApiExecutionRequest,
        preserve_param_types: bool,
        pool: SessionPool,
        own_pool: bool,
    ) -> ApiExecutionOutcome:
        stage: ErrorStage = "preflight"
        client: HttpClient | None = None
        owns_client = False
        public_request: JsonObject | None = None
        # 已分类异常；finally 中 close 失败时不得覆盖它。
        classified: EngineError | None = None
        # 本次执行的按值替换集合（H1：执行级并集，挂在会话池上；只在本次执行内存活）。
        secrets: tuple[str, ...] = pool.snapshot_secrets()

        def logins() -> list[JsonObject]:
            # Case 路径的登录记录挂在 Case 结果上，步骤级不重复携带。
            return pool.public_logins() if own_pool else []

        try:
            api = self._load_api(request.api_id, request.assets_root)
            base_url = self._resolve_base_url(api, request.environment)
            # 身份解析属于预检：失败抛 invalid_environment，此时尚未发出任何请求。
            resolved_auth = resolve_identity(api, request.auth)
            login_material: LoginAuth | None = None
            login_host = ""
            if resolved_auth is not None and isinstance(resolved_auth.material, LoginAuth):
                login_material = resolved_auth.material
                login_host = preflight_login(
                    resolved_auth.identity, login_material, request.environment, self._strategies
                )
            pool.add_secrets(secret_values(resolved_auth))
            secrets = pool.snapshot_secrets()
            full_url = f"{base_url}{api.url}"
            if preserve_param_types:
                # 冻结快照含 tuple/MappingProxy，渲染前解冻以便整字段占位符回填纯 JSON。
                resolved = resolve_variables(
                    api,
                    _thaw_json(request.params),
                    preserve_types=True,  # type: ignore[arg-type]
                )
            else:
                resolved = resolve_variables(api, dict(request.params))
            body_type = resolved.get("body_type", "json")

            # 3.3b 按需登录（D8）：只有用到 login 身份的这一步才会触发登录。
            entry: SessionEntry | None = None
            login_headers: dict[str, str] | None = None
            reauthenticated = False
            cache_scope = request.auth.cache_scope if request.auth is not None else None
            if login_material is not None:
                assert resolved_auth is not None
                stage = "execution"
                try:
                    entry = await pool.ensure(
                        resolved_auth.identity,
                        login_material,
                        login_host,
                        timeout_seconds=request.timeout_seconds,
                        connections=request.auth.connections,  # type: ignore[union-attr,arg-type]
                        cache_scope=cache_scope,
                    )
                except LoginFailedError as failure:
                    # D9：登录失败是业务结果（200 + 保存），不是系统异常。
                    return ApiExecutionOutcome(
                        status="failed",
                        error=failure.error_payload(),
                        auth=public_auth(
                            resolved_auth,
                            [],
                            login_ref=failure.login_ref,
                            session_source="login",
                            reauthenticated=False,
                        ),
                        logins=logins(),
                    )
                login_headers = render_headers(
                    login_material.inject_headers, entry.values, login_host
                )
                secrets = pool.snapshot_secrets()

            send_headers, public_headers, overridden = merge_headers(
                resolved.get("headers"), resolved_auth, login_headers
            )
            body_kwargs, send_headers = send_kwargs(
                body_type, resolved.get("body"), send_headers, resolved.get("content_type")
            )
            if body_type == "raw" and send_headers is not None:
                # Q4：raw 自动补的 Content-Type 同样出现在公开快照中（非秘密）。
                public_headers = {
                    **(public_headers or {}),
                    **{
                        k: v
                        for k, v in send_headers.items()
                        if k.lower() == "content-type" and not has_header(public_headers, k)
                    },
                }

            # 先拿到 HTTP client；_acquire_client 失败仍属 preflight/internal-preflight。
            # 仅在 client 就绪后、真实 HTTP 调用前切换到 execution。
            # login 身份使用会话池中的专用 client（自带 cookie jar，由池负责关闭）。
            if entry is not None:
                client, owns_client = entry.client, False
            else:
                client, owns_client = self._acquire_client()
            stage = "execution"
            response = await client.request(
                method=resolved["method"],
                url=full_url,
                # 注入点：凭证只出现在本次调用的 headers 参数里，不修改 client 的共享状态。
                headers=send_headers,
                timeout=request.timeout_seconds,
                **body_kwargs,
            )

            # 3.3d §3.2：业务 401 → 作废会话 → 重登 → 同方法/URL/body 重发一次。
            # 公开 request/response 只保留第二次；第一次 401 响应体不进公开结果与日志。
            if (
                entry is not None
                and login_material is not None
                and resolved_auth is not None
                and login_material.reauth_on_401
                and response.status_code == 401
            ):
                logger.info("reauth identity=%s trigger_status=401", resolved_auth.identity)
                await pool.invalidate_identity(resolved_auth.identity)
                reauthenticated = True
                try:
                    entry = await pool.ensure(
                        resolved_auth.identity,
                        login_material,
                        login_host,
                        timeout_seconds=request.timeout_seconds,
                        connections=request.auth.connections,  # type: ignore[union-attr,arg-type]
                        cache_scope=cache_scope,
                    )
                except LoginFailedError as failure:
                    return ApiExecutionOutcome(
                        status="failed",
                        error=failure.error_payload(),
                        auth=public_auth(
                            resolved_auth,
                            [],
                            login_ref=failure.login_ref,
                            session_source="login",
                            reauthenticated=True,
                        ),
                        logins=logins(),
                    )
                login_headers = render_headers(
                    login_material.inject_headers, entry.values, login_host
                )
                secrets = pool.snapshot_secrets()
                send_headers, public_headers, overridden = merge_headers(
                    resolved.get("headers"), resolved_auth, login_headers
                )
                body_kwargs, send_headers = send_kwargs(
                    body_type,
                    resolved.get("body"),
                    send_headers,
                    resolved.get("content_type"),
                )
                if body_type == "raw" and send_headers is not None:
                    public_headers = {
                        **(public_headers or {}),
                        **{
                            k: v
                            for k, v in send_headers.items()
                            if k.lower() == "content-type" and not has_header(public_headers, k)
                        },
                    }
                client, owns_client = entry.client, False
                response = await client.request(
                    method=resolved["method"],
                    url=full_url,
                    headers=send_headers,
                    timeout=request.timeout_seconds,
                    **body_kwargs,
                )

            if entry is not None:
                # D12：Cookie 身份的业务响应同样更新 jar；新 Cookie 值先入集合再构造快照。
                pool.absorb_cookies(entry.client)
                secrets = pool.snapshot_secrets()

            cookie_names = pool.cookie_names(entry) if entry is not None else []
            public_request = self._redact_values(
                self._build_public_request(
                    method=resolved["method"],
                    url=full_url,
                    headers=public_headers,
                    body=resolved.get("body"),
                    body_type=body_type,
                    extra_sensitive=self._injected_names(resolved_auth),
                ),
                secrets,
            )
            auth_info = public_auth(
                resolved_auth,
                overridden,
                cookie_names=cookie_names,
                login_ref=None if entry is None else entry.login_ref,
                session_source=None if entry is None else entry.session_source,
                reauthenticated=reauthenticated,
            )
            public_response = self._redact_values(
                self._build_public_response(
                    response, extra_sensitive=self._injected_names(resolved_auth)
                ),
                secrets,
            )
            success = check_success(response, resolved.get("success", api.success))

            if success:
                return ApiExecutionOutcome(
                    status="success",
                    request=public_request,
                    response=public_response,
                    auth=auth_info,
                    logins=logins(),
                )

            # 断言失败是业务结果，不是系统异常；Control 应记为 HTTP 200 + failed。
            return ApiExecutionOutcome(
                status="failed",
                request=public_request,
                response=public_response,
                error={
                    "kind": "assertion",
                    "message": "断言失败: 响应不符合成功条件",
                },
                auth=auth_info,
                logins=logins(),
            )

        except EngineError as exc:
            # 已分类异常直接上抛，避免再次包装丢失 kind/stage。
            classified = self._redact_error(exc, secrets)
            raise
        except Exception as exc:
            classified = self._redact_error(self._map_exception(exc, stage), secrets)
            # 日志只写安全字段：原始异常文本（可能夹带 password/token/Cookie 值）
            # 与 traceback（exc_info 会格式化异常消息）一律不进日志；
            # 完整原因仅保留在 classified.log_reason 与 __cause__ 链上，由调用方自行决策。
            logger.error(
                "engine execute failed kind=%s stage=%s exc_type=%s",
                classified.kind,
                classified.stage,
                type(exc).__name__,
            )
            raise classified from exc
        finally:
            # 仅关闭本引擎创建的 client；注入的 mock/共享 client 由调用方管理生命周期。
            # close 失败不得覆盖已分类的 timeout/transport；成功路径 close 失败归一为 internal。
            if owns_client and client is not None and hasattr(client, "close"):
                try:
                    await client.close()  # type: ignore[misc]
                except Exception as close_exc:
                    if classified is not None:
                        # 只记异常类型名，不记 close 异常文本（同样可能含敏感值）。
                        logger.warning(
                            "http client close failed after classified error "
                            "kind=%s stage=%s exc_type=%s",
                            classified.kind,
                            classified.stage,
                            type(close_exc).__name__,
                        )
                    else:
                        raise self._redact_error(
                            EngineInternalError(
                                stage,
                                log_reason=(
                                    f"client.close failed: {type(close_exc).__name__}: {close_exc}"
                                ),
                            ),
                            secrets,
                        ) from close_exc

    def _acquire_client(self) -> tuple[HttpClient, bool]:
        """获取 HTTP client；返回 (client, owns_client)。匿名 / 静态 header 身份每次新建。"""
        if self._injected_client is not None:
            return self._injected_client, False
        if self._client_factory is not None:
            return self._client_factory(), True
        return HttpxClient(), True

    def _acquire_login_client(self) -> tuple[HttpClient, bool]:
        """login 身份的专用 client（每身份每次登录一个，cookie jar 随之隔离）。

        注入了共享 ``http_client``（旧测试 seam）时只能复用它，此时不具备 jar 隔离；
        需要隔离的测试应使用 ``client_factory``。
        """
        return self._acquire_client()

    def _load_api(self, api_id: str, assets_root: Any) -> Api:
        """加载资产，并把文件系统层异常映射为公开异常族。"""
        registry = Registry(assets_root=assets_root)
        try:
            return registry.load_api(api_id)
        except FileNotFoundError as exc:
            raise AssetNotFoundError(
                public_message=f"API 资产不存在: {api_id}",
                log_reason=str(exc),
            ) from exc
        except ValueError as exc:
            raise InvalidAssetError(
                public_message=f"API 资产无效: {api_id}",
                log_reason=str(exc),
            ) from exc

    def _resolve_base_url(self, api: Api, environment: Mapping[str, JsonValue]) -> str:
        """从已解析环境中取 host；缺失或类型错误一律 invalid_environment。"""
        api_hosts = environment.get("api_hosts")
        if not isinstance(api_hosts, Mapping):
            raise InvalidEnvironmentError(
                public_message="执行环境缺少 api_hosts 配置",
                log_reason=f"api_hosts type={type(api_hosts).__name__}",
            )
        raw = api_hosts.get(api.host)
        if not isinstance(raw, str) or not raw.strip():
            raise InvalidEnvironmentError(
                public_message=f"执行环境缺少 host: {api.host}",
                log_reason=f"host={api.host!r} value={raw!r}",
            )
        return raw.rstrip("/")

    def _map_exception(self, exc: Exception, stage: ErrorStage) -> EngineError:
        """把底层异常集中归一到公开异常族。

        分类原则：
        - 可判别的 timeout / transport → 对应 kind，stage=execution。
        - 其余未知缺陷 → internal，stage 取实际抛出点（preflight 或 execution）。
        - 绝不把系统异常吞成业务 failed。
        """
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            return EngineTimeoutError(log_reason=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, (ConnectionError, httpx.RequestError)):
            return EngineTransportError(log_reason=f"{type(exc).__name__}: {exc}")
        return EngineInternalError(
            stage,
            log_reason=f"{type(exc).__name__}: {exc}",
        )

    @staticmethod
    def _injected_names(resolved_auth: ResolvedAuth | None) -> frozenset[str]:
        """本次注入的 header 名（小写）；不论是否在敏感名单中，都按敏感处理（H1）。"""
        if resolved_auth is None:
            return frozenset()
        return frozenset(name.lower() for name in resolved_auth.header_names)

    @staticmethod
    def _redact_values(snapshot: JsonObject, secrets: tuple[str, ...]) -> JsonObject:
        """按值替换：在按键脱敏**之后**执行，覆盖嵌套字符串中的子串。"""
        if not secrets:
            return snapshot
        keyed = redact_json(snapshot)
        result = redact_secret_values(keyed, secrets)
        assert isinstance(result, dict)
        return result

    @staticmethod
    def _redact_error(error: EngineError, secrets: tuple[str, ...]) -> EngineError:
        """已分类异常的 public_message / log_reason 也做按值替换。

        正常路径下二者本就不含秘密（固定文案 + 类型名）；这里兜底防止底层异常文本
        （例如 httpx 错误描述）夹带凭证后经 log_reason 被调用方误用。
        """
        if not secrets:
            return error
        error.public_message = redact_secret_text(error.public_message, secrets)
        if error.log_reason is not None:
            error.log_reason = redact_secret_text(error.log_reason, secrets)
        return error

    def _build_public_request(
        self,
        *,
        method: str,
        url: str,
        headers: Any,
        body: Any,
        body_type: str = "json",
        extra_sensitive: frozenset[str] = frozenset(),
    ) -> JsonObject:
        """构造可 JSON 序列化且已按名脱敏的公开请求快照（按值替换由调用方叠加）。

        3.3b：新增 ``body_type``（M4）；query 类型的参数不进 ``url``，单独放 ``query``（Q8）。
        """
        snapshot: JsonObject = {
            "method": method,
            "url": url,
            "headers": self._sanitize_headers(headers, extra_sensitive),
        }
        for key, value in public_body(body_type, body).items():
            snapshot[key] = self._json_safe(value)
        return snapshot

    def _build_public_response(
        self, response: HttpResponse, *, extra_sensitive: frozenset[str] = frozenset()
    ) -> JsonObject:
        """构造可 JSON 序列化且已按名脱敏的公开响应快照（按值替换由调用方叠加）。"""
        return {
            "status_code": response.status_code,
            "headers": self._sanitize_headers(response.headers, extra_sensitive),
            "body": self._json_safe(response.body),
        }

    def _sanitize_headers(
        self, headers: Any, extra_sensitive: frozenset[str] = frozenset()
    ) -> JsonObject:
        """复制请求/响应头，敏感名替换为占位符。

        敏感名 = 统一名单（``is_sensitive_key``）∪ 本次注入的凭证头名（``extra_sensitive``，
        小写）。后者让 ``X-Tenant-Token`` 这类自定义凭证头同样被遮蔽。
        """
        if not isinstance(headers, Mapping):
            return {}
        sanitized: JsonObject = {}
        for key, value in headers.items():
            name = str(key)
            if is_sensitive_key(name) or name.lower() in extra_sensitive:
                sanitized[name] = "***"
            else:
                sanitized[name] = str(value)
        return sanitized

    def _json_safe(self, value: Any) -> JsonValue:
        """把响应体收敛为 JSON 可序列化结构；非 JSON 文本保持为字符串。"""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        if isinstance(value, Mapping):
            return {str(k): self._json_safe(v) for k, v in value.items()}
        return str(value)


def create_engine(
    *,
    login_strategies: Sequence[LoginStrategy] = (),
    login_cache_max_entries: int = 256,
) -> SingleApiEngine:
    """公开工厂：返回 ``SingleApiEngine``，供 Control 包适配器解析。

    ``login_strategies``（L1/E1）：Control 启动时把“entry point ∩ 白名单”的插件实例传入；
    内置 ``generic_flow`` 总是注册。重名或与内置冲突 → ``ValueError``（消息只含策略名）。
    ``login_cache_max_entries``（3.3d）：跨执行登录缓存容量；``0`` 关闭（等同 0.4.0）；
    负数 → ``ValueError``。无参调用默认开启容量 256 的缓存（无 ``cache_scope`` 时不写入）。
    """
    return SingleApiEngine(
        login_strategies=login_strategies,
        login_cache_max_entries=login_cache_max_entries,
    )


async def run_api(
    api_id: str,
    registry: Registry,
    env: str,
    params: dict[str, Any] | None = None,
) -> ApiResult:
    """兼容旧入口：从资产旁/内的 env 文件加载配置后委托 ``SingleApiEngine``。

    新 Control 集成应使用 ``SingleApiEngine.execute``，由 Control 传入已解析
    ``environment``，不再依赖本函数猜测 ``config_root``。
    """
    env_config = _load_env_config_legacy(registry, env)
    engine = SingleApiEngine()
    request = ApiExecutionRequest(
        api_id=api_id,
        assets_root=registry.assets_root,
        environment=env_config,
        params=params or {},
    )
    try:
        outcome = await engine.execute(request)
    except EngineError as exc:
        # 旧 ApiResult 只能表达业务 failed；系统错误降级为 failed 文本供示例使用。
        return ApiResult.failed(error=f"{exc.kind}: {exc.public_message}")

    if outcome.status == "success":
        return ApiResult.success(outcome.response)
    error_obj = outcome.error or {}
    message = str(error_obj.get("message") or "业务失败")
    return ApiResult.failed(error=message, response=outcome.response)


def _load_env_config_legacy(registry: Registry, env: str) -> dict[str, Any]:
    """仅供旧 ``run_api`` 使用的环境文件查找。

    查找顺序保留基线行为：先 ``assets_root.parent/config/envs``，再
    ``assets_root/config/envs``。``EnginePort`` 路径不得调用此函数。
    """
    config_path = registry.assets_root.parent / "config" / "envs" / f"{env}.yaml"
    if not config_path.exists():
        config_path = registry.assets_root / "config" / "envs" / f"{env}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"环境配置文件不存在: {config_path}")
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"环境配置必须是字典: {config_path}")
    return data
