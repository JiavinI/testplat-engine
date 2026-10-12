"""内部 StepExecutor registry 契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from testplat_engine.domain.case import CaseStep
from testplat_engine.engine.case_context import bind_alias_response
from testplat_engine.interface import StepOutcome
from testplat_engine.ports.step_executor import (
    StepExecutionContext,
    StepExecutorRegistry,
    UnknownStepTypeError,
)
from testplat_engine.shared.redact import redact_json


class _FakeApiExecutor:
    async def execute_step(self, step: CaseStep, context: StepExecutionContext) -> StepOutcome:
        return StepOutcome(
            step_id=step.step_id,
            type=step.type,
            status="success",
            duration_seconds=0.0,
            output=step.output,
        )


def test_registry_unknown_type_is_deterministic() -> None:
    registry = StepExecutorRegistry()
    registry.register("api", _FakeApiExecutor())
    assert registry.contains("api")
    assert registry.registered_types() == frozenset({"api"})

    with pytest.raises(UnknownStepTypeError) as ei:
        registry.get("ui")
    assert ei.value.step_type == "ui"
    assert "ui" in str(ei.value)

    with pytest.raises(UnknownStepTypeError):
        registry.get("redis")


@pytest.mark.asyncio
async def test_registered_executor_receives_context_only() -> None:
    registry = StepExecutorRegistry()
    registry.register("api", _FakeApiExecutor())
    executor = registry.get("api")
    step = CaseStep(step_id="s1", type="api", api_id="api.demo.get")
    ctx = StepExecutionContext(
        assets_root=Path("/assets"),
        environment={"api_hosts": {"local": "http://127.0.0.1"}},
        case_params={"k": "v"},
        aliases={},
        timeout_seconds=5.0,
    )
    outcome = await executor.execute_step(step, ctx)
    assert outcome.step_id == "s1"
    assert outcome.status == "success"


def test_redaction_blocks_secrets_from_alias_binding() -> None:
    """alias 绑定唯一语义：敏感键被移除（而非替换为 ``***``），公开结果脱敏则保留键。"""
    raw = {
        "status_code": 200,
        "headers": {"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
        "body": {
            "id": "1",
            "password": "p@ss",
            "nested": {"access_token": "tok", "ok": True},
        },
    }
    safe = bind_alias_response(raw)
    # 敏感键整体消失：下游引用会得到 variable_resolution，而不是一个看似合法的 "***"。
    assert "Authorization" not in safe["headers"]
    assert safe["headers"]["Content-Type"] == "application/json"
    assert "password" not in safe["body"]
    assert "access_token" not in safe["body"]["nested"]
    assert safe["body"]["nested"]["ok"] is True
    assert safe["body"]["id"] == "1"
    assert "***" not in str(safe)
    assert "secret-token" not in str(safe)
    assert "p@ss" not in str(safe)
    # 公开结果脱敏（redact_json）保留键并替换为占位符，两者语义分明。
    dumped = redact_json(raw)
    assert dumped["headers"]["Authorization"] == "***"
    assert "secret-token" not in str(dumped)
    assert "p@ss" not in str(dumped)
