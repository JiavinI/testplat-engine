"""create_engine 公开工厂测试。"""

from __future__ import annotations

from testplat_engine import SingleApiEngine, create_engine


def test_create_engine_returns_single_api_engine() -> None:
    """工厂返回可执行的 SingleApiEngine 实例。"""
    engine = create_engine()
    assert isinstance(engine, SingleApiEngine)
    assert callable(engine.execute)
