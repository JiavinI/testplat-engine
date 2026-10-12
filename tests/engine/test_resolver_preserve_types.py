"""resolver 的 ``preserve_types`` 选项与 alias 脱敏移除的单元测试。"""

from __future__ import annotations

from testplat_engine.domain.api import Api
from testplat_engine.engine.resolver import resolve_variables
from testplat_engine.shared.redact import redact_json, strip_sensitive_json


def _api() -> Api:
    return Api(
        id="api.demo.x",
        name="x",
        host="local",
        method="POST",
        url="/x",
        request={
            "headers": {"X-Id": "${v}"},
            "body": {"whole": "${v}", "embedded": "id-${v}", "list": ["${v}"]},
        },
        success={"value": "${v}"},
    )


def test_default_keeps_legacy_string_interpolation() -> None:
    resolved = resolve_variables(_api(), {"v": 42})
    assert resolved["body"] == {"whole": "42", "embedded": "id-42", "list": ["42"]}


def test_preserve_types_only_changes_whole_placeholders() -> None:
    resolved = resolve_variables(_api(), {"v": {"k": [1, True]}}, preserve_types=True)
    assert resolved["body"]["whole"] == {"k": [1, True]}
    assert resolved["body"]["list"] == [{"k": [1, True]}]
    assert resolved["body"]["embedded"].startswith("id-")  # 嵌入仍是字符串插值
    assert resolved["success"]["value"] == {"k": [1, True]}
    # headers 永远是字符串。
    assert isinstance(resolved["headers"]["X-Id"], str)


def test_preserve_types_returns_independent_copies() -> None:
    value = {"k": [1]}
    resolved = resolve_variables(_api(), {"v": value}, preserve_types=True)
    resolved["body"]["whole"]["k"].append(2)
    assert value == {"k": [1]}
    assert resolved["body"]["list"][0] == {"k": [1]}


def test_preserve_types_missing_param_keeps_literal_like_legacy() -> None:
    resolved = resolve_variables(_api(), {}, preserve_types=True)
    assert resolved["body"]["whole"] == "${v}"


def test_strip_removes_sensitive_keys_while_redact_replaces() -> None:
    data = {"Token": "t", "a": {"PASSWORD": "p", "b": [{"api_key": "k", "n": 1}]}}
    assert strip_sensitive_json(data) == {"a": {"b": [{"n": 1}]}}
    assert redact_json(data)["Token"] == "***"
