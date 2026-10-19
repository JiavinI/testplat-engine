# 0.5.2：结构化 API 与调用身份

0.5.2 通过 Git 精确提交集成交付，不打标签或发布包索引。原模板资产行为保持兼容。

API 可显式声明 `request.template: structured`，完整 `${x}` 保留 JSON 类型；`${x!}` 在缺失或 null 时返回 invalid_input/preflight，不发登录或业务请求；false、0、空串不等同缺失。`${x|默认}` 的默认值为字符串，嵌套对象使用点分路径，缺失可选字段省略。支持源 GSB 契约使用的 now()（UTC ISO 秒）和 date_add_days(path)（本地日历整数天），不执行任意代码。request.inputs 的 default 是低优先级默认值，运行参数覆盖它。

`ApiExecutionRequest.identity` 和 Case API 步骤的 `identity` 为调用身份，显式 none 表示匿名。优先级：调用身份 > 旧 API auth > AuthTable.default_identity > 匿名。旧资产 auth 继续加载，迁入契约不使用。Case 仍为 schema 0.2，仅增加可选字段，旧步骤序列化不变。

API 顶层 `projects: [go2]` 表示项目专属，执行环境须提供 business_project。公共 API 不声明 projects。加载与项目/身份预检均不触发 HTTP；当前不根据目录或账号名猜项目。完整请求覆盖跳过模板 body，但保留 Header 和成功条件渲染及项目/身份预检。

Control 配套通过运行请求的 identity 透传，且启动期拒绝缺少新契约的 Engine。配套 Control 必须精确锁定包含本扩展的提交；scripts/dev.sh 仅用于显式本地开发。checks DSL、自动重试、场景数据与真实业务验收不属于此扩展。
