"""Control 与 Engine 之间的唯一执行协议。

本模块只定义数据边界和异常语义，不负责加载资产、发送请求或保存执行记录。
0.2 在保持单 API DTO 兼容的同时，冻结 Case 请求/结果与步骤结果不变量。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, TypeAlias, overload

from testplat_engine.shared.redact import redact_json

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
ExecutionStatus: TypeAlias = Literal["success", "failed"]
StepStatus: TypeAlias = Literal["success", "failed", "skipped"]
ErrorStage: TypeAlias = Literal["preflight", "execution"]
_ERROR_STAGES: dict[str, frozenset[ErrorStage]] = {
    "asset_not_found": frozenset({"preflight"}),
    "invalid_asset": frozenset({"preflight"}),
    "invalid_environment": frozenset({"preflight"}),
    # Case 参数在执行前可确定缺失：稳定 kind，Control 映射 HTTP 422。
    "invalid_input": frozenset({"preflight"}),
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


def _freeze_json(value: JsonValue) -> object:
    """深冻结 JSON 快照：dict→MappingProxyType，list→tuple，禁止就地改写。"""
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze_json(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> JsonValue:
    """产出全新可变 JSON 副本，供 to_dict() 公开序列化。"""
    if isinstance(value, Mapping):
        return {str(k): _thaw_json(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if isinstance(value, list):
        return [_thaw_json(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    raise TypeError(f"无法解冻非 JSON 值: {type(value).__name__}")


def _snapshot_object(value: object, field_name: str, *, redact: bool) -> object:
    """复制并可选脱敏后深冻结；用于请求/结果内部不可变快照。"""
    copied = _json_object(value, field_name)
    if redact:
        copied = redact_json(copied)
        assert isinstance(copied, dict)
    return _freeze_json(copied)


def _snapshot_value(value: object, field_name: str, *, redact: bool) -> object:
    """复制并可选脱敏后深冻结任意 JSON 值。"""
    copied = _json_value(value, field_name)
    if redact:
        copied = redact_json(copied)
    return _freeze_json(copied)


def _finite_non_negative_seconds(value: float, field_name: str) -> float:
    """耗时必须是有限非负秒数，防止 NaN/负值进入公开结果。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} 必须是数字")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} 必须是有限非负秒数")
    return number


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
        # 请求快照深冻结；不脱敏，以免破坏后续执行参数解析。
        object.__setattr__(
            self, "environment", _snapshot_object(self.environment, "environment", redact=False)
        )
        object.__setattr__(self, "params", _snapshot_object(self.params, "params", redact=False))

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示。"""
        return {
            "api_id": self.api_id,
            "assets_root": str(self.assets_root),
            "environment": _thaw_json(self.environment),  # type: ignore[arg-type]
            "params": _thaw_json(self.params),  # type: ignore[arg-type]
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
        # 公开结果强制递归脱敏后再冻结，防止嵌套秘密随 to_dict 外泄。
        if self.request is not None:
            object.__setattr__(
                self, "request", _snapshot_object(self.request, "request", redact=True)
            )
        if self.response is not None:
            object.__setattr__(
                self, "response", _snapshot_value(self.response, "response", redact=True)
            )
        if self.error is not None:
            object.__setattr__(self, "error", _snapshot_object(self.error, "error", redact=True))

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示（全新可变副本）。"""
        return {
            "status": self.status,
            "request": None if self.request is None else _thaw_json(self.request),
            "response": None if self.response is None else _thaw_json(self.response),
            "error": None if self.error is None else _thaw_json(self.error),
        }


