"""公共 Engine interface 的契约测试。"""

import json
import math
from pathlib import Path

import pytest

from testplat_engine import (
    ApiExecutionOutcome,
    ApiExecutionRequest,
    AssetNotFoundError,
    EngineError,
    EngineInternalError,
    EnginePort,
    EngineTimeoutError,
    EngineTransportError,
    EngineUnavailableError,
    InvalidAssetError,
    InvalidEnvironmentError,
    InvalidInputError,
)


def make_request() -> ApiExecutionRequest:
    return ApiExecutionRequest(
        api_id="api.common.health_check",
        assets_root=Path("/assets"),
        environment={"base_url": "http://127.0.0.1"},
        params={"request_id": "test"},
    )


def test_request_is_frozen_and_json_serializable() -> None:
    request = make_request()

    with pytest.raises(AttributeError):
        request.api_id = "api.other"  # type: ignore[misc]

    encoded = json.dumps(request.to_dict())
    assert '"assets_root": "/assets"' in encoded


def test_outcome_supports_business_failed_without_exception() -> None:
    outcome = ApiExecutionOutcome(
        status="failed",
        request={"method": "GET"},
        response={"status_code": 200, "body": {"ok": False}},
        error={"kind": "assertion", "message": "业务断言失败"},
    )

    assert outcome.status == "failed"
    assert json.loads(json.dumps(outcome.to_dict()))["error"]["kind"] == "assertion"


def test_outcome_rejects_invalid_or_non_json_values() -> None:
    with pytest.raises(ValueError, match="status"):
        ApiExecutionOutcome(status="pending")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="failed"):
        ApiExecutionOutcome(status="failed")
    with pytest.raises(ValueError, match="success"):
        ApiExecutionOutcome(status="success", error={"message": "unexpected"})
    with pytest.raises(TypeError, match="JSON"):
        ApiExecutionOutcome(status="success", response=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="JSON 对象"):
        ApiExecutionOutcome(status="success", request=["GET"])  # type: ignore[arg-type]


def test_request_rejects_invalid_timeout_and_non_json_values() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ApiExecutionRequest("api.test", Path("/assets"), {}, {}, timeout_seconds=0)
    for invalid_timeout in (math.nan, math.inf):
        with pytest.raises(ValueError, match="有限正数"):
            ApiExecutionRequest(
                "api.test", Path("/assets"), {}, {}, timeout_seconds=invalid_timeout
            )
    with pytest.raises(TypeError, match="JSON"):
        ApiExecutionRequest("api.test", Path("/assets"), {"path": Path("/tmp")}, {})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="JSON 对象"):
        ApiExecutionRequest("api.test", Path("/assets"), [], {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="NaN"):
        ApiExecutionRequest("api.test", Path("/assets"), {"ratio": math.nan}, {})


@pytest.mark.parametrize(
    ("error_type", "kind", "stage"),
    [
        (AssetNotFoundError, "asset_not_found", "preflight"),
        (InvalidAssetError, "invalid_asset", "preflight"),
        (InvalidEnvironmentError, "invalid_environment", "preflight"),
        (InvalidInputError, "invalid_input", "preflight"),
        (EngineTimeoutError, "timeout", "execution"),
        (EngineTransportError, "transport", "execution"),
        (EngineUnavailableError, "unavailable", "preflight"),
    ],
)
def test_system_error_has_stable_kind_stage_and_safe_message(
    error_type, kind: str, stage: str
) -> None:
    error = error_type(log_reason="private traceback / token=secret")

    assert isinstance(error, EngineError)
    assert error.kind == kind
    assert error.stage == stage
    assert "secret" not in json.dumps(error.to_dict())
    assert error.log_reason is not None


def test_internal_error_requires_explicit_stage() -> None:
    error = EngineInternalError("execution", log_reason="private traceback")

    assert error.kind == "internal"
    assert error.stage == "execution"
    assert error.to_dict() == {
        "kind": "internal",
        "stage": "execution",
        "message": "执行引擎内部错误",
    }


def test_engine_error_rejects_unknown_kind_or_stage() -> None:
    with pytest.raises(ValueError, match="kind"):
        EngineError("unknown", "preflight", "bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="stage"):
        EngineError("transport", "unknown", "bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="不允许"):
        EngineError("transport", "preflight", "bad")


def test_outcome_json_rejects_infinite_float() -> None:
    with pytest.raises(ValueError, match="无穷"):
        ApiExecutionOutcome(status="success", response={"value": math.inf})


def test_engine_port_is_protocol_for_single_entry_execution() -> None:
    class FakeEngine:
        async def execute(self, request: ApiExecutionRequest) -> ApiExecutionOutcome:
            return ApiExecutionOutcome(status="success", request=request.to_dict())

    engine: EnginePort = FakeEngine()
    assert engine is not None
