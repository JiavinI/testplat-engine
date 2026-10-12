"""3.3a 公开 DTO：SecretStr / StaticHeaderAuth / UnavailableAuth / AuthTable 与请求 auth 字段。

覆盖设计稿 §6-1 的 Engine 部分（to_dict / repr 不含秘密）与 M1（构造错误消息不含秘密值）。
"""

from __future__ import annotations

import ast
import copy
import json
import pickle
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from testplat_engine import (
    ApiExecutionOutcome,
    ApiExecutionRequest,
    AuthTable,
    CaseExecutionRequest,
    SecretStr,
    StaticHeaderAuth,
    StepOutcome,
    UnavailableAuth,
)
from tests.engine.auth_support import (
    ADMIN_HEADER_VALUE,
    ADMIN_SECRET,
    PARTNER_SECRET,
    admin_material,
    assert_no_secret,
    auth_table,
    unavailable,
)

SRC_ROOT = Path(__file__).parents[1] / "src" / "testplat_engine"


# ------------------------------------------------------------------ SecretStr


def test_secret_str_never_renders_value() -> None:
    secret = SecretStr(ADMIN_SECRET)
    assert repr(secret) == "***"
    assert str(secret) == "***"
    assert f"{secret}" == "***"
    assert f"{secret!r}" == "***"
    assert format(secret, ">10") == "***"
    assert secret.reveal() == ADMIN_SECRET


@pytest.mark.parametrize(
    "action",
    [
        lambda s: json.dumps(s),
        lambda s: json.dumps({"k": s}),
        lambda s: pickle.dumps(s),
        lambda s: copy.copy(s),
        lambda s: copy.deepcopy(s),
        lambda s: s.to_dict(),
    ],
    ids=["json", "json-nested", "pickle", "copy", "deepcopy", "to_dict"],
)
def test_secret_str_serialization_raises(action: Callable[[SecretStr], Any]) -> None:
    secret = SecretStr(ADMIN_SECRET)
    with pytest.raises(TypeError) as exc_info:
        action(secret)
    assert ADMIN_SECRET not in str(exc_info.value)


def test_secret_str_immutable_and_rejects_non_str() -> None:
    secret = SecretStr(ADMIN_SECRET)
    with pytest.raises(AttributeError):
        secret._value = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        SecretStr(12345678)  # type: ignore[arg-type]


def test_reveal_called_only_in_injection_module() -> None:
    """全仓只有注入模块调用 reveal()（AST 约束，防止未来新增泄密路径）。"""
    callers: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "reveal"
            ):
                callers.append(path.relative_to(SRC_ROOT).as_posix())
    assert callers, "未找到 reveal 调用，注入点缺失"
    assert set(callers) == {"engine/auth_injection.py"}


# ------------------------------------------------------------- 材料与身份表


def test_static_header_auth_repr_and_to_dict_hide_values() -> None:
    material = admin_material()
    assert material.to_dict() == {"kind": "static_headers", "header_names": ["Authorization"]}
    assert_no_secret(repr(material), str(material), material.to_dict())
    with pytest.raises(TypeError):
        pickle.dumps(material)
    with pytest.raises(TypeError):
        material.headers["X"] = SecretStr(ADMIN_SECRET)  # type: ignore[index]


def test_auth_table_to_dict_and_repr_only_names() -> None:
    table = auth_table(partner=unavailable())
    assert table.to_dict() == {
        "identities": {
            "admin": {"kind": "static_headers", "header_names": ["Authorization"]},
            "partner": {"kind": "unavailable"},
        },
        "default_identity": "admin",
    }
    assert_no_secret(repr(table), str(table), table.to_dict())


def test_requests_to_dict_include_auth_summary_without_secrets() -> None:
    table = auth_table()
    api = ApiExecutionRequest(
        api_id="api.auth.admin_get", assets_root=Path("/a"), environment={}, params={}, auth=table
    )
    case = CaseExecutionRequest(
        case_id="case.auth.x", assets_root=Path("/a"), environment={}, params={}, auth=table
    )
    for request in (api, case):
        dumped = request.to_dict()
        assert dumped["auth"] == table.to_dict()
        assert_no_secret(dumped, repr(request), str(request))


def test_requests_without_auth_match_v020_shape() -> None:
    """auth=None 时 to_dict 与 v0.2.0 逐键一致（不新增 auth 键）。"""
    api = ApiExecutionRequest(api_id="a", assets_root=Path("/a"), environment={}, params={})
    case = CaseExecutionRequest(case_id="c", assets_root=Path("/a"), environment={}, params={})
    assert set(api.to_dict()) == {"api_id", "assets_root", "environment", "params",
                                  "timeout_seconds"}
    assert set(case.to_dict()) == {"case_id", "assets_root", "environment", "params",
                                   "timeout_seconds"}
    assert api.auth is None and case.auth is None


