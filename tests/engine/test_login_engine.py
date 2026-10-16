"""3.3b 登录鉴权：Engine 层覆盖设计稿 §6 第 11–21 条与手册 §0.4 的 Engine 裁决。

全部基于 httpx.MockTransport 假目标（真实 cookie jar 语义、零网络），断言既看公开结果，
也看假目标记录的**实际发出**请求。
"""

from __future__ import annotations

import logging

import httpx
import pytest

import testplat_engine.engine.login_session as login_session
from testplat_engine import (
    ApiExecutionRequest,
    CaseExecutionRequest,
    EngineTimeoutError,
    ExpiresFrom,
    ExtractRule,
    InvalidAssetError,
    InvalidEnvironmentError,
    LoginBody,
    LoginContext,
    LoginStep,
    SecretStr,
    Validity,
    create_engine,
)
from tests.engine.login_support import (
    BUYER_PASSWORD,
    CSRF_TOKEN,
    ENV,
    FAIL_DETAIL,
    HOST,
    LOGIN_ASSETS,
    PLUGIN_KEY,
    PLUGIN_TOKEN,
    REDIS_PASSWORD,
    FakePlugin,
    FakeTarget,
    assert_no_secrets,
    buyer_material,
    dump,
    engine_for,
    plugin_material,
    seller_material,
    table,
)


def api(api_id: str, auth, params=None) -> ApiExecutionRequest:
    return ApiExecutionRequest(
        api_id=api_id, assets_root=LOGIN_ASSETS, environment=ENV, params=params or {}, auth=auth
    )


def case(case_id: str, auth, params=None) -> CaseExecutionRequest:
    return CaseExecutionRequest(
        case_id=case_id, assets_root=LOGIN_ASSETS, environment=ENV, params=params or {}, auth=auth
    )


def login_paths(target: FakeTarget) -> list[str]:
    return [p for p in target.paths() if p.startswith(("/login", "/csrf"))]


# ------------------------------------------------------------ 第 1 档：通用单步


@pytest.mark.parametrize(
    ("username", "password"),
    [("张建雨", "123456"), ("u", "p"), ("", ""), (" user\t ", "pw\r\n\x00 ")],
)
async def test_short_credentials_login_cache_and_redaction(username: str, password: str) -> None:
    class ShortCredentialTarget(FakeTarget):
        def _login_ok(self, user, supplied, *, csrf=False):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "token": self._token("short-account"),
                        "echo_user": user,
                        "echo_credential": supplied,
                    },
                },
            )

        def handler(self, request):
            response = super().handler(request)
            if request.url.path == "/echo/token":
                return httpx.Response(
                    200, json={"echo_user": username, "echo_credential": password}
                )
            return response

    target = ShortCredentialTarget()
    step = LoginStep(
        name="login",
        method="POST",
        path="/login/json",
        body=LoginBody(
            type="json",
            fields={"username": SecretStr(username), "password": SecretStr(password)},
        ),
        extract=(ExtractRule(source="body_json", path="$.data.token", as_name="token"),),
    )
    auth = table(
        buyer=buyer_material(
            step=step,
            secret_values=(SecretStr(username), SecretStr(password)),
            validity=Validity(ttl_seconds=600),
        ),
        cache_scope="short-credential-regression",
    )
    engine = engine_for(target)
    for source in ("login", "cache"):
        result = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
        assert result["status"] == "success"
        assert result["auth"]["session_source"] == source
        assert result["response"]["body"] == {
            "echo_user": "***" if username else "",
            "echo_credential": "***" if password else "",
        }
        if source == "login":
            record = result["logins"][0]["steps"][0]
            assert record["request"]["body"] == {
                "username": "***" if username else "",
                "password": "***",
            }
            assert record["response"]["body"]["data"]["echo_credential"] == (
                "***" if password else ""
            )
    assert len(login_paths(target)) == 1


