# Engine 0.2.0 兼容说明

日期：2026-10-12  
版本：`0.2.0`（开发线；本批不打 tag、不推送）

## 兼容承诺

1. **唯一执行入口不变**：`EnginePort.execute(request)` 仍是跨仓唯一 seam。
2. **单 API 行为兼容**：`ApiExecutionRequest` / `ApiExecutionOutcome` 字段与校验语义保持 0.1 行为；现有单 API 调用与测试继续通过。
3. **公开异常族扩展而非替换**：保留 `asset_not_found` / `invalid_asset` / `invalid_environment` / `timeout` / `transport` / `unavailable` / `internal`；新增：
   - `invalid_input`（stage=`preflight`）：Case 参数在执行前可判定缺失等输入错误；Control 后续映射 HTTP 422。
4. **execution-stage 异常可携带 `partial_outcome`**：仅 Case 执行中途的 timeout / transport / internal；preflight 异常不得伪造执行结果。`to_dict()` 在存在时包含安全可序列化的 `partial_outcome`。
5. **`create_engine()` / `SingleApiEngine`**：`execute(request)` 现按请求类型判别——`ApiExecutionRequest` 走 0.1 单 API 路径（字符串插值语义不变），`CaseExecutionRequest` 走串行 `CaseRunner`（`api` 步骤复用同一单 API Module，整字段占位符保持 JSON 类型）。仍是唯一公开入口。
6. **`CaseExecutionRequest.timeout_seconds` 是每步骤 HTTP 超时，不是 Case 总预算**：该值（有限正数，默认 30 秒）原样传给 Case 中**每个** `api` 步骤的 HTTP 调用，各步骤独立计时。N 个步骤的 Case 最坏耗时可达 `N × timeout_seconds` 量级；Engine 不提供 Case 级总超时，需要整体限时的调用方（Control）应在外层自行施加。任一步骤超时按 `timeout`（stage=`execution`）上抛，并携带 `partial_outcome`。
7. **日志不含系统异常原文**：Engine 日志只记录 kind、stage、case id、step id 与异常类型名；`log_reason` 与 traceback 不写入日志、公开结果或持久化记录（`log_reason` 仅保留在异常对象上供调用方决策）。

## 新增公共表面

- DTO：`CaseExecutionRequest`、`CaseExecutionOutcome`、`StepOutcome`
- 类型别名：`ExecutionRequest`、`ExecutionOutcome`
- 异常：`InvalidInputError`
- 版本：`testplat_engine.__version__ == "0.2.0"`

## 不在 0.2 公共表面

- `StepExecutor` / `StepExecutorRegistry`：Engine **内部**端口，从 `testplat_engine.ports.step_executor` 使用，不从包顶层导出。
- Case 领域模型 / loader：`testplat_engine.domain.case`、`testplat_engine.adapters.filesystem.case_loader`；供 Engine 内部与后续 CaseRunner 使用。
- 不引入 `fail_fast` 请求字段：第一批固定 fail-fast=on，调用方不可关闭。
- 不引入 `metadata`、旧键别名（`kind`/`use`/`with`/`->`）或新的 assert DSL。

## Control 依赖提示

- Control 若锁定 `testplat-engine>=0.1.0,<0.2`，接入 Case 前需放宽到 `0.2.x` 并消费本说明中的 DTO/异常。
- 旧 Control `parse_case_yaml` 不是执行权威；结构化 Case 详情应消费 Engine 规范化模型/结果。
