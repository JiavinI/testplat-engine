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
# engine/auth_injection.py、engine/login_render.py 与（3.3d）engine/login_cache.py 调用；
# 任何 repr/str/to_dict/异常消息都不含秘密值。
# ---------------------------------------------------------------------------

# 登录产出 token 的兼容规则；被测系统输入凭据不使用这个长度限制。
MIN_SECRET_LENGTH = 8
# header 名必须是 RFC 7230 token；同时保证错误消息里回显的名字不含控制字符。
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# 资产里 `auth: none` 表示显式匿名，因此不能作为身份名（Q6）。
RESERVED_IDENTITY_NONE = "none"


class SecretStr:
    """最小秘密包装（不引入 pydantic）。

    - ``repr`` / ``str`` / ``format`` 一律为 ``***``；
    - ``to_dict``、pickle/copy、``json.dumps`` 都显式抛错，防止误写入快照或记录；
    - 只能经 ``reveal()`` 取值；该调用点受测试约束（只允许注入模块与登录渲染模块，L3）。
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
    凭据值的长度和内容由被测系统校验。
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


# ---------------------------------------------------------------------------
# 3.3b 登录鉴权 DTO（设计稿 §5.3，D7–D15）
#
# 边界：Control 解析 auth 配置 + 环境变量，构造下列 DTO（模板中的 ${secret:} 已渲染为
# SecretStr，${login:名字} / ${login_host} 原样保留给 Engine 在登录/注入时渲染）。
# 不变量：repr / to_dict 只暴露策略名、tier、host、步骤名、提取名、header 名；
# 一律禁止 pickle/copy；构造错误消息不回显任何字段值。
# ---------------------------------------------------------------------------

LOGIN_BODY_TYPES = frozenset({"json", "form", "query", "raw"})
EXTRACT_SOURCES = frozenset({"body_json", "response_header", "cookies", "html_meta"})
LOGIN_TIERS = frozenset({"generic", "configured", "plugin"})
EXPIRES_UNITS = frozenset({"seconds", "epoch_ms", "epoch_s"})
# login_failed 的 reason 枚举（手册 §4 第 8 条）。
LOGIN_FAILURE_REASONS = (
    "assertion",
    "extract_missing",
    "token_too_short",
    "probe_failed",
    "timeout",
    "transport",
    "strategy_error",
)
# 内置策略名；插件不得与之重名（create_engine 启动期拒绝）。
GENERIC_FLOW = "generic_flow"
# 命名值 / 策略名：保守的标识符语法，保证可安全出现在错误消息与日志里。
_LOGIN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}$")
# 模板中引用命名值的占位符：${login:名字}
LOGIN_REF_RE = re.compile(r"\$\{login:([^}]*)\}")
LOGIN_HOST_PLACEHOLDER = "${login_host}"
# 3.3d：跨执行缓存作用域（非秘密，但不进公开结果 / to_dict）。
_CACHE_SCOPE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


Template: TypeAlias = "str | SecretStr"


def _check_login_name(value: object, field_name: str) -> str:
    """校验命名值 / 步骤名 / 策略名；错误消息只报字段位置。"""
    if not isinstance(value, str) or not _LOGIN_NAME_RE.fullmatch(value):
        raise ValueError(f"{field_name} 必须是 1–64 位标识符")
    return value


def _check_template(value: object, field_name: str) -> None:
    if not isinstance(value, (str, SecretStr)):
        raise TypeError(f"{field_name} 必须是 str 或 SecretStr")


def _template_text_refs(value: object) -> set[str]:
    """取模板里的 ${login:名字} 引用；SecretStr 内部的引用由渲染模块解析（此处不 reveal）。"""
    if isinstance(value, str):
        return set(LOGIN_REF_RE.findall(value))
    return set()


def _copy_templates(value: object, field_name: str, *, headers: bool) -> MappingProxyType:
    """复制 header / 参数模板映射；header 名按 RFC 7230 与大小写去重校验。"""
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} 必须是映射")
    copied: dict[str, object] = {}
    seen: set[str] = set()
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_name} 含非法键")
        if headers:
            if not _HEADER_NAME_RE.fullmatch(key):
                raise ValueError(f"{field_name} 含非法 header 名")
            if key.lower() in seen:
                raise ValueError(f"{field_name} 中 header 名大小写重复: {key}")
            seen.add(key.lower())
            _check_template(item, f"{field_name}[{key}]")
        copied[key] = item
    return MappingProxyType(copied)


def _check_body_fields(value: object, field_name: str) -> object:
    """登录请求体字段：JSON 值或 SecretStr，任意嵌套；返回深冻结副本。"""
    if isinstance(value, SecretStr) or value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} 不能含 NaN/无穷")
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_check_body_fields(item, field_name) for item in value)
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} 的键必须是字符串")
            out[key] = _check_body_fields(item, field_name)
        return MappingProxyType(out)
    raise TypeError(f"{field_name} 只允许 JSON 值或 SecretStr")


class _NoPickle:
    """含（或可能含）秘密的 DTO 共用：禁止 pickle/copy。"""

    __slots__ = ()

    def __reduce__(self) -> object:
        raise TypeError(f"{type(self).__name__} 不可 pickle/copy")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError(f"{type(self).__name__} 不可 pickle/copy")


@dataclass(frozen=True, slots=True, repr=False)
class ExtractRule(_NoPickle):
    """一条提取规则（D10）：从登录响应取一个命名值。

    - ``body_json``：``path``（简化 JSONPath，``$.a.b``）；
    - ``response_header``：``name``（不区分大小写）；
    - ``cookies``：``name`` 按名取值，或 ``format="cookie_header"`` 取整个 jar 的 Cookie 头；
    - ``html_meta``：``name`` 为 ``<meta name=...>`` 的名字，取其 content。
    ``as_name`` 即配置里的 ``as``。``optional=True`` 时取不到不算失败。
    """

    source: str
    as_name: str
    path: str | None = None
    name: str | None = None
    format: str | None = None
    optional: bool = False

    def __post_init__(self) -> None:
        if self.source not in EXTRACT_SOURCES:
            raise ValueError("ExtractRule.source 不受支持")
        _check_login_name(self.as_name, "ExtractRule.as")
        if self.source == "body_json":
            if not isinstance(self.path, str) or not self.path.startswith("$"):
                raise ValueError("ExtractRule(body_json) 需要以 $ 开头的 path")
        elif self.source == "cookies":
            if self.format not in (None, "cookie_header"):
                raise ValueError("ExtractRule(cookies).format 只支持 cookie_header")
            if self.format is None and (not isinstance(self.name, str) or not self.name):
                raise ValueError("ExtractRule(cookies) 需要 name 或 format=cookie_header")
        elif not isinstance(self.name, str) or not self.name:
            raise ValueError(f"ExtractRule({self.source}) 需要 name")
        if not isinstance(self.optional, bool):
            raise TypeError("ExtractRule.optional 必须是布尔值")

    def __repr__(self) -> str:
        return f"ExtractRule(source={self.source!r}, as={self.as_name!r})"

    __str__ = __repr__

    def to_dict(self) -> JsonObject:
        return {"source": self.source, "as": self.as_name}


@dataclass(frozen=True, slots=True, repr=False)
class LoginBody(_NoPickle):
    """登录请求体（D11）：json / form / query 用 ``fields``；raw 用 ``content``。

    Q4：显式 ``Content-Type`` 头优先；raw 必须有 ``content_type`` 或显式头（由
    ``LoginStep`` 校验，因为头在步骤上）。
    """

    type: str = "json"
    fields: Mapping[str, object] | None = None
    content: Template | None = None
    content_type: str | None = None

    def __post_init__(self) -> None:
        if self.type not in LOGIN_BODY_TYPES:
            # multipart 本批不支持（§9）。
            raise ValueError("LoginBody.type 必须是 json/form/query/raw")
        if self.type == "raw":
            _check_template(self.content, "LoginBody.content")
            if self.fields is not None:
                raise ValueError("LoginBody(raw) 不能带 fields")
        else:
            if self.content is not None:
                raise ValueError("只有 raw 请求体可以带 content")
            fields = self.fields if self.fields is not None else {}
            if not isinstance(fields, Mapping):
                raise TypeError("LoginBody.fields 必须是映射")
            frozen = _check_body_fields(fields, "LoginBody.fields")
            if self.type in ("form", "query"):
                for key, item in fields.items():
                    if not isinstance(item, (str, int, float, bool, SecretStr)):
                        raise ValueError(f"LoginBody({self.type}).fields[{key}] 必须是标量")
            object.__setattr__(self, "fields", frozen)
        if self.content_type is not None and not isinstance(self.content_type, str):
            raise TypeError("LoginBody.content_type 必须是字符串")

    def __repr__(self) -> str:
        return f"LoginBody(type={self.type!r})"

    __str__ = __repr__


def _check_success_spec(value: object, field_name: str) -> MappingProxyType | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} 必须是映射")
    return MappingProxyType(_json_object(value, field_name))


@dataclass(frozen=True, slots=True, repr=False)
class LoginStep(_NoPickle):
    """``generic_flow`` 的一个登录步骤。

    ``path`` / ``headers`` / ``body`` 可引用 ``${login:名字}``（前序步骤提取）与
    ``${login_host}``；``success`` 未写时默认“状态码 < 400”（Q3）。
    """

    name: str
    method: str
    path: str
    headers: Mapping[str, Template] | None = None
    body: LoginBody | None = None
    success: Mapping[str, JsonValue] | None = None
    extract: Sequence[ExtractRule] = ()

    def __post_init__(self) -> None:
        _check_login_name(self.name, "LoginStep.name")
        if not isinstance(self.method, str) or not self.method.strip():
            raise ValueError(f"LoginStep[{self.name}].method 不能为空")
        object.__setattr__(self, "method", self.method.upper())
        if not isinstance(self.path, str) or not self.path:
            raise ValueError(f"LoginStep[{self.name}].path 不能为空")
        object.__setattr__(
            self,
            "headers",
            _copy_templates(self.headers, f"LoginStep[{self.name}].headers", headers=True),
        )
        if self.body is not None and not isinstance(self.body, LoginBody):
            raise TypeError(f"LoginStep[{self.name}].body 必须是 LoginBody")
        if self.body is not None and self.body.type == "raw":
            has_header = any(k.lower() == "content-type" for k in self.headers)  # type: ignore[union-attr]
            if not self.body.content_type and not has_header:
                raise ValueError(f"LoginStep[{self.name}] 的 raw 请求体需要 content_type")
        object.__setattr__(
            self, "success", _check_success_spec(self.success, f"LoginStep[{self.name}].success")
        )
        rules = tuple(self.extract)
        for rule in rules:
            if not isinstance(rule, ExtractRule):
                raise TypeError(f"LoginStep[{self.name}].extract 元素必须是 ExtractRule")
        object.__setattr__(self, "extract", rules)

    def referenced_names(self) -> set[str]:
        """步骤模板（明文部分）引用的命名值，供预检核对来源。"""
        refs = _template_text_refs(self.path)
        for item in self.headers.values():  # type: ignore[union-attr]
            refs |= _template_text_refs(item)
        if self.body is not None:
            refs |= _template_text_refs(self.body.content)
            refs |= _nested_refs(self.body.fields)
        return refs

    def __repr__(self) -> str:
        return (
            f"LoginStep(name={self.name!r}, header_names={list(self.headers or {})!r}, "
            f"extract={[r.as_name for r in self.extract]!r})"
        )

    __str__ = __repr__


def _nested_refs(value: object) -> set[str]:
    if isinstance(value, str):
        return _template_text_refs(value)
    if isinstance(value, Mapping):
        out: set[str] = set()
        for item in value.values():
            out |= _nested_refs(item)
        return out
    if isinstance(value, (list, tuple)):
        out = set()
        for item in value:
            out |= _nested_refs(item)
        return out
    return set()


@dataclass(frozen=True, slots=True, repr=False)
class LoginFlow(_NoPickle):
    """``generic_flow`` 的步骤序列（第 1 档单步 / 第 2 档多步）。"""

    steps: Sequence[LoginStep]

    def __post_init__(self) -> None:
        steps = tuple(self.steps)
        if not steps:
            raise ValueError("LoginFlow.steps 不能为空")
        names: set[str] = set()
        for step in steps:
            if not isinstance(step, LoginStep):
                raise TypeError("LoginFlow.steps 元素必须是 LoginStep")
            if step.name in names:
                raise ValueError(f"LoginFlow 步骤名重复: {step.name}")
            names.add(step.name)
        object.__setattr__(self, "steps", steps)

    def extracted_names(self) -> set[str]:
        return {rule.as_name for step in self.steps for rule in step.extract}

    def __repr__(self) -> str:
        return f"LoginFlow(steps={[s.name for s in self.steps]!r})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class ProbeSpec(_NoPickle):
    """登录后立即执行的会话验证请求；失败 → ``login_failed(reason=probe_failed)``。

    ``success`` 未写时默认“状态码 < 400”（Q3）；另支持 ``body_not_contains`` 与
    ``reject_location_contains``。
    """

    method: str = "GET"
    path: str = "/"
    headers: Mapping[str, Template] | None = None
    success: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not self.method.strip():
            raise ValueError("ProbeSpec.method 不能为空")
        object.__setattr__(self, "method", self.method.upper())
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("ProbeSpec.path 不能为空")
        object.__setattr__(
            self, "headers", _copy_templates(self.headers, "ProbeSpec.headers", headers=True)
        )
        object.__setattr__(self, "success", _check_success_spec(self.success, "ProbeSpec.success"))

    def __repr__(self) -> str:
        return f"ProbeSpec(method={self.method!r}, path={self.path!r})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class ExpiresFrom(_NoPickle):
    """从登录响应取过期时间（§5.4）；优先于 ``ttl_seconds``。

    ``step`` 省略时取最后一个登录步骤的响应。
    """

    path: str
    unit: str = "seconds"
    source: str = "body_json"
    step: str | None = None

    def __post_init__(self) -> None:
        if self.source != "body_json":
            raise ValueError("ExpiresFrom.source 只支持 body_json")
        if not isinstance(self.path, str) or not self.path.startswith("$"):
            raise ValueError("ExpiresFrom.path 必须以 $ 开头")
        if self.unit not in EXPIRES_UNITS:
            raise ValueError("ExpiresFrom.unit 必须是 seconds/epoch_ms/epoch_s")
        if self.step is not None:
            _check_login_name(self.step, "ExpiresFrom.step")

    def __repr__(self) -> str:
        return f"ExpiresFrom(unit={self.unit!r})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class Validity(_NoPickle):
    """有效性规则（D13/E3）：ttl / 响应过期时间 / Cookie expires，固定提前 30 秒判失效。"""

    ttl_seconds: float | None = None
    expires_from: ExpiresFrom | None = None

    def __post_init__(self) -> None:
        if self.ttl_seconds is not None:
            if isinstance(self.ttl_seconds, bool) or not isinstance(self.ttl_seconds, (int, float)):
                raise TypeError("Validity.ttl_seconds 必须是数字")
            if not math.isfinite(self.ttl_seconds) or self.ttl_seconds <= 0:
                raise ValueError("Validity.ttl_seconds 必须是有限正数")
        if self.expires_from is not None and not isinstance(self.expires_from, ExpiresFrom):
            raise TypeError("Validity.expires_from 必须是 ExpiresFrom")

    def __repr__(self) -> str:
        return (
            f"Validity(ttl_seconds={self.ttl_seconds!r}, "
            f"expires_from={self.expires_from is not None})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class LoginAuth(_NoPickle):
    """3.3b 登录身份（kind = "login"）：按需登录换取命名值 / Cookie 后注入。

    - ``strategy``：``generic_flow``（第 1/2 档，必须带 ``flow``）或插件名（第 3 档）；
    - ``tier``：Control 按 §1.1 判定的档位，非秘密，进入公开 ``auth`` / ``logins``；
    - ``params``：插件参数，SecretStr 值全部进入执行级按值替换集合；
    - ``secret_values``：渲染前的原始 ``${secret:}`` 值，不校验被测系统的凭据规则；
    - ``inject_headers``：header 名 → 模板（字面量 / ``${login:名字}`` / SecretStr 任意组合）；
    - Q1/Q2：至少一个 inject 值引用 ``${login:}``，或 ``session_cookie=True``。
    """

    strategy: str
    host: str
    tier: str = "generic"
    flow: LoginFlow | None = None
    params: Mapping[str, Template] | None = None
    secret_values: Sequence[SecretStr] = ()
    validity: Validity | None = None
    session_cookie: bool = False
    probe: ProbeSpec | None = None
    inject_headers: Mapping[str, Template] | None = None
    # 3.3d：业务响应 401 时是否重登并重发一次（默认开启；指纹包含该布尔）。
    reauth_on_401: bool = True
    kind: Literal["login"] = "login"

    def __post_init__(self) -> None:
        if self.kind != "login":
            raise ValueError("LoginAuth.kind 必须是 login")
        _check_login_name(self.strategy, "LoginAuth.strategy")
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("LoginAuth.host 必须是非空字符串")
        if self.tier not in LOGIN_TIERS:
            raise ValueError("LoginAuth.tier 必须是 generic/configured/plugin")
        if self.strategy == GENERIC_FLOW:
            if not isinstance(self.flow, LoginFlow):
                raise ValueError("generic_flow 身份必须提供 LoginFlow")
        elif self.flow is not None and not isinstance(self.flow, LoginFlow):
            raise TypeError("LoginAuth.flow 必须是 LoginFlow 或 None")
        object.__setattr__(
            self, "params", _copy_templates(self.params, "LoginAuth.params", headers=False)
        )
        for key, item in self.params.items():  # type: ignore[union-attr]
            _check_template(item, f"LoginAuth.params[{key}]")
        if isinstance(self.secret_values, (str, bytes)) or not isinstance(
            self.secret_values, Sequence
        ):
            raise TypeError("LoginAuth.secret_values 必须是 SecretStr 序列")
        raw = tuple(self.secret_values)
        for index, item in enumerate(raw):
            if not isinstance(item, SecretStr):
                raise TypeError(f"LoginAuth.secret_values[{index}] 必须是 SecretStr")
        object.__setattr__(self, "secret_values", raw)
        if self.validity is None:
            object.__setattr__(self, "validity", Validity())
        elif not isinstance(self.validity, Validity):
            raise TypeError("LoginAuth.validity 必须是 Validity")
        if not isinstance(self.session_cookie, bool):
            raise TypeError("LoginAuth.session_cookie 必须是布尔值")
        if not isinstance(self.reauth_on_401, bool):
            raise TypeError("LoginAuth.reauth_on_401 必须是布尔值")
        if self.probe is not None and not isinstance(self.probe, ProbeSpec):
            raise TypeError("LoginAuth.probe 必须是 ProbeSpec")
        object.__setattr__(
            self,
            "inject_headers",
            _copy_templates(self.inject_headers, "LoginAuth.inject_headers", headers=True),
        )
        if not self.session_cookie and not self.inject_refs() and not self._secret_inject():
            raise ValueError("login 身份至少需要一个引用 ${login:} 的 inject 值或 session: cookie")

    def _secret_inject(self) -> bool:
        # SecretStr 模板内部可能含 ${login:}（Control 渲染 ${secret:} 后整体包装），
        # 这里无法在不 reveal 的前提下判断，因此视为可能满足 Q1，交给渲染期核对。
        return any(isinstance(v, SecretStr) for v in self.inject_headers.values())  # type: ignore[union-attr]

    def inject_refs(self) -> set[str]:
        """inject 模板明文部分引用的命名值。"""
        refs: set[str] = set()
        for item in self.inject_headers.values():  # type: ignore[union-attr]
            refs |= _template_text_refs(item)
        return refs

    @property
    def header_names(self) -> tuple[str, ...]:
        return tuple(self.inject_headers or {})

    def to_dict(self) -> JsonObject:
        """只输出非秘密结构信息（含 reauth_on_401 布尔；不含任何凭证值）。"""
        return {
            "kind": self.kind,
            "strategy": self.strategy,
            "tier": self.tier,
            "host": self.host,
            "steps": [] if self.flow is None else [s.name for s in self.flow.steps],
            "extract": [] if self.flow is None else sorted(self.flow.extracted_names()),
            "header_names": list(self.header_names),
            "session_cookie": self.session_cookie,
            "reauth_on_401": self.reauth_on_401,
        }

    def __repr__(self) -> str:
        return f"LoginAuth({self.to_dict()!r})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class LoginResult(_NoPickle):
    """策略返回值：命名值（全部视为秘密）与可选过期时间（epoch 秒）。"""

    values: Mapping[str, str]
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.values, Mapping):
            raise TypeError("LoginResult.values 必须是映射")
        copied: dict[str, str] = {}
        for key, item in self.values.items():
            _check_login_name(key, "LoginResult.values 键")
            if not isinstance(item, str):
                raise TypeError(f"LoginResult.values[{key}] 必须是字符串")
            copied[key] = item
        object.__setattr__(self, "values", MappingProxyType(copied))
        if self.expires_at is not None:
            if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float)):
                raise TypeError("LoginResult.expires_at 必须是数字")
            if not math.isfinite(self.expires_at):
                raise ValueError("LoginResult.expires_at 必须有限")

    def __repr__(self) -> str:
        has_expires = self.expires_at is not None
        return f"LoginResult(names={sorted(self.values)!r}, has_expires={has_expires})"

    __str__ = __repr__


class LoginRecorder(Protocol):
    """登录过程记录回调：Engine 先缓存原始请求/响应，``login()`` 结束后统一脱敏（Q7）。"""

    def __call__(
        self,
        request: Mapping[str, object],
        response: Mapping[str, object] | None,
        *,
        name: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class LoginContext(_NoPickle):
    """Engine 交给登录策略的全部材料（硬约束 3）。

    插件只能看到：该身份专用 HTTP client（自带 cookie jar，本次执行内存活）、已解析的
    登录 host URL、策略参数、``connections``、记录回调；另附 ``flow``（本身份自己的流程，
    插件策略通常为 None）、``timeout_seconds``（每个请求的超时）与身份名。
    拿不到资产、业务 client、其它身份状态或 Control 对象。
    """

    identity: str
    http: object
    host_url: str
    params: Mapping[str, Template]
    connections: Mapping[str, Mapping[str, Template]]
    record: LoginRecorder
    flow: LoginFlow | None = None
    timeout_seconds: float = 30.0

    def __repr__(self) -> str:
        return (
            f"LoginContext(identity={self.identity!r}, host_url={self.host_url!r}, "
            f"param_names={sorted(self.params)!r}, "
            f"connections={ {k: sorted(v) for k, v in self.connections.items()}!r})"
        )

    __str__ = __repr__


class LoginStrategy(Protocol):
    """登录策略插件协议（D15 方案 C）；内置实现为 ``generic_flow``。

    稳定性承诺见 ``docs/COMPATIBILITY-0.4.md``：0.4.x 内只增不改。
    - ``validate``：预检期调用，不得发请求；抛任何异常 → ``invalid_environment``；
    - ``login``：抛任何异常 → ``login_failed(reason=strategy_error)``（timeout/transport
      类异常分别归为 ``timeout`` / ``transport``），只记异常类型名。
    """

    name: str

    def validate(self, auth: LoginAuth) -> None: ...

    async def login(self, ctx: LoginContext) -> LoginResult: ...


def _copy_connections(value: object) -> MappingProxyType:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("AuthTable.connections 必须是映射")
    out: dict[str, MappingProxyType] = {}
    for name, fields in value.items():
        if not isinstance(name, str) or not name:
            raise ValueError("AuthTable.connections 含非法连接名")
        if not isinstance(fields, Mapping):
            raise TypeError(f"AuthTable.connections[{name}] 必须是映射")
        inner: dict[str, object] = {}
        for key, item in fields.items():
            if not isinstance(key, str):
                raise TypeError(f"AuthTable.connections[{name}] 键必须是字符串")
            if not isinstance(item, (str, int, float, bool, SecretStr)) or item is None:
                raise TypeError(f"AuthTable.connections[{name}][{key}] 必须是标量或 SecretStr")
            inner[key] = item
        out[name] = MappingProxyType(inner)
    return MappingProxyType(out)


# 判别联合（L2）：3.3b 追加单一 ``LoginAuth``（kind="login"，第 1/2/3 档共用）。
# Engine 遇到未知 kind 在预检报 invalid_environment，绝不静默降级为匿名（设计稿 §9）。
AuthMaterial: TypeAlias = StaticHeaderAuth | UnavailableAuth | LoginAuth


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
    ``cache_scope``（3.3d）：跨执行缓存作用域；``None`` → 本表身份不跨执行缓存。
    校验通过后仍不进 ``to_dict`` / 公开结果（C1）。
    """

    identities: Mapping[str, AuthMaterial]
    default_identity: str | None = None
    # 3.3b：插件可用的连接信息（非秘密字段原样，密码为 SecretStr）；只交给插件策略。
    connections: Mapping[str, Mapping[str, Template]] | None = None
    # 3.3d：Control 传 f"{project.id}:{project.env}"；None / 缺省 → 不跨执行缓存。
    cache_scope: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "connections", _copy_connections(self.connections))
        if self.cache_scope is not None:
            if not isinstance(self.cache_scope, str) or not _CACHE_SCOPE_RE.fullmatch(
                self.cache_scope
            ):
                # 错误消息不回显原值（可能被误填为秘密）。
                raise ValueError("AuthTable.cache_scope 必须是 ≤200 字符的 [A-Za-z0-9._:-] 或 None")
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
            elif isinstance(material, LoginAuth):
                entry = material.to_dict()
            identities[name] = entry
        payload: JsonObject = {
            "identities": identities,
            "default_identity": self.default_identity,
        }
        if self.connections:
            # 只输出连接名，字段（含密码）一律不外显。
            payload["connection_names"] = sorted(self.connections)  # type: ignore[arg-type]
        return payload

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


