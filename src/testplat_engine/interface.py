"""Control 与 Engine 之间的唯一执行协议。

本模块只定义数据边界和异常语义，不负责加载资产、发送请求或保存执行记录。
0.2 在保持单 API DTO 兼容的同时，冻结 Case 请求/结果与步骤结果不变量。
"""

from __future__ import annotations

import math
import re
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


# ---------------------------------------------------------------------------
# 3.3a 鉴权材料（静态 header 凭证）
#
# 边界：Control 负责读取 auth 配置与环境变量并构造下列 DTO；Engine 只在预检时
# 按资产声明查表、在 client.request 调用处注入 header、并在公开面按名 + 按值脱敏。
# 不变量：秘密值只能经 SecretStr.reveal() 取出，且全仓只有
# engine/auth_injection.py 调用；任何 repr/str/to_dict/异常消息都不含秘密值。
# ---------------------------------------------------------------------------

# 秘密最小长度：过短的值做按值替换会误伤正常文本（设计稿 §5.3）。
MIN_SECRET_LENGTH = 8
# header 名必须是 RFC 7230 token；同时保证错误消息里回显的名字不含控制字符。
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# 资产里 `auth: none` 表示显式匿名，因此不能作为身份名（Q6）。
RESERVED_IDENTITY_NONE = "none"


