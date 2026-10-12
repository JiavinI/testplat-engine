"""Engine 执行入口：单 API 执行 Module + Case 串行执行接线。

本模块实现公开 ``EnginePort``：消费 Control 已解析的环境配置，加载资产、
渲染变量、发送 HTTP 请求并做断言。不负责读取 ``config_root``、落库或 HTTP 映射。

``execute(request)`` 是唯一公开入口，按请求类型判别：
``ApiExecutionRequest`` → 单 API（0.1 行为不变）；
``CaseExecutionRequest`` → ``CaseRunner`` 串行执行，``api`` 步骤委托回本模块的单 API 路径。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
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
from testplat_engine.engine.case_runner import CaseRunner
from testplat_engine.engine.resolver import resolve_variables
from testplat_engine.interface import (
    ApiExecutionOutcome,
    ApiExecutionRequest,
    AssetNotFoundError,
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
    _thaw_json,
)
from testplat_engine.plugins.httpx_client import HttpxClient
from testplat_engine.ports.http_client import HttpClient, HttpResponse
from testplat_engine.ports.step_executor import StepExecutorRegistry

logger = logging.getLogger(__name__)

# 公开结果中必须脱敏的请求/响应头；完整值只允许进入日志链路。
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
    }
)


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

    def __init__(self, http_client: HttpClient | None = None) -> None:
        self._injected_client = http_client
        # 第一批 registry 只注册 api；其他 type 已由 Case 契约层在 preflight 拒绝。
        registry = StepExecutorRegistry()
        registry.register("api", ApiStepExecutor(self))
        self._case_runner = CaseRunner(registry)

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
                case, _thaw_json(request.params)  # type: ignore[arg-type]
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
        )

    def preflight_api(
        self, api_id: str, assets_root: Path, environment: Mapping[str, JsonValue]
    ) -> None:
        """只做资产加载与 host 解析，不发请求；供 Case 在执行前整体预检。"""
        api = self._load_api(api_id, assets_root)
        self._resolve_base_url(api, environment)

    async def execute_api(
        self, request: ApiExecutionRequest, *, preserve_param_types: bool = False
    ) -> ApiExecutionOutcome:
        """执行单 API，返回业务结果或抛出已分类系统异常。

        ``preserve_param_types`` 仅供 Case 的 api 步骤使用（不属于 ``EnginePort``）：
        默认 False 保持 0.1 的字符串插值行为。
        """
        stage: ErrorStage = "preflight"
        client: HttpClient | None = None
        owns_client = False
        public_request: JsonObject | None = None
        # 已分类异常；finally 中 close 失败时不得覆盖它。
        classified: EngineError | None = None

        try:
            api = self._load_api(request.api_id, request.assets_root)
            base_url = self._resolve_base_url(api, request.environment)
            full_url = f"{base_url}{api.url}"
            if preserve_param_types:
                # 冻结快照含 tuple/MappingProxy，渲染前解冻以便整字段占位符回填纯 JSON。
                resolved = resolve_variables(
                    api, _thaw_json(request.params), preserve_types=True  # type: ignore[arg-type]
                )
            else:
                resolved = resolve_variables(api, dict(request.params))
            public_request = self._build_public_request(
                method=resolved["method"],
                url=full_url,
                headers=resolved.get("headers"),
                body=resolved.get("body"),
            )

            # 先拿到 HTTP client；_acquire_client 失败仍属 preflight/internal-preflight。
            # 仅在 client 就绪后、真实 HTTP 调用前切换到 execution。
            client, owns_client = self._acquire_client()
            stage = "execution"
            response = await client.request(
                method=resolved["method"],
                url=full_url,
                json=resolved.get("body"),
                headers=resolved.get("headers"),
                timeout=request.timeout_seconds,
            )
            public_response = self._build_public_response(response)
            success = check_success(response, resolved.get("success", api.success))

            if success:
                return ApiExecutionOutcome(
                    status="success",
                    request=public_request,
                    response=public_response,
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
            )

        except EngineError as exc:
            # 已分类异常直接上抛，避免再次包装丢失 kind/stage。
            classified = exc
            raise
        except Exception as exc:
            classified = self._map_exception(exc, stage)
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
                        raise EngineInternalError(
                            stage,
                            log_reason=(
                                f"client.close failed: {type(close_exc).__name__}: {close_exc}"
                            ),
                        ) from close_exc

    def _acquire_client(self) -> tuple[HttpClient, bool]:
        """获取 HTTP client；返回 (client, owns_client)。"""
        if self._injected_client is not None:
            return self._injected_client, False
        return HttpxClient(), True

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

    def _resolve_base_url(
        self, api: Api, environment: Mapping[str, JsonValue]
    ) -> str:
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

    def _build_public_request(
        self,
        *,
        method: str,
        url: str,
        headers: Any,
        body: Any,
    ) -> JsonObject:
        """构造可 JSON 序列化且已脱敏的公开请求快照。"""
        return {
            "method": method,
            "url": url,
            "headers": self._sanitize_headers(headers),
            "body": self._json_safe(body),
        }

    def _build_public_response(self, response: HttpResponse) -> JsonObject:
        """构造可 JSON 序列化且已脱敏的公开响应快照。"""
        return {
            "status_code": response.status_code,
            "headers": self._sanitize_headers(response.headers),
            "body": self._json_safe(response.body),
        }

    def _sanitize_headers(self, headers: Any) -> JsonObject:
        """复制请求/响应头，敏感名替换为占位符，避免 Authorization/Cookie 泄漏。"""
        if not isinstance(headers, Mapping):
            return {}
        sanitized: JsonObject = {}
        for key, value in headers.items():
            name = str(key)
            if name.lower() in _SENSITIVE_HEADER_NAMES:
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



def create_engine() -> SingleApiEngine:
    """公开工厂：返回默认 ``SingleApiEngine``，供 Control 包适配器解析。

    与直接构造 ``SingleApiEngine()`` 等价；提供稳定可发现入口，
    避免 Control 依赖模块级 ``execute`` 或私有路径。
    """
    return SingleApiEngine()


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
