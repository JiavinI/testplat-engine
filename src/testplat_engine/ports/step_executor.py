"""内部 StepExecutor 端口与注册表（Engine 内部 seam）。

不从包顶层公开；接口只携带执行上下文与步骤定义，
不携带 FastAPI / ORM / WebSocket 类型。
第一批由 Agent 2 注册 ``api`` 实现；本模块只固定契约。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from testplat_engine.domain.case import CaseStep
from testplat_engine.interface import JsonValue, StepOutcome


@dataclass(frozen=True, slots=True)
class StepExecutionContext:
    """单步执行上下文；只含已解析、可脱敏的执行所需数据。"""

    assets_root: Path
    environment: Mapping[str, JsonValue]
    case_params: Mapping[str, JsonValue]
    aliases: Mapping[str, JsonValue] = field(default_factory=dict)
    timeout_seconds: float = 30.0


class StepExecutor(Protocol):
    """可注册的步骤执行器；第一批仅实现 type=api。"""

    async def execute_step(
        self, step: CaseStep, context: StepExecutionContext
    ) -> StepOutcome:
        """执行单个步骤并返回符合不变量的 StepOutcome。"""
        ...


class UnknownStepTypeError(LookupError):
    """注册表遇到未注册 type 时的确定性错误。"""

    def __init__(self, step_type: str) -> None:
        self.step_type = step_type
        super().__init__(f"未注册的步骤 type: {step_type}")


class StepExecutorRegistry:
    """按 type 字符串分发 StepExecutor；未知 type 抛确定性错误。"""

    def __init__(self) -> None:
        self._executors: dict[str, StepExecutor] = {}

    def register(self, step_type: str, executor: StepExecutor) -> None:
        """注册或覆盖某 type 的执行器。"""
        if not step_type:
            raise ValueError("step_type 不能为空")
        self._executors[step_type] = executor

    def get(self, step_type: str) -> StepExecutor:
        """查找执行器；未注册时抛 ``UnknownStepTypeError``。"""
        try:
            return self._executors[step_type]
        except KeyError as exc:
            raise UnknownStepTypeError(step_type) from exc

    def contains(self, step_type: str) -> bool:
        return step_type in self._executors

    def registered_types(self) -> frozenset[str]:
        return frozenset(self._executors)
