"""CaseRunner 单元矩阵：用可控的假 StepExecutor 隔离出调度/上下文/异常边界语义。

不依赖网络；真实 HTTP 行为见 ``test_case_golden.py``。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from testplat_engine.adapters.filesystem.case_loader import parse_case_dict
from testplat_engine.domain.case import Case, CaseStep
from testplat_engine.engine.case_runner import CaseRunner
from testplat_engine.interface import (
    AssetNotFoundError,
    EngineInternalError,
    EngineTimeoutError,
    EngineTransportError,
    StepOutcome,
)
from testplat_engine.ports.step_executor import StepExecutionContext, StepExecutorRegistry

ASSETS = Path("/unused-assets-root")


def make_case(*steps: dict[str, Any], params: dict[str, Any] | None = None) -> Case:
    """经真实 loader 解析，保证被测 Case 与线上资产同一套校验。"""
    data: dict[str, Any] = {
        "schema_version": "0.2",
        "id": "case.demo.unit",
        "steps": [{"type": "api", "api_id": "api.demo.fake", **step} for step in steps],
    }
    if params is not None:
        data["params"] = params
    return parse_case_dict(data, expected_case_id="case.demo.unit")


def ok_response(body: Any = None, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status_code": 200, "headers": headers or {}, "body": body if body is not None else {}}


class FakeExecutor:
    """按 step_id 执行脚本；记录调用顺序、收到的 params 与当时的 alias 视图。"""

    def __init__(self, script: dict[str, Callable[[CaseStep], StepOutcome]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[str] = []
        self.params_seen: dict[str, dict[str, Any]] = {}
        self.aliases_seen: dict[str, set[str]] = {}
        self.contexts: list[StepExecutionContext] = []
        self.preflighted: list[str] = []

    async def execute_step(self, step: CaseStep, context: StepExecutionContext) -> StepOutcome:
        self.calls.append(step.step_id)
        self.params_seen[step.step_id] = dict(step.params)
        self.aliases_seen[step.step_id] = set(context.aliases)
        self.contexts.append(context)
        handler = self.script.get(step.step_id)
        if handler is not None:
            return handler(step)
        return success(step, ok_response({"id": step.step_id}))


def success(step: CaseStep, response: Any, **kw: Any) -> StepOutcome:
    return StepOutcome(
        step_id=step.step_id,
        type=step.type,
        status="success",
        duration_seconds=0.001,
        name=step.name,
        request={"method": "GET", "url": "http://x", "headers": {}, "body": None},
        response=response,
        output=step.output,
        **kw,
    )


def assertion_failed(step: CaseStep) -> StepOutcome:
    return StepOutcome(
        step_id=step.step_id,
        type=step.type,
        status="failed",
        duration_seconds=0.001,
        error={"kind": "assertion", "message": "断言失败"},
        response=ok_response(),
    )


def raiser(exc: BaseException) -> Callable[[CaseStep], StepOutcome]:
    def _raise(step: CaseStep) -> StepOutcome:
        raise exc

    return _raise


def runner_for(executor: FakeExecutor, step_type: str = "api") -> CaseRunner:
    registry = StepExecutorRegistry()
    registry.register(step_type, executor)
    return CaseRunner(registry)


async def run(runner: CaseRunner, case: Case, params: dict[str, Any] | None = None):
    return await runner.run(
        case,
        assets_root=ASSETS,
        environment={"api_hosts": {}},
        case_params={**case.params, **(params or {})},
        timeout_seconds=5.0,
    )


# ------------------------------------------------------------ 调度与汇总


async def test_serial_order_matches_declaration() -> None:
    executor = FakeExecutor()
    case = make_case({"step_id": "a"}, {"step_id": "b"}, {"step_id": "c"})
    outcome = await run(runner_for(executor), case)
    assert executor.calls == ["a", "b", "c"]
    assert [s.step_id for s in outcome.steps] == ["a", "b", "c"]
    assert outcome.status == "success"
    assert outcome.summary == {"total": 3, "success": 3, "failed": 0, "skipped": 0}


async def test_single_step_success() -> None:
    executor = FakeExecutor()
    outcome = await run(runner_for(executor), make_case({"step_id": "only"}))
    assert outcome.status == "success"
    assert outcome.summary == {"total": 1, "success": 1, "failed": 0, "skipped": 0}


async def test_swapped_declaration_order_swaps_execution_order() -> None:
    """执行顺序只由声明顺序决定：交换声明，调用与结果顺序同步交换。"""
    executor = FakeExecutor()
    case = make_case({"step_id": "b"}, {"step_id": "a"})
    outcome = await run(runner_for(executor), case)
    assert executor.calls == ["b", "a"]
    assert [s.step_id for s in outcome.steps] == ["b", "a"]


async def test_outcome_serialization_and_duration() -> None:
    outcome = await run(runner_for(FakeExecutor()), make_case({"step_id": "a"}))
    payload = outcome.to_dict()
    json.dumps(payload)
    assert payload["case_id"] == "case.demo.unit"
    assert payload["duration_seconds"] >= 0


# ------------------------------------------------------------ fail-fast


async def test_business_failure_stops_and_marks_rest_skipped() -> None:
    executor = FakeExecutor({"b": assertion_failed})
    case = make_case({"step_id": "a"}, {"step_id": "b"}, {"step_id": "c"}, {"step_id": "d"})
    outcome = await run(runner_for(executor), case)
    assert executor.calls == ["a", "b"]  # fail-fast：c、d 从未执行
    assert [s.status for s in outcome.steps] == ["success", "failed", "skipped", "skipped"]
    assert outcome.status == "failed"
    assert outcome.summary == {"total": 4, "success": 1, "failed": 1, "skipped": 2}
    assert all(s.skip_reason == "fail_fast" for s in outcome.steps[2:])
    assert outcome.steps[2].duration_seconds == 0
    assert outcome.steps[1].error == {"kind": "assertion", "message": "断言失败"}


async def test_first_step_failure_skips_everything_after() -> None:
    executor = FakeExecutor({"a": assertion_failed})
    outcome = await run(runner_for(executor), make_case({"step_id": "a"}, {"step_id": "b"}))
    assert executor.calls == ["a"]
    assert outcome.summary == {"total": 2, "success": 0, "failed": 1, "skipped": 1}


# ------------------------------------------------------------ 上下文 / alias


async def test_alias_value_propagates_with_json_type() -> None:
    executor = FakeExecutor(
        {"a": lambda s: success(s, ok_response({"id": 7, "ok": True, "obj": {"k": 1}}))}
    )
    case = make_case(
        {"step_id": "a", "output": "x"},
        {
            "step_id": "b",
            "params": {
                "id": "${x.body.id}",
                "ok": "${x.body.ok}",
                "obj": "${x.body.obj}",
                "code": "${x.status_code}",
                "text": "id=${x.body.id}/${x.body.ok}",
            },
        },
    )
    await run(runner_for(executor), case)
    seen = executor.params_seen["b"]
    assert seen["id"] == 7 and type(seen["id"]) is int
    assert seen["ok"] is True
    assert seen["obj"] == {"k": 1}
    assert seen["code"] == 200
    assert seen["text"] == "id=7/true"  # 嵌入时用 JSON 文本，不是 Python repr


async def test_only_declared_alias_enters_context() -> None:
    """绕开 alias 声明（把每步结果都写进上下文）会让此用例失败。"""
    executor = FakeExecutor()
    case = make_case(
        {"step_id": "a", "output": "x"},
        {"step_id": "b"},  # 未声明 output：结果不得进入上下文
        {"step_id": "c"},
    )
    await run(runner_for(executor), case)
    assert executor.aliases_seen["a"] == set()
    assert executor.aliases_seen["b"] == {"x"}
    assert executor.aliases_seen["c"] == {"x"}  # 不含 b 的 step_id，也不含任何别名


async def test_failed_step_does_not_publish_alias() -> None:
    executor = FakeExecutor({"a": assertion_failed})
    case = make_case({"step_id": "a", "output": "x"}, {"step_id": "b"})
    await run(runner_for(executor), case)
    assert executor.calls == ["a"]  # 失败步骤的 alias 不会被后续使用，因为后续已被跳过


async def test_alias_snapshot_is_read_only_for_executors() -> None:
    executor = FakeExecutor()
    case = make_case({"step_id": "a", "output": "x"}, {"step_id": "b"})
    await run(runner_for(executor), case)
    ctx = executor.contexts[1]
    with pytest.raises(TypeError):
        ctx.aliases["evil"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.aliases["x"]["body"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.case_params["new"] = 1  # type: ignore[index]


async def test_case_params_reference_resolved_before_step() -> None:
    executor = FakeExecutor()
    case = make_case(
        {"step_id": "a", "params": {"n": "${params.name}", "s": "hi-${params.name}"}},
        params={"name": "demo"},
    )
    await run(runner_for(executor), case, {"name": "override"})
    assert executor.params_seen["a"] == {"n": "override", "s": "hi-override"}


async def test_executor_gets_resolved_copy_original_case_unchanged() -> None:
    executor = FakeExecutor()
    case = make_case({"step_id": "a", "params": {"n": "${params.name}"}}, params={"name": "d"})
    await run(runner_for(executor), case)
    assert case.steps[0].params == {"n": "${params.name}"}


@pytest.mark.parametrize(
    "ref,reason",
    [
        ("x.body.missing", "路径"),
        ("x.body.id.deeper", "路径"),  # 中途遇到非对象
        ("x.body.list_value", "对象/数组"),  # 仅在嵌入字符串时才有此失败，见下方 embedded
    ],
)
async def test_unresolvable_reference_is_stable_business_failure(ref: str, reason: str) -> None:
    executor = FakeExecutor(
        {"a": lambda s: success(s, ok_response({"id": 1, "list_value": [1, 2]}))}
    )
    value = f"p-${{{ref}}}" if "list_value" in ref else f"${{{ref}}}"
    case = make_case(
        {"step_id": "a", "output": "x"},
        {"step_id": "b", "params": {"v": value}},
        {"step_id": "c"},
    )
    outcome = await run(runner_for(executor), case)
    assert executor.calls == ["a"]  # b 的 executor 不会被调用：引用在执行前解析
    b = outcome.steps[1]
    assert b.status == "failed"
    assert b.request is None and b.response is None
    assert b.error is not None
    assert b.error["kind"] == "variable_resolution"
    assert b.error["step_id"] == "b"
    assert b.error["path"] == ref
    assert reason in b.error["message"]
    assert outcome.steps[2].status == "skipped"
    assert outcome.status == "failed"


# ------------------------------------------------------------ 系统错误归属


def _system_errors() -> list[tuple[str, BaseException, type]]:
    return [
        ("timeout", EngineTimeoutError(log_reason="t"), EngineTimeoutError),
        ("transport", EngineTransportError(log_reason="t"), EngineTransportError),
        ("internal", EngineInternalError("execution", log_reason="t"), EngineInternalError),
        ("internal", RuntimeError("secret-boom-detail"), EngineInternalError),
    ]


@pytest.mark.parametrize("kind,boom,exc_type", _system_errors())
@pytest.mark.parametrize("fail_at", [0, 1, 2])
async def test_system_error_attributed_to_current_step(
    kind: str, boom: BaseException, exc_type: type, fail_at: int
) -> None:
    ids = ["a", "b", "c"]
    executor = FakeExecutor({ids[fail_at]: raiser(boom)})
    case = make_case(*[{"step_id": i} for i in ids])
    with pytest.raises(exc_type) as ei:
        await run(runner_for(executor), case)
    err = ei.value
    assert (err.kind, err.stage) == (kind, "execution")
    partial = err.partial_outcome
    assert partial is not None
    expected = ["success"] * fail_at + ["failed"] + ["skipped"] * (2 - fail_at)
    assert [s.status for s in partial.steps] == expected
    assert [s.step_id for s in partial.steps] == ids
    current = partial.steps[fail_at]
    assert current.error is not None
    assert current.error["kind"] == kind and current.error["stage"] == "execution"
    assert partial.status == "failed"
    assert executor.calls == ids[: fail_at + 1]  # 之后不再执行
    # 外显 payload 序列化安全，底层异常文本不泄漏。
    text = json.dumps(err.to_dict())
    assert "secret-boom-detail" not in text
    assert err.to_dict()["partial_outcome"]["summary"]["failed"] == 1


async def test_original_exception_chained_for_logs() -> None:
    boom = RuntimeError("root-cause")
    executor = FakeExecutor({"a": raiser(boom)})
    with pytest.raises(EngineInternalError) as ei:
        await run(runner_for(executor), make_case({"step_id": "a"}))
    assert ei.value.__cause__ is boom
    assert ei.value.log_reason is not None and "root-cause" in ei.value.log_reason


async def test_preflight_stage_error_after_start_becomes_internal_execution() -> None:
    """Case 已开始后出现的 preflight 类异常（如资产被中途删除）不得再冒充 preflight。"""
    executor = FakeExecutor({"b": raiser(AssetNotFoundError("gone"))})
    case = make_case({"step_id": "a"}, {"step_id": "b"})
    with pytest.raises(EngineInternalError) as ei:
        await run(runner_for(executor), case)
    err = ei.value
    assert (err.kind, err.stage) == ("internal", "execution")
    assert err.partial_outcome is not None
    assert [s.status for s in err.partial_outcome.steps] == ["success", "failed"]


async def test_cancellation_is_not_swallowed() -> None:
    executor = FakeExecutor({"a": raiser(asyncio.CancelledError())})
    with pytest.raises(asyncio.CancelledError):
        await run(runner_for(executor), make_case({"step_id": "a"}))


# ------------------------------------------------------------ 注册表与结果形状


async def test_unregistered_executor_is_preflight_internal_and_runs_nothing() -> None:
    api_exec = FakeExecutor()
    case = Case(
        schema_version="0.2",
        id="case.demo.unit",
        steps=(
            CaseStep(step_id="a", type="api", api_id="api.demo.fake"),
            CaseStep(step_id="b", type="ui", api_id="api.demo.fake"),
        ),
    )
    with pytest.raises(EngineInternalError) as ei:
        await run(runner_for(api_exec), case)
    err = ei.value
    assert (err.kind, err.stage) == ("internal", "preflight")
    assert err.partial_outcome is None
    assert "ui" in err.public_message
    assert api_exec.calls == []  # 零副作用：连第一个已注册步骤都没执行


async def test_preflight_runs_for_all_steps_before_first_execution() -> None:
    order: list[str] = []

    class Pre(FakeExecutor):
        def preflight_step(self, step: CaseStep, context: StepExecutionContext) -> None:
            order.append(f"pre:{step.step_id}")

        async def execute_step(self, step, context):  # type: ignore[no-untyped-def]
            order.append(f"run:{step.step_id}")
            return await super().execute_step(step, context)

    await run(runner_for(Pre()), make_case({"step_id": "a"}, {"step_id": "b"}))
    assert order == ["pre:a", "pre:b", "run:a", "run:b"]


async def test_preflight_failure_raises_before_any_step() -> None:
    class Pre(FakeExecutor):
        def preflight_step(self, step: CaseStep, context: StepExecutionContext) -> None:
            if step.step_id == "b":
                raise AssetNotFoundError("missing")

    executor = Pre()
    with pytest.raises(AssetNotFoundError) as ei:
        await run(runner_for(executor), make_case({"step_id": "a"}, {"step_id": "b"}))
    assert ei.value.partial_outcome is None
    assert executor.calls == []


@pytest.mark.parametrize(
    "response",
    [
        "plain text",
        {"headers": {}, "body": {}},  # 缺 status_code
        {"status_code": 200, "body": {}},  # 缺 headers
        {"status_code": 200, "headers": {}},  # 缺 body
        {"status_code": "200", "headers": {}, "body": {}},  # status_code 类型错
        {"status_code": True, "headers": {}, "body": {}},  # bool 不是整数状态码
        {"status_code": 200, "headers": [], "body": {}},  # headers 非对象
        None,
    ],
)
async def test_result_shape_mismatch_for_declared_output_is_internal(response: Any) -> None:
    executor = FakeExecutor({"a": lambda s: success(s, response)})
    case = make_case({"step_id": "a", "output": "x"}, {"step_id": "b"})
    with pytest.raises(EngineInternalError) as ei:
        await run(runner_for(executor), case)
    err = ei.value
    assert (err.kind, err.stage) == ("internal", "execution")
    assert err.partial_outcome is not None
    assert [s.status for s in err.partial_outcome.steps] == ["failed", "skipped"]
    assert executor.calls == ["a"]


async def test_response_shape_not_checked_when_no_output_declared() -> None:
    executor = FakeExecutor({"a": lambda s: success(s, "anything")})
    outcome = await run(runner_for(executor), make_case({"step_id": "a"}))
    assert outcome.status == "success"


@pytest.mark.parametrize(
    "bad",
    [
        lambda s: success(CaseStep(step_id="other", type="api", api_id="x"), ok_response()),
        lambda s: StepOutcome(step_id=s.step_id, type="ui", status="success", duration_seconds=0.0),
        lambda s: StepOutcome(
            step_id=s.step_id,
            type=s.type,
            status="skipped",
            duration_seconds=0.0,
            skip_reason="fail_fast",
        ),
        lambda s: "not-a-step-outcome",
    ],
)
async def test_executor_returning_foreign_or_invalid_outcome_is_internal(
    bad: Callable[[CaseStep], Any],
) -> None:
    executor = FakeExecutor({"a": bad})
    with pytest.raises(EngineInternalError) as ei:
        await run(runner_for(executor), make_case({"step_id": "a"}, {"step_id": "b"}))
    assert (ei.value.kind, ei.value.stage) == ("internal", "execution")
    assert ei.value.partial_outcome is not None
    assert [s.status for s in ei.value.partial_outcome.steps] == ["failed", "skipped"]


# ------------------------------------------------------------ 脱敏（runner 层，嵌套）


NESTED_RESPONSE = {
    "status_code": 200,
    "headers": {"Authorization": "tok-SECRET-1", "X-Trace": "t1", "Set-Cookie": "sid=SECRET"},
    "body": {
        "TOKEN": "tok-SECRET-1",
        "user": {"Password": "pw-SECRET-2", "name": "n", "deep": [{"api_key": "k", "v": 1}]},
        "ok": True,
    },
}


async def test_nested_secrets_redacted_in_public_outcome() -> None:
    executor = FakeExecutor({"a": lambda s: success(s, NESTED_RESPONSE)})
    outcome = await run(runner_for(executor), make_case({"step_id": "a", "output": "x"}))
    text = json.dumps(outcome.to_dict())
    for secret in ("tok-SECRET-1", "pw-SECRET-2", "sid=SECRET"):
        assert secret not in text
    body = outcome.to_dict()["steps"][0]["response"]["body"]
    assert body["TOKEN"] == "***"  # 大小写不敏感
    assert body["user"]["Password"] == "***"
    assert body["user"]["deep"][0] == {"api_key": "***", "v": 1}


@pytest.mark.parametrize(
    "ref",
    [
        "x.headers.Authorization",
        "x.headers.Set-Cookie",
        "x.body.TOKEN",
        "x.body.user.Password",
    ],
)
async def test_redacted_values_are_absent_downstream(ref: str) -> None:
    """关闭"移除敏感键"（改成 *** 占位）会让此用例失败：占位值不得流向下游。"""
    executor = FakeExecutor({"a": lambda s: success(s, NESTED_RESPONSE)})
    case = make_case(
        {"step_id": "a", "output": "x"},
        {"step_id": "b", "params": {"v": f"${{{ref}}}"}},
    )
    outcome = await run(runner_for(executor), case)
    assert executor.calls == ["a"]
    assert outcome.steps[1].error is not None
    assert outcome.steps[1].error["kind"] == "variable_resolution"
    assert outcome.steps[1].error["path"] == ref


async def test_non_sensitive_nested_values_still_flow() -> None:
    executor = FakeExecutor({"a": lambda s: success(s, NESTED_RESPONSE)})
    case = make_case(
        {"step_id": "a", "output": "x"},
        {"step_id": "b", "params": {"n": "${x.body.user.name}", "t": "${x.headers.X-Trace}"}},
    )
    await run(runner_for(executor), case)
    assert executor.params_seen["b"] == {"n": "n", "t": "t1"}


async def test_alias_excludes_request_and_error_models() -> None:
    """alias 只绑定 status_code/headers/body，不暴露 request 或完整 StepOutcome。"""
    executor = FakeExecutor({"a": lambda s: success(s, ok_response({"id": 1}))})
    case = make_case(
        {"step_id": "a", "output": "x"},
        {"step_id": "b", "params": {"v": "${x.request}"}},
    )
    outcome = await run(runner_for(executor), case)
    assert outcome.steps[1].error is not None
    assert outcome.steps[1].error["kind"] == "variable_resolution"
    # 另跑一个无引用的 Case，直接观察 executor 看到的 alias 键集合。
    probe = FakeExecutor({"a": lambda s: success(s, ok_response({"id": 1}))})
    await run(runner_for(probe), make_case({"step_id": "a", "output": "x"}, {"step_id": "b"}))
    assert set(probe.contexts[1].aliases["x"]) == {"status_code", "headers", "body"}