@dataclass(frozen=True, slots=True)
class CaseExecutionRequest:
    """Case 执行输入；fail-fast 固定开启，调用方不可覆盖。

    Control 只传身份、资产根、已解析环境和可覆盖参数；不携带 ORM/Web 对象。
    """

    case_id: str
    assets_root: Path
    environment: Mapping[str, JsonValue]
    params: Mapping[str, JsonValue]
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id 不能为空")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是有限正数")
        object.__setattr__(
            self, "environment", _snapshot_object(self.environment, "environment", redact=False)
        )
        object.__setattr__(self, "params", _snapshot_object(self.params, "params", redact=False))

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示（全新可变副本）。"""
        return {
            "case_id": self.case_id,
            "assets_root": str(self.assets_root),
            "environment": _thaw_json(self.environment),  # type: ignore[arg-type]
            "params": _thaw_json(self.params),  # type: ignore[arg-type]
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """单个声明步骤的结果；顺序与 Case 资产 steps 一一对应。"""

    step_id: str
    type: str
    status: StepStatus
    duration_seconds: float
    name: str | None = None
    request: JsonObject | None = None
    response: JsonValue | None = None
    error: JsonObject | None = None
    output: str | None = None
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.step_id:
            raise ValueError("step_id 不能为空")
        if not self.type:
            raise ValueError("type 不能为空")
        if self.status not in ("success", "failed", "skipped"):
            raise ValueError("status 必须是 success、failed 或 skipped")
        duration = _finite_non_negative_seconds(self.duration_seconds, "duration_seconds")
        object.__setattr__(self, "duration_seconds", duration)

        if self.status == "success":
            if self.error is not None:
                raise ValueError("success 步骤不能包含 error")
            if self.skip_reason is not None:
                raise ValueError("success 步骤不能包含 skip_reason")
        elif self.status == "failed":
            if not self.error:
                raise ValueError("failed 步骤必须包含 error")
            if self.skip_reason is not None:
                raise ValueError("failed 步骤不能包含 skip_reason")
        else:  # skipped：不得伪造执行证据
            if duration != 0:
                raise ValueError("skipped 步骤的 duration_seconds 必须为 0")
            if self.request is not None or self.response is not None or self.error is not None:
                raise ValueError("skipped 步骤的 request/response/error 必须为 null")
            if self.skip_reason != "fail_fast":
                raise ValueError("skipped 步骤的 skip_reason 必须为 fail_fast")

        # S1：统一在构造期递归脱敏 request/response/error，再深冻结。
        if self.request is not None:
            object.__setattr__(
                self, "request", _snapshot_object(self.request, "request", redact=True)
            )
        if self.response is not None:
            object.__setattr__(
                self, "response", _snapshot_value(self.response, "response", redact=True)
            )
        if self.error is not None:
            object.__setattr__(self, "error", _snapshot_object(self.error, "error", redact=True))

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示（全新可变副本）。"""
        return {
            "step_id": self.step_id,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "request": None if self.request is None else _thaw_json(self.request),
            "response": None if self.response is None else _thaw_json(self.response),
            "error": None if self.error is None else _thaw_json(self.error),
            "output": self.output,
            "skip_reason": self.skip_reason,
        }


def _compute_case_summary(steps: Sequence[StepOutcome]) -> JsonObject:
    """由步骤结果计算汇总；禁止接收调用方自报值。"""
    return {
        "total": len(steps),
        "success": sum(1 for step in steps if step.status == "success"),
        "failed": sum(1 for step in steps if step.status == "failed"),
        "skipped": sum(1 for step in steps if step.status == "skipped"),
    }


def _derive_case_status(steps: Sequence[StepOutcome]) -> ExecutionStatus:
    """Case success 当且仅当全部步骤 success。"""
    if steps and all(step.status == "success" for step in steps):
        return "success"
    return "failed"


