"""``api`` 步骤执行器：复用现有单 API 执行 Module，不重新实现 HTTP/断言/渲染。

本模块只做三件事：把 Case 步骤翻译成 ``ApiExecutionRequest``、委托给单 API 引擎、
把 ``ApiExecutionOutcome`` 翻译成 ``StepOutcome``。

异常边界：timeout / transport / internal 等 ``EngineError`` 原样上抛，不在这里吞成
断言失败；它们由 CaseRunner 统一归属到当前步骤并补齐 partial_outcome。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from testplat_engine.domain.case import CaseStep
from testplat_engine.interface import (
    ApiExecutionOutcome,
    ApiExecutionRequest,
    AuthTable,
    JsonValue,
    StepOutcome,
    _thaw_json,
)
from testplat_engine.ports.step_executor import StepExecutionContext


class ApiRunner(Protocol):
    """单 API 执行 Module 在本执行器视角下的最小能力（便于测试替身）。"""

    async def execute_api(
        self, request: ApiExecutionRequest, *, preserve_param_types: bool = False
    ) -> ApiExecutionOutcome: ...

    def preflight_api(
        self,
        api_id: str,
        assets_root: Path,
        environment: Mapping[str, JsonValue],
        *,
        auth: AuthTable | None = None,
    ) -> None: ...


class ApiStepExecutor:
    """``type=api`` 的 StepExecutor。"""

    def __init__(self, api_runner: ApiRunner) -> None:
        self._api_runner = api_runner

    def preflight_step(self, step: CaseStep, context: StepExecutionContext) -> None:
        """预检 API 资产与环境 host；失败抛 preflight 阶段的公开异常。"""
        self._api_runner.preflight_api(
            step.api_id,
            context.assets_root,
            _thaw_json(context.environment),  # type: ignore[arg-type]
            auth=context.auth,  # H2：预检同样按身份表解析，Case 在执行前一次性发现不可用身份
        )

    async def execute_step(self, step: CaseStep, context: StepExecutionContext) -> StepOutcome:
        """执行一个 API 步骤。

        ``step.params`` 在到达这里时已由 CaseRunner 解析完 Case 层引用，因此
        直接作为 API 参数；``preserve_param_types=True`` 让整字段占位符保持 JSON 类型。
        """
        started = time.perf_counter()
        request = ApiExecutionRequest(
            api_id=step.api_id,
            assets_root=context.assets_root,
            # 请求快照是深冻结的（含 tuple）；单 API DTO 只接受 list/dict，所以先解冻。
            environment=_thaw_json(context.environment),  # type: ignore[arg-type]
            params=dict(step.params),
            timeout_seconds=context.timeout_seconds,
            # H2：步骤请求必须携带身份表，否则凭证 header 不会被注入。
            auth=context.auth,
        )
        outcome = await self._api_runner.execute_api(request, preserve_param_types=True)
        duration = time.perf_counter() - started
        # to_dict() 产出已解冻的纯 JSON；直接传冻结快照会因 tuple 被 DTO 校验拒绝。
        public = outcome.to_dict()
        return StepOutcome(
            step_id=step.step_id,
            type=step.type,
            status=outcome.status,
            duration_seconds=duration,
            name=step.name,
            request=public["request"],  # type: ignore[arg-type]
            response=public["response"],
            error=public["error"],  # type: ignore[arg-type]
            # output 只回显 alias 名，不复制响应值；失败步骤不产出 alias。
            output=step.output if outcome.status == "success" else None,
            auth=public["auth"],  # type: ignore[arg-type]
        )
