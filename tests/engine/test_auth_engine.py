"""3.3a 注入、预检与公开面脱敏（单 API + Case）。

对应设计稿 §6 的 Engine 层条目（1/2/3/4/5/8/9/10）、§6.1 Q2–Q6 与手册 §0.4 的 H1/H2/M3。
所有断言都基于假 client 记录的**实际发出**请求，而不只是公开结果里的 auth 字段。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import testplat_engine.engine.executor as executor_module
from testplat_engine import (
    ApiExecutionRequest,
    AuthTable,
    CaseExecutionRequest,
    EngineError,
    InvalidAssetError,
    InvalidEnvironmentError,
    SecretStr,
    SingleApiEngine,
    StaticHeaderAuth,
)
from testplat_engine.shared.redact import _SENSITIVE_KEYS, is_sensitive_key
from tests.engine.auth_support import (
    ADMIN_HEADER_VALUE,
    ADMIN_SECRET,
    ALL_SECRETS,
    AUTH_ASSETS,
    ENV,
    PARTNER_SECRET,
    EchoClient,
    assert_no_secret,
    auth_table,
    unavailable,
)


def api_request(api_id: str, auth: AuthTable | None) -> ApiExecutionRequest:
    return ApiExecutionRequest(
        api_id=api_id, assets_root=AUTH_ASSETS, environment=ENV, params={}, auth=auth
    )


def case_request(case_id: str, auth: AuthTable | None) -> CaseExecutionRequest:
    return CaseExecutionRequest(
        case_id=case_id, assets_root=AUTH_ASSETS, environment=ENV, params={}, auth=auth
    )


def header(sent_headers: dict[str, str], name: str) -> list[str]:
    """按不区分大小写取出全部同名 header（用于断言只发了一份）。"""
    return [v for k, v in sent_headers.items() if k.lower() == name.lower()]


# ------------------------------------------------------------- 注入基本路径


async def test_admin_identity_injects_header_and_hides_secret() -> None:
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(api_request("api.auth.admin_get", auth_table()))

    assert header(client.sent[0].headers, "Authorization") == [ADMIN_HEADER_VALUE]
    dumped = outcome.to_dict()
    assert dumped["status"] == "success"
    assert dumped["auth"] == {
        "identity": "admin", "kind": "static_headers", "tier": None,
        "header_names": ["Authorization"], "overridden_headers": [],
        "cookie_names": [], "login_ref": None,
    }
    assert dumped["request"]["headers"] == {"Authorization": "***"}
    # §6-2：回显到非敏感键下 → 按值替换为 ***，键保留。
    assert dumped["response"]["body"]["echo"] == {"value": "***", "tenant": None}
    assert_no_secret(dumped)


async def test_partner_custom_header_injected() -> None:
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(
        api_request("api.auth.partner_get", auth_table())
    )
    assert header(client.sent[0].headers, "X-Tenant-Token") == [PARTNER_SECRET]
    assert header(client.sent[0].headers, "Authorization") == []
    assert outcome.to_dict()["response"]["body"]["echo"]["tenant"] == "***"
    assert_no_secret(outcome.to_dict())


# ------------------------------------------------------------------- H1


async def test_h1_injected_header_name_is_sensitive_without_value_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关掉按值替换后，自定义头名 X-Tenant-Token 仍因“注入即敏感”被遮蔽。"""
    assert not is_sensitive_key("X-Tenant-Token")
    monkeypatch.setattr(executor_module, "redact_secret_values", lambda value, secrets: value)
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(
        api_request("api.auth.partner_get", auth_table())
    )
    assert outcome.to_dict()["request"]["headers"] == {"X-Tenant-Token": "***"}


