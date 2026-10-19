"""Case 资产领域模型（Phase 3 / Engine 0.2）。

只表达裁决稿冻结的 schema；不包含 metadata、旧键别名或 assert DSL。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from testplat_engine.interface import JsonObject, JsonValue, _json_object

# Case ID: case.<segment>[.<segment>...]
_CASE_ID_RE = re.compile(r"^case\.[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$")
# step_id / output alias
_STEP_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

CASE_SCHEMA_VERSION = "0.2"
SUPPORTED_STEP_TYPES = frozenset({"api"})

# schema-owned 顶层与 step 字段；未知键一律拒绝。
CASE_TOP_LEVEL_KEYS = frozenset({"schema_version", "id", "name", "desc", "tags", "params", "steps"})
API_STEP_KEYS = frozenset({"step_id", "type", "api_id", "name", "params", "output", "identity"})


def is_valid_case_id(case_id: str) -> bool:
    """Case ID 格式校验；不允许空 segment、路径分隔符或大小写归一。"""
    if not isinstance(case_id, str) or not case_id:
        return False
    if "/" in case_id or "\\" in case_id:
        return False
    return _CASE_ID_RE.fullmatch(case_id) is not None


def is_valid_step_id(step_id: str) -> bool:
    """step_id / output alias 标识符格式。"""
    return isinstance(step_id, str) and _STEP_ID_RE.fullmatch(step_id) is not None


@dataclass(frozen=True, slots=True)
class CaseStep:
    """第一批仅支持 type=api 的步骤定义。"""

    step_id: str
    type: str
    api_id: str
    name: str | None = None
    params: dict[str, JsonValue] = field(default_factory=dict)
    output: str | None = None
    identity: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_step_id(self.step_id):
            raise ValueError(f"非法 step_id: {self.step_id!r}")
        if not self.type:
            raise ValueError("type 不能为空")
        if not self.api_id:
            raise ValueError("api_id 不能为空")
        if self.identity is not None:
            from testplat_engine.interface import RESERVED_IDENTITY_NONE, _validate_identity_name

            if self.identity != RESERVED_IDENTITY_NONE:
                _validate_identity_name(self.identity, "步骤调用身份")
        if self.name is not None and (not isinstance(self.name, str) or not self.name):
            raise ValueError("name 若提供则必须为非空字符串")
        if self.output is not None:
            if not is_valid_step_id(self.output):
                raise ValueError(f"非法 output alias: {self.output!r}")
            if self.output == "params":
                raise ValueError("output alias 不得为保留名 params")
        object.__setattr__(self, "params", _json_object(self.params, "params"))

    def to_dict(self) -> JsonObject:
        """规范化字典表示，供 DTO/夹具对照。"""
        payload = {
            "step_id": self.step_id,
            "type": self.type,
            "api_id": self.api_id,
            "name": self.name,
            "params": dict(self.params),
            "output": self.output,
        }
        if self.identity is not None:
            payload["identity"] = self.identity
        return payload


@dataclass(frozen=True, slots=True)
class Case:
    """规范化 Case 资产；数组顺序即执行顺序。"""

    schema_version: str
    id: str
    steps: tuple[CaseStep, ...]
    name: str | None = None
    desc: str | None = None
    tags: tuple[str, ...] = ()
    params: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != CASE_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version 必须为 {CASE_SCHEMA_VERSION!r}，实际为 {self.schema_version!r}"
            )
        if not is_valid_case_id(self.id):
            raise ValueError(f"非法 Case id: {self.id!r}")
        if not self.steps:
            raise ValueError("steps 至少包含一个步骤")
        if self.name is not None and (not isinstance(self.name, str) or not self.name):
            raise ValueError("name 若提供则必须为非空字符串")
        if self.desc is not None and not isinstance(self.desc, str):
            raise ValueError("desc 必须是字符串")
        for tag in self.tags:
            if not isinstance(tag, str) or not tag:
                raise ValueError("tags 元素必须为非空字符串")
        object.__setattr__(self, "params", _json_object(self.params, "params"))
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "tags", tuple(self.tags))

        step_ids: set[str] = set()
        outputs: set[str] = set()
        for step in self.steps:
            if not isinstance(step, CaseStep):
                raise TypeError("steps 元素必须是 CaseStep")
            if step.step_id in step_ids:
                raise ValueError(f"重复 step_id: {step.step_id}")
            step_ids.add(step.step_id)
            if step.output is not None:
                if step.output in outputs:
                    raise ValueError(f"重复 output alias: {step.output}")
                outputs.add(step.output)

    def to_dict(self) -> JsonObject:
        """规范化字典表示。"""
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "desc": self.desc,
            "tags": list(self.tags),
            "params": dict(self.params),
            "steps": [step.to_dict() for step in self.steps],
        }


def merge_case_params(
    asset_params: dict[str, JsonValue], request_params: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """浅层覆盖合并：请求顶层键覆盖资产默认值；不做深合并。"""
    merged = dict(asset_params)
    merged.update(request_params)
    return merged
