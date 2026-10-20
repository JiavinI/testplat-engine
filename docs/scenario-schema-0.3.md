# 场景 schema 0.3（Engine 0.6.0）

这是资产契约版本，由 Engine 0.6.0 提供。Case 0.2 继续走原加载器与运行器，旧结果不增加 trace/result_version 字段。Control 应精确锁定提供此版本的远程提交，并在启动检查公开 describe_case 能力。

`case.demo.repeat` 对应 `scenarios/demo/repeat.case.yaml`，只允许 case 独立运行。case 字段为 schema_version/id/name?/params?/steps；flow 字段为 schema_version/id/name?/inputs/outputs/steps，不含业务默认值。

```yaml
schema_version: '0.3'
id: case.demo.repeat
params:
  common: {enabled: true}
  private: {first: {quantity: 1}, second: {quantity: 2}}
steps:
  - step_id: first
    type: flow
    flow_id: flow.demo.echo
    params: {value: '${params.private.first}'}
    output: first
  - step_id: second
    type: flow
    flow_id: flow.demo.echo
    params: {value: '${params.private.second}'}
    output: second
```

```yaml
schema_version: '0.3'
id: flow.demo.echo
inputs:
  value: {type: object}
outputs: {value: '${echo.body.value}'}
steps:
  - step_id: echo
    type: api
    api_id: api.demo.echo
    params: {value: '${inputs.value}'}
    output: echo
```

flow 路径为 `scenarios/demo/echo.flow.yaml`。inputs 所有项必填，绑定键必须完全匹配，支持 string/integer/number/boolean/object/array/null 与 nullable 布尔选项；outputs 必须非空。API 使用 api_id，可选 identity；flow 使用 flow_id，不接受 flow identity。

另支持 `type: map`：必须指定 `flow_id`、单一变量引用 `source` 和与 flow.inputs 完全匹配的 `params`。source 必须在执行前解析为非空数组；Engine 按数组顺序串行调用 flow，每次绑定局部 `${item}` 与从 0 开始的整数 `${index}`。map 不接受 identity、并发或自动重试；单项失败会停止并跳过剩余项。map output 为每次成功 flow 输出的有序数组。描述和运行 trace 都为每个迭代保留父节点、索引与嵌套步骤。

case 只读取 params 与此前步骤的 alias；flow 只读取 inputs 与本次调用此前产生的 alias。整字段引用保留 JSON 类型，嵌入字符串只允许标量，路径仅支持点分对象键。common/private 是普通命名空间；单次参数按顶层键覆盖，覆盖 private 时需提供完整 private。业务参数及绑定不允许敏感字段，凭据通过既有身份表提供。

运行前递归校验资产、Host/身份配置、输入绑定、静态引用和循环（最大 16 层）；map source 已知时逐项预检 flow 输入，source 来自前序响应时预检静态依赖，并在 map 首个子请求前校验真实数组及全部项的输入类型。流程仅导出声明 outputs，API alias 使用既有安全响应结构；各次调用 inputs/alias 独立，HTTP、登录、会话池和断言复用现有 API 执行器。

公开 `describe_case(case_id, assets_root)` 返回验证后的递归定义，不发请求；旧 0.2 返回 None。Control 使用这一入口展示引用，不复制 DSL 解析。

新版运行结果顶层为 `result_version: '0.3'`，每步可带 `trace`：node_id、parent_id、asset_id、inputs、outputs、bindings、children。bindings 保存目标路径、引用、来源节点、脱敏值与 JSON 类型；children 为递归步骤结果。汇总统计本层声明步骤。flow 不伪造 HTTP 请求，导出失败归属调用节点；登录失败、业务失败和系统异常停止后续步骤。系统异常通过既有异常的 partial_outcome 保留嵌套结果，登录记录仍只在根结果提供。

复现：`uv run pytest -q tests/engine/test_scenario.py`。配套 Assets 的 `examples/scenario-parameters` 和 Control `tests/integration/test_scenario_flow.py` 验证集中参数、重复调用、输出与历史；全部使用假目标。map 是有界的数组到 flow 编排原语；金额、提取、断言及列表操作见下文。配套 Control 展示和 GO2 资产已通过隔离假服务验收，真实业务验收与跨平台执行另行进行。

## 受限提取与精准匹配

`extract` 是无网络调用的局部步骤，必须显式声明 source、selector、cardinality、value_type 和 output。以下例子从安全 API alias 选择恰好一个商品，随后可用 `${product}` 或 `${product.price}` 传递：

```yaml
- step_id: choose_product
  type: extract
  source: '${search.body}'
  selector:
    path: [data, products]
    where:
      - path: [code]
        equals: '${inputs.code}'
  cardinality: one
  value_type: object
  output: product
```

source 必须是单一完整变量引用，作用域沿用 case/flow 局部规则。path 是结构化列表，字符串为对象键，非负整数为数组下标；空列表选择来源本身。禁止 JSONPath、通配符、递归、负数下标、路径插值及任意表达式。where 是可选的非空条件列表，多个条件按 AND 组合；仅允许 path 和 equals，条件值可绑定局部变量，不拼接为选择器代码。对象与数组递归比较，数字按数值比较，字符串不转数字，布尔与数字严格区分。

