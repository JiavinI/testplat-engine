# Filesystem Adapter

文件系统适配器模块,提供从文件系统加载测试资产的功能。

## 模块结构

```
filesystem/
├── __init__.py          # 模块入口
├── yaml_loader.py       # YAML 加载逻辑
├── registry.py          # 资产注册表
└── README.md           # 本文档
```

## 功能特性

- **YAML 资产加载**: 从 YAML 文件加载测试资产定义
- **路径映射**: 自动将资产 ID 映射到文件系统路径
- **数据验证**: 当前字段格式验证和错误提示
- **类型安全**: 加载的数据转换为领域模型,保证类型安全

## 快速开始

### 1. 安装依赖

```bash
pip install pyyaml
```

### 2. 准备资产文件

创建资产目录结构:

```
assets/
└── api/
    └── common/
        └── health_check.yaml
```

编写 API 契约文件 (`health_check.yaml`):

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

### 3. 加载资产

```python
from pathlib import Path
from testplat_engine.adapters.filesystem.registry import Registry

# 初始化资产注册表
registry = Registry(Path("assets"))

# 加载 API 契约
api = registry.load_api("api.common.health_check")

print(f"API: {api.name}")
print(f"方法: {api.method}")
print(f"路径: {api.url}")
print(f"完整 URL: {api.full_url}")
```

输出:

```
API: 健康检查
方法: GET
路径: /health
完整 URL: GET /health
```

## API 参考

### Registry

资产注册表,提供统一的资产加载接口。

#### 初始化

```python
registry = Registry(assets_root: Path)
```

**参数:**
- `assets_root`: 资产根目录路径

#### 方法

##### load_api(api_id: str) -> Api

加载 API 契约。

**参数:**
- `api_id`: API 标识,格式为 `api.{category}.{name}`

**返回:**
- `Api`: API 领域模型实例

**异常:**
- `FileNotFoundError`: API 文件不存在
- `ValueError`: YAML 格式错误或数据不符合要求

**示例:**

```python
api = registry.load_api("api.common.health_check")
```

### yaml_loader

YAML 加载工具函数模块。

#### load_yaml(file_path: Path) -> dict

加载 YAML 文件。

**参数:**
- `file_path`: YAML 文件路径

**返回:**
- `dict`: 解析后的字典数据

**异常:**
- `FileNotFoundError`: 文件不存在
- `ValueError`: YAML 格式错误

#### yaml_to_api(data: dict) -> Api

将 YAML 数据转换为 Api 领域模型。

**参数:**
- `data`: YAML 数据字典

**返回:**
- `Api`: API 领域模型实例

**异常:**
- `ValueError`: 数据格式不符合要求

## 路径映射规则

API ID 到文件路径的映射规则:

| API ID | 文件路径 |
|--------|---------|
| `api.common.health_check` | `assets/api/common/health_check.yaml` |
| `api.user.profile.get` | `assets/api/user/profile/get.yaml` |
| `api.test` | `assets/api/test.yaml` |

映射算法:
1. 将 API ID 按点号分割
2. 将各部分转换为路径分隔符
3. 添加 `.yaml` 扩展名
4. 拼接到资产根目录

## YAML 文件格式

### 必需字段

- `id`: API 唯一标识 (字符串)
- `name`: API 可读名称 (字符串)
- `method`: HTTP 方法 (字符串,支持 GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS)
- `url`: API 相对路径 (字符串)
- `host`: 主机标识 (字符串)

### 可选字段

- `request`: 请求定义 (字典,默认 `{}`)
  - `headers`: 请求头 (字典)
  - `params`: 查询参数 (字典)
  - `body`: 请求体 (任意类型)
- `success`: 成功判定条件 (字典,默认 `{}`)
  - `status_code`: 期望的状态码 (整数)
  - 当前执行器实际支持 `status_code`，以及独立的 `path` + `value` 条件

### 完整示例

参见 `docs/examples/api_contract_example.yaml`

## 错误处理

### FileNotFoundError

当请求的资产文件不存在时抛出:

```python
try:
    api = registry.load_api("api.nonexistent.api")
except FileNotFoundError as e:
    print(f"文件不存在: {e}")
```

### ValueError

当 YAML 格式错误或数据不符合要求时抛出:

```python
try:
    api = registry.load_api("api.invalid.format")
except ValueError as e:
    print(f"数据错误: {e}")
```

可能的错误情况:
- YAML 文件为空
- YAML 格式错误
- 缺少必需字段
- 字段类型不正确
- HTTP 方法无效
- API ID 与文件中的 ID 不匹配

## 测试

运行单元测试:

```bash
pytest tests/adapters/test_yaml_loader.py -v
```

运行逻辑验证:

```bash
python3 tests/adapters/verify_logic.py
```

测试覆盖:
- ✅ 有效 YAML 文件加载
- ✅ 文件不存在错误处理
- ✅ 空文件错误处理
- ✅ 格式错误处理
- ✅ 完整数据转换
- ✅ 默认值处理
- ✅ 缺少字段检测
- ✅ 类型错误检测
- ✅ API ID 与文件 ID 不匹配检测
- ✅ 嵌套路径支持

## 设计原则

1. **单一职责**: 每个模块专注于一个职责
   - `yaml_loader.py`: YAML 文件加载和数据转换
   - `registry.py`: 资产查找和加载流程

2. **依赖倒置**: 依赖领域模型,不依赖具体实现
   - 使用 `Api` 领域模型作为输出
   - 不暴露 YAML 解析细节

3. **错误明确**: 清晰的错误消息和异常类型
   - `FileNotFoundError`: 文件系统错误
   - `ValueError`: 数据格式错误

4. **易于扩展**: 可以轻松添加其他资产类型
   - 在 `Registry` 中添加 `load_xxx()` 方法
   - 在 `yaml_loader.py` 中添加 `yaml_to_xxx()` 函数

## 未来扩展

可能的扩展方向:

- [ ] 支持 JSON 格式资产文件
- [ ] 支持资产文件热重载
- [ ] 支持资产文件缓存
- [ ] 支持资产文件版本管理
- [ ] 支持其他资产类型 (测试用例、环境配置等)
- [ ] 支持资产文件模板和继承

## 贡献指南

添加新的资产类型时:

1. 在 `domain/` 下定义领域模型
2. 在 `yaml_loader.py` 中添加转换函数
3. 在 `registry.py` 中添加加载方法
4. 在 `tests/adapters/` 中添加单元测试
5. 更新本文档

## 许可证

许可证文本尚未确认，当前文档不声明许可证。
