"""测试执行引擎

提供 API 测试执行的核心逻辑,协调各个模块完成测试执行。
"""

from typing import Any

from testplat_engine.adapters.filesystem.registry import Registry
from testplat_engine.domain.result import ApiResult
from testplat_engine.engine.assert_engine import check_success
from testplat_engine.engine.resolver import resolve_variables
from testplat_engine.plugins.httpx_client import HttpxClient


async def run_api(
    api_id: str,
    registry: Registry,
    env: str,
    params: dict[str, Any] | None = None
) -> ApiResult:
    """执行单个 API 测试

    这是 testplat-engine 的核心入口函数,负责协调各个模块完成 API 测试执行。

    执行流程:
    1. 从 Registry 加载 API 契约
    2. 解析环境配置,获取完整 URL
    3. 使用 params 渲染请求参数
    4. 发送 HTTP 请求
    5. 验证响应是否符合成功条件
    6. 返回执行结果

    Args:
        api_id: API 标识,如 "api.common.health_check"
        registry: 资产注册表,用于加载 API 契约和配置
        env: 环境名称,如 "test", "staging"
        params: 请求参数字典,用于变量渲染

    Returns:
        ApiResult: 执行结果,包含状态、响应和可选的错误信息

    Raises:
        FileNotFoundError: API 文件或配置文件不存在
        ValueError: 配置格式错误或必需参数缺失

    Examples:
        >>> registry = Registry(Path("test_assets"))
        >>> result = await run_api(
        ...     api_id="api.common.health_check",
        ...     registry=registry,
        ...     env="test"
        ... )
        >>> result.is_success
        True
    """
    client = None

    try:
        # 1. 加载 API 契约
        api = registry.load_api(api_id)

        # 2. 加载环境配置
        env_config = _load_env_config(registry, env)

        # 3. 构建完整 URL
        base_url = env_config["api_hosts"][api.host]
        full_url = f"{base_url}{api.url}"

        # 4. 渲染变量 (使用 params)
        resolved = resolve_variables(api, params or {})

        # 5. 发送 HTTP 请求
        client = HttpxClient()
        response = await client.request(
            method=resolved["method"],
            url=full_url,
            json=resolved.get("body"),
            headers=resolved.get("headers")
        )

        # 6. 验证响应（使用已渲染的 success 条件）
        success = check_success(response, resolved.get("success", api.success))

        if success:
            return ApiResult.success(response)
        else:
            return ApiResult.failed(
                error="断言失败: 响应不符合成功条件",
                response=response
            )

    except Exception as e:
        return ApiResult.failed(
            error=f"执行异常: {type(e).__name__}: {e}",
            response=None
        )

    finally:
        # 7. 清理资源
        if client:
            await client.close()


def _load_env_config(registry: Registry, env: str) -> dict[str, Any]:
    """加载环境配置

    Args:
        registry: 资产注册表
        env: 环境名称

    Returns:
        环境配置字典

    Raises:
        FileNotFoundError: 配置文件不存在
    """
    import yaml

    config_path = registry.assets_root.parent / "config" / "envs" / f"{env}.yaml"

    # 如果配置在 assets_root 内部
    if not config_path.exists():
        config_path = registry.assets_root / "config" / "envs" / f"{env}.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"环境配置文件不存在: {config_path}")

    with open(config_path, encoding='utf-8') as f:
        return yaml.safe_load(f)
