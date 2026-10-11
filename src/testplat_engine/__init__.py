"""testplat-engine: 通用接口自动化测试平台 - 执行引擎"""

__version__ = "0.1.0"

from testplat_engine.engine.executor import SingleApiEngine, create_engine, run_api
from testplat_engine.interface import (
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
)

# 保留旧入口供基线示例使用；新 Control 集成依赖 EnginePort / create_engine / SingleApiEngine。

__all__ = [
    "ApiExecutionOutcome",
    "ApiExecutionRequest",
    "AssetNotFoundError",
    "EngineError",
    "EngineInternalError",
    "EnginePort",
    "EngineTimeoutError",
    "EngineTransportError",
    "EngineUnavailableError",
    "InvalidAssetError",
    "InvalidEnvironmentError",
    "SingleApiEngine",
    "create_engine",
    "run_api",
]
