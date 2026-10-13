"""旧项目五个 profile 的 ``generic_flow`` 等价配置（第 1/2 档），对假目标各跑一条。

变量名一律假设为 ``TESTPLAT_`` 前缀（此处直接给出 Control 渲染后的 SecretStr）。
对应 Agent 0 的旧 profile 可表达性结论：
- gsb_token：密码放在登录 URL 的 query 里（query 请求体 + 快照 url 不含参数，Q8）；
- gsb_csrf：Q1 放宽后可表达（inject 含字面量 X-SOURCE）；
- admin_go2_api：follow_redirects=False，302 可见，probe 拒绝 /login 跳转与登录表单；
- df_picker_api：步骤显式 Content-Type 优先（Q4）；
- cat_api：cookie jar + html_meta / cookies / response_header 多来源提取。
"""

from __future__ import annotations

from testplat_engine import (
    ApiExecutionRequest,
    ExtractRule,
    LoginAuth,
    LoginBody,
    LoginFlow,
    LoginStep,
    ProbeSpec,
    SecretStr,
    Validity,
)
from tests.engine.login_support import (
    BUYER_PASSWORD,
    CSRF_SEED,
    CSRF_TOKEN,
    ENV,
    HOST,
    LOGIN_ASSETS,
    SELLER_PASSWORD,
    FakeTarget,
    assert_no_secrets,
    engine_for,
    table,
)


def _run(target: FakeTarget, material: LoginAuth, api_id: str = "api.login.buyer_echo"):
    identity = "buyer" if api_id.endswith("buyer_echo") else "seller"
    request = ApiExecutionRequest(
        api_id=api_id,
        assets_root=LOGIN_ASSETS,
        environment=ENV,
        params={},
        auth=table(**{identity: material}),
    )
    return engine_for(target).execute(request)


async def test_legacy_gsb_token_query_login() -> None:
    target = FakeTarget()
    material = LoginAuth(
        strategy="generic_flow",
        host="target",
        tier="configured",
        flow=LoginFlow(
            steps=(
                LoginStep(
                    name="login",
                    method="GET",
                    path="/login/query",
                    body=LoginBody(
                        type="query",
                        fields={
                            "username": "buyer001",
                            "password": SecretStr(BUYER_PASSWORD),
                            "source": "gsb",
                        },
                    ),
                    success={"path": "$.code", "value": 0},
                    extract=(
                        ExtractRule(source="body_json", path="$.data.hash", as_name="token"),
                        ExtractRule(source="body_json", path="$.data.userId", as_name="user_id"),
                    ),
                ),
            )
        ),
        validity=Validity(ttl_seconds=14400),
        secret_values=(SecretStr(BUYER_PASSWORD),),
        inject_headers={"X-TOKEN": "${login:token}", "X-USER-ID": "${login:user_id}"},
    )
    d = (await _run(target, material)).to_dict()
    assert d["status"] == "success"
    assert target.requests[0].url.params["password"] == BUYER_PASSWORD
    step = d["logins"][0]["steps"][0]["request"]
    assert step["url"] == f"{HOST}/login/query"
    assert step["query"] == {"username": "buyer001", "password": "***", "source": "gsb"}
    assert target.requests[-1].headers["X-USER-ID"] == "10001"
    assert_no_secrets(target, d)


async def test_legacy_gsb_csrf_literal_inject() -> None:
    target = FakeTarget()
    material = LoginAuth(
        strategy="generic_flow",
        host="target",
        tier="configured",
        flow=LoginFlow(
            steps=(
                LoginStep(
                    name="fetch_csrf",
                    method="GET",
                    path="/login/query-csrf",
                    body=LoginBody(
                        type="query",
                        fields={
                            "username": "buyer001",
                            "password": SecretStr(BUYER_PASSWORD),
                        },
                    ),
                    extract=(
                        ExtractRule(
                            source="body_json", path="$.data.csrfToken", as_name="visit_csrf_token"
                        ),
                        ExtractRule(source="body_json", path="$.data.userId", as_name="user_id"),
                    ),
                ),
            )
        ),
        secret_values=(SecretStr(BUYER_PASSWORD),),
        inject_headers={
            "X-CSRF-Token": "${login:visit_csrf_token}",
            "X-USER-ID": "${login:user_id}",
            "X-SOURCE": "web-h5",
        },
    )
    d = (await _run(target, material)).to_dict()
    sent = target.requests[-1]
    assert sent.headers["X-CSRF-Token"] == CSRF_TOKEN and sent.headers["X-SOURCE"] == "web-h5"
    assert d["request"]["headers"]["X-SOURCE"] == "***"
    # 该 profile 不提取 token：假目标顺带返回的 $.data.hash 不是命名值，按设计不做按值替换
    # （只有命名值 / Cookie / 秘密参数进入集合）；这里只断言密码与提取值不外泄。
    text = str(d)
    assert BUYER_PASSWORD not in text and CSRF_TOKEN not in text