async def test_tier1_buyer_login_inject_and_redact() -> None:
    target = FakeTarget()
    outcome = await engine_for(target).execute(api("api.login.buyer_echo", table()))
    d = outcome.to_dict()
    assert d["status"] == "success"
    token = target.issued[0]
    business = target.requests[-1]
    assert business.headers["X-TOKEN"] == token
    # §6-12：回显到非敏感键 → ***
    assert d["response"]["body"]["echo"]["x_token"] == "***"
    assert d["request"]["headers"] == {"X-TOKEN": "***"}
    assert d["auth"] == {
        "identity": "buyer",
        "kind": "login",
        "tier": "generic",
        "header_names": ["X-TOKEN"],
        "overridden_headers": [],
        "cookie_names": [],
        "login_ref": 0,
        "session_source": "login",
        "reauthenticated": False,
    }
    [record] = d["logins"]
    assert record["identity"] == "buyer" and record["strategy"] == "generic_flow"
    assert record["tier"] == "generic" and record["status"] == "success"
    assert record["error"] is None and record["probe"] is None
    step = record["steps"][0]
    assert step["name"] == "login"
    # Q3：登录步骤一律 follow_redirects=False；密码按名脱敏，token 按名/按值脱敏。
    assert step["request"]["body"]["password"] == "***"
    assert step["request"]["body_type"] == "json"
    assert step["response"]["body"]["data"]["token"] == "***"
    assert_no_secrets(target, d)  # §6-11


async def test_login_steps_use_follow_redirects_false() -> None:
    seen: list[dict] = []

    class Spy:
        async def request(self, method, url, **kwargs):
            seen.append(kwargs)
            from testplat_engine.ports.http_client import HttpResponse

            return HttpResponse(200, {}, {"code": 0, "data": {"token": "spy-token-123456"}})

        def cookie_jar(self):
            return []

    engine = create_engine()
    engine._client_factory = Spy  # type: ignore[attr-defined]
    await engine.execute(api("api.login.buyer_echo", table()))
    assert seen[0]["follow_redirects"] is False
    assert "follow_redirects" not in seen[1]  # 业务请求保持 0.3.0 调用方式


async def test_inject_overrides_asset_header_case_insensitive() -> None:
    target = FakeTarget()
    d = (await engine_for(target).execute(api("api.login.buyer_override", table()))).to_dict()
    sent = target.requests[-1]
    assert sent.headers.get_list("x-token") == [target.issued[0]]
    assert sent.headers["X-Trace"] == "trace-1"
    assert d["auth"]["overridden_headers"] == ["x-token"]
    assert d["request"]["headers"]["X-TOKEN"] == "***"


async def test_inject_mixes_literal_login_and_secret() -> None:
    """Q1：inject 值可为字面量 / ${login:} / SecretStr 任意组合；字面量头同样按名遮蔽。"""
    target = FakeTarget()
    buyer = buyer_material(
        inject_headers={
            "X-TOKEN": "${login:token}",
            "X-SOURCE": "web-h5",
            "Authorization": SecretStr("Key tp-fake-buyer-pass-71c2 ${login:token}"),
        }
    )
    d = (
        await engine_for(target).execute(api("api.login.buyer_echo", table(buyer=buyer)))
    ).to_dict()
    sent = target.requests[-1]
    assert sent.headers["X-SOURCE"] == "web-h5"
    assert sent.headers["Authorization"] == f"Key {BUYER_PASSWORD} {target.issued[0]}"
    assert d["request"]["headers"] == {"X-TOKEN": "***", "X-SOURCE": "***", "Authorization": "***"}
    assert d["response"]["body"]["echo"]["authz"] == "Key *** ***"
    assert_no_secrets(target, d)


# ------------------------------------------------------------ 第 2 档：多步 Cookie + probe


