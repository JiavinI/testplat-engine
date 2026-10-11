"""Control 与 Engine 之间的单 API 执行协议。

本模块只定义数据边界和异常语义，不负责加载资产、发送请求或保存执行记录。
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
ExecutionStatus: TypeAlias = Literal["success", "failed"]
ErrorStage: TypeAlias = Literal["preflight", "execution"]
_ERROR_STAGES: dict[str, frozenset[ErrorStage]] = {
    "asset_not_found": frozenset({"preflight"}),
    "invalid_asset": frozenset({"preflight"}),
    "invalid_environment": frozenset({"preflight"}),
    "timeout": frozenset({"execution"}),
    "transport": frozenset({"execution"}),
    "unavailable": frozenset({"preflight"}),
    # internal 由实际抛出点决定阶段，其他 kind 的阶段则属于稳定协议。
    "internal": frozenset({"preflight", "execution"}),
}


def _json_value(value: object, field_name: str) -> JsonValue:
    """复制并验证 JSON 值，避免把 Path、异常或 HTTP 对象泄漏到协议中。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} 不能包含 NaN 或无穷浮点数")
        return value
    if isinstance(value, list):
        return [_json_value(item, field_name) for item in value]
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} 的 JSON 对象键必须是字符串")
            result[key] = _json_value(item, field_name)
        return result
    raise TypeError(f"{field_name} 必须只包含 JSON 可序列化值")


def _json_object(value: object, field_name: str) -> JsonObject:
    """验证协议中要求为对象的字段，并复制为普通 dict。"""
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} 必须是 JSON 对象")
    result = _json_value(value, field_name)
    assert isinstance(result, dict)
    return result


@dataclass(frozen=True, slots=True)
class ApiExecutionRequest:
    """单 API 执行输入；Control 传入已解析的环境配置。"""

    api_id: str
    assets_root: Path
    environment: Mapping[str, JsonValue]
    params: Mapping[str, JsonValue]
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.api_id:
            raise ValueError("api_id 不能为空")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是有限正数")
        object.__setattr__(self, "environment", _json_object(self.environment, "environment"))
        object.__setattr__(self, "params", _json_object(self.params, "params"))

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示。"""
        return {
            "api_id": self.api_id,
            "assets_root": str(self.assets_root),
            "environment": dict(self.environment),
            "params": dict(self.params),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ApiExecutionOutcome:
    """单 API 业务结果；断言失败通过此对象返回，而不是抛异常。"""

    status: ExecutionStatus
    request: JsonObject | None = None
    response: JsonValue | None = None
    error: JsonObject | None = None

    def __post_init__(self) -> None:
        if self.status not in ("success", "failed"):
            raise ValueError("status 必须是 success 或 failed")
        if self.status == "failed" and not self.error:
            raise ValueError("业务 failed 结果必须包含 error")
        if self.status == "success" and self.error is not None:
            raise ValueError("业务 success 结果不能包含 error")
        if self.request is not None:
            object.__setattr__(self, "request", _json_object(self.request, "request"))
        if self.response is not None:
            object.__setattr__(self, "response", _json_value(self.response, "response"))
        if self.error is not None:
            object.__setattr__(self, "error", _json_object(self.error, "error"))

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示。"""
        return {
            "status": self.status,
            "request": self.request,
            "response": self.response,
            "error": self.error,
        }


class EnginePort(Protocol):
    """唯一的 Engine 执行 seam；不依赖 Control、ORM 或 Web 框架。"""

    async def execute(self, request: ApiExecutionRequest) -> ApiExecutionOutcome:
        """执行单 API，并返回业务结果或抛出已分类系统异常。"""
        ...


class EngineError(Exception):
    """公开的 Engine 系统错误，`log_reason` 永远不进入外显 payload。"""

    kind: str
    stage: ErrorStage
    public_message: str
    log_reason: str | None

    def __init__(
        self,
        kind: str,
        stage: ErrorStage,
        public_message: str,
        *,
        log_reason: str | None = None,
    ) -> None:
        if not public_message:
            raise ValueError("public_message 不能为空")
        allowed_stages = _ERROR_STAGES.get(kind)
        if allowed_stages is None:
            raise ValueError(f"不支持的 Engine error kind: {kind}")
        if stage not in ("preflight", "execution"):
            raise ValueError(f"不支持的 Engine error stage: {stage}")
        if stage not in allowed_stages:
            raise ValueError(f"Engine error kind {kind} 不允许 stage {stage}")
        self.kind = kind
        self.stage = stage
        self.public_message = public_message
        self.log_reason = log_reason
        super().__init__(public_message)

    def to_dict(self) -> JsonObject:
        """返回安全的错误外显；底层原因只用于日志链路。"""
        return {"kind": self.kind, "stage": self.stage, "message": self.public_message}


class AssetNotFoundError(EngineError):
    def __init__(
        self, public_message: str = "API 资产不存在", *, log_reason: str | None = None
    ) -> None:
        super().__init__("asset_not_found", "preflight", public_message, log_reason=log_reason)


class InvalidAssetError(EngineError):
    def __init__(
        self, public_message: str = "API 资产无效", *, log_reason: str | None = None
    ) -> None:
        super().__init__("invalid_asset", "preflight", public_message, log_reason=log_reason)


class InvalidEnvironmentError(EngineError):
    def __init__(
        self, public_message: str = "执行环境配置无效", *, log_reason: str | None = None
    ) -> None:
        super().__init__("invalid_environment", "preflight", public_message, log_reason=log_reason)


class EngineTimeoutError(EngineError):
    def __init__(
        self, public_message: str = "API 请求超时", *, log_reason: str | None = None
    ) -> None:
        super().__init__("timeout", "execution", public_message, log_reason=log_reason)


class EngineTransportError(EngineError):
    def __init__(
        self, public_message: str = "API 传输失败", *, log_reason: str | None = None
    ) -> None:
        super().__init__("transport", "execution", public_message, log_reason=log_reason)


class EngineUnavailableError(EngineError):
    def __init__(
        self, public_message: str = "执行引擎不可用", *, log_reason: str | None = None
    ) -> None:
        super().__init__("unavailable", "preflight", public_message, log_reason=log_reason)


class EngineInternalError(EngineError):
    def __init__(
        self,
        stage: ErrorStage,
        public_message: str = "执行引擎内部错误",
        *,
        log_reason: str | None = None,
    ) -> None:
        super().__init__("internal", stage, public_message, log_reason=log_reason)


__all__ = [
    "ApiExecutionOutcome",
    "ApiExecutionRequest",
    "AssetNotFoundError",
    "EngineError",
    "EngineInternalError",
    "EnginePort",
    "EngineTimeoutError",
    "EngineTransportError",
    "EngineUnavailableError",
    "InvalidAssetError",
    "InvalidEnvironmentError",
    "JsonObject",
    "JsonValue",
]
