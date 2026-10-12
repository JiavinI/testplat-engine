"""Engine 内部端口。

HTTP 客户端端口供插件实现；StepExecutor 供 CaseRunner 注册步骤类型。
二者均不从包顶层 ``testplat_engine`` 导出。
"""

from testplat_engine.ports.http_client import HttpClient, HttpResponse
from testplat_engine.ports.step_executor import (
    PreflightableStepExecutor,
    StepExecutionContext,
    StepExecutor,
    StepExecutorRegistry,
    UnknownStepTypeError,
)

__all__ = [
    "HttpClient",
    "HttpResponse",
    "PreflightableStepExecutor",
    "StepExecutionContext",
    "StepExecutor",
    "StepExecutorRegistry",
    "UnknownStepTypeError",
]
