"""Golden G1–G6 + M8：经公开 ``execute(CaseExecutionRequest)`` 跑真实 CaseRunner。

全部使用 127.0.0.1 本地 HTTP server 或注入的可控 client，不访问公网。
每个用例同时断言：步骤顺序、summary、JSON 序列化、秘密不出现、服务端实际收到的请求。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from testplat_engine import (
    ApiExecutionOutcome,
    ApiExecutionRequest,
    AssetNotFoundError,
    CaseExecutionOutcome,
    CaseExecutionRequest,
    EngineInternalError,
    EngineTimeoutError,
    EngineTransportError,
    InvalidAssetError,
    InvalidEnvironmentError,
    InvalidInputError,
    SingleApiEngine,
)
from testplat_engine.ports.http_client import HttpClient, HttpResponse
from tests.engine.case_support import (
    SECRETS,
    copy_assets,
    start_server,
    stop_server,
    write_yaml,
)


@pytest.fixture
def server():
    srv, thread, state, base = start_server()
    try:
        yield state, base
    finally:
        stop_server(srv, thread, state)


@pytest.fixture
def assets_root(tmp_path: Path) -> Path:
    return copy_assets(tmp_path)


def _request(
    assets_root: Path,
    base: str,
    case_id: str,
    params: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> CaseExecutionRequest:
    return CaseExecutionRequest(
        case_id=case_id,
        assets_root=assets_root,
        environment={"api_hosts": {"local": base}},
        params=params or {},
        timeout_seconds=timeout,
    )


def _assert_safe_json(payload: Any) -> str:
    """序列化必须成功，且任何已知秘密都不出现在文本里。"""
    text = json.dumps(payload, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in text, f"秘密泄漏: {secret}"
    return text


def _header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


# ---------------------------------------------------------------- G1


async def test_g1_success_two_steps_alias_propagates(assets_root: Path, server) -> None:
    state, base = server
    outcome = await SingleApiEngine().execute(_request(assets_root, base, "case.demo.g1_success"))

    assert isinstance(outcome, CaseExecutionOutcome)
    assert outcome.status == "success"
    assert outcome.summary == {"total": 2, "success": 2, "failed": 0, "skipped": 0}
    assert [s.step_id for s in outcome.steps] == ["create", "get"]
    assert [s.status for s in outcome.steps] == ["success", "success"]
    # output 只回显 alias 名，不复制响应值。
    assert outcome.steps[0].output == "created"
    assert outcome.steps[1].output is None

    # 服务端视角：顺序 + 第二步确实收到了第一步响应里的 id（int 101 → 头里的 "101"）。
    assert state.paths() == ["/demo/resources", "/demo/resources/item"]
    assert state.requests[0].body == {"name": "demo"}
    assert _header(state.requests[1].headers, "X-Resource-Id") == "101"

    payload = outcome.to_dict()
    _assert_safe_json(payload)
    # 嵌套脱敏：token / password / refresh_token / Set-Cookie 全是占位符。
    resp = payload["steps"][0]["response"]
    assert resp["body"]["token"] == "***"
    assert resp["body"]["profile"] == {"password": "***", "nick": "n1"}
    assert resp["body"]["items"][0] == {"refresh_token": "***", "ok": True}
    assert _header(resp["headers"], "set-cookie") == "***"
    # "无重复 output 快照"：output 只是 alias 名字符串，不是响应副本。
    assert payload["steps"][0]["output"] == "created"
    assert payload["steps"][1]["output"] is None


async def test_request_params_override_case_defaults(assets_root: Path, server) -> None:
    state, base = server
    outcome = await SingleApiEngine().execute(
        _request(assets_root, base, "case.demo.g1_success", {"resource_name": "override"})
    )
    assert outcome.status == "success"
    assert state.requests[0].body == {"name": "override"}


async def test_single_step_case_success(assets_root: Path, server) -> None:
    state, base = server
    outcome = await SingleApiEngine().execute(
        _request(assets_root, base, "case.demo.m5_omit_output")
    )
    assert outcome.status == "success"
    assert outcome.summary == {"total": 1, "success": 1, "failed": 0, "skipped": 0}
    assert outcome.steps[0].output is None


# ---------------------------------------------------------------- G2


async def test_g2_assertion_failed_is_business_failure_with_skipped(
    assets_root: Path, server
) -> None:
    state, base = server
    outcome = await SingleApiEngine().execute(
        _request(assets_root, base, "case.demo.g2_api_assert_failed")
    )
    assert outcome.status == "failed"
    assert outcome.summary == {"total": 2, "success": 0, "failed": 1, "skipped": 1}
    first, second = outcome.steps
    assert first.status == "failed"
    assert first.error is not None and first.error["kind"] == "assertion"
    assert first.response is not None and first.response["status_code"] == 200  # HTTP 200 业务结果
    assert second.status == "skipped" and second.skip_reason == "fail_fast"
    assert second.request is None and second.response is None and second.duration_seconds == 0
    # fail-fast：第二步从未发出请求。
    assert state.paths() == ["/status/200"]
    _assert_safe_json(outcome.to_dict())


# ---------------------------------------------------------------- G3


async def test_g3_typed_propagation_keeps_json_types(assets_root: Path, server) -> None:
    state, base = server
    outcome = await SingleApiEngine().execute(
        _request(assets_root, base, "case.demo.g3_typed_propagation")
    )
    assert outcome.status == "success"
    assert [s.status for s in outcome.steps] == ["success"] * 3
    assert outcome.summary == {"total": 3, "success": 3, "failed": 0, "skipped": 0}

    bodies = [r.body for r in state.requests]
    assert bodies == [{"value": 42}, {"value": True}, {"value": {"k": "v", "n": 1}}]
    # 精确类型：不是 "42" / "True" / "{...}" 字符串。
    assert type(bodies[0]["value"]) is int
    assert type(bodies[1]["value"]) is bool
    assert isinstance(bodies[2]["value"], dict)
    _assert_safe_json(outcome.to_dict())


async def test_single_api_keeps_legacy_string_interpolation(assets_root: Path, server) -> None:
    """兼容守卫：单 API 路径仍是 0.1 的字符串插值，类型保持只属于 Case 路径。"""
    state, base = server
    outcome = await SingleApiEngine().execute(
        ApiExecutionRequest(
            api_id="api.demo.echo_number",
            assets_root=assets_root,
            environment={"api_hosts": {"local": base}},
            params={"value": 42},
        )
    )
    assert isinstance(outcome, ApiExecutionOutcome)
    assert outcome.status == "success"
    assert state.requests[0].body == {"value": "42"}


# ---------------------------------------------------------------- G4 / G5 / M8


async def test_g4a_undeclared_alias_preflight_no_http(assets_root: Path, server) -> None:
    state, base = server
    with pytest.raises(InvalidAssetError) as ei:
        await SingleApiEngine().execute(
            _request(assets_root, base, "case.demo.g4a_undeclared_alias")
        )
    assert (ei.value.kind, ei.value.stage) == ("invalid_asset", "preflight")
    assert ei.value.partial_outcome is None
    assert state.paths() == []


async def test_g4b_missing_runtime_path_is_business_failure(assets_root: Path, server) -> None:
    state, base = server
    outcome = await SingleApiEngine().execute(
        _request(assets_root, base, "case.demo.g4b_missing_runtime_path")
    )
    assert outcome.status == "failed"
    assert outcome.summary == {"total": 3, "success": 1, "failed": 1, "skipped": 1}
    create, consume, after = outcome.steps
    assert create.status == "success"
    assert consume.status == "failed"
    assert consume.request is None and consume.response is None
    assert consume.error is not None
    assert consume.error["kind"] == "variable_resolution"
    assert consume.error["step_id"] == "consume"
    assert consume.error["path"] == "created.body.missing"
    assert after.status == "skipped"
    # 只有 create 发过请求：缺失引用在执行步骤前就失败，没有降级为空串去请求。
    assert state.paths() == ["/demo/resources"]
    _assert_safe_json(outcome.to_dict())


async def test_g5_unsupported_step_type_preflight_no_http(assets_root: Path, server) -> None:
    state, base = server
    with pytest.raises(InvalidAssetError) as ei:
        await SingleApiEngine().execute(
            _request(assets_root, base, "case.demo.g5_unsupported_step")
        )
    assert (ei.value.kind, ei.value.stage) == ("invalid_asset", "preflight")
    assert "ui" in ei.value.public_message
    assert state.paths() == []


async def test_m8_missing_params_is_invalid_input_no_http(assets_root: Path, server) -> None:
    state, base = server
    with pytest.raises(InvalidInputError) as ei:
        await SingleApiEngine().execute(
            _request(assets_root, base, "case.demo.m8_missing_params")
        )
    assert (ei.value.kind, ei.value.stage) == ("invalid_input", "preflight")
    assert state.paths() == []


# ---------------------------------------------------------------- G6


async def test_g6a_timeout_partial_outcome(assets_root: Path, server) -> None:
    state, base = server
    with pytest.raises(EngineTimeoutError) as ei:
        await SingleApiEngine().execute(
            _request(assets_root, base, "case.demo.g6a_timeout", timeout=0.3)
        )
    err = ei.value
    assert (err.kind, err.stage) == ("timeout", "execution")
    partial = err.partial_outcome
    assert partial is not None
    assert [s.step_id for s in partial.steps] == ["before", "slow", "after"]
    assert [s.status for s in partial.steps] == ["success", "failed", "skipped"]
    assert partial.status == "failed"
    assert partial.summary == {"total": 3, "success": 1, "failed": 1, "skipped": 1}
    assert partial.steps[1].error is not None
    assert partial.steps[1].error["kind"] == "timeout"
    assert partial.steps[1].error["stage"] == "execution"
    assert state.paths() == ["/demo/resources", "/demo/timeout"]
    _assert_safe_json(err.to_dict())


async def test_g6b_transport_partial_outcome(assets_root: Path, server) -> None:
    state, base = server
    with pytest.raises(EngineTransportError) as ei:
        await SingleApiEngine().execute(_request(assets_root, base, "case.demo.g6b_transport"))
    err = ei.value
    assert (err.kind, err.stage) == ("transport", "execution")
    partial = err.partial_outcome
    assert partial is not None
    assert [s.status for s in partial.steps] == ["success", "failed", "skipped"]
    assert partial.steps[1].error is not None
    assert partial.steps[1].error["kind"] == "transport"
    assert state.paths() == ["/demo/resources", "/demo/transport"]
    _assert_safe_json(err.to_dict())


# ---------------------------------------------------------------- 注入 client：首步/中间步系统错误


class _ScriptedClient(HttpClient):
    """可控 client：按调用序号返回响应或抛异常，并统计 close。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []
        self.closed = 0

    async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self._script[len(self.calls) - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed += 1


def _ok(body: Any, status: int = 201) -> HttpResponse:
    return HttpResponse(status_code=status, headers={"content-type": "application/json"}, body=body)


@pytest.mark.parametrize(
    "boom,exc_type,kind",
    [
        (TimeoutError("slow"), EngineTimeoutError, "timeout"),
        (ConnectionError("down"), EngineTransportError, "transport"),
        (RuntimeError("secret-internal-detail"), EngineInternalError, "internal"),
    ],
)
@pytest.mark.parametrize("position", ["first", "middle"])
async def test_system_errors_first_and_middle_step(
    assets_root: Path, boom: Exception, exc_type: type, kind: str, position: str
) -> None:
    # g6a 的三步结构：before(create) → slow(timeout_target) → after(get)。
    script: list[Any] = [boom] if position == "first" else [_ok({"id": 1}), boom]
    client = _ScriptedClient(script)
    engine = SingleApiEngine(http_client=client)
    request = CaseExecutionRequest(
        case_id="case.demo.g6a_timeout",
        assets_root=assets_root,
        environment={"api_hosts": {"local": "http://127.0.0.1:9"}},
        params={},
    )
    with pytest.raises(exc_type) as ei:
        await engine.execute(request)
    err = ei.value
    assert (err.kind, err.stage) == (kind, "execution")
    partial = err.partial_outcome
    assert partial is not None
    failed_index = 0 if position == "first" else 1
    statuses = [s.status for s in partial.steps]
    expected = ["skipped"] * 3
    for i in range(failed_index):
        expected[i] = "success"
    expected[failed_index] = "failed"
    assert statuses == expected
    current = partial.steps[failed_index]
    assert current.error is not None
    assert current.error["kind"] == kind and current.error["stage"] == "execution"
    # internal 的底层细节只进日志，不进外显 payload / partial。
    assert "secret-internal-detail" not in json.dumps(err.to_dict())
    assert len(client.calls) == failed_index + 1  # 失败后不再发请求


async def test_owned_client_closed_for_every_step_even_on_failure(assets_root: Path) -> None:
    """资源清理：引擎自建的 client 每步都关闭，系统错误路径也不例外。"""
    clients: list[_ScriptedClient] = []

    class _OwningEngine(SingleApiEngine):
        def _acquire_client(self):  # type: ignore[override]
            script = [_ok({"id": 1})] if not clients else [ConnectionError("down")]
            client = _ScriptedClient(script)
            clients.append(client)
            return client, True

    request = CaseExecutionRequest(
        case_id="case.demo.g6b_transport",
        assets_root=assets_root,
        environment={"api_hosts": {"local": "http://127.0.0.1:9"}},
        params={},
    )
    with pytest.raises(EngineTransportError):
        await _OwningEngine().execute(request)
    assert len(clients) == 2
    assert [c.closed for c in clients] == [1, 1]


# ---------------------------------------------------------------- preflight 整体前置


async def test_missing_api_asset_in_later_step_fails_before_any_http(
    assets_root: Path, server
) -> None:
    state, base = server
    write_yaml(
        assets_root / "scenarios" / "demo" / "late_missing.case.yaml",
        'schema_version: "0.2"\n'
        "id: case.demo.late_missing\n"
        "steps:\n"
        "  - {step_id: a, type: api, api_id: api.demo.get, params: {resource_id: '1'}}\n"
        "  - {step_id: b, type: api, api_id: api.demo.does_not_exist}\n",
    )
    with pytest.raises(AssetNotFoundError) as ei:
        await SingleApiEngine().execute(_request(assets_root, base, "case.demo.late_missing"))
    assert (ei.value.kind, ei.value.stage) == ("asset_not_found", "preflight")
    assert state.paths() == []  # 第一步也没有执行：预检先于任何副作用


async def test_missing_environment_host_preflight_no_http(assets_root: Path, server) -> None:
    state, base = server
    request = CaseExecutionRequest(
        case_id="case.demo.g1_success",
        assets_root=assets_root,
        environment={"api_hosts": {}},
        params={},
    )
    with pytest.raises(InvalidEnvironmentError):
        await SingleApiEngine().execute(request)
    assert state.paths() == []


async def test_execute_rejects_unknown_request_type() -> None:
    with pytest.raises(EngineInternalError) as ei:
        await SingleApiEngine().execute(object())  # type: ignore[call-overload]
    assert (ei.value.kind, ei.value.stage) == ("internal", "preflight")


# ---------------------------------------------------------------- 脱敏与"秘密不下传"


def _write_case(assets_root: Path, name: str, steps_yaml: str) -> str:
    case_id = f"case.demo.{name}"
    write_yaml(
        assets_root / "scenarios" / "demo" / f"{name}.case.yaml",
        f'schema_version: "0.2"\nid: {case_id}\nsteps:\n{steps_yaml}',
    )
    return case_id


@pytest.mark.parametrize(
    "ref",
    [
        "created.body.token",
        "created.body.profile.password",
        "created.headers.set-cookie",
        "created.headers.Set-Cookie",
    ],
)
async def test_redacted_values_never_flow_downstream(
    assets_root: Path, server, ref: str
) -> None:
    state, base = server
    case_id = _write_case(
        assets_root,
        "leak_probe",
        "  - {step_id: create, type: api, api_id: api.demo.create, params: {name: x},"
        " output: created}\n"
        "  - step_id: use\n    type: api\n    api_id: api.demo.get\n"
        f"    params: {{resource_id: '${{{ref}}}'}}\n",
    )
    outcome = await SingleApiEngine().execute(_request(assets_root, base, case_id))
    assert outcome.status == "failed"
    use = outcome.steps[1]
    assert use.error is not None and use.error["kind"] == "variable_resolution"
    assert use.error["path"] == ref
    # 不是 "***" 被当作值发出去：第二步根本没有请求。
    assert state.paths() == ["/demo/resources"]
    _assert_safe_json(outcome.to_dict())


async def test_nested_sensitive_keys_stripped_but_other_nested_values_flow(
    assets_root: Path, server
) -> None:
    state, base = server
    case_id = _write_case(
        assets_root,
        "nested_flow",
        "  - {step_id: create, type: api, api_id: api.demo.create, params: {name: x},"
        " output: created}\n"
        "  - step_id: echo\n    type: api\n    api_id: api.demo.echo_object\n"
        "    params: {value: '${created.body.items}'}\n"
        "  - step_id: nick\n    type: api\n    api_id: api.demo.echo_object\n"
        "    params: {value: 'nick=${created.body.profile.nick} code=${created.status_code}'}\n",
    )
    outcome = await SingleApiEngine().execute(_request(assets_root, base, case_id))
    assert outcome.status == "success"
    # 列表整体被传递，但其中的 refresh_token 已被移除而非保留占位符。
    assert state.requests[1].body == {"value": [{"ok": True}]}
    assert state.requests[2].body == {"value": "nick=n1 code=201"}
    _assert_safe_json(outcome.to_dict())


async def test_request_secrets_redacted_in_public_result(assets_root: Path, server) -> None:
    """请求侧：Authorization/Cookie 真实发出，但公开结果里是占位符。"""
    state, base = server
    write_yaml(
        assets_root / "api" / "demo" / "auth_probe.yaml",
        "id: api.demo.auth_probe\nname: 鉴权探针\nhost: local\nmethod: GET\nurl: /status/200\n"
        "request:\n  headers:\n    Authorization: \"${auth}\"\n    Cookie: \"sid=${sid}\"\n"
        "  body:\n    password: \"${pw}\"\n    nested: {access_token: \"${auth}\", ok: 1}\n"
        "success:\n  status_code: 200\n",
    )
    case_id = _write_case(
        assets_root,
        "auth_case",
        "  - step_id: probe\n    type: api\n    api_id: api.demo.auth_probe\n"
        "    params: {auth: '${params.auth}', sid: '${params.sid}', pw: '${params.pw}'}\n",
    )
    outcome = await SingleApiEngine().execute(
        _request(
            assets_root,
            base,
            case_id,
            {"auth": SECRETS[4], "sid": SECRETS[2], "pw": SECRETS[1]},
        )
    )
    assert outcome.status == "success"
    # 服务端确实收到了真实凭证（脱敏不能影响真实请求）。
    assert _header(state.requests[0].headers, "Authorization") == SECRETS[4]
    request = outcome.to_dict()["steps"][0]["request"]
    assert _header(request["headers"], "Authorization") == "***"
    assert _header(request["headers"], "Cookie") == "***"
    assert request["body"]["password"] == "***"
    assert request["body"]["nested"] == {"access_token": "***", "ok": 1}
    _assert_safe_json(outcome.to_dict())


async def test_interpolation_scalar_ok_and_object_is_business_failure(
    assets_root: Path, server
) -> None:
    state, base = server
    case_id = _write_case(
        assets_root,
        "interp_obj",
        "  - {step_id: create, type: api, api_id: api.demo.create, params: {name: x},"
        " output: created}\n"
        "  - step_id: bad\n    type: api\n    api_id: api.demo.echo_object\n"
        "    params: {value: 'prefix-${created.body.profile}'}\n",
    )
    outcome = await SingleApiEngine().execute(_request(assets_root, base, case_id))
    assert outcome.status == "failed"
    bad = outcome.steps[1]
    assert bad.error is not None and bad.error["kind"] == "variable_resolution"
    assert bad.error["path"] == "created.body.profile"
    assert state.paths() == ["/demo/resources"]


async def test_state_isolated_between_executions(assets_root: Path, server) -> None:
    """同一引擎连续执行两个 Case：alias 不会跨 Case 泄漏。"""
    state, base = server
    engine = SingleApiEngine()
    first = await engine.execute(_request(assets_root, base, "case.demo.g1_success"))
    second = await engine.execute(
        _request(assets_root, base, "case.demo.g4b_missing_runtime_path")
    )
    assert first.status == "success"
    assert second.steps[1].error is not None
    assert second.steps[1].error["kind"] == "variable_resolution"
