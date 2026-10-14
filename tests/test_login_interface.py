"""3.3b 公开 DTO：repr / to_dict / pickle 只暴露结构信息；构造错误不回显值；skip_reason 枚举。"""

from __future__ import annotations

import copy
import pickle

import httpx
import pytest

from testplat_engine import (
    AuthTable,
    ExtractRule,
    LoginAuth,
    LoginBody,
    LoginFlow,
    LoginResult,
    LoginStep,
    SecretStr,
    StepOutcome,
)
from testplat_engine.plugins.httpx_client import HttpxClient
from testplat_engine.ports.http_client import CookieInfo

SECRET = "tp-fake-dto-secret-8899"


def material() -> LoginAuth:
    return LoginAuth(
        strategy="generic_flow",
        host="main",
        tier="configured",
        flow=LoginFlow(
            steps=(
                LoginStep(
                    name="login",
                    method="post",
                    path="/login",
                    headers={"X-Key": SecretStr(SECRET)},
                    body=LoginBody(fields={"username": "buyer001", "password": SecretStr(SECRET)}),
                    extract=(
                        ExtractRule(source="body_json", path="$.data.token", as_name="token"),
                    ),
                ),
            )
        ),
        params={"k": SecretStr(SECRET)},
        secret_values=(SecretStr(SECRET),),
        inject_headers={"X-TOKEN": "${login:token}"},
    )


def test_login_auth_repr_to_dict_only_structure() -> None:
    m = material()
    for text in (repr(m), str(m), repr(m.flow), repr(m.flow.steps[0]), repr(AuthTable({"b": m}))):
        assert SECRET not in text and "buyer001" not in text
    assert m.to_dict() == {
        "kind": "login",
        "strategy": "generic_flow",
        "tier": "configured",
        "host": "main",
        "steps": ["login"],
        "extract": ["token"],
        "header_names": ["X-TOKEN"],
        "session_cookie": False,
        "reauth_on_401": True,
    }
    assert AuthTable({"b": m}).to_dict()["identities"]["b"]["kind"] == "login"


@pytest.mark.parametrize(
    "obj", [material, lambda: material().flow, lambda: LoginResult({"token": SECRET})]
)
def test_login_dtos_not_picklable(obj) -> None:
    value = obj()
    with pytest.raises(TypeError):
        pickle.dumps(value)
    with pytest.raises(TypeError):
        copy.deepcopy(value)


def test_login_result_repr_hides_values() -> None:
    assert SECRET not in repr(LoginResult({"token": SECRET}))


@pytest.mark.parametrize(
    "build",
    [
        lambda: LoginAuth(strategy="generic_flow", host="h", inject_headers={"X": "${login:t}"}),
        lambda: LoginAuth(strategy="p", host="h", inject_headers={"X": "literal"}),
        lambda: LoginAuth(strategy="p", host="h", tier="nope", session_cookie=True),
        lambda: LoginAuth(
            strategy="p", host="h", session_cookie=True, secret_values=(SecretStr("short"),)
        ),
        lambda: LoginBody(type="multipart"),
        lambda: LoginStep(
            name="s", method="POST", path="/", body=LoginBody(type="raw", content=SECRET)
        ),
        lambda: ExtractRule(source="xpath", as_name="t"),
        lambda: LoginFlow(steps=()),
    ],
)
def test_login_dto_construction_errors_never_echo_values(build) -> None:
    with pytest.raises((TypeError, ValueError)) as info:
        build()
    assert SECRET not in f"{info.value!s}{info.value!r}"


def test_session_cookie_without_inject_is_allowed_q2() -> None:
    LoginAuth(strategy="p", host="h", tier="plugin", session_cookie=True)


def test_skip_reason_enum_h4() -> None:
    for reason in ("fail_fast", "login_failed"):
        StepOutcome(
            step_id="s", type="api", status="skipped", duration_seconds=0, skip_reason=reason
        )
    with pytest.raises(ValueError):
        StepOutcome(step_id="s", type="api", status="skipped", duration_seconds=0, skip_reason="x")


def test_cookie_info_repr_hides_value() -> None:
    assert SECRET not in repr(CookieInfo(name="SESSION", value=SECRET))


async def test_httpx_client_exposes_cookies_and_text_h2() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<p>hi</p>",
            headers=[("Set-Cookie", "a=1111111111; Path=/"), ("Set-Cookie", "b=2; Max-Age=60")],
        )

    client = HttpxClient(transport=httpx.MockTransport(handler))
    response = await client.request("GET", "http://t.test/")
    assert response.text == "<p>hi</p>"
    assert sorted(c.name for c in response.cookies) == ["a", "b"]
    jar = {c.name: c for c in client.cookie_jar()}
    assert jar["a"].value == "1111111111" and jar["b"].expires is not None
    await client.close()
