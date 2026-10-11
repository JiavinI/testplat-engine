"""testplat-engine: 通用接口自动化测试平台 - 执行引擎"""

__version__ = "0.1.0"

# 导出核心 API
# 注意: 需要先安装依赖 (pip install pyyaml httpx)
try:
    from testplat_engine.engine.executor import run_api
    __all__ = ["run_api"]
except ImportError:
    # 依赖未安装时不导出
    __all__ = []
