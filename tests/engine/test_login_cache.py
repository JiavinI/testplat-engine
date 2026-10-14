"""3.3d 跨执行登录缓存 + 401 重登重发：设计稿 §6 第 31–43 条与硬约束自查。

全部基于 ``client_factory`` + ``httpx.MockTransport``；过期用替换 ``_now``，不 sleep。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import testplat_engine.engine.login_session as login_session
from testplat_engine import (
    ApiExecutionRequest,
    AuthTable,
    ExpiresFrom,
    LoginAuth,
    SecretStr,
    Validity,
    create_engine,
)
from testplat_engine.engine.login_cache import LoginCache
from tests.engine.login_support import (
    BUYER_PASSWORD,
    ENV,
    HOST,
    LOGIN_ASSETS,
    FakeTarget,
    assert_no_secrets,
    buyer_material,
    engine_for,
    table,
)


def api(api_id: str, auth: AuthTable | None, **kwargs: object) -> ApiExecutionRequest:
    return ApiExecutionRequest(
        api_id=api_id,
        assets_root=LOGIN_ASSETS,
        environment=ENV,
        params={},
        auth=auth,
        **kwargs,  # type: ignore[arg-type]
    )


def login_paths(target: FakeTarget) -> list[str]:
    return [p for p in target.paths() if p.startswith(("/login", "/csrf"))]


def engine_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """只收集 Engine 自己的日志行（排除 httpx 等第三方）。"""
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("testplat_engine"))


def scoped_buyer(**overrides: object) -> AuthTable:
    """带 cache_scope 与足够 ttl 的 buyer 表，便于跨执行命中。"""
    validity = overrides.pop("validity", Validity(ttl_seconds=3600))
    scope = overrides.pop("cache_scope", "proj-a:dev")
    return table(
        buyer=buyer_material(validity=validity, **overrides),  # type: ignore[arg-type]
        cache_scope=scope,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ §6-31 缓存命中


@pytest.mark.asyncio
async def test_31_cache_hit_second_execution_no_login() -> None:
    target = FakeTarget()
    engine = engine_for(target)
    auth = scoped_buyer()
    first = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert first["auth"]["session_source"] == "login"
    assert first["auth"]["login_ref"] == 0
    assert len(first["logins"]) == 1
    second = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert second["status"] == "success"
    assert second["auth"]["session_source"] == "cache"
    assert second["auth"]["login_ref"] is None
    assert second["logins"] == []
    assert login_paths(target) == ["/login/json"]  # 只登录一次
    # inject 仍生效
    assert target.requests[-1].headers["X-TOKEN"] == target.issued[0]
    assert_no_secrets(target, first, second)


# ------------------------------------------------------------------ §6-32 脱敏（硬约束）


@pytest.mark.asyncio
async def test_32_cache_hit_redacts_token_and_login_password() -> None:
    """缓存命中后业务响应回显 token 与登录密码 → 均为 ***。"""
    target = FakeTarget()
    engine = engine_for(target)
    auth = scoped_buyer()
    await engine.execute(api("api.login.buyer_echo_secrets", auth))
    second = (await engine.execute(api("api.login.buyer_echo_secrets", auth))).to_dict()
    assert second["auth"]["session_source"] == "cache"
    echo = second["response"]["body"]["echo"]
    assert echo["x_token"] == "***"
    assert echo["password_echo"] == "***"
    assert target.issued[0] not in str(second)
    assert BUYER_PASSWORD not in str(second)
    assert_no_secrets(target, second)


# ------------------------------------------------------------------ §6-33 过期（替换时钟）


@pytest.mark.asyncio
async def test_33_ttl_expiry_forces_relogin(monkeypatch: pytest.MonkeyPatch) -> None:
    target = FakeTarget()
    engine = engine_for(target)
    clock = [1_000_000.0]
    monkeypatch.setattr(login_session, "_now", lambda: clock[0])
    # ttl=32 → 有效窗口 32-30=2 秒；前进 3 秒后应 miss。
    auth = scoped_buyer(validity=Validity(ttl_seconds=32))
    await engine.execute(api("api.login.buyer_echo", auth))
    assert login_paths(target) == ["/login/json"]
    clock[0] += 3.0
    second = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert second["auth"]["session_source"] == "login"
    assert login_paths(target) == ["/login/json", "/login/json"]


@pytest.mark.asyncio
async def test_33_expires_from_expiry_forces_relogin(monkeypatch: pytest.MonkeyPatch) -> None:
    target = FakeTarget(expires_in=32)  # 响应里的 seconds
    engine = engine_for(target)
    clock = [1_000_000.0]
    monkeypatch.setattr(login_session, "_now", lambda: clock[0])
    auth = scoped_buyer(
        validity=Validity(expires_from=ExpiresFrom(path="$.data.expiresIn", unit="seconds"))
    )
    await engine.execute(api("api.login.buyer_echo", auth))
    clock[0] += 3.0
    second = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert second["auth"]["session_source"] == "login"
    assert len(login_paths(target)) == 2


# ------------------------------------------------------------------ §6-34 R2 无有效期不缓存


@pytest.mark.asyncio
async def test_34_no_ttl_no_expires_from_never_caches() -> None:
    target = FakeTarget()
    engine = engine_for(target)
    # Validity() 默认无 ttl、无 expires_from → 不写缓存。
    auth = scoped_buyer(validity=Validity())
    await engine.execute(api("api.login.buyer_echo", auth))
    second = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert second["auth"]["session_source"] == "login"
    assert len(login_paths(target)) == 2


# ------------------------------------------------------------------ §6-35 R5 cookie 不跨执行缓存


@pytest.mark.asyncio
async def test_35_session_cookie_not_cross_execution_cached() -> None:
    from tests.engine.login_support import seller_material

    target = FakeTarget()
    engine = engine_for(target)
    auth = table(seller=seller_material(validity=Validity(ttl_seconds=3600)), cache_scope="p:e")
    await engine.execute(api("api.login.seller_echo", auth))
    await engine.execute(api("api.login.seller_echo", auth))
    # 每次执行都走完整 CSRF 登录
    assert login_paths(target).count("/csrf/login") >= 4  # 每执行 GET+POST


# ------------------------------------------------------------------ §6-36 指纹失效


@pytest.mark.asyncio
async def test_36_password_change_invalidates_fingerprint() -> None:
    target = FakeTarget()
    engine = engine_for(target)
    auth1 = scoped_buyer()
    await engine.execute(api("api.login.buyer_echo", auth1))
    # 换密码：指纹变 → 不得复用旧 token。目标仍只认 BUYER_PASSWORD，
    # 所以用不同的 secret_values + body 字段构造新材料。
    other_pw = "tp-fake-buyer-pass-OTHER"
    from testplat_engine import ExtractRule, LoginBody, LoginFlow, LoginStep

    new_buyer = LoginAuth(
        strategy="generic_flow",
        host="target",
        tier="generic",
        flow=LoginFlow(
            steps=(
                LoginStep(
                    name="login",
                    method="POST",
                    path="/login/json",
                    body=LoginBody(
                        type="json",
                        fields={"username": "buyer001", "password": SecretStr(other_pw)},
                    ),
                    success={"path": "$.code", "value": 0},
                    extract=(
                        ExtractRule(source="body_json", path="$.data.token", as_name="token"),
                    ),
                ),
            )
        ),
        secret_values=(SecretStr(other_pw),),
        inject_headers={"X-TOKEN": "${login:token}"},
        validity=Validity(ttl_seconds=3600),
    )
    # 目标会因密码错误失败；关键是不得命中旧缓存（若命中会成功且 session_source=cache）。
    auth2 = table(buyer=new_buyer, cache_scope="proj-a:dev")
    second = (await engine.execute(api("api.login.buyer_echo", auth2))).to_dict()
    assert second["auth"]["session_source"] != "cache"
    assert second["status"] == "failed"  # 密码错误 → login_failed
    assert len([p for p in target.paths() if p == "/login/json"]) == 2


@pytest.mark.asyncio
async def test_36_path_change_invalidates_fingerprint() -> None:
    target = FakeTarget()
    engine = engine_for(target)
    await engine.execute(api("api.login.buyer_echo", scoped_buyer()))
    auth2 = scoped_buyer()
    # 改登录 path
    from testplat_engine import ExtractRule, LoginBody, LoginStep

    changed = buyer_material(
        step=LoginStep(
            name="login",
            method="POST",
            path="/login/form",  # 不同 path
            body=LoginBody(
                type="form",
                fields={"username": "buyer001", "password": SecretStr(BUYER_PASSWORD)},
            ),
            success={"path": "$.code", "value": 0},
            extract=(ExtractRule(source="body_json", path="$.data.token", as_name="token"),),
        ),
        validity=Validity(ttl_seconds=3600),
    )
    auth2 = table(buyer=changed, cache_scope="proj-a:dev")
    second = (await engine.execute(api("api.login.buyer_echo", auth2))).to_dict()
    assert second["auth"]["session_source"] == "login"
    assert "/login/form" in target.paths()


# ------------------------------------------------------------------ §6-37 作用域隔离


@pytest.mark.asyncio
async def test_37_cache_scope_isolation() -> None:
    target = FakeTarget()
    engine = engine_for(target)
    a = scoped_buyer(cache_scope="proj-a:dev")
    b = scoped_buyer(cache_scope="proj-b:dev")
    await engine.execute(api("api.login.buyer_echo", a))
    await engine.execute(api("api.login.buyer_echo", b))
    assert login_paths(target) == ["/login/json", "/login/json"]
    # 各自再命中自己的缓存
    await engine.execute(api("api.login.buyer_echo", a))
    await engine.execute(api("api.login.buyer_echo", b))
    assert login_paths(target) == ["/login/json", "/login/json"]


# ------------------------------------------------------------------ §6-38/39/40/41 401 重发


@pytest.mark.asyncio
async def test_38_reauth_on_401_success_post_body_identical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    target = FakeTarget()
    engine = engine_for(target)
    auth = scoped_buyer()
    # 先登录并写入缓存
    await engine.execute(api("api.login.buyer_echo_post", auth))
    target.revoke_all()
    # 第二次：缓存命中 → 业务 401 → 重登 → 重发 200
    d = (await engine.execute(api("api.login.buyer_echo_post", auth))).to_dict()
    assert d["status"] == "success"
    assert d["auth"]["reauthenticated"] is True
    assert d["auth"]["session_source"] == "login"
    assert len(d["logins"]) == 1  # 只有重登那条（缓存命中未记）
    assert d["response"]["body"]["echo"]["x_token"] == "***"
    # 公开响应是 200，不含第一次 401 的假秘密
    assert target.first_401_body_secret not in str(d)
    # 两次业务 POST 的 body 字节相同
    business = [r for r in target.requests if r.url.path == "/echo/token-post"]
    assert len(business) == 3  # 第一次执行 1 次 + 第二次执行 401+重发
    # 第二次执行的两次（401 与 200）body 相同
    assert business[-2].content == business[-1].content
    assert business[-2].method == business[-1].method == "POST"
    logs = engine_log_text(caplog)
    assert "reauth" in logs and "trigger_status=401" in logs
    # Engine 日志不含 URL / 指纹 / 秘密 / 第一次 401 响应体
    assert HOST not in logs
    assert BUYER_PASSWORD not in logs
    assert "fingerprint" not in logs.lower()
    assert target.first_401_body_secret not in logs
    assert_no_secrets(target, d)


@pytest.mark.asyncio
async def test_39_reauth_still_401_asserts_once() -> None:
    target = FakeTarget(always_401=True)
    engine = engine_for(target)
    auth = scoped_buyer()
    d = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert d["status"] == "failed"
    assert d["error"]["kind"] == "assertion"
    assert d["auth"]["reauthenticated"] is True
    # 1 次初登 + 1 次重登；业务请求恰好 2 次
    assert login_paths(target) == ["/login/json", "/login/json"]
    assert target.paths().count("/echo/token") == 2


@pytest.mark.asyncio
async def test_40_reauth_disabled_no_retry() -> None:
    target = FakeTarget(always_401=True)
    engine = engine_for(target)
    auth = scoped_buyer(reauth_on_401=False)
    d = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert d["auth"]["reauthenticated"] is False
    assert login_paths(target) == ["/login/json"]
    assert target.paths().count("/echo/token") == 1


@pytest.mark.asyncio
async def test_41_reauth_login_failed() -> None:
    """重登失败 → login_failed + reauthenticated=true。"""
    target = FakeTarget()
    engine = engine_for(target)
    auth = scoped_buyer()
    await engine.execute(api("api.login.buyer_echo", auth))
    target.revoke_all()
    # 把密码改错使重登失败：通过替换目标行为更简单 — 让第二次登录失败
    original = target._login_ok

    def bad_login(user: str, password: str | None, *, csrf: bool = False):
        # 第一次登录已过；之后全部失败
        if len(target.issued) >= 1 and any(r.url.path == "/echo/token" for r in target.requests):
            return __import__("httpx").Response(200, json={"code": 1, "msg": "bad"})
        return original(user, password, csrf=csrf)

    target._login_ok = bad_login  # type: ignore[method-assign]
    d = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    # 缓存命中后业务 401 → 重登失败
    assert d["status"] == "failed"
    assert d["error"]["kind"] == "login_failed"
    assert d["auth"]["reauthenticated"] is True


# ------------------------------------------------------------------ §6-42 缓存作废后命中新 token


@pytest.mark.asyncio
async def test_42_after_reauth_cache_stores_new_token() -> None:
    target = FakeTarget()
    engine = engine_for(target)
    auth = scoped_buyer()
    await engine.execute(api("api.login.buyer_echo", auth))
    target.revoke_all()
    mid = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert mid["auth"]["reauthenticated"] is True
    new_token = target.issued[-1]
    # 再执行一次：应命中重登后的新 token，目标不再收到登录
    logins_before = len(login_paths(target))
    third = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert third["auth"]["session_source"] == "cache"
    assert len(login_paths(target)) == logins_before
    assert target.requests[-1].headers["X-TOKEN"] == new_token


# ------------------------------------------------------------------ §6-43 并发 single-flight


@pytest.mark.asyncio
async def test_43_concurrent_cold_start_single_flight() -> None:
    target = FakeTarget()
    engine = engine_for(target)
    auth = scoped_buyer()

    async def once() -> object:
        return await engine.execute(api("api.login.buyer_echo", auth))

    results = await asyncio.gather(once(), once())
    assert all(r.status == "success" for r in results)  # type: ignore[union-attr]
    assert login_paths(target).count("/login/json") == 1
    sources = {r.to_dict()["auth"]["session_source"] for r in results}  # type: ignore[union-attr]
    # 一个 login，一个 cache（或两个都 login 若竞态极窄——single-flight 下应为 1 次登录）
    assert "login" in sources
    assert sources <= {"login", "cache", "execution"}


# ------------------------------------------------------------------ generation 竞态


@pytest.mark.asyncio
async def test_generation_invalidate_does_not_delete_newer_entry() -> None:
    """A 执行 401 作废前，B 已写入新条目 → A 的作废不删 B 的条目。"""
    cache = LoginCache(max_entries=16)
    from testplat_engine.engine.login_cache import CacheKey

    key: CacheKey = ("scope", "buyer", "fp-deadbeef")
    gen1 = cache.store(key, {"token": "tok-old-aaaaaaaa"}, expires_at=1e12)
    gen2 = cache.store(key, {"token": "tok-new-bbbbbbbb"}, expires_at=1e12)
    assert gen2 > gen1
    # 用旧 generation 作废 → 不得删新条目
    assert cache.invalidate(key, gen1) is False
    entry = cache.get(key)
    assert entry is not None
    assert entry.generation == gen2
    assert entry.values["token"] == "tok-new-bbbbbbbb"
    # 用新 generation 作废 → 删除
    assert cache.invalidate(key, gen2) is True
    assert cache.get(key) is None


# ------------------------------------------------------------------ max_entries=0


@pytest.mark.asyncio
async def test_max_entries_zero_matches_0_4_0() -> None:
    target = FakeTarget()
    engine = engine_for(target, login_cache_max_entries=0)
    auth = scoped_buyer()
    await engine.execute(api("api.login.buyer_echo", auth))
    second = (await engine.execute(api("api.login.buyer_echo", auth))).to_dict()
    assert second["auth"]["session_source"] == "login"
    assert len(login_paths(target)) == 2


# ------------------------------------------------------------------ repr 脱敏


def test_repr_hides_scope_fingerprint_values() -> None:
    cache = LoginCache(8)
    key = cache.make_key("proj:env", "buyer", "abc123fingerprint")
    cache.store(key, {"token": "tok-secret-zzzzzzzz"}, expires_at=1e12)
    text = repr(cache)
    assert "proj:env" not in text
    assert "abc123" not in text
    assert "tok-secret" not in text
    assert "entries=1" in text

    auth = scoped_buyer(cache_scope="secret-scope:x")
    assert "secret-scope" not in repr(auth)
    assert "secret-scope" not in str(auth.to_dict())
    buyer = auth.identities["buyer"]
    assert isinstance(buyer, LoginAuth)
    assert BUYER_PASSWORD not in repr(buyer)
    assert "reauth_on_401" in buyer.to_dict()


def test_cache_scope_validation() -> None:
    with pytest.raises(ValueError):
        AuthTable(identities={"b": buyer_material()}, cache_scope="bad scope!")
    with pytest.raises(ValueError):
        AuthTable(identities={"b": buyer_material()}, cache_scope="")
    with pytest.raises(ValueError):
        AuthTable(identities={"b": buyer_material()}, cache_scope="x" * 201)
    AuthTable(identities={"b": buyer_material()}, cache_scope="ok.scope:1")


def test_login_cache_max_entries_negative() -> None:
    with pytest.raises(ValueError):
        create_engine(login_cache_max_entries=-1)
    with pytest.raises(ValueError):
        LoginCache(-5)


def test_login_cache_not_picklable() -> None:
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(LoginCache(1))


# ------------------------------------------------------------------ 日志字段约束


@pytest.mark.asyncio
async def test_cache_logs_only_allowed_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    target = FakeTarget()
    engine = engine_for(target)
    auth = scoped_buyer()
    await engine.execute(api("api.login.buyer_echo", auth))
    await engine.execute(api("api.login.buyer_echo", auth))
    text = engine_log_text(caplog)
    assert "cache=store" in text or "cache=hit" in text or "cache=miss" in text
    assert "identity=buyer" in text
    # 禁止字段（只约束 Engine 日志，不含 httpx）
    assert "fingerprint" not in text.lower()
    assert HOST not in text
    assert "expires_at" not in text
    assert BUYER_PASSWORD not in text
    for token in target.issued:
        assert token not in text