async def test_tier2_seller_cookie_session_with_probe() -> None:
    target = FakeTarget()
    d = (await engine_for(target).execute(api("api.login.seller_echo", table()))).to_dict()
    assert d["status"] == "success"
    assert login_paths(target) == ["/csrf/login", "/csrf/login", "/csrf/protected"]
    submit = target.requests[1]
    # ${login_host} 渲染 + 前序 html_meta 提取值注入表单 + H2 两条 Set-Cookie 均入 jar
    assert submit.headers["Referer"] == f"{HOST}/csrf/login"
    assert "_csrf=seed-0a0b0c0d0e" in submit.headers["Cookie"]
    assert "lang=zh-CN" in submit.headers["Cookie"]
    business = target.requests[-1]
    assert "SESSION=" in business.headers["Cookie"]
    assert business.headers["X-CSRF-Token"] == CSRF_TOKEN
    assert d["auth"]["kind"] == "login" and d["auth"]["tier"] == "configured"
    assert d["auth"]["cookie_names"] == ["SESSION", "_csrf", "lang"]
    echo = d["response"]["body"]["echo"]
    assert "sess-" not in echo["cookie"] and "***" in echo["cookie"]
    assert echo["csrf"] == "***"
    record = d["logins"][0]
    assert [s["name"] for s in record["steps"]] == ["bootstrap", "submit"]
    assert record["steps"][1]["response"]["status_code"] == 302  # Q3：302 可见
    assert record["steps"][1]["response"]["headers"]["set-cookie"] == "***"
    assert record["steps"][1]["request"]["body_type"] == "form"
    assert record["steps"][1]["request"]["body"]["password"] == "***"
    assert record["probe"]["request"]["url"] == f"{HOST}/csrf/protected"
    assert_no_secrets(target, d)


async def test_cookie_identity_jar_updated_by_business_responses() -> None:
    """D12：业务响应 Set-Cookie 也更新该身份 jar，且新值进入替换集合。"""
    target = FakeTarget()
    original = target.handler

    def handler(request):
        response = original(request)
        if request.url.path == "/echo/token":
            response.headers["Set-Cookie"] = "rotated=rot-9988776655; Path=/"
        return response

    target.handler = handler  # type: ignore[method-assign]
    d = (await engine_for(target).execute(case("case.login.seller_twice", table()))).to_dict()
    target.issued.append("rot-9988776655")
    second = target.requests[-1]
    assert "rotated=rot-9988776655" in second.headers["Cookie"]
    assert "rotated" in d["steps"][1]["auth"]["cookie_names"]
    assert_no_secrets(target, d)


# ------------------------------------------------------------ §6-13 / 14 / 15 / 16


async def test_two_identities_alternate_without_crosstalk_and_anon_has_no_cookie() -> None:
    target = FakeTarget()
    d = (await engine_for(target).execute(case("case.login.alternate", table()))).to_dict()
    assert d["status"] == "success"
    business = [r for r in target.requests if r.url.path == "/echo/token"]
    b1, s1, a1, b2, s2 = business
    buyer_token = target.issued[0]
    for r in (b1, b2):
        assert r.headers["X-TOKEN"] == buyer_token and "Cookie" not in r.headers
    for r in (s1, s2):
        assert "SESSION=" in r.headers["Cookie"] and "X-TOKEN" not in r.headers
    assert "Cookie" not in a1.headers and "X-TOKEN" not in a1.headers
    # 每个身份只登录一次（§6-15），login_ref 区分
    assert [lg["identity"] for lg in d["logins"]] == ["buyer", "seller"]
    refs = [s["auth"]["login_ref"] if s["auth"] else None for s in d["steps"]]
    assert refs == [0, 1, None, 0, 1]
    assert_no_secrets(target, d)


async def test_on_demand_login_happens_only_when_identity_first_used() -> None:
    target = FakeTarget()
    d = (await engine_for(target).execute(case("case.login.on_demand", table()))).to_dict()
    assert target.paths() == ["/echo/token", "/login/json", "/echo/token", "/echo/token"]
    assert len(d["logins"]) == 1


