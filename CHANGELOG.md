# Changelog


## 0.5.1（本地候选，未远程发布）

- 新增单接口 `request_override={"body": ...}`：整体替换、不可变快照，不写回资产、不渲染占位符；支持 JSON/Query/Form/Raw 和显式 JSON null。
- 被测输入凭据不再要求最小长度或内容限制；短秘密按完整字段匹配脱敏，登录缓存仍收集秘密。登录产出 token 的现有规则不变。
- Control 配套本地开发入口与 wheel 集成验证；真实系统行为仍由调用方在隔离环境配置后验收。

## 0.5.0 — 2026-10-14

### 破坏性变更

- 公开 `auth` 由 **7 键变为 9 键**：新增 `session_source`（`"login"` / `"execution"` / `"cache"` / `null`）与 `reauthenticated`（`bool`）。
  严格按键校验的调用方必须升级；静态 header 身份固定为 `session_source=null`、`reauthenticated=false`。
- Control 约束建议：`testplat-engine>=0.5.0,<0.6`。

### 新增

- **跨执行登录缓存**（进程内存，不落盘）：`create_engine(login_cache_max_entries=256)`；
  `AuthTable.cache_scope` 非空且身份非 `session: cookie`、且算出了足够长的 `expires_at` 时写入。
  `0` 关闭缓存（行为等同 0.4.0）。缓存键含登录配置 HMAC 指纹（改密码/配置自动失效）。
- **401 重登重发**：`LoginAuth.reauth_on_401=True`（默认）；业务响应 HTTP 401 时作废会话、
  重登并同方法/URL/body 重发一次。公开 `request`/`response` 只保留第二次；第一次 401 响应不进公开结果。
- 单步最长耗时可达「2 次业务请求 + 1 次登录」（各用一次 `timeout_seconds`），不另设预算。

### 不变

- 过期时间取值顺序与 30 秒安全余量仍沿用 0.4.0（R1）。
- `login_failed` 的 reason 枚举不变；不公开 `expires_at` / 缓存键 / 指纹 / generation。
- 无新依赖；缓存不落盘、不跨进程共享。

## 0.4.0

见 `docs/COMPATIBILITY-0.4.md`。