def test_request_auth_must_be_auth_table() -> None:
    with pytest.raises(TypeError):
        ApiExecutionRequest(
            api_id="a", assets_root=Path("/a"), environment={}, params={},
            auth={"admin": ADMIN_SECRET},  # type: ignore[arg-type]
        )


def test_auth_table_rejects_reserved_none_and_dangling_default() -> None:
    with pytest.raises(ValueError):
        AuthTable(identities={"none": admin_material()})
    with pytest.raises(ValueError):
        AuthTable(identities={"admin": admin_material()}, default_identity="ghost")


def test_auth_table_accepts_unknown_kind_for_preflight() -> None:
    """未知 kind 可构造（跨版本前向兼容），在被用到时由预检拒绝。"""
    table = AuthTable(identities={"future": SimpleNamespace(kind="login_token")})
    assert table.to_dict()["identities"] == {"future": {"kind": "login_token"}}


def test_unavailable_auth_requires_reason() -> None:
    assert UnavailableAuth(public_reason="变量 TESTPLAT_X 缺失").to_dict() == {
        "kind": "unavailable"
    }
    with pytest.raises(TypeError):
        UnavailableAuth(public_reason="")


# ----------------------------------------------------- M1：构造错误不回显值

_BAD_CONSTRUCTIONS: list[tuple[str, Callable[[], object]]] = [
    ("secretstr-non-str", lambda: SecretStr(ADMIN_SECRET.encode())),  # type: ignore[arg-type]
    ("header-raw-str", lambda: StaticHeaderAuth(headers={"Authorization": ADMIN_SECRET})),
    ("header-name-is-secret", lambda: StaticHeaderAuth(
        headers={f"bad name {ADMIN_SECRET}": SecretStr(ADMIN_SECRET)})),
    ("header-dup-case", lambda: StaticHeaderAuth(headers={
        "Authorization": SecretStr(ADMIN_SECRET), "authorization": SecretStr(PARTNER_SECRET)})),
    ("header-short", lambda: StaticHeaderAuth(headers={"X-Key": SecretStr("tp-fake")})),
    ("raw-short", lambda: StaticHeaderAuth(
        headers={"Authorization": SecretStr(ADMIN_HEADER_VALUE)},
        secret_values=(SecretStr("tp-fak7"),))),
    ("raw-not-secretstr", lambda: StaticHeaderAuth(
        headers={"Authorization": SecretStr(ADMIN_HEADER_VALUE)},
        secret_values=(ADMIN_SECRET,))),  # type: ignore[arg-type]
    ("raw-is-str", lambda: StaticHeaderAuth(
        headers={"Authorization": SecretStr(ADMIN_HEADER_VALUE)},
        secret_values=ADMIN_SECRET)),  # type: ignore[arg-type]
    ("headers-not-mapping", lambda: StaticHeaderAuth(headers=[ADMIN_SECRET])),  # type: ignore
    ("table-bad-material", lambda: AuthTable(identities={"admin": ADMIN_SECRET})),
    ("table-non-str-name", lambda: AuthTable(identities={1: admin_material()})),  # type: ignore
    ("request-raw-auth", lambda: ApiExecutionRequest(
        api_id="a", assets_root=Path("/a"), environment={}, params={},
        auth=ADMIN_SECRET)),  # type: ignore[arg-type]
    ("outcome-auth-extra-key", lambda: ApiExecutionOutcome(
        status="success", auth={"identity": "admin", "header_names": [],
                                "overridden_headers": [], "secret": ADMIN_SECRET})),
]


@pytest.mark.parametrize(
    "build", [item[1] for item in _BAD_CONSTRUCTIONS], ids=[item[0] for item in _BAD_CONSTRUCTIONS]
)
def test_construction_errors_never_contain_secret(build: Callable[[], object]) -> None:
    with pytest.raises((TypeError, ValueError)) as exc_info:
        build()
    text = f"{exc_info.value!s} {exc_info.value!r} {exc_info.value.args!r}"
    for secret in (ADMIN_SECRET, PARTNER_SECRET, "tp-fake", "tp-fak7"):
        assert secret not in text


# ------------------------------------------------------- 公开 auth 字段（Q1）


def test_outcome_auth_field_shape() -> None:
    info = {"identity": "admin", "header_names": ["Authorization"], "overridden_headers": []}
    outcome = ApiExecutionOutcome(status="success", auth=info)
    assert outcome.to_dict()["auth"] == info
    assert ApiExecutionOutcome(status="success").to_dict()["auth"] is None
    step = StepOutcome(step_id="s", type="api", status="success", duration_seconds=0, auth=info)
    assert step.to_dict()["auth"] == info


def test_skipped_step_cannot_carry_auth() -> None:
    with pytest.raises(ValueError):
        StepOutcome(
            step_id="s", type="api", status="skipped", duration_seconds=0,
            skip_reason="fail_fast",
            auth={"identity": "admin", "header_names": [], "overridden_headers": []},
        )
