# testplat-engine

`testplat-engine` 是一个 Python API 资产执行库的当前来源基线。它负责读取 YAML API 资产、解析变量、发送 HTTP 请求，并根据有限的状态码或 JSON 路径条件返回结果。

## 当前范围

- 文件系统 API 资产加载：`api.common.health_check` 映射到 `api/common/health_check.yaml`
- `${name}` 形式的递归变量替换
- 基于 `httpx.AsyncClient` 的异步 HTTP 请求
- `status_code` 和简单 `$.field.nested` 条件检查
- `ApiResult` 的 `success` / `failed` 结果模型

当前基线不提供 Case 编排、重试策略、认证管理、报告、队列、WebSocket 或 UI 集成。异常分类、Control 端口和 JSON 边界将在 Phase 2.5 中定义和实现。

## 安装

```bash
uv pip install -e ".[dev]"
```

运行时依赖只有 `httpx` 和 `pyyaml`；`pydantic`、`playwright`、`jinja2` 尚未被当前实现使用。

## 使用

```python
import asyncio
from pathlib import Path

from testplat_engine import run_api
from testplat_engine.adapters.filesystem import Registry


async def main() -> None:
    registry = Registry(assets_root=Path("assets"))
    result = await run_api(
        api_id="api.common.health_check",
        registry=registry,
        env="test",
        params={},
    )
    print(result.status)


asyncio.run(main())
```

真实调用需要调用方提供资产目录和环境配置。仓库中的测试与示例使用本地 HTTP 服务，不依赖公网或相邻目录。

## 开发检查

```bash
uv run pytest -q
uv run ruff check .
uv build
```

## 目录

```text
src/testplat_engine/
├── adapters/       # 文件系统资产加载
├── domain/         # API 与结果模型
├── engine/         # 变量解析、断言和当前执行入口
├── plugins/        # HTTP 客户端实现
└── ports/          # HTTP 客户端抽象
```

本仓库尚未确认许可证文本，因此不在此声明许可证。