async def test_legacy_admin_go2_api_cookie_probe() -> None:
    target = FakeTarget()
    material = LoginAuth(
        strategy="generic_flow",
        host="target",
        tier="configured",
        flow=LoginFlow(
            steps=(
                LoginStep(
                    name="bootstrap",
                    method="GET",
                    path="/csrf/login",
                    extract=(ExtractRule(source="html_meta", name="csrf-token", as_name="csrf"),),
                ),
                LoginStep(
                    name="submit",
                    method="POST",
                    path="/csrf/login",
                    body=LoginBody(
                        type="form",
                        fields={
                            "_csrf": "${login:csrf}",
                            "username": "admin",
                            "password": SecretStr(SELLER_PASSWORD),
                        },
                    ),
                    success={"status_code": 302},
                ),
            )
        ),
        validity=Validity(ttl_seconds=1000),
        session_cookie=True,
        secret_values=(SecretStr(SELLER_PASSWORD),),
        probe=ProbeSpec(
            path="/csrf/protected",
            success={
                "status_code": 200,
                "reject_location_contains": "/login",
                "body_not_contains": '<form id="loginFrom"',
            },
        ),
    )
    d = (await _run(target, material, "api.login.seller_echo")).to_dict()
    assert d["status"] == "success"
    assert d["logins"][0]["steps"][1]["response"]["status_code"] == 302
    assert d["logins"][0]["steps"][1]["response"]["headers"]["location"] == "/home"
    assert "SESSION=" in target.requests[-1].headers["Cookie"]
    assert d["auth"]["header_names"] == []  # Q2：session: cookie 时 inject 可省略
    assert_no_secrets(target, d)


async def test_legacy_df_picker_explicit_content_type() -> None:
    target = FakeTarget()
    explicit = "application/x-www-form-urlencoded;charset=UTF-8"
    material = LoginAuth(
        strategy="generic_flow",
        host="target",
        tier="configured",
        flow=LoginFlow(
            steps=(
                LoginStep(
                    name="login",
                    method="POST",
                    path="/login/form",
                    headers={"Content-Type": explicit},
                    body=LoginBody(
                        type="form",
                        fields={
                            "username": "picker",
                            "password": SecretStr(BUYER_PASSWORD),
                            "source": "df",
                        },
                    ),
                    success={"path": "$.code", "value": 0},
                    extract=(
                        ExtractRule(source="body_json", path="$.data.token", as_name="token"),
                        ExtractRule(source="body_json", path="$.data.userId", as_name="user_id"),
                    ),
                ),
            )
        ),
        validity=Validity(ttl_seconds=14400),
        secret_values=(SecretStr(BUYER_PASSWORD),),
        inject_headers={"Authorization": "${login:token}"},
    )
    d = (await _run(target, material)).to_dict()
    assert target.requests[0].headers.get_list("Content-Type") == [explicit]
    assert target.requests[-1].headers["Authorization"] == target.issued[0]
    assert d["response"]["body"]["echo"]["authz"] == "***"
    assert_no_secrets(target, d)


async def test_legacy_cat_api_multi_source_extract() -> None:
    target = FakeTarget()
    material = LoginAuth(
        strategy="generic_flow",
        host="target",
        tier="configured",
        flow=LoginFlow(
            steps=(
                LoginStep(
                    name="bootstrap",
                    method="GET",
                    path="/csrf/login",
                    extract=(
                        ExtractRule(source="cookies", format="cookie_header", as_name="cookie_jar"),
                        ExtractRule(source="cookies", name="_csrf", as_name="csrf_seed"),
                        ExtractRule(source="html_meta", name="csrf-token", as_name="csrf_token"),
                    ),
                ),
                LoginStep(
                    name="submit",
                    method="POST",
                    path="/csrf/login",
                    headers={"Referer": "${login_host}/login"},
                    body=LoginBody(
                        type="form",
                        fields={
                            "_csrf": "${login:csrf_token}",
                            "LoginForm[username]": "cat",
                            "LoginForm[password]": SecretStr(SELLER_PASSWORD),
                            "login-button": "",
                        },
                    ),
                    success={"status_code": 302, "reject_location_contains": "/login"},
                    extract=(
                        ExtractRule(
                            source="cookies", format="cookie_header", as_name="cookie_jar2"
                        ),
                        ExtractRule(
                            source="cookies", name="_csrf", as_name="csrf_seed2", optional=True
                        ),
                    ),
                ),
                LoginStep(
                    name="probe_uid",
                    method="GET",
                    path="/csrf/protected",
                    extract=(
                        ExtractRule(
                            source="response_header", name="x-uid", as_name="user_id", optional=True
                        ),
                    ),
                ),
            )
        ),
        validity=Validity(ttl_seconds=7200),
        session_cookie=True,
        secret_values=(SecretStr(SELLER_PASSWORD),),
        inject_headers={"X-CSRF-Token": "${login:csrf_token}", "X-UID": "${login:user_id}"},
    )
    d = (await _run(target, material, "api.login.seller_echo")).to_dict()
    assert d["status"] == "success"
    sent = target.requests[-1]
    assert sent.headers["X-UID"] == "777" and sent.headers["X-CSRF-Token"] == CSRF_TOKEN
    assert CSRF_SEED in sent.headers["Cookie"]
    assert_no_secrets(target, d)