_PUBLIC_AUTH_KEYS = (
    "identity",
    "kind",
    "tier",
    "header_names",
    "overridden_headers",
    "cookie_names",
    "login_ref",
    "session_source",
    "reauthenticated",
)
_PUBLIC_AUTH_KINDS = frozenset({"static_headers", "login"})
_SESSION_SOURCES = frozenset({"login", "execution", "cache"})


def _public_auth(value: object) -> object:
    """公开 auth 字段（0.5 九键）。

    在 0.4 七键基础上新增 ``session_source`` / ``reauthenticated``。
    静态 header 身份：``tier=None``、``cookie_names=[]``、``login_ref=None``、
    ``session_source=None``、``reauthenticated=False``。
    """
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(_PUBLIC_AUTH_KEYS):
        raise ValueError("auth 必须恰好包含 " + " / ".join(_PUBLIC_AUTH_KEYS))
    identity = value["identity"]
    if not isinstance(identity, str) or not identity:
        raise TypeError("auth.identity 必须是非空字符串")
    kind = value["kind"]
    if kind not in _PUBLIC_AUTH_KINDS:
        raise ValueError("auth.kind 必须是 static_headers 或 login")
    tier = value["tier"]
    if kind == "static_headers":
        if tier is not None:
            raise ValueError("静态 header 身份的 auth.tier 必须为 null")
    elif tier not in LOGIN_TIERS:
        raise ValueError("login 身份的 auth.tier 必须是 generic/configured/plugin")
    names: dict[str, list[str]] = {}
    for key in ("header_names", "overridden_headers", "cookie_names"):
        items = value[key]
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise TypeError(f"auth.{key} 必须是字符串列表")
        if not all(isinstance(item, str) for item in items):
            raise TypeError(f"auth.{key} 必须是字符串列表")
        names[key] = list(items)
    login_ref = value["login_ref"]
    if login_ref is not None and (
        isinstance(login_ref, bool) or not isinstance(login_ref, int) or login_ref < 0
    ):
        raise TypeError("auth.login_ref 必须是非负整数或 null")
    session_source = value["session_source"]
    if session_source is not None and session_source not in _SESSION_SOURCES:
        raise ValueError("auth.session_source 必须是 login/execution/cache 或 null")
    reauthenticated = value["reauthenticated"]
    if not isinstance(reauthenticated, bool):
        raise TypeError("auth.reauthenticated 必须是布尔值")
    if kind == "static_headers":
        if login_ref is not None or names["cookie_names"]:
            raise ValueError("静态 header 身份不能带 cookie_names / login_ref")
        if session_source is not None or reauthenticated is not False:
            raise ValueError(
                "静态 header 身份的 session_source 必须为 null 且 reauthenticated 为 false"
            )
    return _freeze_json(
        {
            "identity": identity,
            "kind": kind,
            "tier": tier,
            "header_names": names["header_names"],
            "overridden_headers": names["overridden_headers"],
            "cookie_names": names["cookie_names"],
            "login_ref": login_ref,
            "session_source": session_source,
            "reauthenticated": reauthenticated,
        }
    )


