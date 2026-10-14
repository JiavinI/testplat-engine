# Engine 0.4.0 兼容说明（Phase 3.3b：登录鉴权）

日期：2026-10-13
版本：`0.4.0`（开发线；本批不打 tag、不推送）
权威设计：`notes/phase3.3b-login-auth-design-draft.md`（D7–D15、E1–E4、§12 Q1–Q8 / H1–H5）
Control 约束建议：`testplat-engine>=0.4.0,<0.5`（D14）。

## 兼容承诺

1. **无 login 身份时行为与 0.3.0 一致**：匿名与静态 header 身份发出的请求、断言、错误 kind、
   HTTP 映射均不变；唯一可见差异是下面列出的公开结果新增键。
2. **请求体默认 `json`**：未声明 `request.type` 的资产仍以 `json=body` 发送，调用参数与 0.3.0
   逐字一致（M4）；公开请求快照新增 `body_type: "json"`。
3. **Engine 仍不读环境变量 / 秘密文件，不做跨执行缓存，不做 401 重登，不做登录步骤 retry，
   不支持 multipart，不新增核心运行时依赖**（仍只有 httpx、pyyaml），不修改共享 client 状态。

## 公开结果变更（契约扩展）

| 位置 | 0.3.0 | 0.4.0 |
|---|---|---|
| `auth` | `{identity, header_names, overridden_headers}` | `{identity, kind, tier, header_names, overridden_headers, cookie_names, login_ref}`；静态 header 身份 `kind="static_headers"`、`tier=null`、`cookie_names=[]`、`login_ref=null` |
| `ApiExecutionOutcome.to_dict()` / `CaseExecutionOutcome.to_dict()` | — | 新增 `logins: [...]`（无登录时为 `[]`） |
| 步骤 / 单 API `error.kind` | assertion / variable_resolution / 系统错误 | 新增 `login_failed`（业务失败，HTTP 200，保存） |
| `skip_reason` | 只有 `fail_fast` | 枚举 `fail_fast` \| `login_failed`（H4） |
| 公开请求快照 | `{method, url, headers, body}` | 新增 `body_type`；`query` 类型另有 `query` 对象，`url` 不含这些参数、`body=null`（Q8） |

### `logins[]` 形状

```json
{
  "identity": "buyer", "strategy": "generic_flow", "tier": "generic",
  "status": "success | failed", "duration_seconds": 0.01,
  "steps": [{"name": "login", "request": {...}, "response": {...} | null}],
  "probe": {"name": "probe", "request": {...}, "response": {...}} | null,
  "error": null | {"reason": "...", "failed_step": "login" | "probe" | null}
}
```

步骤 `auth.login_ref` 是本步骤所用那次登录在 `logins` 中的下标；同一身份失效重登会追加新记录。

### `login_failed` 错误

```json
{"kind": "login_failed", "message": "登录失败：身份 buyer",
 "login": {"identity": "buyer", "strategy": "generic_flow", "failed_step": "login", "reason": "assertion"}}
```

`reason ∈ assertion | extract_missing | token_too_short | probe_failed | timeout | transport | strategy_error`。
登录阶段的超时 / 连接失败也是 `login_failed`（E2/H3），**不**映射为 504/502；业务请求的超时 /
连接失败仍是 Phase 3 系统错误。Case 中：触发登录的步骤 `failed`，其后全部
`skipped(skip_reason=login_failed)`；单 API：`status=failed`、`request/response=null`、同形 `error`。

## 预检（invalid_environment，422，不保存，不发任何请求）

被执行到的 login 身份：策略未注册、`strategy.validate()` 抛错、登录 host 不在 `api_hosts`、
inject / probe / 步骤模板引用的 `${login:名字}` 无前序来源。Case 在任何步骤执行前一次性检查。
消息只含身份名、策略名、host 名。

## 资产 DSL

- `request.type ∈ json | form | query | raw`；`multipart` 与未知值 → `invalid_asset`。
- `form` / `query` 的 body 必须是一层标量键值；`raw` 的 body 是字符串，且必须有 `request.content_type`
  或显式 `Content-Type` 头（Q4）；显式 `Content-Type` 永远优先。
- 资产任何位置出现 `${secret:`、`${login:`、`${login_host}` → `invalid_asset`（Q5）。

## 新增公共表面（`testplat_engine` 顶层导出）

- `LoginAuth(strategy, host, tier="generic", flow=None, params=None, secret_values=(), validity=None,
  session_cookie=False, probe=None, inject_headers=None, kind="login")`
  - Q1/Q2：至少一个 inject 值引用 `${login:}`，或 `session_cookie=True`；inject 值可为字面量、
    `${login:名字}`、SecretStr 任意组合；所有注入头在公开面一律按名遮蔽。