async def test_no_state_shared_between_executions() -> None:
    target = FakeTarget()
    engine = engine_for(target)
    first = (await engine.execute(case("case.login.buyer_thrice", table()))).to_dict()
    second = (await engine.execute(case("case.login.buyer_thrice", table()))).to_dict()
    assert login_paths(target) == ["/login/json", "/login/json"]
    assert len(first["logins"]) == len(second["logins"]) == 1
    assert target.issued[0] != target.issued[1]


async def test_short_ttl_triggers_relogin_with_distinct_login_ref() -> None:
    """§6-16 + E3：ttl=30 秒 ≤ 安全余量 → 下一次使用即重登。"""
    target = FakeTarget()
    buyer = buyer_material(validity=Validity(ttl_seconds=30))
    d = (
        await engine_for(target).execute(case("case.login.buyer_thrice", table(buyer=buyer)))
    ).to_dict()
    assert len(d["logins"]) == 3
    assert [s["auth"]["login_ref"] for s in d["steps"]] == [0, 1, 2]
    tokens = [r.headers["X-TOKEN"] for r in target.requests if r.url.path == "/echo/token"]
    assert tokens == target.issued[:3]


async def test_expires_from_response_and_safety_margin(monkeypatch) -> None:
    """expiresIn=31 秒：登录后 0.5 秒仍有效，1.5 秒后进入 30 秒余量 → 重登。"""
    clock = [1_000_000.0]
    monkeypatch.setattr(login_session, "_now", lambda: clock[0])
    target = FakeTarget(expires_in=31)
    buyer = buyer_material(
        validity=Validity(ttl_seconds=99999, expires_from=ExpiresFrom(path="$.data.expiresIn"))
    )
    original = target.handler

    def handler(request):
        clock[0] += 0.5
        return original(request)

    target.handler = handler  # type: ignore[method-assign]
    d = (
        await engine_for(target).execute(case("case.login.buyer_thrice", table(buyer=buyer)))
    ).to_dict()
    # 登录 t+0.5 → 到期 t+31.5；b1 t+1.0 不检查，b2 检查于 t+1.0 < 1.5 有效；b3 检查于 t+1.5 → 失效
    assert [s["auth"]["login_ref"] for s in d["steps"]] == [0, 0, 1]


async def test_cookie_expires_triggers_relogin() -> None:
    target = FakeTarget(session_max_age=20)  # 20 秒 < 30 秒余量
    d = (await engine_for(target).execute(case("case.login.seller_twice", table()))).to_dict()
    assert len(d["logins"]) == 2
    assert [s["auth"]["login_ref"] for s in d["steps"]] == [0, 1]


# ------------------------------------------------------------ §6-17 七种 reason


def _fail_buyer(reason: str):
    if reason == "assertion":
        return buyer_material(path="/login/fail")
    if reason == "extract_missing":
        return buyer_material(
            step=LoginStep(
                name="login",
                method="POST",
                path="/login/json",
                body=LoginBody(fields={"username": "u", "password": SecretStr(BUYER_PASSWORD)}),
                extract=(ExtractRule(source="body_json", path="$.data.nope", as_name="token"),),
            )
        )
    if reason == "token_too_short":
        return buyer_material(path="/login/short")
    if reason == "timeout":
        return buyer_material(path="/login/slow")
    if reason == "transport":
        return buyer_material(path="/login/down")
    raise AssertionError(reason)