有 where 时，path 必须指向数组；缺少条件路径的项不匹配（不同于显式 null）。one 必须恰好命中一项，零条或重复均失败；many 返回全部匹配项并保持顺序，允许空数组。无 where 时 one 选择路径自身（可为数组或 null），many 要求路径为数组。value_type 校验 one 的结果或 many 的每个元素；nullable 默认为 false，缺失路径始终失败，显式 null 仅在 nullable=true 或 value_type=null 时有效。0、false、空数组均保留原值。

已知数据在首个请求前预检；响应依赖在来源成功后执行。提取成功才发布 output；失败节点保留 trace.extracts 中的选择器、命中数（路径失败时为 null）和状态，下游 skipped。trace.bindings 指向来源节点与条件参数，成功值经现有敏感字段移除/脱敏规则处理。提取不保存原始响应或凭据。map 动态数组产生后会在首个迭代请求前预检全部项，包括子流程的已知提取。

配套 Control 可展示详情/运行/历史的提取字段。提取、Decimal、断言和列表操作共同支持 GO2 多商品待支付资产的假服务闭环；不代表真实业务状态已验收。

## Decimal 金额运算

`decimal` 是无网络调用的局部步骤，沿用 case/flow 输入、alias 作用域和成功后发布规则：

```yaml
- step_id: line_amount
  type: decimal
  operation: multiply
  operands: ['${inputs.price}', '${inputs.quantity}']
  scale: 2
  rounding: HALF_UP
  output: amount
```

operation 仅支持 add、multiply、sum。operands 为数组或单一完整变量引用（必须解析为数组）；add/multiply 至少两项，sum 可为空并返回零。sum 可声明 `path: [amount]`，读取每项的 amount 后求和，适用于 `${lines}` 等 map 聚合输出；path 只允许静态对象键与非负数组下标，其他操作不接受 path。不支持 eval、除法、隐式转换或嵌套运算语法，组合通过多个具名步骤表达。

scale 必填，范围 0..8；rounding 必填，支持 HALF_UP（半值远离零）、HALF_EVEN（半值取偶）、DOWN（向零截断）。输入及中间计算不量化，步骤结束量化一次；输出固定小数位字符串，例如 `"1.00"`，下游直接用 `${amount}` 引用，flow 输入契约应声明 string。两位 HALF_UP 与旧 money_add/money_mul 的舍入点一致，但未迁移旧 YAML。资产必须明确选择逐行舍入还是汇总后舍入。

金额输入接受无空白的十进制字符串（不接受指数、千分位、NaN/Infinity）或有限 JSON 数字，数字按 Decimal(str(value)) 转换。拒绝 bool/null、对象/数组、缺失路径；允许负数，负零统一输出正零。float 输入不能恢复其到达 Engine 之前丢失的精度，高精度金额用字符串传入。最多 1024 个操作数；每个数及每步中间结果最多 64 位系数、小数指数不低于 -32、最高有效位指数不超过 32；超限直接失败。独立 Decimal 上下文保证环境精度设置不改变结果。

预检与 extract 一致：已知值零请求验证，动态值在对应步骤执行时验证；动态 map 首个子请求前预检所有项。trace.calculations 保存 operation/path/scale/rounding/target/status 及成功 value；trace.inputs/bindings 保存脱敏操作数及来源。失败不发布 output，后续 skipped。正整数数量校验与场景断言见下文，配套 Control 展示计算证据。GO2 完整场景验证使用假服务，真实金额和订单状态仍须业务验收。

## 受限场景断言

`assert` 是无网络步骤，支持 `non_empty`、`positive_integer`、`equal` 和 `decimal_equal`。actual 必须是单一完整引用；equal 与 decimal_equal 必须显式 expected。equal 要求 JSON 类型和值都相同；decimal_equal 使用 Decimal 比较。non_empty 拒绝 null、空字符串、空数组和空对象；positive_integer 只接受大于零的 JSON 整数。断言不产生 output alias。

断言失败生成 `assertion_failed`，trace.assertions 保存操作符、脱敏 actual/expected 和 status；后续步骤标记 skipped。断言结构与根作用域在加载时验证，实际判定在运行到节点时发生。若需在首个商品请求前校验每项数量，应先用 positive_integer_sum 预检全数组，不能只在 map 子流程中检查当前项。任意表达式、范围、正则、列表长度及其他业务断言留给后续扩展。

## 有序列表投影与数量汇总

`transform` 为无网络步骤，必须声明 operation、单一完整引用 source、结构化 path 及 output；name 可选。operation 仅允许 project、positive_integer_sum，不接受其他 API/身份/表达式字段。

```yaml
- step_id: order_lines
  type: transform
  operation: project
  source: '${resolved_items}'
  path: [order_item]
  output: order_lines
```

project 对来源数组每项读取同一路径，保留顺序及 JSON 类型，允许空数组；任一项缺路径或容器类型不符即失败。positive_integer_sum 沿 path 读取数量，要求非空数组且每项为 JSON 正整数（不转换字符串、浮点或布尔），总和以 JSON 整数输出，上限 2^53−1。来源最多 1024 项，path 仅允许对象键/非负数组下标，空 path 读取每项自身。

已知来源在首个请求前预检，动态来源在节点执行时验证；成功才发布 output。trace.transforms 记录 operation/path/target/status 和成功值；bindings 记录 source 来源。Assets 的 `examples/go2-pending` 用现有 API 契约与内存假目标验证该能力。资产启用须在 Control 精确锁定新版 Engine 并完成干净安装验证之后；启用不代表执行过真实建单或支付。