@dataclass(frozen=True, slots=True)
class CaseExecutionOutcome:
    """Case 业务结果；summary 由 steps 推导，不接受调用方自报。"""

    case_id: str
    status: ExecutionStatus
    duration_seconds: float
    steps: tuple[StepOutcome, ...]

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id 不能为空")
        if self.status not in ("success", "failed"):
            raise ValueError("status 必须是 success 或 failed")
        duration = _finite_non_negative_seconds(self.duration_seconds, "duration_seconds")
        object.__setattr__(self, "duration_seconds", duration)

        steps = tuple(self.steps)
        if not steps:
            raise ValueError("Case outcome 必须包含至少一个步骤结果")
        object.__setattr__(self, "steps", steps)

        seen: set[str] = set()
        for step in steps:
            if not isinstance(step, StepOutcome):
                raise TypeError("steps 元素必须是 StepOutcome")
            if step.step_id in seen:
                raise ValueError(f"步骤结果 step_id 重复: {step.step_id}")
            seen.add(step.step_id)

        derived = _derive_case_status(steps)
        if self.status != derived:
            raise ValueError(
                f"Case status={self.status} 与步骤结果不一致（应为 {derived}）"
            )

    @property
    def summary(self) -> JsonObject:
        """步骤计数汇总：total / success / failed / skipped。"""
        return _compute_case_summary(self.steps)

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示（步骤与 summary 均为新副本）。"""
        return {
            "case_id": self.case_id,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "steps": [step.to_dict() for step in self.steps],
            "summary": dict(self.summary),
        }


ExecutionRequest: TypeAlias = ApiExecutionRequest | CaseExecutionRequest
ExecutionOutcome: TypeAlias = ApiExecutionOutcome | CaseExecutionOutcome


class EnginePort(Protocol):
    """唯一的 Engine 执行 seam；不依赖 Control、ORM 或 Web 框架。

    实现可用 overload/判别联合同时接受 API 与 Case 请求；现有单 API 调用保持兼容。
    """

    @overload
    async def execute(self, request: ApiExecutionRequest) -> ApiExecutionOutcome: ...

    @overload
    async def execute(self, request: CaseExecutionRequest) -> CaseExecutionOutcome: ...

    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        """执行 API 或 Case，返回业务结果或抛出已分类系统异常。"""
        ...


def _validate_partial_outcome_consistency(
    kind: str, partial_outcome: CaseExecutionOutcome
) -> None:
    """异常 kind/stage 必须与 partial 中当前失败步骤的 error 一致。

    P1：execution-stage 系统错误要求当前失败步 error.kind 匹配且 stage="execution"；
    拒绝缺失/错误 stage、多个 failed、失败前 skipped、失败后非 skipped。
    """
    failed_steps = [step for step in partial_outcome.steps if step.status == "failed"]
    if not failed_steps:
        raise ValueError("execution-stage partial_outcome 必须包含一个 failed 步骤")
    # fail-fast：恰有一个失败步骤，其后全是 skipped。
    if len(failed_steps) != 1:
        raise ValueError("partial_outcome 在 fail-fast 下只能有一个 failed 步骤")
    current = failed_steps[0]
    error = current.error or {}
    error_kind = error.get("kind")
    if error_kind != kind:
        raise ValueError(
            f"异常 kind={kind} 与当前步骤 error.kind={error_kind!r} 不一致"
        )
    error_stage = error.get("stage")
    if error_stage != "execution":
        raise ValueError(
            f"当前步骤 error.stage 必须为 execution，实际为 {error_stage!r}"
        )
    # 失败步之后必须全部 skipped，之前不得出现 skipped（第一批无条件跳过）。
    failed_index = partial_outcome.steps.index(current)
    for step in partial_outcome.steps[:failed_index]:
        if step.status == "skipped":
            raise ValueError("partial_outcome 中失败步骤之前不得出现 skipped")
    for step in partial_outcome.steps[failed_index + 1 :]:
        if step.status != "skipped":
            raise ValueError("partial_outcome 中失败步骤之后必须全部为 skipped")


class EngineError(Exception):
    """公开的 Engine 系统错误，`log_reason` 永远不进入外显 payload。

    execution-stage 可携带安全 `partial_outcome`；preflight 不得伪造执行结果。
    """

    kind: str
    stage: ErrorStage
    public_message: str
    log_reason: str | None
    partial_outcome: CaseExecutionOutcome | None

    def __init__(
        self,
        kind: str,
        stage: ErrorStage,
        public_message: str,
        *,
        log_reason: str | None = None,
        partial_outcome: CaseExecutionOutcome | None = None,
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
        if partial_outcome is not None:
            if stage != "execution":
                raise ValueError("preflight 异常不得携带 partial_outcome")
            if not isinstance(partial_outcome, CaseExecutionOutcome):
                raise TypeError("partial_outcome 必须是 CaseExecutionOutcome")
            _validate_partial_outcome_consistency(kind, partial_outcome)
        self.kind = kind
        self.stage = stage
        self.public_message = public_message
        self.log_reason = log_reason
        self.partial_outcome = partial_outcome
        super().__init__(public_message)

    def to_dict(self) -> JsonObject:
        """返回安全的错误外显；底层原因只用于日志链路。"""
        payload: JsonObject = {
            "kind": self.kind,
            "stage": self.stage,
            "message": self.public_message,
        }
        if self.partial_outcome is not None:
            payload["partial_outcome"] = self.partial_outcome.to_dict()
        return payload


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
        super().__init__(
            "invalid_environment", "preflight", public_message, log_reason=log_reason
        )


class InvalidInputError(EngineError):
    """请求参数在执行前可判定无效（例如缺失的 Case params 引用）。"""

    def __init__(
        self, public_message: str = "执行输入无效", *, log_reason: str | None = None
    ) -> None:
        super().__init__("invalid_input", "preflight", public_message, log_reason=log_reason)


class EngineTimeoutError(EngineError):
    def __init__(
        self,
        public_message: str = "API 请求超时",
        *,
        log_reason: str | None = None,
        partial_outcome: CaseExecutionOutcome | None = None,
    ) -> None:
        super().__init__(
            "timeout",
            "execution",
            public_message,
            log_reason=log_reason,
            partial_outcome=partial_outcome,
        )


class EngineTransportError(EngineError):
    def __init__(
        self,
        public_message: str = "API 传输失败",
        *,
        log_reason: str | None = None,
        partial_outcome: CaseExecutionOutcome | None = None,
    ) -> None:
        super().__init__(
            "transport",
            "execution",
            public_message,
            log_reason=log_reason,
            partial_outcome=partial_outcome,
        )


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
        partial_outcome: CaseExecutionOutcome | None = None,
    ) -> None:
        super().__init__(
            "internal",
            stage,
            public_message,
            log_reason=log_reason,
            partial_outcome=partial_outcome,
        )


__all__ = [
    "ApiExecutionOutcome",
    "ApiExecutionRequest",
    "AssetNotFoundError",
    "CaseExecutionOutcome",
    "CaseExecutionRequest",
    "EngineError",
    "EngineInternalError",
    "EnginePort",
    "EngineTimeoutError",
    "EngineTransportError",
    "EngineUnavailableError",
    "ExecutionOutcome",
    "ExecutionRequest",
    "InvalidAssetError",
    "InvalidEnvironmentError",
    "InvalidInputError",
    "JsonObject",
    "JsonValue",
    "StepOutcome",
    "StepStatus",
]
