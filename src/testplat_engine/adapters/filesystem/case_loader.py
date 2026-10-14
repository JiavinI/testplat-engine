"""Case 资产加载与严格校验（Engine 0.2）。

路径权威：``case.demo.example`` → ``{assets_root}/scenarios/demo/example.case.yaml``。
未知 schema 字段、旧键别名与 assert DSL 一律在 preflight 拒绝。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from testplat_engine.adapters.filesystem.yaml_loader import load_yaml
from testplat_engine.domain.case import (
    API_STEP_KEYS,
    CASE_SCHEMA_VERSION,
    CASE_TOP_LEVEL_KEYS,
    SUPPORTED_STEP_TYPES,
    Case,
    CaseStep,
    is_valid_case_id,
    is_valid_step_id,
    merge_case_params,
)
from testplat_engine.interface import (
    AssetNotFoundError,
    InvalidAssetError,
    InvalidInputError,
    JsonObject,
    JsonValue,
    _json_object,
)

# 下标引用显式禁止。
_STEPS_INDEX_RE = re.compile(r"^steps\.\d+")


def _format_keys(keys: set[object]) -> str:
    """安全格式化键集合，避免非字符串键触发 TypeError。"""
    return ", ".join(sorted(str(k) for k in keys))


def _reject_non_string_keys(mapping: Mapping[Any, Any], *, where: str) -> None:
    """schema-owned 对象的字段名必须是字符串；否则 invalid_asset/preflight。"""
    bad = [k for k in mapping.keys() if not isinstance(k, str)]
    if not bad:
        return
    rendered = ", ".join(repr(k) for k in bad)
    raise InvalidAssetError(
        public_message=f"{where} 字段名必须是字符串，收到非字符串键: {rendered}",
        log_reason=f"non-string keys at {where}: {bad!r}",
    )


def case_id_to_path(case_id: str, assets_root: Path) -> Path:
    """将 Case id 映射为 ``.case.yaml`` 路径，并强制落在 scenarios 根内。"""
    if not is_valid_case_id(case_id):
        raise InvalidAssetError(
            public_message=f"Case id 非法: {case_id}",
            log_reason=f"invalid case_id format: {case_id!r}",
        )
    # case.<segments...> → scenarios/<segments...>.case.yaml
    parts = case_id.split(".")
    assert parts[0] == "case"
    segments = parts[1:]
    relative = Path("scenarios", *segments[:-1], f"{segments[-1]}.case.yaml")
    assets_root = Path(assets_root)
    candidate = (assets_root / relative).resolve()
    scenarios_root = (assets_root / "scenarios").resolve()
    if not candidate.is_relative_to(scenarios_root):
        raise InvalidAssetError(
            public_message=f"Case 路径逃逸 assets_root/scenarios: {case_id}",
            log_reason=f"resolved={candidate}",
        )
    return candidate


def load_case(case_id: str, assets_root: Path) -> Case:
    """按规范路径加载并校验 Case 资产；文件内 id 必须精确等于请求 id。"""
    file_path = case_id_to_path(case_id, assets_root)
    try:
        data = load_yaml(file_path)
    except FileNotFoundError as exc:
        raise AssetNotFoundError(
            public_message=f"Case 资产不存在: {case_id}",
            log_reason=str(exc),
        ) from exc
    except ValueError as exc:
        raise InvalidAssetError(
            public_message=f"Case 资产无效: {case_id}",
            log_reason=str(exc),
        ) from exc

    case = parse_case_dict(data, expected_case_id=case_id)
    return case


def parse_case_dict(data: Mapping[str, Any], *, expected_case_id: str | None = None) -> Case:
    """将字典解析为 Case，并做严格 schema / 引用校验。"""
    if not isinstance(data, Mapping):
        raise InvalidAssetError(
            public_message="Case 根节点必须是对象",
            log_reason=f"type={type(data).__name__}",
        )

    # S2：先拒绝非字符串键，再用安全格式化报告未知字段。
    _reject_non_string_keys(data, where="Case 顶层")
    unknown_top = set(data.keys()) - CASE_TOP_LEVEL_KEYS
    if unknown_top:
        keys = _format_keys(unknown_top)
        raise InvalidAssetError(
            public_message=f"Case 含未知字段: {keys}",
            log_reason=f"unknown top-level keys: {sorted(str(k) for k in unknown_top)}",
        )

    schema_version = data.get("schema_version")
    if schema_version != CASE_SCHEMA_VERSION:
        raise InvalidAssetError(
            public_message=f'Case schema_version 必须为 "{CASE_SCHEMA_VERSION}"',
            log_reason=f"schema_version={schema_version!r}",
        )

    case_id = data.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise InvalidAssetError(
            public_message="Case id 必填",
            log_reason="missing or empty id",
        )
    if not is_valid_case_id(case_id):
        raise InvalidAssetError(
            public_message=f"Case id 非法: {case_id}",
            log_reason=f"id={case_id!r}",
        )
    if expected_case_id is not None and case_id != expected_case_id:
        # A1：不允许大小写或连字符自动归一。
        raise InvalidAssetError(
            public_message=(f"Case 文件内 id ({case_id}) 与请求 id ({expected_case_id}) 不一致"),
            log_reason=f"file_id={case_id!r} request_id={expected_case_id!r}",
        )

    name = data.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        raise InvalidAssetError(
            public_message="Case name 若提供则必须为非空字符串",
            log_reason=f"name={name!r}",
        )
    desc = data.get("desc")
    if desc is not None and not isinstance(desc, str):
        raise InvalidAssetError(
            public_message="Case desc 必须是字符串",
            log_reason=f"desc type={type(desc).__name__}",
        )

    # P4：缺省 → 默认 []；显式 null → 拒绝（与 missing 语义分离）。
    if "tags" in data:
        tags_raw = data["tags"]
        if tags_raw is None:
            raise InvalidAssetError(
                public_message="Case tags 不得为 null（缺省请省略该字段）",
                log_reason="tags=null",
            )
    else:
        tags_raw = []
    if not isinstance(tags_raw, list):
        raise InvalidAssetError(
            public_message="Case tags 必须是字符串数组",
            log_reason=f"tags type={type(tags_raw).__name__}",
        )
    tags: list[str] = []
    for tag in tags_raw:
        if not isinstance(tag, str) or not tag:
            raise InvalidAssetError(
                public_message="Case tags 元素必须为非空字符串",
                log_reason=f"tag={tag!r}",
            )
        tags.append(tag)

    # P4：顶层 params 显式 null 非法；缺省才回落为 {}。
    if "params" in data:
        params_raw = data["params"]
        if params_raw is None:
            raise InvalidAssetError(
                public_message="Case params 不得为 null（缺省请省略该字段）",
                log_reason="params=null",
            )
    else:
        params_raw = {}
    try:
        params = _json_object(params_raw, "params")
    except (TypeError, ValueError) as exc:
        raise InvalidAssetError(
            public_message="Case params 必须是 JSON 对象",
            log_reason=str(exc),
        ) from exc

    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise InvalidAssetError(
            public_message="Case steps 必须是非空数组",
            log_reason=f"steps={steps_raw!r}",
        )

    steps = [_parse_step(item, index=index) for index, item in enumerate(steps_raw)]
    _validate_step_uniqueness(steps)
    _validate_reference_graph(steps)

    try:
        return Case(
            schema_version=schema_version,
            id=case_id,
            name=name,
            desc=desc,
            tags=tuple(tags),
            params=params,
            steps=tuple(steps),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidAssetError(
            public_message=f"Case 资产无效: {exc}",
            log_reason=str(exc),
        ) from exc


def validate_case_request_params(case: Case, request_params: Mapping[str, JsonValue]) -> JsonObject:
    """合并参数并检查 ``${params.*}`` 是否可在执行前解析。

    缺失键 → ``invalid_input`` / preflight（Control 后续映射 422），不创建记录。
    """
    try:
        request_obj = _json_object(dict(request_params), "params")
    except (TypeError, ValueError) as exc:
        raise InvalidInputError(
            public_message="Case 请求 params 必须是 JSON 对象",
            log_reason=str(exc),
        ) from exc

    merged = merge_case_params(dict(case.params), request_obj)
    missing: list[str] = []
    for step in case.steps:
        for ref in _iter_placeholders(step.params):
            root, _, rest = ref.partition(".")
            if root != "params":
                continue
            # P2：沿完整对象路径行走；中途缺失或非对象 → invalid_input。
            if _params_path_unresolvable(merged, rest):
                missing.append(ref)
    if missing:
        # 去重但保持发现顺序，便于稳定断言。
        ordered = list(dict.fromkeys(missing))
        raise InvalidInputError(
            public_message=f"Case 参数缺失: {', '.join(ordered)}",
            log_reason=f"missing params refs={ordered}",
        )
    return merged


def _parse_step(raw: Any, *, index: int) -> CaseStep:
    if not isinstance(raw, Mapping):
        raise InvalidAssetError(
            public_message=f"步骤[{index}] 必须是对象",
            log_reason=f"type={type(raw).__name__}",
        )

    _reject_non_string_keys(raw, where=f"步骤[{index}]")
    unknown = set(raw.keys()) - API_STEP_KEYS
    # 即便 type 非 api，也先拒绝未知键与旧别名，避免静默吞掉未实现能力。
    if unknown:
        keys = _format_keys(unknown)
        raise InvalidAssetError(
            public_message=f"步骤[{index}] 含未知字段: {keys}",
            log_reason=f"unknown step keys: {sorted(str(k) for k in unknown)}",
        )

    step_id = raw.get("step_id")
    if not isinstance(step_id, str) or not is_valid_step_id(step_id):
        raise InvalidAssetError(
            public_message=f"步骤[{index}] step_id 非法",
            log_reason=f"step_id={step_id!r}",
        )

    step_type = raw.get("type")
    if not isinstance(step_type, str) or not step_type:
        raise InvalidAssetError(
            public_message=f"步骤[{index}] type 必填",
            log_reason=f"type={step_type!r}",
        )
    if step_type not in SUPPORTED_STEP_TYPES:
        raise InvalidAssetError(
            public_message=f"不支持的步骤 type: {step_type}",
            log_reason=f"step_id={step_id} type={step_type!r}",
        )

    api_id = raw.get("api_id")
    if not isinstance(api_id, str) or not api_id:
        raise InvalidAssetError(
            public_message=f"步骤 {step_id} 的 api_id 必填",
            log_reason=f"api_id={api_id!r}",
        )

    name = raw.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        raise InvalidAssetError(
            public_message=f"步骤 {step_id} 的 name 若提供则必须非空",
            log_reason=f"name={name!r}",
        )

    # P4：步骤 params 显式 null 非法；缺省才回落为 {}。
    if "params" in raw:
        params_raw = raw["params"]
        if params_raw is None:
            raise InvalidAssetError(
                public_message=f"步骤 {step_id} 的 params 不得为 null（缺省请省略该字段）",
                log_reason=f"step={step_id} params=null",
            )
    else:
        params_raw = {}
    try:
        params = _json_object(params_raw, "params")
    except (TypeError, ValueError) as exc:
        raise InvalidAssetError(
            public_message=f"步骤 {step_id} 的 params 必须是 JSON 对象",
            log_reason=str(exc),
        ) from exc

    output = raw.get("output")
    if output is not None:
        if not isinstance(output, str) or not is_valid_step_id(output):
            raise InvalidAssetError(
                public_message=f"步骤 {step_id} 的 output alias 非法",
                log_reason=f"output={output!r}",
            )
        if output == "params":
            raise InvalidAssetError(
                public_message=f"步骤 {step_id} 的 output 不得为保留名 params",
                log_reason="output=params",
            )

    try:
        return CaseStep(
            step_id=step_id,
            type=step_type,
            api_id=api_id,
            name=name,
            params=params,
            output=output,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidAssetError(
            public_message=f"步骤 {step_id} 无效: {exc}",
            log_reason=str(exc),
        ) from exc


def _validate_step_uniqueness(steps: list[CaseStep]) -> None:
    seen_ids: set[str] = set()
    seen_outputs: set[str] = set()
    for step in steps:
        if step.step_id in seen_ids:
            raise InvalidAssetError(
                public_message=f"重复 step_id: {step.step_id}",
                log_reason=f"duplicate step_id={step.step_id}",
            )
        seen_ids.add(step.step_id)
        if step.output is not None:
            if step.output in seen_outputs:
                raise InvalidAssetError(
                    public_message=f"重复 output alias: {step.output}",
                    log_reason=f"duplicate output={step.output}",
                )
            seen_outputs.add(step.output)


def _validate_reference_graph(steps: list[CaseStep]) -> None:
    """静态校验跨步引用：只允许 params 与更早步骤已声明的 alias。"""
    declared_aliases: set[str] = set()
    for step in steps:
        for ref in _iter_placeholders(step.params):
            _validate_one_ref(ref, step.step_id, declared_aliases)
        if step.output is not None:
            declared_aliases.add(step.output)


def _validate_one_ref(ref: str, step_id: str, declared_aliases: set[str]) -> None:
    if not ref or ref.startswith(".") or ref.endswith(".") or ".." in ref:
        raise InvalidAssetError(
            public_message=f"步骤 {step_id} 含非法引用: ${{{ref}}}",
            log_reason=f"malformed ref={ref!r}",
        )
    if _STEPS_INDEX_RE.match(ref) or ref.startswith("steps."):
        raise InvalidAssetError(
            public_message=f"步骤 {step_id} 禁止下标引用: ${{{ref}}}",
            log_reason=f"index ref={ref!r}",
        )
    # 第一批路径只支持点分键；拒绝方括号等。
    if "[" in ref or "]" in ref:
        raise InvalidAssetError(
            public_message=f"步骤 {step_id} 含不支持的引用路径: ${{{ref}}}",
            log_reason=f"unsupported path={ref!r}",
        )

    root, _, _rest = ref.partition(".")
    if not root:
        raise InvalidAssetError(
            public_message=f"步骤 {step_id} 含非法引用: ${{{ref}}}",
            log_reason=f"empty root ref={ref!r}",
        )
    if root == "params":
        return
    if root not in declared_aliases:
        raise InvalidAssetError(
            public_message=f"步骤 {step_id} 引用未声明或尚未产生的 alias: ${{{ref}}}",
            log_reason=f"undeclared/forward alias root={root!r} ref={ref!r}",
        )


def _params_path_unresolvable(merged: Mapping[str, JsonValue], rest: str) -> bool:
    """判断 ``params.<rest>`` 是否无法在合并参数中解析。

    缺失任一片段，或中途节点不是对象，均视为不可解析（P2）。
    """
    if not rest:
        return True
    segments = rest.split(".")
    if any(part == "" for part in segments):
        return True
    node: object = merged
    for part in segments:
        if not isinstance(node, Mapping):
            return True
        if part not in node:
            return True
        node = node[part]
    return False


def _scan_placeholders_in_string(text: str) -> list[str]:
    """整串扫描占位符；未闭合 / 空 / 多余花括号一律拒绝，禁止静默降级（S3）。"""
    refs: list[str] = []
    cursor = 0
    while True:
        start = text.find("${", cursor)
        if start < 0:
            break
        close = text.find("}", start + 2)
        if close < 0:
            snippet = text[start : start + 32]
            raise InvalidAssetError(
                public_message=f"占位符未闭合: {snippet}",
                log_reason=f"unclosed placeholder at {start}: {snippet!r}",
            )
        inner = text[start + 2 : close]
        if inner == "":
            raise InvalidAssetError(
                public_message="占位符不能为空: ${}",
                log_reason="empty placeholder ${}",
            )
        # 多余花括号：${{a}} / ${a{b}} 等，内容中不得再含 { 或 }。
        if "{" in inner or "}" in inner:
            raise InvalidAssetError(
                public_message=f"占位符含多余花括号: ${{{inner}}}",
                log_reason=f"extra braces in placeholder={inner!r}",
            )
        refs.append(inner)
        cursor = close + 1
    return refs


def _iter_placeholders(value: Any) -> list[str]:
    """收集 params 树中全部占位符路径（不含 ${}）；畸形语法在此拒绝。"""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            found.extend(_scan_placeholders_in_string(node))

    walk(value)
    return found
