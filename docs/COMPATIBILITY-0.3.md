# Engine 0.3.0 兼容说明（Phase 3.3a：静态 header 凭证）

日期：2026-10-12
版本：`0.3.0`（开发线；本批不打 tag、不推送）
权威设计：`notes/phase3.3-auth-design-draft.md`（D1–D6、§6.1 Q1–Q7）

## 兼容承诺

1. **默认行为与 `v0.2.0` 一致**：`ApiExecutionRequest.auth` / `CaseExecutionRequest.auth` 默认 `None`。
   资产未声明 `auth`（或 `auth: null`）、或声明 `auth: none` 时，发出的请求与 0.2.0 完全相同；
   `auth=None` 时请求的 `to_dict()` 不新增键。
2. **唯一例外（Q2）**：`auth=None` 而资产声明了具体身份 → `invalid_environment`（preflight），
   不静默匿名执行。
3. **错误 kind 集合不变**：身份不存在、`UnavailableAuth`、未知凭证 kind、无身份表却声明身份，
   一律 `invalid_environment` / `preflight`（D4），`public_message` 只含身份名
   （`UnavailableAuth` 附带 Control 提供的 `public_reason`）。资产 `auth` 类型错误（`""`、列表等）
   为 `invalid_asset`；Case 步骤上写 `auth:` 同样是 `invalid_asset`（未知步骤字段，D3）。
4. **Engine 不读环境变量、不读秘密文件、不登录、不缓存、不修改共享 client**。

## 新增公共表面

- `SecretStr`：`repr`/`str`/`format` 为 `***`；`to_dict`、`json.dumps`、pickle/copy 抛 `TypeError`；
  只能经 `reveal()` 取值，全仓只有 `engine/auth_injection.py` 调用（测试约束）。
- `StaticHeaderAuth(headers: Mapping[str, SecretStr], secret_values: Sequence[SecretStr] = (), kind="static_headers")`
  - `headers`：渲染后的完整 header 值；header 名须为合法 token，同一身份内大小写重复即拒绝。
  - `secret_values`：渲染前的原始变量值（Q5），每个 ≥ 8；为空时 header 值本身视为原始秘密并要求 ≥ 8。
  - 按值替换集合 = `secret_values` ∪ 长度 ≥ 8 的完整 header 值，按长度降序替换。
- `UnavailableAuth(public_reason: str, kind="unavailable")`：`public_reason` 由 Control 保证只含变量名。
- `AuthMaterial = StaticHeaderAuth | UnavailableAuth`（判别联合，按 `kind`）。
- `AuthTable(identities: Mapping[str, AuthMaterial], default_identity: str | None = None)`：
  `none` 为保留身份名；`default_identity` 必须存在于 `identities`。
- `ApiExecutionRequest.auth`、`CaseExecutionRequest.auth`：`AuthTable | None = None`；
  `to_dict()` 仅在非 None 时输出 `auth`，内容只有身份名、kind、header 名、`default_identity`。
- `ApiExecutionOutcome.auth` 与 `StepOutcome.auth`（Q1）：`{identity, header_names, overridden_headers}` 或 `null`；
  `to_dict()` 总是输出该键。skipped 步骤恒为 `null`；系统错误归属的失败步骤为 `null`。
- API 资产新增可选字段 `auth: <身份名> | none`。
- `testplat_engine.__version__ == "0.3.0"`。

## 身份解析与注入

1. `auth: none` → 匿名（即使有 `default_identity`）。
2. `auth: <名>` → 查表；无表或无此名 → 422。
3. 未声明 → `default_identity`，再无则匿名。
4. Case 在执行任何步骤前对全部步骤一次性完成上述检查。
5. 注入发生在每次 `client.request(headers=...)`：资产 header 与凭证 header 按不区分大小写判定冲突，
   凭证优先、只发一份；`overridden_headers` 记录资产里的原始写法（Q3）。

## 公开面脱敏

- 注入的 header 名一律按敏感处理（值为 `***`），不论是否在 `_SENSITIVE_KEYS` 中；
  执行器不再维护独立的头名单，统一使用 `shared.redact._SENSITIVE_KEYS`。
- 本次执行的全部秘密值在公开请求、公开响应（含响应头）、错误、`partial_outcome`、alias 绑定值中
  按子串替换为 `***`（在按键脱敏之后执行）；alias 中键保留（Q4，与按键移除的 `strip_sensitive_json` 不同）。
- 已分类异常的 `public_message` / `log_reason` 也做按值替换兜底；日志只记 kind、stage、ids、异常类型名。

## 3.3b 预留的 kind 扩展规则

- 新凭证类型以新的 `kind` 字符串加入 `AuthMaterial` 联合（例如 `login_token`、`login_cookie`）。
- `AuthTable` 接受任何带字符串 `kind` 的材料；旧 Engine 遇到不认识的 kind 在**被用到时**报
  `invalid_environment`（preflight），绝不降级为匿名。
- 新 kind 产生的运行期秘密（例如登录换来的 token）必须加入同一按值替换集合。

## Control 依赖提示

- 约束改为 `testplat-engine>=0.3.0,<0.4`（D6）。
- Control 构造 `StaticHeaderAuth` 时应同时传 `secret_values`（原始变量值），否则只回显裸 token 的目标无法被按值替换覆盖。
- 构造错误（`TypeError`/`ValueError`）消息只含字段名/身份名/header 名，不含值（M1），但 Control 仍应避免把 `str(exc)` 直接作为 422 detail。
