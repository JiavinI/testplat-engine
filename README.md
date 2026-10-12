# testplat-engine

`testplat-engine` 是一个 Python API 资产执行库。它负责读取 YAML API 资产、解析变量、发送 HTTP 请求，并根据有限的状态码或 JSON 路径条件返回结果。

## 当前范围

- 文件系统 API 资产加载：`api.common.health_check` 映射到 `api/common/health_check.yaml`
- `${name}` 形式的递归变量替换
- 基于 `httpx.AsyncClient` 的异步 HTTP 请求
- `status_code` 和简单 `$.field.nested` 条件检查
- Phase 2.5 公共 `EnginePort.execute(request)` 协议、JSON DTO 和稳定系统异常
- 真实实现：`SingleApiEngine`（消费 Control 已解析的 `environment`，不再猜测 `config_root`）

0.2 冻结 Case 公共 DTO、资产 schema 与校验契约；串行 CaseRunner 与 `api` StepExecutor 已接入同一 `execute` seam：`execute(CaseExecutionRequest)` 按声明顺序串行执行，默认 fail-fast，业务失败返回结果，timeout/transport/internal 抛异常并携带安全 `partial_outcome`。
仍不提供重试策略、认证管理、报告、队列、WebSocket 或 UI 集成。兼容说明见 `docs/COMPATIBILITY-0.2.md`。

## 安装

```bash
uv pip install -e ".[dev]"
```

运行时依赖只有 `httpx` 和 `pyyaml`。

## 使用（EnginePort）

```python
import asyncio
from pathlib import Path

from testplat_engine import ApiExecutionRequest, create_engine


async def main() -> None:
    engine = create_engine()  # 等价于 SingleApiEngine()
    outcome = await engine.execute(
        ApiExecutionRequest(
            api_id="api.common.health_check",
            assets_root=Path("assets"),
            # Control 负责从 config_root 解析环境后传入；Engine 只消费 api_hosts。
            environment={"api_hosts": {"test_server": "http://127.0.0.1:8080"}},
            params={},
            timeout_seconds=30.0,
        )
    )
    print(outcome.status)


asyncio.run(main())
```

真实调用需要调用方提供资产目录和**已解析**的环境配置。仓库中的测试与示例使用本地 HTTP 服务，不依赖公网或相邻目录。

Case 通过同一入口执行（`CaseExecutionRequest`）。注意其 `timeout_seconds` 是**每个步骤**的 HTTP 超时，不是整个 Case 的总时长预算；Case 级限时需由调用方在外层施加（详见 `docs/COMPATIBILITY-0.2.md`）。

旧入口 `run_api(api_id, registry, env, params)` 仍可用于本地示例，但会自行查找 env 文件；新 Control 集成应只依赖官方 `create_engine()`（实现 `EnginePort`），不要再猜测其它入口。

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
├── engine/         # 变量解析、断言和 SingleApiEngine
├── interface.py    # 公共 EnginePort / API+Case DTO / 异常族
├── plugins/        # HTTP 客户端实现
└── ports/          # HTTP 客户端抽象
```

本仓库尚未确认许可证文本，因此不在此声明许可证。


## Phase 2.5 集成说明

- 公开运行时入口：`create_engine()` → `SingleApiEngine`（实现 `EnginePort`）
- Control 通过已安装的 Python 包解析该入口；不要修改 `sys.path`
- 本地联调：Control 可用 uv path editable 指向本 worktree（相对路径）
- 正式分发锁定为 git commit / tag 后，由 Control `pyproject.toml` 声明；本阶段未推送远程时以本地 editable 验收