# skip_reason 枚举（H4）：fail_fast = 前序业务/系统失败；login_failed = 前序登录失败。
SKIP_REASONS = ("fail_fast", "login_failed")


def _public_logins(value: object) -> tuple[object, ...]:
    """公开登录记录列表：每条为 JSON 对象，键级脱敏后深冻结。

    按值替换由 Engine 在构造前完成（Q7：先缓存后脱敏），这里只做兜底的按键脱敏。
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise TypeError("logins 必须是 JSON 对象列表")
    out: list[object] = []
    for index, item in enumerate(value):
        if isinstance(item, Mapping):
            out.append(_snapshot_object(item, f"logins[{index}]", redact=True))
        else:
            raise TypeError("logins 元素必须是 JSON 对象")
    return tuple(out)


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
    # 本次调试的实际请求覆盖；body 整体替换，不渲染，不修改资产。
    request_override: Mapping[str, JsonValue] | None = None
    # 调用者选择优先于旧资产 auth；none 为显式匿名，省略沿用兼容回退。
    identity: str | None = None

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
        if self.identity is not None and self.identity != RESERVED_IDENTITY_NONE:
            _validate_identity_name(self.identity, "调用身份")
        if self.request_override is not None:
            if not isinstance(self.request_override, Mapping) or set(self.request_override) != {
                "body"
            }:
                raise ValueError("request_override 仅支持 body")
            object.__setattr__(
                self,
                "request_override",
                _snapshot_object(self.request_override, "request_override", redact=False),
            )

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
        if self.request_override is not None:
            payload["request_override"] = _thaw_json(self.request_override)
        if self.identity is not None:
            payload["identity"] = self.identity
        return payload


@dataclass(frozen=True, slots=True)
class ApiExecutionOutcome:
    """单 API 业务结果；断言失败通过此对象返回，而不是抛异常。"""

    status: ExecutionStatus
    request: JsonObject | None = None
    response: JsonValue | None = None
    error: JsonObject | None = None
    # 公开身份信息（0.5 九键）或 None（匿名）。
    auth: JsonObject | None = None
    # 3.3b：本次执行发生的登录记录（按发生顺序，已脱敏）；步骤 auth.login_ref 为其下标。
    logins: Sequence[JsonObject] = ()

    def __post_init__(self) -> None:
        if self.status not in ("success", "failed"):
            raise ValueError("status 必须是 success 或 failed")
        object.__setattr__(self, "logins", _public_logins(self.logins))
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
            "logins": [_thaw_json(item) for item in self.logins],
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
    # 与单 API 结果同形的公开身份信息（0.5 九键）；skipped 步骤必须为 None。
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
            if self.skip_reason not in SKIP_REASONS:
                raise ValueError("skipped 步骤的 skip_reason 必须为 fail_fast 或 login_failed")
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
    # 3.3b：整个 Case 的登录记录（执行级，按发生顺序）；步骤 auth.login_ref 为其下标。
    logins: Sequence[JsonObject] = ()

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id 不能为空")
        object.__setattr__(self, "logins", _public_logins(self.logins))
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
            raise ValueError(f"Case status={self.status} 与步骤结果不一致（应为 {derived}）")

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
            "logins": [_thaw_json(item) for item in self.logins],
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


def _validate_partial_outcome_consistency(kind: str, partial_outcome: CaseExecutionOutcome) -> None:
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
        raise ValueError(f"异常 kind={kind} 与当前步骤 error.kind={error_kind!r} 不一致")
    error_stage = error.get("stage")
    if error_stage != "execution":
        raise ValueError(f"当前步骤 error.stage 必须为 execution，实际为 {error_stage!r}")
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
        super().__init__("invalid_environment", "preflight", public_message, log_reason=log_reason)


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
    "ExpiresFrom",
    "ExtractRule",
    "InvalidAssetError",
    "InvalidEnvironmentError",
    "InvalidInputError",
    "JsonObject",
    "JsonValue",
    "LoginAuth",
    "LoginBody",
    "LoginContext",
    "LoginFlow",
    "LoginResult",
    "LoginStep",
    "LoginStrategy",
    "ProbeSpec",
    "SecretStr",
    "StaticHeaderAuth",
    "StepOutcome",
    "StepStatus",
    "UnavailableAuth",
    "Validity",
]
