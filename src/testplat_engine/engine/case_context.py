"""Case 执行上下文：参数/alias 引用解析与 alias 安全绑定。

所有权约定（本模块是 CaseRunner 唯一读写 alias 的地方）：

- ``params`` 根：只读的合并后 Case 参数（资产默认值被请求顶层键覆盖）。
- alias 根：只有"已成功完成且显式声明 ``output``"的步骤才会写入；写入值只来自
  安全输出模型 ``{status_code, headers, body}``，且敏感键已被**移除**（不是替换为
  ``***``），所以被脱敏的值永远不会流到下游。
- 引用在步骤执行**前**解析；无法解析时抛 ``VariableResolutionError``，
  由 CaseRunner 转成该步骤的业务 ``failed``（``variable_resolution``），绝不降级为空串。

这里只实现 Case 层的 ``${params.x}`` / ``${alias.path}`` 引用；API 资产内部的
``${name}`` 渲染仍由 ``engine.resolver`` 负责，二者不重叠。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from types import MappingProxyType

from testplat_engine.interface import (
    JsonObject,
    JsonValue,
    _freeze_json,
    _json_value,
    _thaw_json,
)
from testplat_engine.shared.redact import strip_sensitive_json

# loader 已在 preflight 严格校验过占位符语法；这里只负责取值，不再校验语法。
_PLACEHOLDER_RE = re.compile(r"\$\{([^{}]*)\}")

# alias 只暴露这三个键；request/error/完整 StepOutcome 一律不可引用。
_ALIAS_KEYS = ("status_code", "headers", "body")


class VariableResolutionError(Exception):
    """步骤引用在运行时无法解析；携带安全的 step_id 与引用路径（不含值）。"""

    def __init__(self, step_id: str, path: str, reason: str) -> None:
        self.step_id = step_id
        self.path = path
        self.reason = reason
        super().__init__(f"步骤 {step_id} 的引用 ${{{path}}} 无法解析: {reason}")

    def to_error(self) -> JsonObject:
        """业务失败的 error 载荷：只含 kind、message、step_id、path，不含任何值。"""
        return {
            "kind": "variable_resolution",
            "message": str(self),
            "step_id": self.step_id,
            "path": self.path,
        }


class AliasShapeError(ValueError):
    """步骤结果不满足安全输出模型，无法绑定为 alias（属于 Engine/executor 缺陷）。"""


def bind_alias_response(response: object) -> Mapping[str, object]:
    """把步骤 response 收敛为只读的安全 alias 值：status_code / headers / body。

    - 形状不符（非对象、缺键、status_code 非整数）→ ``AliasShapeError``，
      由 CaseRunner 归为 internal，而不是静默绑定半成品。
    - 敏感键递归移除；返回深冻结快照，下游步骤/executor 无法改写。
    """
    if not isinstance(response, Mapping):
        raise AliasShapeError("声明了 output 的步骤 response 必须是对象")
    missing = [key for key in _ALIAS_KEYS if key not in response]
    if missing:
        raise AliasShapeError(f"response 缺少安全输出字段: {', '.join(missing)}")
    status_code = response["status_code"]
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise AliasShapeError("response.status_code 必须是整数")
    headers = response["headers"]
    if not isinstance(headers, Mapping):
        raise AliasShapeError("response.headers 必须是对象")
    safe = {
        "status_code": status_code,
        "headers": strip_sensitive_json(headers),
        "body": strip_sensitive_json(response["body"]),
    }
    frozen = _freeze_json(_json_value(safe, "alias"))
    assert isinstance(frozen, MappingProxyType)
    return frozen


def resolve_step_params(
    step_id: str,
    params: Mapping[str, JsonValue],
    *,
    case_params: Mapping[str, object],
    aliases: Mapping[str, object],
) -> JsonObject:
    """解析步骤 params 中的 Case 层引用，返回全新的纯 JSON dict。

    - 整个字符串恰好是一个占位符：保留引用值的 JSON 类型（数字、布尔、对象、null）。
    - 占位符嵌入普通字符串：只允许 JSON 标量，按字符串插值；对象/数组 → 失败。
    - 路径只支持点分对象键；中途缺失或遇到非对象 → 失败，不降级。
    """

    def lookup(ref: str) -> JsonValue:
        root, _, rest = ref.partition(".")
        if root == "params":
            node: object = case_params
            if not rest:
                raise VariableResolutionError(step_id, ref, "params 引用必须指向具体键")
        elif root in aliases:
            node = aliases[root]
        else:
            raise VariableResolutionError(step_id, ref, f"alias {root} 尚未产生")
        if rest:
            for part in rest.split("."):
                if not isinstance(node, Mapping) or part not in node:
                    raise VariableResolutionError(step_id, ref, f"路径 {part} 不存在")
                node = node[part]
        return _thaw_json(node)

    def render(text: str) -> JsonValue:
        whole = _PLACEHOLDER_RE.fullmatch(text)
        if whole is not None:
            return lookup(whole.group(1))

        def substitute(match: re.Match[str]) -> str:
            ref = match.group(1)
            value = lookup(ref)
            if isinstance(value, (dict, list)):
                raise VariableResolutionError(step_id, ref, "对象/数组不能嵌入字符串插值")
            if isinstance(value, str):
                return value
            # bool / None / 数字：使用 JSON 文本（true / null / 42），不用 Python repr。
            return json.dumps(value)

        return _PLACEHOLDER_RE.sub(substitute, text)

    def walk(node: JsonValue) -> JsonValue:
        if isinstance(node, str):
            return render(node)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, Mapping):
            return {key: walk(item) for key, item in node.items()}
        return node

    resolved = walk(dict(params))
    assert isinstance(resolved, dict)
    return resolved