@pytest.mark.parametrize(
    ("reason", "failed_step"),
    [
        ("assertion", "login"),
        ("extract_missing", "login"),
        ("token_too_short", None),
        ("timeout", "login"),
        ("transport", "login"),
    ],
)
async def test_login_failure_reasons_case_shape(reason, failed_step, caplog) -> None:
    target = FakeTarget()
    caplog.set_level(logging.DEBUG)
    engine = engine_for(target)
    d = (
        await engine.execute(case("case.login.login_fail_mid", table(buyer=_fail_buyer(reason))))
    ).to_dict()
    assert d["status"] == "failed"
    statuses = [(s["status"], s["skip_reason"]) for s in d["steps"]]
    assert statuses == [
        ("success", None),
        ("failed", None),
        ("skipped", "login_failed"),
        ("skipped", "login_failed"),
    ]
    error = d["steps"][1]["error"]
    assert error["kind"] == "login_failed"
    assert error["login"] == {
        "identity": "buyer",
        "strategy": "generic_flow",
        "failed_step": failed_step,
        "reason": reason,
    }
    assert d["steps"][1]["auth"]["login_ref"] == 0
    assert d["steps"][1]["request"] is None
    assert d["logins"][0]["status"] == "failed"
    assert d["logins"][0]["error"] == {"reason": reason, "failed_step": failed_step}
    assert_no_secrets(target, d, caplog.text)


async def test_probe_failure_reason() -> None:
    target = FakeTarget()
    seller = seller_material(
        probe=__import__("testplat_engine").ProbeSpec(
            path="/csrf/protected", success={"status_code": 200, "body_not_contains": "ok"}
        )
    )
    d = (
        await engine_for(target).execute(api("api.login.seller_echo", table(seller=seller)))
    ).to_dict()
    assert d["status"] == "failed"
    assert d["error"]["login"]["reason"] == "probe_failed"
    assert d["error"]["login"]["failed_step"] == "probe"
    assert d["logins"][0]["probe"]["response"]["status_code"] == 200
    assert_no_secrets(target, d)


async def test_bad_csrf_redirect_to_login_is_assertion_failure() -> None:
    target = FakeTarget()
    steps = list(seller_material().flow.steps)
    bad = LoginStep(
        name="submit",
        method="POST",
        path="/csrf/login",
        body=LoginBody(
            type="form",
            fields={"_csrf": "wrong", "password": SecretStr("tp-fake-seller-pass-93ab")},
        ),
        success={"status_code": 302, "reject_location_contains": "/login"},
    )
    from testplat_engine import LoginFlow

    seller = seller_material(flow=LoginFlow(steps=(steps[0], bad)))
    d = (
        await engine_for(target).execute(api("api.login.seller_echo", table(seller=seller)))
    ).to_dict()
    assert d["error"]["login"] == {
        "identity": "seller",
        "strategy": "generic_flow",
        "failed_step": "submit",
        "reason": "assertion",
    }