- `LoginFlow(steps)`、`LoginStep(name, method, path, headers, body, success, extract)`、
  `LoginBody(type, fields | content, content_type)`、`ExtractRule(source, as_name, path|name|format, optional)`、
  `ProbeSpec(method, path, headers, success)`、`Validity(ttl_seconds, expires_from)`、
  `ExpiresFrom(path, unit=seconds|epoch_ms|epoch_s, source=body_json, step=None)`。
- `LoginStrategy` 协议、`LoginContext`、`LoginResult(values, expires_at)`。
- `AuthMaterial = StaticHeaderAuth | UnavailableAuth | LoginAuth`。
- `AuthTable(..., connections=None)`：插件可用的连接信息；`to_dict()` 只输出连接名。
- `create_engine(*, login_strategies=())`：内置 `generic_flow` 总是注册；插件重名或名为
  `generic_flow` → `ValueError`（消息只含策略名）。无参调用与 0.3.0 等价。
- `SingleApiEngine(http_client=None, *, login_strategies=(), client_factory=None)`：`client_factory`
  是测试 seam（例如返回包了 `httpx.MockTransport` 的 `HttpxClient`）。
- 内部端口（非稳定公开面）：`HttpResponse` 新增 `cookies` / `text`；`HttpClient.cookie_jar()`；
  `HttpxClient(transport=...)`。

所有新 DTO 的 `repr` / `to_dict` 只暴露策略名、tier、host、步骤名、提取名、header 名；
一律禁止 pickle / copy；构造错误消息不回显字段值。

## 执行语义

- 按需登录（D8）：只有当某步骤解析出的身份是 login 身份时才登录；同一执行内按身份保存会话。
- 有效性（D13/E3）：`expires_from`（优先）/ `ttl_seconds` / Cookie `expires`，固定提前 30 秒判失效，
  失效后再次用到自动重登（全新专用 client）。不跨执行缓存。
- 会话池：每次 `execute` 一个；每个 login 身份一个专用 client（cookie jar 隔离），执行结束统一关闭；
  匿名 / 静态 header 身份仍每次调用新建 client。Cookie 身份的业务响应同样更新其 jar（D12）。
- 登录步骤一律 `follow_redirects=False`，未写 `success` 时默认“状态码 < 400”（Q3）；`success` 另支持
  `body_not_contains`、`reject_location_contains`。
- 名为 `token` 的命名值长度 < 8 → `token_too_short`；其它命名值长度不限，但只有 ≥ 8 的进入按值替换。

## 脱敏

- H1：按值替换集合是**执行级**并集（全部身份的命名值、jar Cookie 值、登录参数 / 连接 SecretStr、
  静态 header 秘密），每个快照（请求 url / headers / body / query、响应、登录记录）都用它替换。
- Q7：登录记录先缓存；登录结束后先把命名值与 Cookie 值加入集合，再统一脱敏落定；登录失败时，
  登录响应中长度 ≥ 20 的字符串叶子值一律 `***`。
- `Cookie` / `Set-Cookie` 等敏感头继续按名脱敏；注入头（含字面量头）按名脱敏。
- 未被提取为命名值的登录响应字段不做按值替换（只有命名值才被识别为秘密）。
- 日志只记身份名、策略名、步骤名、reason、异常类型名；不用 `logger.exception` / `exc_info`。
- `SecretStr.reveal()` 只允许在 `engine/auth_injection.py` 与 `engine/login_render.py` 调用（AST 测试）。

## 插件协议稳定性承诺

0.4.x 内 `LoginStrategy` / `LoginContext` / `LoginResult` 只增不改：

```python
class LoginStrategy(Protocol):
    name: str

    def validate(self, auth: LoginAuth) -> None: ...  # 预检期，不得发请求
    async def login(self, ctx: LoginContext) -> LoginResult: ...
```

`LoginContext` 字段：`identity`、`http`（该身份专用 client，本次执行内存活）、`host_url`、`params`、
`connections`、`record(request, response, *, name=None)`、`flow`、`timeout_seconds`。
插件 `login()` 整体受 `timeout_seconds` 限时（超时 → `reason=timeout`）；抛出任何异常 →
`strategy_error`（httpx 超时 / 传输异常分别为 `timeout` / `transport`），只记异常类型名。
插件拿不到资产、业务 client、其它身份的状态或 Control 对象。插件发现（entry point
`testplat_engine.login_strategies` ∩ Control 白名单 `login_plugins`）由 Control 负责（E1）。
