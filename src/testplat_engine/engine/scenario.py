"""版本化公共流程：集中参数、显式作用域与递归执行计划。

旧 Case 0.2 不经过此模块。描述和运行使用同一份已验证计划，Control 不解析 DSL。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from testplat_engine.adapters.filesystem.yaml_loader import load_yaml
from testplat_engine.domain.case import CaseStep, is_valid_step_id
from testplat_engine.engine.api_step_executor import ApiStepExecutor
from testplat_engine.engine.case_context import (
    VariableResolutionError,
    bind_alias_response,
    resolve_step_params,
)
from testplat_engine.engine.scenario_assert import AssertionEvaluationError, evaluate
from testplat_engine.engine.scenario_decimal import ROUNDINGS, CalculationError, calculate
from testplat_engine.engine.scenario_extract import SelectionError, select, valid_path
from testplat_engine.engine.scenario_list import transform
from testplat_engine.interface import (
    AssetNotFoundError,
    CaseExecutionOutcome,
    CaseExecutionRequest,
    EngineError,
    EngineInternalError,
    InvalidAssetError,
    InvalidInputError,
    StepOutcome,
    _json_object,
    _thaw_json,
)
from testplat_engine.ports.step_executor import StepExecutionContext
from testplat_engine.shared.redact import is_sensitive_key, redact_json, strip_sensitive_json

_ID = re.compile(r"^(case|flow)\.[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$")
_REF = re.compile(r"\$\{([^{}]+)\}")
_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}
_UNKNOWN = object()


def invalid(message: str) -> InvalidAssetError:
    """资产错误只描述结构，不回显业务取值。"""
    return InvalidAssetError(public_message=message, log_reason=message)


def asset_path(root: Path, asset_id: str, kind: str) -> Path:
    if (
        not isinstance(asset_id, str)
        or not _ID.fullmatch(asset_id)
        or not asset_id.startswith(kind + ".")
    ):
        raise invalid(f"非法 {kind} ID")
    parts = asset_id.split(".")[1:]
    path = (root / "scenarios" / Path(*parts[:-1]) / f"{parts[-1]}.{kind}.yaml").resolve()
    # 不允许 scenarios 自身被符号链接移到资产根以外。
    if not path.is_relative_to(root.resolve()) or not path.is_relative_to(
        (root / "scenarios").resolve()
    ):
        raise invalid("场景路径越界")
    return path


def read_document(root: Path, asset_id: str, kind: str) -> dict:
    try:
        return load_yaml(asset_path(root, asset_id, kind))
    except FileNotFoundError as exc:
        raise AssetNotFoundError(
            public_message=f"场景资产不存在: {asset_id}", log_reason="missing scenario"
        ) from exc
    except ValueError as exc:
        raise invalid(f"场景 YAML 无效: {asset_id}") from exc


def contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(is_sensitive_key(k) or contains_secret(v) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_secret(v) for v in value)
    return False


def keys(value: Any, allowed: set[str], where: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise invalid(f"{where} 必须是字符串键对象")
    if set(value) - allowed:
        raise invalid(f"{where} 含未知字段")
    return value


def references(value: Any):
    if isinstance(value, str):
        remainder = _REF.sub("", value)
        if "${" in remainder:
            raise invalid("占位符语法错误")
        for match in _REF.finditer(value):
            path = match.group(1)
            if any(not is_valid_step_id(p) for p in path.split(".")):
                raise invalid("引用仅支持点分对象路径")
            yield path
    elif isinstance(value, dict):
        for item in value.values():
            yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def has_unknown(value: Any) -> bool:
    if value is _UNKNOWN:
        return True
    if isinstance(value, dict):
        return any(has_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(has_unknown(item) for item in value)
    return False


def preflight_value(step_id: str, value: Any, scope: dict, aliases: dict) -> Any:
    """复用运行解析器校验已知引用；未知响应仅在当前叶子保留占位。"""
    if isinstance(value, dict):
        return {key: preflight_value(step_id, item, scope, aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [preflight_value(step_id, item, scope, aliases) for item in value]
    if not isinstance(value, str):
        return value
    unknown = False
    for ref in references(value):
        parts = ref.split(".")
        node = scope.get(parts[0], aliases.get(parts[0]))
        for part in parts[1:]:
            if node is _UNKNOWN:
                break
            if not isinstance(node, dict) or part not in node:
                # 交给既有解析器生成同一类缺路径错误。
                node = None
                break
            node = node[part]
        if has_unknown(node):
            if _REF.fullmatch(value):
                # 整体透传仍保留容器形状与已知兄弟字段，供嵌套流程继续预检。
                return node
            unknown = True
        else:
            resolve(step_id, {"value": "${" + ref + "}"}, scope, aliases)
    return _UNKNOWN if unknown else resolve(step_id, {"value": value}, scope, aliases)["value"]


def type_matches(value: Any, spec: dict) -> bool:
    if value is None:
        return spec.get("nullable", False) or spec["type"] == "null"
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "null": False,
    }[spec["type"]]


@dataclass
class PlannedStep:
    step_id: str
    type: str
    asset_id: str
    name: str | None
    params: dict
    output: str | None
    identity: str | None
    flow: Definition | None = None
    source: str | None = None
    extraction: dict | None = None
    calculation: dict | None = None
    transformation: dict | None = None

    def describe(self) -> dict:
        result = {
            "step_id": self.step_id,
            "type": self.type,
            "asset_id": self.asset_id,
            "name": self.name,
            "params": redact_json(self.params),
            "output": self.output,
            "identity": self.identity,
        }
        if self.flow:
            result["definition"] = self.flow.describe()
        if self.type == "map":
            result["source"] = self.source
        if self.extraction is not None:
            result.update(self.extraction)
            result["source"] = self.source
        if self.calculation is not None:
            result.update(self.calculation)
            if "operands" in self.params:
                result["operands"] = redact_json(self.params["operands"])
        if self.transformation is not None:
            result.update(self.transformation)
            result["source"] = self.source
        return result


@dataclass
class Definition:
    id: str
    kind: str
    name: str | None
    params: dict
    inputs: dict
    outputs: dict
    steps: list[PlannedStep] = field(default_factory=list)

    def describe(self) -> dict:
        return {
            "schema_version": "0.3",
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "params": redact_json(self.params),
            "inputs": self.inputs,
            "outputs": self.outputs,
            "steps": [step.describe() for step in self.steps],
        }


def load_definition(root: Path, asset_id: str, *, stack: tuple[str, ...] = ()) -> Definition:
    kind = asset_id.split(".")[0]
    if asset_id in stack or len(stack) >= 16:
        raise invalid("公共流程存在循环引用或超过 16 层深度")
    raw = read_document(root, asset_id, kind)
    allowed = {"schema_version", "id", "name", "steps"} | (
        {"params"} if kind == "case" else {"inputs", "outputs"}
    )
    keys(raw, allowed, asset_id)
    if raw.get("schema_version") != "0.3" or raw.get("id") != asset_id:
        raise invalid("场景版本或文件 ID 不匹配")
    if "name" in raw and (not isinstance(raw["name"], str) or not raw["name"]):
        raise invalid("场景 name 必须是非空字符串")
    try:
        params = _json_object(raw.get("params", {}), "params")
        outputs = _json_object(raw.get("outputs", {}), "outputs")
    except (ValueError, TypeError) as exc:
        raise invalid("场景参数或输出必须是 JSON 对象") from exc
    if contains_secret(params) or contains_secret(outputs):
        raise invalid("业务参数不得保存凭据；请使用调用身份")
    inputs = raw.get("inputs", {})
    if not isinstance(inputs, dict):
        raise invalid("flow.inputs 必须是对象")
    for name, spec in inputs.items():
        if not is_valid_step_id(name):
            raise invalid("输入名非法")
        keys(spec, {"type", "nullable"}, "输入契约")
        if (not isinstance(spec.get("type"), str) or spec["type"] not in _TYPES) or (
            "nullable" in spec and not isinstance(spec["nullable"], bool)
        ):
            raise invalid("输入类型或 nullable 无效")
    if kind == "flow" and (not inputs or not outputs):
        raise invalid("公共流程必须声明非空 inputs 和 outputs")
    for name in outputs:
        if not is_valid_step_id(name) or name in {"params", "inputs"}:
            raise invalid("输出名非法或保留")
    definition = Definition(asset_id, kind, raw.get("name"), params, inputs, outputs)
    if not isinstance(raw.get("steps"), list) or not raw["steps"]:
        raise invalid("steps 必须是非空数组")
    seen_ids: set[str] = set()
    aliases: set[str] = set()
    roots = {"params"} if kind == "case" else {"inputs"}
    for step in raw["steps"]:
        keys(
            step,
            {
                "step_id",
                "type",
                "api_id",
                "flow_id",
                "source",
                "name",
                "params",
                "output",
                "identity",
                "selector",
                "cardinality",
                "value_type",
                "nullable",
                "operation",
                "operands",
                "scale",
                "rounding",
                "path",
                "actual",
                "expected",
            },
            "步骤",
        )
        sid, stype = step.get("step_id"), step.get("type")
        if (
            not is_valid_step_id(sid)
            or sid in seen_ids
            or (
                not isinstance(stype, str)
                or stype not in {"api", "flow", "map", "extract", "decimal", "assert", "transform"}
            )
        ):
            raise invalid("步骤 ID 重复/非法或类型不支持")
        seen_ids.add(sid)
        if stype == "transform":
            keys(
                step,
                {"step_id", "type", "name", "operation", "source", "path", "output"},
                "transform",
            )
            if (
                step.get("operation") not in ("project", "positive_integer_sum")
                or not isinstance(step.get("source"), str)
                or not _REF.fullmatch(step["source"])
                or not valid_path(step.get("path"))
            ):
                raise invalid("transform 必须声明受限操作、完整 source 和结构化 path")
            for ref in references(step["source"]):
                parts = ref.split(".")
                if (
                    parts[0] not in roots | aliases
                    or (parts[0] in roots and len(parts) < 2)
                    or (parts[0] == "inputs" and parts[1] not in inputs)
                ):
                    raise invalid(f"步骤 {sid} 列表引用越界或尚未产生")
            output = step.get("output")
            if (
                not is_valid_step_id(output)
                or output in {"inputs", "params", "item", "index"} | aliases
            ):
                raise invalid("transform.output 非法、保留或重复")
            try:
                CaseStep(sid, "api", "api.transform.local", name=step.get("name"))
            except (TypeError, ValueError) as exc:
                raise invalid("transform 名称非法") from exc
            definition.steps.append(
                PlannedStep(
                    sid,
                    stype,
                    asset_id,
                    step.get("name"),
                    {"source": step["source"]},
                    output,
                    None,
                    source=step["source"],
                    transformation={"operation": step["operation"], "path": step["path"]},
                )
            )
            aliases.add(output)
            continue
        if stype == "assert":
            keys(step, {"step_id", "type", "name", "operation", "actual", "expected"}, "assert")
            if step.get("operation") not in {
                "non_empty",
                "equal",
                "positive_integer",
                "decimal_equal",
            }:
                raise invalid("断言 operation 无效")
            if not isinstance(step.get("actual"), str) or not _REF.fullmatch(step["actual"]):
                raise invalid("断言 actual 必须是单一完整引用")
            if step["operation"] in {"equal", "decimal_equal"} and "expected" not in step:
                raise invalid("相等断言必须提供 expected")
            if step["operation"] in {"non_empty", "positive_integer"} and "expected" in step:
                raise invalid("此断言不接受 expected")
            try:
                _json_object({"value": step.get("expected")}, "expected")
            except (TypeError, ValueError) as exc:
                raise invalid("断言 expected 必须为 JSON") from exc
            refs = [step["actual"], step.get("expected")]
            for ref in refs:
                for path in references(ref):
                    root_name = path.split(".")[0]
                    if root_name not in roots | aliases or (root_name in roots and "." not in path):
                        raise invalid(f"步骤 {sid} 断言引用越界或尚未产生")
                    if root_name == "inputs" and path.split(".")[1] not in inputs:
                        raise invalid(f"步骤 {sid} 断言引用未声明输入")
            definition.steps.append(
                PlannedStep(
                    sid,
                    stype,
                    asset_id,
                    step.get("name"),
                    {"actual": step["actual"], "expected": step.get("expected")},
                    None,
                    None,
                    calculation={"operation": step["operation"]},
                )
            )
            continue
        if stype == "decimal":
            keys(
                step,
                {
                    "step_id",
                    "type",
                    "name",
                    "operation",
                    "operands",
                    "scale",
                    "rounding",
                    "path",
                    "output",
                },
                "decimal",
            )
            operation = step.get("operation")
            if (
                operation not in ("add", "multiply", "sum")
                or type(step.get("scale")) is not int
                or not 0 <= step["scale"] <= 8
                or not isinstance(step.get("rounding"), str)
                or step["rounding"] not in ROUNDINGS
                or not valid_path(step.get("path", []))
                or ("path" in step and operation != "sum")
                or "operands" not in step
            ):
                raise invalid("decimal 运算、精度、舍入或路径无效")
            operands = step["operands"]
            if not isinstance(operands, list) and not (
                isinstance(operands, str) and _REF.fullmatch(operands)
            ):
                raise invalid("decimal.operands 必须是数组或单一完整引用")
            try:
                bindings = _json_object({"operands": operands}, "decimal")
            except (TypeError, ValueError) as exc:
                raise invalid("decimal 操作数必须为 JSON") from exc
            if contains_secret(bindings):
                raise invalid("decimal 操作数不得保存凭据")
            for ref in references(bindings):
                parts = ref.split(".")
                if (
                    parts[0] not in roots | aliases
                    or (parts[0] in roots and len(parts) < 2)
                    or (parts[0] == "inputs" and parts[1] not in inputs)
                ):
                    raise invalid(f"步骤 {sid} 金额引用越界或尚未产生")
            output = step.get("output")
            if (
                not is_valid_step_id(output)
                or output in {"inputs", "params", "item", "index"} | aliases
            ):
                raise invalid("decimal.output 非法、保留或重复")
            try:
                CaseStep(sid, "api", "api.decimal.local", name=step.get("name"))
            except (TypeError, ValueError) as exc:
                raise invalid("decimal 名称非法") from exc
            definition.steps.append(
                PlannedStep(
                    sid,
                    stype,
                    asset_id,
                    step.get("name"),
                    bindings,
                    output,
                    None,
                    calculation={
                        "operation": operation,
                        "scale": step["scale"],
                        "rounding": step["rounding"],
                        "path": step.get("path", []),
                    },
                )
            )
            aliases.add(output)
            continue
        if {"operation", "operands", "scale", "rounding", "path"} & step.keys():
            raise invalid("金额字段仅用于 decimal 步骤")
        if stype == "extract":
            forbidden = {"api_id", "flow_id", "params", "identity"}
            if forbidden & step.keys() or not isinstance(step.get("source"), str):
                raise invalid("extract 只接受 source 与受限选择器")
            selector = keys(step.get("selector"), {"path", "where"}, "extract.selector")
            if not valid_path(selector.get("path")):
                raise invalid("extract.path 必须是对象键或非负数组下标的列表")
            if "where" in selector:
                conditions = selector["where"]
                if not isinstance(conditions, list) or not conditions:
                    raise invalid("extract.where 必须是非空条件列表")
                for condition in conditions:
                    keys(condition, {"path", "equals"}, "extract.where")
                    if not valid_path(condition.get("path")) or "equals" not in condition:
                        raise invalid("extract.where 必须声明 path 和 equals")
            if (
                step.get("cardinality") not in ("one", "many")
                or not isinstance(step.get("value_type"), str)
                or step["value_type"] not in _TYPES
                or ("nullable" in step and not isinstance(step["nullable"], bool))
                or not is_valid_step_id(step.get("output"))
            ):
                raise invalid("extract 必须声明 cardinality、value_type 和 output")
            if contains_secret(selector):
                raise invalid("extract 条件不得保存凭据")
            bindings = {"source": step["source"], "selector": selector}
            for ref in references(bindings):
                parts = ref.split(".")
                if (
                    parts[0] not in roots | aliases
                    or (parts[0] in roots and len(parts) < 2)
                    or (parts[0] == "inputs" and parts[1] not in inputs)
                ):
                    raise invalid(f"步骤 {sid} 提取引用越界或尚未产生")
            if not _REF.fullmatch(step["source"]):
                raise invalid("extract.source 必须是单一完整变量引用")
            output = step["output"]
            if output in roots | {"inputs", "params", "item", "index"} | aliases:
                raise invalid("extract.output 非法、保留或重复")
            try:
                CaseStep(sid, "api", "api.extract.local", name=step.get("name"))
            except (TypeError, ValueError) as exc:
                raise invalid("extract 名称非法") from exc
            extraction = {
                "selector": selector,
                "cardinality": step["cardinality"],
                "value_type": step["value_type"],
                "nullable": step.get("nullable", False),
            }
            definition.steps.append(
                PlannedStep(
                    sid,
                    stype,
                    asset_id,
                    step.get("name"),
                    bindings,
                    output,
                    None,
                    source=step["source"],
                    extraction=extraction,
                )
            )
            aliases.add(output)
            continue
        if {"selector", "cardinality", "value_type", "nullable"} & step.keys():
            raise invalid("提取字段仅用于 extract 步骤")
        expected = "api_id" if stype == "api" else "flow_id"
        other = "flow_id" if stype == "api" else "api_id"
        target = step.get(expected)
        if (
            other in step
            or not isinstance(target, str)
            or not target.startswith("flow." if stype == "map" else stype + ".")
            or (stype == "map" and not isinstance(step.get("source"), str))
            or (stype != "map" and "source" in step)
        ):
            raise invalid("步骤目标类型不匹配")
        if stype in {"flow", "map"} and "identity" in step:
            raise invalid("公共流程不固定调用身份；请在 API 步骤选择身份")
        try:
            bindings = _json_object(step.get("params", {}), "params")
        except (TypeError, ValueError) as exc:
            raise invalid("步骤 params 必须是 JSON 对象") from exc
        if contains_secret(bindings):
            raise invalid("步骤参数不得保存凭据；请使用调用身份")
        for ref in references(bindings):
            root_name = ref.split(".")[0]
            local_roots = {"item", "index"} if stype == "map" else set()
            if (
                root_name not in roots | aliases | local_roots
                or root_name in roots
                and "." not in ref
            ):
                raise invalid(f"步骤 {sid} 引用越界或尚未产生的变量")
            if root_name == "inputs" and ref.split(".")[1] not in inputs:
                raise invalid(f"步骤 {sid} 引用未声明输入")
        output = step.get("output")
        if output is not None:
            if not is_valid_step_id(output) or output in {"inputs", "params"} or output in aliases:
                raise invalid("步骤 output 非法、保留或重复")
            aliases.add(output)
        try:
            # 复用身份与步骤名约束；flow 使用独立的目标字段。
            CaseStep(sid, "api", target, name=step.get("name"), identity=step.get("identity"))
        except (TypeError, ValueError) as exc:
            raise invalid(f"步骤 {sid} 名称或身份非法") from exc
        flow = (
            load_definition(root, target, stack=(*stack, asset_id))
            if stype in {"flow", "map"}
            else None
        )
        if flow and set(bindings) != set(flow.inputs):
            raise invalid(f"步骤 {sid} 输入与公共流程契约不匹配")
        source = step.get("source")
        if stype == "map":
            source_refs = list(references(source))
            if len(source_refs) != 1:
                raise invalid(f"步骤 {sid} map source 必须是单一变量引用")
            source_root = source_refs[0].split(".")[0]
            if source_root not in roots | aliases or (
                source_root in roots and "." not in source_refs[0]
            ):
                raise invalid(f"步骤 {sid} map source 引用越界或尚未产生")
            if source_root == "params" and source_refs[0].split(".")[1] not in params:
                raise invalid(f"步骤 {sid} map source 参数不存在")
        definition.steps.append(
            PlannedStep(
                sid,
                stype,
                target,
                step.get("name"),
                bindings,
                output,
                step.get("identity"),
                flow,
                source,
            )
        )
    for ref in references(outputs):
        parts = ref.split(".")
        if (
            parts[0] not in roots | aliases
            or (parts[0] in roots and len(parts) < 2)
            or (parts[0] == "inputs" and parts[1] not in inputs)
        ):
            raise invalid("flow 输出引用越界或未声明变量")
    return definition


def describe_case(case_id: str, assets_root: Path) -> dict | None:
    """公开、零请求的场景定义描述；公共流程不是独立入口。"""
    raw = read_document(assets_root, case_id, "case")
    if raw.get("schema_version") == "0.3":
        return load_definition(assets_root, case_id).describe()
    # 旧版详情保持文件级展示，不因新增描述能力提前触发旧版运行校验。
    return None


def resolve(step_id: str, value: dict, scope: dict, aliases: dict) -> dict:
    if "inputs" in scope:
        return resolve_step_params(
            step_id, value, case_params={}, aliases={**aliases, "inputs": scope["inputs"]}
        )
    return resolve_step_params(step_id, value, case_params=scope["params"], aliases=aliases)


def bindings_trace(value: Any, scope: dict, aliases: dict, sources: dict, path: str = "") -> list:
    records = []
    if isinstance(value, str):
        for ref in references(value):
            root = ref.split(".")[0]
            resolved = resolve("trace", {"value": "${" + ref + "}"}, scope, aliases)["value"]
            records.append(
                {
                    "target_path": path,
                    "reference": ref,
                    "source_node_id": sources.get(root),
                    "value": redact_json(resolved),
                    "value_type": next(
                        t
                        for t in (
                            "null",
                            "boolean",
                            "integer",
                            "number",
                            "string",
                            "object",
                            "array",
                        )
                        if type_matches(resolved, {"type": t})
                    ),
                }
            )
    elif isinstance(value, (dict, list)):
        for key, item in value.items() if isinstance(value, dict) else enumerate(value):
            records.extend(
                bindings_trace(item, scope, aliases, sources, f"{path}.{key}".lstrip("."))
            )
    return records


class ScenarioRunner:
    """递归公共流程调度，所有 HTTP 仍委托既有 ApiStepExecutor。"""

    def __init__(self, engine: Any):
        self.engine = engine
        self.api = ApiStepExecutor(engine)

    async def run(
        self, request: CaseExecutionRequest, definition: Definition
    ) -> CaseExecutionOutcome:
        params = {**definition.params, **_thaw_json(request.params)}
        if contains_secret(params):
            raise InvalidInputError(
                public_message="业务参数不得保存凭据；请使用调用身份",
                log_reason="sensitive scenario params",
            )
        self.request = request
        self.params = params
        self.counter = 0
        self._preflight(definition, {"params": params})
        started = time.perf_counter()
        self.pool = self.engine.new_session_pool()
        try:
            steps, _, _ = await self._execute(definition, {"params": params}, None)
        finally:
            await self.pool.close()
        result = CaseExecutionOutcome(
            definition.id,
            "success" if all(s.status == "success" for s in steps) else "failed",
            time.perf_counter() - started,
            tuple(steps),
            logins=self.pool.public_logins(),
            result_version="0.3",
        )
        error = self._system_error(steps)
        if error:
            from testplat_engine.interface import EngineTimeoutError, EngineTransportError

            cls = {"timeout": EngineTimeoutError, "transport": EngineTransportError}.get(
                error["kind"]
            )
            if cls:
                raise cls(partial_outcome=result)
            raise EngineInternalError("execution", partial_outcome=result)
        return result

    def _system_error(self, steps):
        for step in steps:
            if step.error and step.error.get("kind") in {"timeout", "transport", "internal"}:
                return step.error
        return None

    def _preflight(self, definition: Definition, scope: dict):
        """仅延迟未知叶值，保留同一绑定内其他已知路径的校验。"""
        aliases = {}
        for step in definition.steps:
            try:
                if step.type == "map":
                    items = preflight_value(step.step_id, step.source, scope, aliases)
                    if items is not _UNKNOWN and (not isinstance(items, list) or not items):
                        raise VariableResolutionError(
                            step.step_id, "source", "map 数据必须是非空数组"
                        )
                    # 未知 source 只做符号预检，不生成运行迭代或发送请求。
                    candidates = [_UNKNOWN] if items is _UNKNOWN else items
                    for index, item in enumerate(candidates):
                        values = preflight_value(
                            step.step_id,
                            step.params,
                            scope,
                            {
                                **aliases,
                                "item": item,
                                "index": _UNKNOWN if items is _UNKNOWN else index,
                            },
                        )
                        self._check_inputs(step.flow, values, preflight=True)
                        self._preflight(step.flow, {"inputs": values})
                    if step.output:
                        aliases[step.output] = _UNKNOWN
                    continue
                values = preflight_value(step.step_id, step.params, scope, aliases)
                if step.type == "assert":
                    # 仅检查引用；断言真假仍由运行节点判定。
                    continue
                if step.type in {"extract", "decimal", "transform"}:
                    if has_unknown(values):
                        aliases[step.output] = _UNKNOWN
                        continue
                    if step.transformation:
                        selected = transform(values["source"], **step.transformation)
                    else:
                        selected = (
                            calculate(values["operands"], **step.calculation)
                            if step.calculation
                            else self._extract(step, values)[0]
                        )
                    aliases[step.output] = strip_sensitive_json(selected)
                    continue
                if step.flow:
                    self._check_inputs(step.flow, values, preflight=True)
                    self._preflight(step.flow, {"inputs": values})
                else:
                    self.engine.preflight_api(
                        step.asset_id,
                        self.request.assets_root,
                        _thaw_json(self.request.environment),
                        auth=self.request.auth,
                        identity=step.identity,
                    )
                if step.output:
                    aliases[step.output] = _UNKNOWN
            except (VariableResolutionError, SelectionError, CalculationError) as exc:
                raise InvalidInputError(
                    public_message=f"步骤 {step.step_id} 场景输入预检失败",
                    log_reason="scenario input preflight failed",
                ) from exc
        # 响应依赖继续延迟；已知输入与已知局部结果中的导出路径必须存在。
        try:
            preflight_value("outputs", definition.outputs, scope, aliases)
        except VariableResolutionError as exc:
            raise InvalidInputError(
                public_message=f"场景 {definition.id} 输出引用缺失",
                log_reason="missing known scenario output",
            ) from exc

    def _extract(self, step, values):
        spec = step.extraction
        selected, count = select(values["source"], values["selector"], spec["cardinality"])
        items = selected if spec["cardinality"] == "many" else [selected]
        if any(
            not type_matches(value, {"type": spec["value_type"], "nullable": spec["nullable"]})
            for value in items
        ):
            raise SelectionError("提取值类型不符", count)
        return selected, count

    def _check_inputs(self, definition, values, *, preflight=False):
        for key, spec in definition.inputs.items():
            if preflight and values.get(key) is _UNKNOWN:
                continue
            if key not in values or not type_matches(values[key], spec):
                if preflight:
                    raise InvalidInputError(
                        public_message=f"公共流程 {definition.id} 输入 {key} 类型不符",
                        log_reason="flow input mismatch",
                    )
                raise VariableResolutionError(definition.id, key, "公共流程输入类型不符")

    async def _execute(self, definition: Definition, scope: dict, parent_id: str | None):
        aliases, sources, results = {}, {"params": "case-params", "inputs": parent_id}, []
        stopped = False
        skip_reason = "fail_fast"
        for step in definition.steps:
            self.counter += 1
            node_id = f"node-{self.counter}"
            trace = {
                "node_id": node_id,
                "parent_id": parent_id,
                "asset_id": step.asset_id,
                "inputs": {},
                "outputs": {},
                "bindings": [],
                "children": [],
            }
            if stopped:
                results.append(
                    StepOutcome(
                        step.step_id,
                        step.type,
                        "skipped",
                        0,
                        name=step.name,
                        skip_reason=skip_reason,
                        trace=trace,
                    )
                )
                continue
            started = time.perf_counter()
            try:
                if step.type == "transform":
                    evidence = {**step.transformation, "target": step.output, "status": "failed"}
                    trace["transforms"] = [evidence]
                    values = resolve(step.step_id, step.params, scope, aliases)
                    trace["bindings"] = bindings_trace(step.params, scope, aliases, sources)
                    try:
                        selected = transform(values["source"], **step.transformation)
                    except SelectionError as exc:
                        raise VariableResolutionError(step.step_id, "source", str(exc)) from exc
                    evidence.update(status="success", value=redact_json(selected))
                    trace["outputs"] = redact_json(selected)
                    aliases[step.output] = strip_sensitive_json(selected)
                    outcome = StepOutcome(
                        step.step_id,
                        step.type,
                        "success",
                        time.perf_counter() - started,
                        name=step.name,
                        output=step.output,
                        trace=trace,
                    )
                elif step.type == "assert":
                    values = resolve(step.step_id, step.params, scope, aliases)
                    actual = values["actual"]
                    expected = values.get("expected")
                    evidence = {
                        "operation": step.calculation["operation"],
                        "actual": redact_json(actual),
                        "expected": redact_json(expected),
                        "status": "failed",
                    }
                    trace["assertions"] = [evidence]
                    trace["inputs"] = redact_json(values)
                    trace["bindings"] = bindings_trace(step.params, scope, aliases, sources)
                    if not evaluate(step.calculation["operation"], actual, expected):
                        raise AssertionEvaluationError("断言失败")
                    evidence["status"] = "success"
                    outcome = StepOutcome(
                        step.step_id,
                        step.type,
                        "success",
                        time.perf_counter() - started,
                        name=step.name,
                        trace=trace,
                    )
                elif step.type == "decimal":
                    evidence = {**step.calculation, "target": step.output, "status": "failed"}
                    trace["calculations"] = [evidence]
                    values = resolve(step.step_id, step.params, scope, aliases)
                    trace["inputs"] = redact_json(values)
                    trace["bindings"] = bindings_trace(step.params, scope, aliases, sources)
                    try:
                        selected = calculate(values["operands"], **step.calculation)
                    except CalculationError as exc:
                        raise VariableResolutionError(step.step_id, "operands", str(exc)) from exc
                    evidence.update(status="success", value=selected)
                    trace["outputs"] = selected
                    aliases[step.output] = selected
                    outcome = StepOutcome(
                        step.step_id,
                        step.type,
                        "success",
                        time.perf_counter() - started,
                        name=step.name,
                        output=step.output,
                        trace=trace,
                    )
                elif step.type == "extract":
                    evidence = {
                        "source": step.source,
                        "selector": redact_json(step.extraction["selector"]),
                        "cardinality": step.extraction["cardinality"],
                        "value_type": step.extraction["value_type"],
                        "target": step.output,
                        "match_count": None,
                        "status": "failed",
                    }
                    trace["extracts"] = [evidence]
                    values = resolve(step.step_id, step.params, scope, aliases)
                    trace["bindings"] = bindings_trace(step.params, scope, aliases, sources)
                    try:
                        selected, count = self._extract(step, values)
                    except SelectionError as exc:
                        evidence["match_count"] = exc.count
                        raise VariableResolutionError(step.step_id, "selector", str(exc)) from exc
                    evidence.update(
                        match_count=count, status="success", value=redact_json(selected)
                    )
                    trace["outputs"] = redact_json(selected)
                    aliases[step.output] = strip_sensitive_json(selected)
                    outcome = StepOutcome(
                        step.step_id,
                        "extract",
                        "success",
                        time.perf_counter() - started,
                        name=step.name,
                        output=step.output,
                        trace=trace,
                    )
                elif step.type == "map":
                    items = resolve(step.step_id, {"source": step.source}, scope, aliases)["source"]
                    if not isinstance(items, list) or not items:
                        raise VariableResolutionError(
                            step.step_id, step.source or "", "map 数据必须是非空数组"
                        )
                    trace["inputs"] = {"count": len(items)}
                    trace["bindings"] = bindings_trace(
                        {"source": step.source}, scope, aliases, sources
                    )
                    # 动态数组产生后，先检查所有项，再允许首个迭代发请求。
                    for index, item in enumerate(items):
                        item_values = resolve(
                            step.step_id,
                            step.params,
                            scope,
                            {**aliases, "item": item, "index": index},
                        )
                        self._check_inputs(step.flow, item_values)
                        try:
                            self._preflight(step.flow, {"inputs": item_values})
                        except InvalidInputError as exc:
                            raise VariableResolutionError(
                                step.step_id, "source", "map 子流程输入预检失败"
                            ) from exc
                    mapped = []
                    failed_item = None
                    for index, item in enumerate(items):
                        self.counter += 1
                        item_node_id = f"node-{self.counter}"
                        item_values = resolve(
                            step.step_id,
                            step.params,
                            scope,
                            {**aliases, "item": item, "index": index},
                        )
                        item_trace = {
                            "node_id": item_node_id,
                            "parent_id": node_id,
                            "asset_id": step.asset_id,
                            "iteration_index": index,
                            "inputs": redact_json(item_values),
                            "outputs": {},
                            "bindings": bindings_trace(
                                step.params,
                                scope,
                                {**aliases, "item": item, "index": index},
                                sources,
                            ),
                            "children": [],
                        }
                        children, exported, export_error = await self._execute(
                            step.flow, {"inputs": item_values}, item_node_id
                        )
                        item_trace["children"] = [s.to_dict() for s in children]
                        failed = next((s for s in children if s.status != "success"), None)
                        if failed or export_error:
                            failed_item = StepOutcome(
                                f"{step.step_id}[{index}]",
                                "flow",
                                "failed",
                                0,
                                name=step.name,
                                error=_thaw_json(failed.error) if failed else export_error,
                                trace=item_trace,
                            )
                            mapped.append(failed_item)
                            for rest_index in range(index + 1, len(items)):
                                self.counter += 1
                                mapped.append(
                                    StepOutcome(
                                        f"{step.step_id}[{rest_index}]",
                                        "flow",
                                        "skipped",
                                        0,
                                        name=step.name,
                                        skip_reason="fail_fast",
                                        trace={
                                            "node_id": f"node-{self.counter}",
                                            "parent_id": node_id,
                                            "asset_id": step.asset_id,
                                            "iteration_index": rest_index,
                                            "inputs": {},
                                            "outputs": {},
                                            "bindings": [],
                                            "children": [],
                                        },
                                    )
                                )
                            break
                        item_trace["outputs"] = redact_json(exported)
                        mapped.append(
                            StepOutcome(
                                f"{step.step_id}[{index}]",
                                "flow",
                                "success",
                                0,
                                name=step.name,
                                output=step.output,
                                trace=item_trace,
                            )
                        )
                        if index == 0:
                            mapped_values = []
                        mapped_values.append(strip_sensitive_json(exported))
                    trace["children"] = [s.to_dict() for s in mapped]
                    if failed_item:
                        outcome = StepOutcome(
                            step.step_id,
                            "map",
                            "failed",
                            time.perf_counter() - started,
                            name=step.name,
                            error=_thaw_json(failed_item.error),
                            trace=trace,
                        )
                    else:
                        trace["outputs"] = redact_json(mapped_values)
                        outcome = StepOutcome(
                            step.step_id,
                            "map",
                            "success",
                            time.perf_counter() - started,
                            name=step.name,
                            output=step.output,
                            trace=trace,
                        )
                        if step.output:
                            aliases[step.output] = mapped_values
                elif step.flow:
                    values = resolve(step.step_id, step.params, scope, aliases)
                    trace["inputs"] = redact_json(values)
                    trace["bindings"] = bindings_trace(step.params, scope, aliases, sources)
                    self._check_inputs(step.flow, values)
                    children, exported, export_error = await self._execute(
                        step.flow, {"inputs": values}, node_id
                    )
                    trace["children"] = [s.to_dict() for s in children]
                    failed = next((s for s in children if s.status != "success"), None)
                    if failed or export_error:
                        outcome = StepOutcome(
                            step.step_id,
                            "flow",
                            "failed",
                            time.perf_counter() - started,
                            name=step.name,
                            error=_thaw_json(failed.error) if failed else export_error,
                            trace=trace,
                        )
                    else:
                        trace["outputs"] = redact_json(exported)
                        outcome = StepOutcome(
                            step.step_id,
                            "flow",
                            "success",
                            time.perf_counter() - started,
                            name=step.name,
                            output=step.output,
                            trace=trace,
                        )
                        if step.output:
                            aliases[step.output] = strip_sensitive_json(exported)
                else:
                    values = resolve(step.step_id, step.params, scope, aliases)
                    trace["inputs"] = redact_json(values)
                    trace["bindings"] = bindings_trace(step.params, scope, aliases, sources)
                    resolved_step = CaseStep(
                        step.step_id,
                        "api",
                        step.asset_id,
                        name=step.name,
                        params=values,
                        output=step.output,
                        identity=step.identity,
                    )
                    context = StepExecutionContext(
                        self.request.assets_root,
                        self.request.environment,
                        self.params,
                        timeout_seconds=self.request.timeout_seconds,
                        auth=self.request.auth,
                        session_pool=self.pool,
                    )
                    api_result = await self.api.execute_step(resolved_step, context)
                    if api_result.status == "success" and step.output:
                        aliases[step.output] = bind_alias_response(api_result.response)
                        trace["outputs"] = _thaw_json(aliases[step.output])
                    public = api_result.to_dict()
                    outcome = StepOutcome(
                        step_id=api_result.step_id,
                        type=api_result.type,
                        status=api_result.status,
                        duration_seconds=api_result.duration_seconds,
                        name=api_result.name,
                        request=public["request"],
                        response=public["response"],
                        error=public["error"],
                        output=api_result.output,
                        skip_reason=api_result.skip_reason,
                        auth=public["auth"],
                        trace=trace,
                    )
                if outcome.status == "success" and step.output:
                    sources[step.output] = node_id
            except AssertionEvaluationError:
                outcome = StepOutcome(
                    step.step_id,
                    step.type,
                    "failed",
                    time.perf_counter() - started,
                    name=step.name,
                    error={
                        "kind": "assertion_failed",
                        "message": "场景断言失败",
                        "step_id": step.step_id,
                    },
                    trace=trace,
                )
            except VariableResolutionError as exc:
                outcome = StepOutcome(
                    step.step_id,
                    step.type,
                    "failed",
                    time.perf_counter() - started,
                    name=step.name,
                    error=exc.to_error(),
                    trace=trace,
                )
            except EngineError as exc:
                outcome = StepOutcome(
                    step.step_id,
                    step.type,
                    "failed",
                    time.perf_counter() - started,
                    name=step.name,
                    error={
                        "kind": exc.kind
                        if exc.kind in {"timeout", "transport", "internal"}
                        else "internal",
                        "stage": "execution",
                        "message": exc.public_message,
                    },
                    trace=trace,
                )
            except Exception:
                outcome = StepOutcome(
                    step.step_id,
                    step.type,
                    "failed",
                    time.perf_counter() - started,
                    name=step.name,
                    error={"kind": "internal", "stage": "execution", "message": "公共流程执行失败"},
                    trace=trace,
                )
            results.append(outcome)
            stopped = outcome.status != "success"
            if stopped and outcome.error and outcome.error.get("kind") == "login_failed":
                skip_reason = "login_failed"
        exported, export_error = {}, None
        if not stopped and definition.kind == "flow":
            try:
                exported = resolve("outputs", definition.outputs, scope, aliases)
            except VariableResolutionError as exc:
                # 导出属于调用节点；不伪造一条 HTTP 请求。
                export_error = exc.to_error()
        return results, exported, export_error