async def test_plugin_exception_is_strategy_error_and_text_not_leaked(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    target = FakeTarget()
    plugin = FakePlugin()
    d = (
        await engine_for(target, (plugin,)).execute(
            api("api.login.plugin_echo", table(plugged=plugin_material(behavior="raise")))
        )
    ).to_dict()
    assert d["error"]["login"]["reason"] == "strategy_error"
    assert d["error"]["login"]["failed_step"] is None
    assert "RuntimeError" in caplog.text
    assert_no_secrets(target, d, caplog.text)


async def test_plugin_timeout_is_login_failed_timeout() -> None:
    target = FakeTarget()
    plugin = FakePlugin()
    request = ApiExecutionRequest(
        api_id="api.login.plugin_echo",
        assets_root=LOGIN_ASSETS,
        environment=ENV,
        params={},
        auth=table(plugged=plugin_material(behavior="sleep")),
        timeout_seconds=0.05,
    )
    d = (await engine_for(target, (plugin,)).execute(request)).to_dict()
    assert d["error"]["login"]["reason"] == "timeout"


async def test_login_timeout_never_raises_system_error() -> None:
    """H3/E2：登录超时 / 连接失败 → login_failed，不抛 EngineTimeoutError/TransportError。"""
    for reason in ("timeout", "transport"):
        target = FakeTarget()
        outcome = await engine_for(target).execute(
            api("api.login.buyer_echo", table(buyer=_fail_buyer(reason)))
        )
        assert outcome.status == "failed" and outcome.error["kind"] == "login_failed"


async def test_business_timeout_after_login_still_system_error() -> None:
    target = FakeTarget()
    with pytest.raises(EngineTimeoutError) as info:
        await engine_for(target).execute(case("case.login.business_timeout", table()))
    partial = info.value.partial_outcome.to_dict()
    assert partial["steps"][1]["error"]["kind"] == "timeout"
    assert len(partial["logins"]) == 1
    assert_no_secrets(target, partial, info.value.to_dict())


async def test_business_401_is_assertion_without_relogin() -> None:
    """关闭 401 重发时：业务 401 直接断言失败，不重登（设计稿 §6-40）。"""
    target = FakeTarget()
    buyer = buyer_material(reauth_on_401=False)
    d = (await engine_for(target).execute(api("api.login.buyer_401", table(buyer=buyer)))).to_dict()
    assert d["error"]["kind"] == "assertion"
    assert d["auth"]["reauthenticated"] is False
    assert login_paths(target) == ["/login/json"]
    assert_no_secrets(target, d)


async def test_failed_login_masks_long_strings_q7() -> None:
    target = FakeTarget()
    d = (
        await engine_for(target).execute(
            api("api.login.buyer_echo", table(buyer=_fail_buyer("assertion")))
        )
    ).to_dict()
    body = d["logins"][0]["steps"][0]["response"]["body"]
    assert body["trace"] == "***"  # ≥ 20 字符
    assert body["msg"] == "locked"  # 短文本保留
    assert FAIL_DETAIL not in dump(d)


async def test_single_api_login_failed_shape() -> None:
    target = FakeTarget()
    d = (
        await engine_for(target).execute(
            api("api.login.buyer_echo", table(buyer=_fail_buyer("assertion")))
        )
    ).to_dict()
    assert d["status"] == "failed" and d["request"] is None and d["response"] is None
    assert d["error"]["kind"] == "login_failed"
    assert d["auth"]["login_ref"] == 0 and len(d["logins"]) == 1
    # 登录失败后业务请求从未发出
    assert "/echo/token" not in target.paths()


# ------------------------------------------------------------ §6-12 / H1 执行级替换集合


async def test_alias_and_cross_identity_echo_redacted() -> None:
    target = FakeTarget()
    token = "tok-buyer001-0001-a8f3e2c1d0"  # 假目标的确定性第一枚 token
    d = (
        await engine_for(target).execute(
            case("case.login.alias_leak", table(), params={"tok": token})
        )
    ).to_dict()
    assert target.issued[0] == token
    leak, use, anon, seller = d["steps"]
    assert use["status"] == "success"
    # alias 引用 → 下游拿到的就是 ***（不是原值）
    assert b"***" in target.requests[2].content and token.encode() not in target.requests[2].content
    assert use["response"]["body"] == {"value": "***"}
    # H1：匿名步骤回显 buyer 的 token → ***（集合是执行级并集）
    assert anon["request"]["body"] == {"value": "***"}
    assert anon["response"]["body"] == {"value": "***"}
    assert_no_secrets(target, d)


# ------------------------------------------------------------ §6-18 插件 / 未注册策略


async def test_fake_plugin_values_injected_and_redacted() -> None:
    target = FakeTarget()
    plugin = FakePlugin()
    d = (
        await engine_for(target, (plugin,)).execute(api("api.login.plugin_echo", table()))
    ).to_dict()
    sent = target.requests[-1]
    assert sent.headers["X-TOKEN"] == PLUGIN_TOKEN
    assert sent.headers["Authorization"] == f"Key {PLUGIN_TOKEN}"
    assert d["auth"]["tier"] == "plugin"
    assert d["logins"][0]["strategy"] == "fake_plugin"
    assert d["logins"][0]["steps"][0]["name"] == "plugin_login"
    assert d["logins"][0]["steps"][0]["request"]["body"]["key"] == "***"
    # M2：url 同样参与按值替换
    assert d["logins"][0]["steps"][0]["request"]["url"] == f"{HOST}/plugin/login?k=***"
    assert d["response"]["body"]["echo"]["authz"] == "Key ***"
    assert plugin.validated == ["fake_plugin"]
    # 插件只拿到 LoginContext（专用 client / host / params / connections / record）
    ctx = plugin.contexts[0]
    assert isinstance(ctx, LoginContext) and ctx.host_url == HOST
    assert target.requests == [sent]  # 插件未经 http 发请求；业务请求只有一次
    assert_no_secrets(target, d)


async def test_unregistered_strategy_is_preflight_422_without_requests() -> None:
    target = FakeTarget()
    with pytest.raises(InvalidEnvironmentError) as info:
        await engine_for(target).execute(case("case.login.plugin_late", table()))
    assert "fake_plugin" in info.value.public_message
    assert target.requests == []


async def test_plugin_validate_failure_is_422_without_secret() -> None:
    target = FakeTarget()
    from testplat_engine import LoginAuth

    bad = LoginAuth(
        strategy="fake_plugin",
        host="target",
        tier="plugin",
        params={"api_key": SecretStr(PLUGIN_KEY)},
        inject_headers={"X-TOKEN": "${login:token}"},
    )
    with pytest.raises(InvalidEnvironmentError) as info:
        await engine_for(target, (FakePlugin(),)).execute(
            api("api.login.plugin_echo", table(plugged=bad))
        )
    assert PLUGIN_KEY not in dump(info.value.to_dict()) + (info.value.log_reason or "")
    assert target.requests == []


async def test_login_host_unresolvable_and_undeclared_inject_ref_are_422() -> None:
    target = FakeTarget()
    with pytest.raises(InvalidEnvironmentError):
        await engine_for(target).execute(
            api("api.login.buyer_echo", table(buyer=buyer_material(host="nowhere")))
        )
    with pytest.raises(InvalidEnvironmentError):
        await engine_for(target).execute(
            api(
                "api.login.buyer_echo",
                table(buyer=buyer_material(inject_headers={"X-U": "${login:user_id}"})),
            )
        )
    assert target.requests == []


def test_create_engine_rejects_duplicate_or_builtin_names() -> None:
    with pytest.raises(ValueError, match="generic_flow"):
        create_engine(login_strategies=(FakePlugin("generic_flow"),))
    with pytest.raises(ValueError, match="fake_plugin"):
        create_engine(login_strategies=(FakePlugin(), FakePlugin()))
    create_engine()  # 无参与 0.3.0 等价


# ------------------------------------------------------------ §6-19 connections


async def test_connections_password_never_public() -> None:
    target = FakeTarget()
    plugin = FakePlugin()
    auth = table()
    d = (await engine_for(target, (plugin,)).execute(api("api.login.plugin_echo", auth))).to_dict()
    ctx = plugin.contexts[0]
    assert ctx.connections["redis_main"]["password"].reveal() == REDIS_PASSWORD
    for text in (repr(ctx), str(ctx), repr(auth), dump(auth.to_dict()), dump(d)):
        assert REDIS_PASSWORD not in text and PLUGIN_KEY not in text
    assert "redis_main" in repr(ctx)


# ------------------------------------------------------------ §6-20 请求体类型


async def test_form_query_raw_snapshots_redact_secrets() -> None:
    target = FakeTarget()
    engine = engine_for(target)
    note = "tok-buyer001-0001-a8f3e2c1d0"  # 等于 buyer 登录后签发的 token → 按值替换
    form = (
        await engine.execute(api("api.login.form_echo", table(), {"user": "u1", "note": note}))
    ).to_dict()
    sent = target.requests[-1]
    assert sent.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert form["request"]["body_type"] == "form"
    assert form["request"]["body"] == {"user": "u1", "note": "***"}
    assert form["response"]["body"]["form"]["note"] == "***"

    target2 = FakeTarget()
    query = (
        await engine_for(target2).execute(
            api("api.login.query_echo", table(), {"user": "u1", "note": note})
        )
    ).to_dict()
    assert dict(target2.requests[-1].url.params) == {"user": "u1", "note": note}
    assert query["request"]["url"] == f"{HOST}/echo/query"  # Q8：url 不含 query 参数
    assert query["request"]["query"] == {"user": "u1", "note": "***"}
    assert query["request"]["body"] is None

    target3 = FakeTarget()
    raw = (
        await engine_for(target3).execute(api("api.login.raw_echo", table(), {"note": note}))
    ).to_dict()
    assert target3.requests[-1].headers["Content-Type"] == "text/plain"
    assert target3.requests[-1].content == f"note={note}".encode()
    assert raw["request"]["body"] == "note=***"
    assert raw["request"]["headers"]["Content-Type"] == "text/plain"
    assert_no_secrets(target, form)
    assert_no_secrets(target2, query)
    assert_no_secrets(target3, raw)


async def test_raw_explicit_content_type_wins() -> None:
    target = FakeTarget()
    d = (await engine_for(target).execute(api("api.login.raw_explicit_ct", None))).to_dict()
    assert target.requests[-1].headers.get_list("Content-Type") == ["application/xml"]
    assert d["request"]["body_type"] == "raw"


@pytest.mark.parametrize(
    "api_id",
    [
        "api.login.multipart",
        "api.login.unknown_type",
        "api.login.raw_no_ct",
        "api.login.secret_placeholder",
        "api.login.login_placeholder",
        "api.login.login_host_placeholder",
    ],
)
async def test_invalid_assets(api_id) -> None:
    target = FakeTarget()
    with pytest.raises(InvalidAssetError):
        await engine_for(target).execute(api(api_id, None))
    assert target.requests == []


async def test_json_default_body_type_zero_impact() -> None:
    """M4：未声明 type 的资产仍以 json= 发送，快照只多出 body_type。"""
    target = FakeTarget()
    d = (
        await engine_for(target).execute(api("api.login.json_default", None, {"value": "v"}))
    ).to_dict()
    assert target.requests[-1].content == b'{"value":"v"}'
    assert d["request"] == {
        "method": "POST",
        "url": f"{HOST}/echo/json",
        "headers": {},
        "body_type": "json",
        "body": {"value": "v"},
    }
    assert d["logins"] == []


# ------------------------------------------------------------ §6-21 回归


async def test_auth_none_and_static_only_regression() -> None:
    target = FakeTarget()
    anon = (await engine_for(target).execute(api("api.login.anon_echo", None))).to_dict()
    assert anon["auth"] is None and anon["logins"] == []
    static = (await engine_for(target).execute(api("api.login.admin_echo", table()))).to_dict()
    assert static["auth"] == {
        "identity": "admin",
        "kind": "static_headers",
        "tier": None,
        "header_names": ["Authorization"],
        "overridden_headers": [],
        "cookie_names": [],
        "login_ref": None,
        "session_source": None,
        "reauthenticated": False,
    }
    assert static["response"]["body"]["echo"]["authz"] == "***"
    assert login_paths(target) == []
    assert_no_secrets(target, anon, static)


# ------------------------------------------------------------ 会话池生命周期 / 日志


async def test_pool_closes_every_client_and_logs_have_no_secrets(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    target = FakeTarget()
    created = []

    def factory():
        client = target.factory()
        original = client.close

        async def close():
            client.closed = True  # type: ignore[attr-defined]
            await original()

        client.close = close  # type: ignore[method-assign]
        created.append(client)
        return client

    from testplat_engine import SingleApiEngine

    engine = SingleApiEngine(client_factory=factory)
    d = (await engine.execute(case("case.login.alternate", table()))).to_dict()
    assert d["status"] == "success"
    # 2 个 login 专用 client + 1 个匿名步骤 client，全部在执行结束时关闭
    assert len(created) == 3
    assert all(getattr(c, "closed", False) for c in created)
    assert_no_secrets(target, caplog.text)