async def test_h1_value_redaction_without_name_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉“注入头名敏感化”后，按值替换仍独立遮蔽非敏感键与响应头 X-Echo。"""
    monkeypatch.setattr(
        SingleApiEngine, "_injected_names", staticmethod(lambda resolved: frozenset())
    )
    client = EchoClient()
    engine = SingleApiEngine(client)
    partner = await engine.execute(api_request("api.auth.partner_get", auth_table()))
    assert partner.to_dict()["request"]["headers"] == {"X-Tenant-Token": "***"}
    reflect = await engine.execute(api_request("api.auth.admin_reflect", auth_table()))
    assert reflect.to_dict()["response"]["headers"]["X-Echo"] == "***"
    assert_no_secret(partner.to_dict(), reflect.to_dict())


def test_h1_single_sensitive_list() -> None:
    assert not hasattr(executor_module, "_SENSITIVE_HEADER_NAMES")
    for name in ("authorization", "proxy-authorization", "cookie", "set-cookie",
                 "x-api-key", "api-key"):
        assert name in _SENSITIVE_KEYS
    source = Path(executor_module.__file__).read_text(encoding="utf-8")
    assert "完整值只允许进入日志链路" not in source


async def test_h1_logs_never_contain_header_values(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = httpx.ConnectError(f"connect failed Authorization: {ADMIN_HEADER_VALUE}")
    client = EchoClient(fail_on="/echo/headers", fail_with=leaky)
    with pytest.raises(EngineError) as exc_info:
        await SingleApiEngine(client).execute(api_request("api.auth.admin_get", auth_table()))
    assert exc_info.value.kind == "transport"
    # 兜底：log_reason 也做了按值替换。
    assert ADMIN_SECRET not in (exc_info.value.log_reason or "")
    assert caplog.records
    for record in caplog.records:
        text = f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
        assert record.exc_info is None
        for secret in ALL_SECRETS:
            assert secret not in text


# --------------------------------------------------- §6-3 错误响应 / 响应头


async def test_error_response_body_redacted() -> None:
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(
        api_request("api.auth.admin_error", auth_table())
    )
    dumped = outcome.to_dict()
    assert dumped["status"] == "failed"
    # 敏感键 Authorization 按名替换；其余字符串也做了按值替换。
    assert dumped["response"]["body"]["received"]["Authorization"] == "***"
    assert_no_secret(dumped, json.dumps(dumped["error"]))


# ------------------------------------------------- §6-8 / Q2 / Q6 / §6-10


async def test_auth_none_ignores_default_identity() -> None:
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(api_request("api.auth.none_get", auth_table()))
    assert client.sent[0].headers == {}
    assert outcome.to_dict()["auth"] is None


@pytest.mark.parametrize("api_id", ["api.auth.default_get", "api.auth.null_get"])
async def test_undeclared_uses_default_identity(api_id: str) -> None:
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(api_request(api_id, auth_table()))
    assert header(client.sent[0].headers, "Authorization") == [ADMIN_HEADER_VALUE]
    assert outcome.to_dict()["auth"]["identity"] == "admin"


async def test_undeclared_without_default_is_anonymous() -> None:
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(
        api_request("api.auth.default_get", auth_table(default_identity=None))
    )
    assert client.sent[0].headers == {"X-Trace": "trace-1"}
    assert outcome.to_dict()["auth"] is None


@pytest.mark.parametrize("api_id", ["api.auth.default_get", "api.auth.none_get"])
async def test_auth_none_table_matches_v020(api_id: str) -> None:
    """§6-10：auth=None 时与 v0.2.0 行为一致（发出的 header 只来自资产）。"""
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(api_request(api_id, None))
    expected = {"X-Trace": "trace-1"} if api_id.endswith("default_get") else {}
    assert client.sent[0].headers == expected
    assert outcome.to_dict()["request"]["headers"] == expected
    assert outcome.to_dict()["auth"] is None


async def test_q2_declared_identity_without_table_is_422() -> None:
    client = EchoClient()
    with pytest.raises(InvalidEnvironmentError) as exc_info:
        await SingleApiEngine(client).execute(api_request("api.auth.admin_get", None))
    assert exc_info.value.stage == "preflight"
    assert "admin" in exc_info.value.public_message
    assert client.sent == []


@pytest.mark.parametrize("api_id", ["api.auth.bad_type", "api.auth.empty_auth"])
async def test_q6_invalid_auth_declaration_is_invalid_asset(api_id: str) -> None:
    client = EchoClient()
    with pytest.raises(InvalidAssetError):
        await SingleApiEngine(client).execute(api_request(api_id, auth_table()))
    assert client.sent == []


# ------------------------------------------------- 预检：未知 / 不可用 / 未知 kind


async def test_unknown_identity_is_preflight_422() -> None:
    client = EchoClient()
    with pytest.raises(InvalidEnvironmentError) as exc_info:
        await SingleApiEngine(client).execute(api_request("api.auth.ghost_get", auth_table()))
    assert exc_info.value.kind == "invalid_environment"
    assert exc_info.value.stage == "preflight"
    assert "ghost" in exc_info.value.public_message
    assert client.sent == []
    assert_no_secret(exc_info.value.public_message, exc_info.value.log_reason or "")


async def test_unavailable_identity_is_422_with_public_reason() -> None:
    client = EchoClient()
    table = auth_table(partner=unavailable("环境变量 TESTPLAT_PARTNER_KEY 未设置"))
    with pytest.raises(InvalidEnvironmentError) as exc_info:
        await SingleApiEngine(client).execute(api_request("api.auth.partner_get", table))
    message = exc_info.value.public_message
    assert "partner" in message and "TESTPLAT_PARTNER_KEY" in message
    assert client.sent == []


async def test_unused_unavailable_identity_does_not_block() -> None:
    client = EchoClient()
    table = auth_table(partner=unavailable())
    outcome = await SingleApiEngine(client).execute(api_request("api.auth.admin_get", table))
    assert outcome.status == "success"


async def test_unavailable_default_identity_reported_when_used() -> None:
    """Q7：default_identity 指向不可用身份 → 真正用到时 422。"""
    client = EchoClient()
    table = auth_table(admin=unavailable("环境变量 TESTPLAT_ADMIN_TOKEN 为空"))
    with pytest.raises(InvalidEnvironmentError):
        await SingleApiEngine(client).execute(api_request("api.auth.default_get", table))
    assert client.sent == []


async def test_unknown_kind_is_preflight_422() -> None:
    client = EchoClient()
    table = AuthTable(identities={"admin": SimpleNamespace(kind="login_token")})
    with pytest.raises(InvalidEnvironmentError) as exc_info:
        await SingleApiEngine(client).execute(api_request("api.auth.admin_get", table))
    assert "login_token" in exc_info.value.public_message
    assert client.sent == []


# ------------------------------------------------------------- M3 / Q3 / §6-9


@pytest.mark.parametrize(
    ("api_id", "original"),
    [("api.auth.override_get", "Authorization"), ("api.auth.override_lower_get", "authorization")],
)
async def test_m3_override_sends_single_credential_header(api_id: str, original: str) -> None:
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(api_request(api_id, auth_table()))
    sent = client.sent[0].headers
    assert header(sent, "Authorization") == [ADMIN_HEADER_VALUE]
    dumped = outcome.to_dict()
    assert dumped["auth"]["overridden_headers"] == [original]
    assert len(header(dumped["request"]["headers"], "Authorization")) == 1
    assert "public-placeholder" not in json.dumps(dumped)
    assert_no_secret(dumped)


# -------------------------------------------------------------------- Q5


async def test_q5_multiple_secrets_in_one_header_each_replaced() -> None:
    first, second = "tp-fake-part-a-11112222", "tp-fake-part-b-33334444"
    material = StaticHeaderAuth(
        headers={"Authorization": SecretStr(f"Key {first}:{second}")},
        secret_values=(SecretStr(first), SecretStr(second)),
    )

    class SplitEcho(EchoClient):
        async def request(self, method, url, **kwargs):  # type: ignore[override]
            await super().request(method, url, **kwargs)
            value = kwargs["headers"]["Authorization"]
            a, b = value.removeprefix("Key ").split(":")
            from testplat_engine.ports.http_client import HttpResponse

            return HttpResponse(200, {}, {"full": value, "a": a, "b": f"x{b}x"})

    outcome = await SingleApiEngine(SplitEcho()).execute(
        api_request("api.auth.admin_get", AuthTable(identities={"admin": material}))
    )
    body = outcome.to_dict()["response"]["body"]
    assert body == {"full": "***", "a": "***", "b": "x***x"}


# ----------------------------------------------------------- §6-5 不串号


async def test_single_api_alternating_identities_share_client_without_crosstalk() -> None:
    client = EchoClient()
    engine = SingleApiEngine(client)
    table = auth_table()
    for api_id in ["api.auth.admin_get", "api.auth.partner_get", "api.auth.none_get",
                   "api.auth.admin_get"]:
        await engine.execute(api_request(api_id, table))
    shapes = [sorted(k.lower() for k in r.headers) for r in client.sent]
    assert shapes == [["authorization"], ["x-tenant-token"], [], ["authorization"]]


# ------------------------------------------------------------------- H2：Case


async def test_h2_case_steps_actually_send_identity_headers() -> None:
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(case_request("case.auth.auth_mixed",
                                                                 auth_table()))
    assert outcome.status == "success"
    sent = [r.headers for r in client.sent]
    assert len(sent) == 5
    assert header(sent[0], "Authorization") == [ADMIN_HEADER_VALUE]
    assert header(sent[0], "X-Tenant-Token") == []
    assert header(sent[1], "X-Tenant-Token") == [PARTNER_SECRET]
    assert header(sent[1], "Authorization") == []
    assert sent[2] == {}
    assert header(sent[3], "Authorization") == [ADMIN_HEADER_VALUE]
    assert header(sent[3], "X-Tenant-Token") == []
    assert header(sent[4], "Authorization") == [ADMIN_HEADER_VALUE]

    dumped = outcome.to_dict()
    identities = [s["auth"] and s["auth"]["identity"] for s in dumped["steps"]]
    assert identities == ["admin", "partner", None, "admin", "admin"]
    assert_no_secret(dumped)


@pytest.mark.parametrize(
    ("case_id", "table"),
    [
        ("case.auth.auth_late_ghost", auth_table()),
        ("case.auth.auth_late_partner", auth_table(partner=unavailable())),
        ("case.auth.auth_mixed", None),  # Q2：Case 中声明身份但没有身份表
    ],
    ids=["unknown", "unavailable", "no-table"],
)
async def test_h2_case_preflight_fails_before_any_step(
    case_id: str, table: AuthTable | None
) -> None:
    client = EchoClient()
    with pytest.raises(InvalidEnvironmentError) as exc_info:
        await SingleApiEngine(client).execute(case_request(case_id, table))
    assert exc_info.value.stage == "preflight"
    assert exc_info.value.partial_outcome is None
    assert client.sent == []


async def test_case_step_level_auth_is_invalid_asset() -> None:
    with pytest.raises(InvalidAssetError):
        await SingleApiEngine(EchoClient()).execute(
            case_request("case.auth.auth_step_override", auth_table())
        )


async def test_q4_alias_gets_redacted_value_with_key_kept() -> None:
    client = EchoClient()
    outcome = await SingleApiEngine(client).execute(case_request("case.auth.auth_alias",
                                                                 auth_table()))
    assert outcome.status == "success"
    # 下游步骤实际发出的请求体里是替换后的 ***，而不是原值（键保留，引用不失败）。
    assert client.sent[1].body == {"value": "***"}
    assert_no_secret(outcome.to_dict(), [r.body for r in client.sent])


async def test_case_system_error_partial_outcome_has_no_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = httpx.ConnectError(f"boom X-Tenant-Token={PARTNER_SECRET}")

    class FailSecond(EchoClient):
        async def request(self, method, url, **kwargs):  # type: ignore[override]
            if "X-Tenant-Token" in (kwargs.get("headers") or {}):
                self.sent.append(None)  # type: ignore[arg-type]
                raise leaky
            return await super().request(method, url, **kwargs)

    with pytest.raises(EngineError) as exc_info:
        await SingleApiEngine(FailSecond()).execute(
            case_request("case.auth.auth_transport", auth_table())
        )
    error = exc_info.value
    assert error.kind == "transport"
    assert error.partial_outcome is not None
    assert_no_secret(error.to_dict(), error.log_reason or "", caplog.text)
