"""create_engine 公开工厂契约测试。

Control 只依赖官方 ``create_engine()`` 入口；本文件锁定该契约。
"""

from __future__ import annotations

import inspect

import testplat_engine as eng
from testplat_engine import SingleApiEngine, create_engine


def test_create_engine_returns_single_api_engine() -> None:
    """工厂返回可执行的 SingleApiEngine 实例。"""
    engine = create_engine()
    assert isinstance(engine, SingleApiEngine)
    assert callable(engine.execute)


def test_create_engine_is_public_package_export() -> None:
    """包根必须导出 create_engine，且与 engine 模块工厂为同一入口。"""
    assert hasattr(eng, "create_engine")
    assert eng.create_engine is create_engine
    engine = eng.create_engine()
    assert inspect.iscoroutinefunction(engine.execute)
