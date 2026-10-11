# Filesystem Adapter 当前基线说明

本文记录当前文件系统适配器的实际范围，作为 Phase 2.5 接入前的来源说明，不是生产就绪声明。

## 实现

- `src/testplat_engine/adapters/filesystem/yaml_loader.py`
  - 使用 `yaml.safe_load` 读取 YAML
  - 检查文件存在、空文档、根节点类型
  - 将必需字段转换为 `Api` 模型
- `src/testplat_engine/adapters/filesystem/registry.py`
  - 将点分 API ID 映射到 YAML 路径
  - 校验文件内 ID 与请求 ID 一致

当前适配器只描述文件读取和模型转换。资产 ID 的安全边界、统一异常类型和 Control 接口属于 Phase 2.5 工作，不在此文档中提前承诺。

## 测试

主要自动化测试位于 `tests/adapters/test_yaml_loader.py`，覆盖有效 YAML、空文件、格式错误、字段错误和 ID 不一致。`tests/adapters/verify_*.py` 是早期的独立验证脚本，不替代对实际实现的 pytest 测试。

## 资产格式

```yaml
id: api.common.health_check
name: 健康检查
method: GET
url: /health
host: test_server
request: {}
success:
  status_code: 200
```

`request` 和 `success` 的字段会保留在 `Api` 模型中；执行器当前只消费已经实现的请求字段和断言条件。更完整的契约示例见 `docs/examples/api_contract_example.yaml`，其中未实现的 DSL 字段不应被视为当前能力。

## 后续边界

Phase 2.5 将由主线程确认并实现：

1. 安全的资产路径约束
2. Control 到 Engine 的单一执行端口
3. 可判别的 preflight / execution 异常
4. 可 JSON 序列化的执行结果

本说明不声明许可证；法律文本需要单独确认。