class SecretStr:
    """最小秘密包装（不引入 pydantic）。

    - ``repr`` / ``str`` / ``format`` 一律为 ``***``；
    - ``to_dict``、pickle/copy、``json.dumps`` 都显式抛错，防止误写入快照或记录；
    - 只能经 ``reveal()`` 取值；该调用点受测试约束（只允许注入模块）。
    构造错误消息只描述类型，绝不回显传入值（M1）。
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("SecretStr 只接受 str")
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("SecretStr 不可修改")

    def reveal(self) -> str:
        """取出原始秘密；只允许在注入点调用。"""
        return self._value

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return "***"

    def __str__(self) -> str:
        return "***"

    def __format__(self, format_spec: str) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        # 只允许同类比较，避免与普通字符串比较时形成“猜值”接口。
        if not isinstance(other, SecretStr):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash((SecretStr, self._value))

    def to_dict(self) -> JsonObject:
        raise TypeError("SecretStr 不可序列化")

    def __reduce__(self) -> object:
        raise TypeError("SecretStr 不可 pickle/copy")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("SecretStr 不可 pickle/copy")

    def __getstate__(self) -> object:
        raise TypeError("SecretStr 不可 pickle/copy")


def _validate_identity_name(name: object, field_name: str) -> str:
    """身份名：非空字符串且不是保留名 none；错误消息不含任何秘密。"""
    if not isinstance(name, str) or not name.strip():
        raise TypeError(f"{field_name} 必须是非空字符串")
    if name == RESERVED_IDENTITY_NONE:
        raise ValueError(f"{field_name} 不能使用保留名 none")
    return name


@dataclass(frozen=True, slots=True, repr=False)
class StaticHeaderAuth:
    """3.3a 唯一可用的凭证 kind：一组静态 header。

    ``headers``：header 名 → ``SecretStr``（Control 渲染后的完整 header 值）。
    ``secret_values``：渲染前的原始变量值（Q5）。按值替换集合 = 原始变量值 +
    完整 header 值；``secret_values`` 为空时，header 值本身即视为原始秘密。
    长度 < 8 的原始秘密在构造期拒绝（Control 应先拦截为 ``UnavailableAuth``）。
    """

    headers: Mapping[str, SecretStr]
    secret_values: Sequence[SecretStr] = ()
    kind: Literal["static_headers"] = "static_headers"

    def __post_init__(self) -> None:
        if self.kind != "static_headers":
            raise ValueError("StaticHeaderAuth.kind 必须是 static_headers")
        if not isinstance(self.headers, Mapping) or not self.headers:
            raise TypeError("StaticHeaderAuth.headers 必须是非空映射")
        copied: dict[str, SecretStr] = {}
        seen_lower: set[str] = set()
        for name, value in self.headers.items():
            if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
                # 不合法的名字可能本身就是误填的秘密，只报位置不回显。
                raise ValueError("StaticHeaderAuth.headers 含非法 header 名")
            if name.lower() in seen_lower:
                raise ValueError(f"StaticHeaderAuth.headers 中 header 名大小写重复: {name}")
            if not isinstance(value, SecretStr):
                raise TypeError(f"StaticHeaderAuth.headers[{name}] 必须是 SecretStr")
            seen_lower.add(name.lower())
            copied[name] = value
        if isinstance(self.secret_values, (str, bytes)) or not isinstance(
            self.secret_values, Sequence
        ):
            raise TypeError("StaticHeaderAuth.secret_values 必须是 SecretStr 序列")
        raw = tuple(self.secret_values)
        for index, item in enumerate(raw):
            if not isinstance(item, SecretStr):
                raise TypeError(f"StaticHeaderAuth.secret_values[{index}] 必须是 SecretStr")
            if len(item) < MIN_SECRET_LENGTH:
                raise ValueError(
                    f"StaticHeaderAuth.secret_values[{index}] 长度不足 {MIN_SECRET_LENGTH}"
                )
        if not raw:
            # 未提供原始变量值时，header 值本身就是原始秘密，同样受长度下限约束。
            for name, value in copied.items():
                if len(value) < MIN_SECRET_LENGTH:
                    raise ValueError(
                        f"StaticHeaderAuth.headers[{name}] 秘密长度不足 {MIN_SECRET_LENGTH}"
                    )
        object.__setattr__(self, "headers", MappingProxyType(copied))
        object.__setattr__(self, "secret_values", raw)

    @property
    def header_names(self) -> tuple[str, ...]:
        return tuple(self.headers)

    def __repr__(self) -> str:
        return f"StaticHeaderAuth(kind='static_headers', header_names={list(self.headers)!r})"

    __str__ = __repr__

    def to_dict(self) -> JsonObject:
        """只输出 kind 与 header 名。"""
        return {"kind": self.kind, "header_names": list(self.headers)}

    def __reduce__(self) -> object:
        raise TypeError("StaticHeaderAuth 不可 pickle/copy")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("StaticHeaderAuth 不可 pickle/copy")


@dataclass(frozen=True, slots=True)
class UnavailableAuth:
    """Control 无法解析的身份（变量缺失/为空/过短/不在白名单）。

    只有被实际执行的资产用到时才在预检报 ``invalid_environment``；
    ``public_reason`` 由 Control 保证只含变量名，Engine 会原样拼进 public_message。
    """

    public_reason: str
    kind: Literal["unavailable"] = "unavailable"

    def __post_init__(self) -> None:
        if self.kind != "unavailable":
            raise ValueError("UnavailableAuth.kind 必须是 unavailable")
        if not isinstance(self.public_reason, str) or not self.public_reason.strip():
            raise TypeError("UnavailableAuth.public_reason 必须是非空字符串")

    def to_dict(self) -> JsonObject:
        return {"kind": self.kind}


# 判别联合：3.3b 追加 LoginTokenAuth / LoginCookieAuth。Engine 遇到未知 kind 在预检报
# invalid_environment，绝不静默降级为匿名（设计稿 §9）。
AuthMaterial: TypeAlias = StaticHeaderAuth | UnavailableAuth


def _material_kind(material: object) -> str | None:
    """取凭证材料的 kind；没有字符串 kind 的对象视为非法材料。"""
    kind = getattr(material, "kind", None)
    return kind if isinstance(kind, str) and kind else None


@dataclass(frozen=True, slots=True, repr=False)
class AuthTable:
    """整张身份表：Control 不解析资产 YAML，因此传全表，由 Engine 预检时查表。

    构造期只校验结构：身份名合法、``default_identity`` 必须存在（Q7）。
    材料须带字符串 ``kind``；未知 kind 允许构造（为跨版本前向兼容），
    但在被实际用到时由预检拒绝。
    """

    identities: Mapping[str, AuthMaterial]
    default_identity: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identities, Mapping):
            raise TypeError("AuthTable.identities 必须是映射")
        copied: dict[str, AuthMaterial] = {}
        for name, material in self.identities.items():
            _validate_identity_name(name, "AuthTable 身份名")
            if _material_kind(material) is None:
                raise TypeError(f"AuthTable 身份 {name} 的凭证材料缺少 kind")
            copied[name] = material
        if self.default_identity is not None:
            _validate_identity_name(self.default_identity, "AuthTable.default_identity")
            if self.default_identity not in copied:
                raise ValueError(
                    f"AuthTable.default_identity 指向不存在的身份: {self.default_identity}"
                )
        object.__setattr__(self, "identities", MappingProxyType(copied))

    def to_dict(self) -> JsonObject:
        """只输出身份名、kind、header 名与 default_identity。"""
        identities: JsonObject = {}
        for name, material in self.identities.items():
            entry: JsonObject = {"kind": _material_kind(material)}
            if isinstance(material, StaticHeaderAuth):
                entry["header_names"] = list(material.headers)
            identities[name] = entry
        return {"identities": identities, "default_identity": self.default_identity}

    def __repr__(self) -> str:
        return f"AuthTable({self.to_dict()!r})"

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise TypeError("AuthTable 不可 pickle/copy")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("AuthTable 不可 pickle/copy")


def _validate_auth_table(value: object) -> None:
    if value is not None and not isinstance(value, AuthTable):
        raise TypeError("auth 必须是 AuthTable 或 None")


_PUBLIC_AUTH_KEYS = ("identity", "header_names", "overridden_headers")


def _public_auth(value: object) -> object:
    """公开 auth 字段（Q1）：{identity, header_names, overridden_headers} 或 None。"""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(_PUBLIC_AUTH_KEYS):
        raise ValueError("auth 必须恰好包含 identity / header_names / overridden_headers")
    identity = value["identity"]
    if not isinstance(identity, str) or not identity:
        raise TypeError("auth.identity 必须是非空字符串")
    names: dict[str, list[str]] = {}
    for key in ("header_names", "overridden_headers"):
        items = value[key]
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise TypeError(f"auth.{key} 必须是字符串列表")
        if not all(isinstance(item, str) for item in items):
            raise TypeError(f"auth.{key} 必须是字符串列表")
        names[key] = list(items)
    return _freeze_json({"identity": identity, **names})


@dataclass(frozen=True, slots=True)
class ApiExecutionRequest:
    """单 API 执行输入；Control 传入已解析的环境配置。"""

    api_id: str
    assets_root: Path
    environment: Mapping[str, JsonValue]
    params: Mapping[str, JsonValue]
    timeout_seconds: float = 30.0
    # None ≡ v0.2.0 行为（匿名）；资产声明具体身份而 auth=None → 预检 422（Q2）。
    auth: AuthTable | None = None

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
        _validate_auth_table(self.auth)

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示。

        ``auth`` 只在非 None 时输出，且只含身份名/kind/header 名，保证 auth=None 时
        与 v0.2.0 的输出逐字一致。
        """
        payload: JsonObject = {
            "api_id": self.api_id,
            "assets_root": str(self.assets_root),
            "environment": _thaw_json(self.environment),  # type: ignore[arg-type]
            "params": _thaw_json(self.params),  # type: ignore[arg-type]
            "timeout_seconds": self.timeout_seconds,
        }
        if self.auth is not None:
            payload["auth"] = self.auth.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class ApiExecutionOutcome:
    """单 API 业务结果；断言失败通过此对象返回，而不是抛异常。"""

    status: ExecutionStatus
    request: JsonObject | None = None
    response: JsonValue | None = None
    error: JsonObject | None = None
    # 公开身份信息（Q1）：{identity, header_names, overridden_headers} 或 None（匿名）。
    auth: JsonObject | None = None

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
        object.__setattr__(self, "auth", _public_auth(self.auth))

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示（全新可变副本）。"""
        return {
            "status": self.status,
            "request": None if self.request is None else _thaw_json(self.request),
            "response": None if self.response is None else _thaw_json(self.response),
            "error": None if self.error is None else _thaw_json(self.error),
            "auth": None if self.auth is None else _thaw_json(self.auth),
        }


@dataclass(frozen=True, slots=True)
class CaseExecutionRequest:
    """Case 执行输入；fail-fast 固定开启，调用方不可覆盖。

    Control 只传身份、资产根、已解析环境和可覆盖参数；不携带 ORM/Web 对象。

    ``timeout_seconds`` 的语义是**每个步骤**的 HTTP 超时（秒，有限正数），
    会原样传给每个 ``api`` 步骤的 HTTP client；它**不是**整个 Case 的总时长预算。
    N 个步骤的 Case 最坏耗时可达 ``N * timeout_seconds`` 量级；如需整体限时，
    由调用方（Control）自行在外层施加，Engine 不提供 Case 级超时。
    """

    case_id: str
    assets_root: Path
    environment: Mapping[str, JsonValue]
    params: Mapping[str, JsonValue]
    timeout_seconds: float = 30.0
    # 整张身份表；Case 步骤沿用各自 API 资产的声明，步骤上不能覆盖（D3）。
    auth: AuthTable | None = None

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id 不能为空")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是有限正数")
        object.__setattr__(
            self, "environment", _snapshot_object(self.environment, "environment", redact=False)
        )
        object.__setattr__(self, "params", _snapshot_object(self.params, "params", redact=False))
        _validate_auth_table(self.auth)

    def to_dict(self) -> JsonObject:
        """返回可直接交给 `json.dumps` 的稳定表示（全新可变副本）。

        ``auth`` 规则同 ``ApiExecutionRequest.to_dict``。
        """
        payload: JsonObject = {
            "case_id": self.case_id,
            "assets_root": str(self.assets_root),
            "environment": _thaw_json(self.environment),  # type: ignore[arg-type]
            "params": _thaw_json(self.params),  # type: ignore[arg-type]
            "timeout_seconds": self.timeout_seconds,
        }
        if self.auth is not None:
            payload["auth"] = self.auth.to_dict()
        return payload


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
    # 与单 API 结果同形的公开身份信息；skipped 步骤必须为 None。
    auth: JsonObject | None = None

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
            if self.auth is not None:
                raise ValueError("skipped 步骤的 auth 必须为 null")

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
        object.__setattr__(self, "auth", _public_auth(self.auth))

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
            "auth": None if self.auth is None else _thaw_json(self.auth),
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
    "AuthMaterial",
    "AuthTable",
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
    "SecretStr",
    "StaticHeaderAuth",
    "StepOutcome",
    "StepStatus",
    "UnavailableAuth",
]
