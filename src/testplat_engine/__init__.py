"""testplat-engine: 通用接口自动化测试平台 - 执行引擎"""

__version__ = "0.2.0"

from testplat_engine.engine.executor import SingleApiEngine, create_engine, run_api
from testplat_engine.interface import (
    ApiExecutionOutcome,
    ApiExecutionRequest,
    AssetNotFoundError,
    CaseExecutionOutcome,
    CaseExecutionRequest,
    EngineError,
    EngineInternalError,
    EnginePort,
    EngineTimeoutError,
    EngineTransportError,
    EngineUnavailableError,
    ExecutionOutcome,
    ExecutionRequest,
    InvalidAssetError,
    InvalidEnvironmentError,
    InvalidInputError,
    StepOutcome,
)

# 保留旧入口供基线示例使用；新 Control 集成依赖 EnginePort / create_engine / SingleApiEngine。
# StepExecutor / Case 领域模型为内部或子模块导出，不从包顶层公开，避免扩大跨仓表面。

__all__ = [
    "ApiExecutionOutcome",
    "ApiExecutionRequest",
    "AssetNotFoundError",
    "CaseExecutionOutcome",
    "CaseExecutionRequest",
    "EngineError",
    "EngineInternalError",
    "EnginePort",
    "EngineTimeoutError",
    "EngineTransportError",
    "EngineUnavailableError",
    "ExecutionOutcome",
    "ExecutionRequest",
    "InvalidAssetError",
    "InvalidEnvironmentError",
    "InvalidInputError",
    "SingleApiEngine",
    "StepOutcome",
    "create_engine",
    "run_api",
    "__version__",
]
