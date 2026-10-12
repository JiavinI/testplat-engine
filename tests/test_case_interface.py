"""Case 公共 DTO / 异常 / EnginePort 契约测试。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from testplat_engine import (
    ApiExecutionOutcome,
    ApiExecutionRequest,
    CaseExecutionOutcome,
    CaseExecutionRequest,
    EngineError,
    EngineInternalError,
    EnginePort,
    EngineTimeoutError,
    EngineTransportError,
    InvalidInputError,
    StepOutcome,
    __version__,
)


def make_case_request() -> CaseExecutionRequest:
    return CaseExecutionRequest(
        case_id="case.demo.g1_success",
        assets_root=Path("/assets"),
        environment={"api_hosts": {"local": "http://127.0.0.1"}},
        params={"resource_name": "demo"},
    )


def make_success_steps() -> tuple[StepOutcome, StepOutcome]:
    return (
        StepOutcome(
            step_id="create",
            type="api",
            status="success",
            duration_seconds=0.1,
            name="创建资源",
            request={"method": "POST"},
            response={"status_code": 201, "headers": {}, "body": {"id": "r1"}},
            output="created",
        ),
        StepOutcome(
            step_id="get",
            type="api",
            status="success",
            duration_seconds=0.05,
            request={"method": "GET"},
            response={"status_code": 200, "headers": {}, "body": {"id": "r1"}},
        ),
    )


def test_version_is_0_2_0() -> None:
    assert __version__ == "0.2.0"


def test_case_request_is_frozen_and_json_serializable() -> None:
    request = make_case_request()
    with pytest.raises(AttributeError):
        request.case_id = "case.other"  # type: ignore[misc]
    encoded = json.dumps(request.to_dict())
    assert '"case_id": "case.demo.g1_success"' in encoded
    # fail-fast 固定开启：请求表面不得出现可覆盖开关
    assert "fail_fast" not in request.to_dict()


def test_case_request_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="case_id"):
        CaseExecutionRequest("", Path("/a"), {}, {})
    with pytest.raises(ValueError, match="timeout_seconds"):
        CaseExecutionRequest("case.demo.x", Path("/a"), {}, {}, timeout_seconds=0)
    with pytest.raises(TypeError, match="JSON"):
        CaseExecutionRequest("case.demo.x", Path("/a"), {"p": Path("/t")}, {})  # type: ignore[arg-type]


def test_step_outcome_invariants_and_json_round_trip() -> None:
    success, get_step = make_success_steps()
    assert json.loads(json.dumps(success.to_dict()))["output"] == "created"
    assert get_step.output is None

    failed = StepOutcome(
        step_id="consume",
        type="api",
        status="failed",
        duration_seconds=0.0,
        error={
            "kind": "variable_resolution",
            "message": "步骤 consume 无法解析 ${created.body.missing}",
            "step_id": "consume",
            "path": "created.body.missing",
        },
        request=None,
        response=None,
    )
    assert failed.request is None and failed.response is None

    skipped = StepOutcome(
        step_id="after",
        type="api",
        status="skipped",
        duration_seconds=0.0,
        skip_reason="fail_fast",
    )
    assert skipped.to_dict()["skip_reason"] == "fail_fast"


def test_step_outcome_rejects_forged_skipped_and_bad_status() -> None:
    with pytest.raises(ValueError, match="duration_seconds"):
        StepOutcome(
            step_id="a",
            type="api",
            status="skipped",
            duration_seconds=1.0,
            skip_reason="fail_fast",
        )
    with pytest.raises(ValueError, match="request/response/error"):
        StepOutcome(
            step_id="a",
            type="api",
            status="skipped",
            duration_seconds=0.0,
            request={"method": "GET"},
            skip_reason="fail_fast",
        )
    with pytest.raises(ValueError, match="fail_fast"):
        StepOutcome(
            step_id="a",
            type="api",
            status="skipped",
            duration_seconds=0.0,
            skip_reason="other",
        )
    with pytest.raises(ValueError, match="failed"):
        StepOutcome(step_id="a", type="api", status="failed", duration_seconds=0.0)
    with pytest.raises(ValueError, match="success"):
        StepOutcome(
            step_id="a",
            type="api",
            status="success",
            duration_seconds=0.0,
            error={"kind": "x"},
        )


def test_case_outcome_summary_derived_and_status_invariant() -> None:
    steps = make_success_steps()
    outcome = CaseExecutionOutcome(
        case_id="case.demo.g1_success",
        status="success",
        duration_seconds=0.2,
        steps=steps,
    )
    assert outcome.summary == {"total": 2, "success": 2, "failed": 0, "skipped": 0}
    payload = json.loads(json.dumps(outcome.to_dict()))
    assert payload["summary"] == outcome.summary
    assert payload["steps"][0]["step_id"] == "create"

    with pytest.raises(ValueError, match="不一致"):
        CaseExecutionOutcome(
            case_id="case.demo.g1_success",
            status="failed",
            duration_seconds=0.2,
            steps=steps,
        )


def test_case_outcome_failed_with_skipped_summary() -> None:
    steps = (
        StepOutcome(
            step_id="bad",
            type="api",
            status="failed",
            duration_seconds=0.01,
            error={"kind": "assertion", "message": "断言失败"},
            request={"method": "GET"},
            response={"status_code": 200, "body": {}},
        ),
        StepOutcome(
            step_id="after",
            type="api",
            status="skipped",
            duration_seconds=0.0,
            skip_reason="fail_fast",
        ),
    )
    outcome = CaseExecutionOutcome(
        case_id="case.demo.g2_api_assert_failed",
        status="failed",
        duration_seconds=0.01,
        steps=steps,
    )
    assert outcome.summary == {"total": 2, "success": 0, "failed": 1, "skipped": 1}


def test_partial_outcome_round_trip_on_execution_errors() -> None:
    partial = CaseExecutionOutcome(
        case_id="case.demo.g6a_timeout",
        status="failed",
        duration_seconds=1.5,
        steps=(
            StepOutcome(
                step_id="before",
                type="api",
                status="success",
                duration_seconds=0.1,
                output="created",
                response={"status_code": 201, "headers": {}, "body": {"id": "1"}},
            ),
            StepOutcome(
                step_id="slow",
                type="api",
                status="failed",
                duration_seconds=1.4,
                error={"kind": "timeout", "message": "API 请求超时", "stage": "execution"},
            ),
            StepOutcome(
                step_id="after",
                type="api",
                status="skipped",
                duration_seconds=0.0,
                skip_reason="fail_fast",
            ),
        ),
    )
    err = EngineTimeoutError(partial_outcome=partial, log_reason="httpx.TimeoutException")
    assert err.kind == "timeout" and err.stage == "execution"
    dumped = json.loads(json.dumps(err.to_dict()))
    assert dumped["partial_outcome"]["summary"] == {
        "total": 3,
        "success": 1,
        "failed": 1,
        "skipped": 1,
    }
    assert "secret" not in json.dumps(dumped)
    assert err.log_reason is not None

    transport_partial = CaseExecutionOutcome(
        case_id="case.demo.g6b_transport",
        status="failed",
        duration_seconds=0.5,
        steps=(
            StepOutcome(
                step_id="before",
                type="api",
                status="success",
                duration_seconds=0.1,
                output="created",
            ),
            StepOutcome(
                step_id="boom",
                type="api",
                status="failed",
                duration_seconds=0.4,
                error={"kind": "transport", "message": "API 传输失败"},
            ),
            StepOutcome(
                step_id="after",
                type="api",
                status="skipped",
                duration_seconds=0.0,
                skip_reason="fail_fast",
            ),
        ),
    )
    terr = EngineTransportError(partial_outcome=transport_partial)
    assert terr.to_dict()["partial_outcome"]["steps"][1]["error"]["kind"] == "transport"


def test_partial_outcome_rejects_kind_mismatch_and_preflight() -> None:
    partial = CaseExecutionOutcome(
        case_id="case.demo.x",
        status="failed",
        duration_seconds=0.1,
        steps=(
            StepOutcome(
                step_id="a",
                type="api",
                status="failed",
                duration_seconds=0.1,
                error={"kind": "transport", "message": "x"},
            ),
        ),
    )
    with pytest.raises(ValueError, match="不一致"):
        EngineTimeoutError(partial_outcome=partial)
    with pytest.raises(ValueError, match="preflight"):
        EngineInternalError("preflight", partial_outcome=partial)
    # preflight kind 不得携带 partial_outcome
    with pytest.raises(ValueError, match="preflight"):
        EngineError(
            "invalid_input",
            "preflight",
            "执行输入无效",
            partial_outcome=partial,
        )


def test_invalid_input_error_stable_kind_stage() -> None:
    err = InvalidInputError("Case 参数缺失: params.missing", log_reason="private")
    assert err.kind == "invalid_input"
    assert err.stage == "preflight"
    assert "private" not in json.dumps(err.to_dict())


def test_engine_port_accepts_api_and_case_statically() -> None:
    class FakeEngine:
        async def execute(
            self, request: ApiExecutionRequest | CaseExecutionRequest
        ) -> ApiExecutionOutcome | CaseExecutionOutcome:
            if isinstance(request, CaseExecutionRequest):
                return CaseExecutionOutcome(
                    case_id=request.case_id,
                    status="success",
                    duration_seconds=0.0,
                    steps=(
                        StepOutcome(
                            step_id="only",
                            type="api",
                            status="success",
                            duration_seconds=0.0,
                        ),
                    ),
                )
            return ApiExecutionOutcome(status="success")

    engine: EnginePort = FakeEngine()
    assert engine is not None


def test_step_outcome_rejects_non_finite_duration() -> None:
    with pytest.raises(ValueError, match="有限非负"):
        StepOutcome(
            step_id="a",
            type="api",
            status="success",
            duration_seconds=math.inf,
        )
