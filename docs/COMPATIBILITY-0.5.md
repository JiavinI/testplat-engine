# Engine 0.5.0 兼容说明（Phase 3.3d：跨执行登录缓存 + 401 重登重发）

日期：2026-10-14
版本：`0.5.0`
权威设计：`notes/phase3.3d-login-cache-reauth-design-draft.md`
Control 约束建议：`testplat-engine>=0.5.0,<0.6`。

## 破坏性：公开 `auth` 九键

| 键 | 取值 | 说明 |
|---|---|---|
| （0.4 七键） | 同 0.4.0 | `identity` / `kind` / `tier` / `header_names` / `overridden_headers` / `cookie_names` / `login_ref` |
| `session_source` | `"login"` / `"execution"` / `"cache"` / `null` | 本步会话来源；静态 header 恒为 `null` |
| `reauthenticated` | `bool` | 本步是否触发了 401 重登重发；静态 header 恒为 `false` |

- `session_source="cache"` 时 `login_ref=null`（本次执行没有登录记录，`logins` 不追加）。
- 401 重发成功后 `session_source="login"`，`login_ref` 指向重登那条记录，`reauthenticated=true`。

## 跨执行缓存

- `create_engine(..., login_cache_max_entries=256)`：Engine 实例持有一份 `LoginCache`；`0` = 关闭。
- `AuthTable.cache_scope`：Control 传 `f"{project.id}:{project.env}"`；`None` 不跨执行缓存。
  校验：非空、≤200、`[A-Za-z0-9._:-]`；**不进** `to_dict` / 公开结果。
- 写入条件：scope 非空、容量 > 0、非 cookie 身份、登录（含 probe）成功、按 0.4.0 规则算出 `expires_at` 且距失效 > 30 秒。
- 无 ttl / 无 expires_from → 不缓存（每次登录）。`session: cookie` → 不跨执行缓存。
- 服务重启清空；不落盘；多进程各一份。

## 401 重登重发

- `LoginAuth.reauth_on_401: bool = True`（构造须为布尔；指纹包含它；`to_dict` 可含该布尔）。
- 条件：login 身份、开关开启、业务响应 `status_code == 401`、本请求尚未重发过。
- 流程：记日志（只含身份名与 `trigger_status=401`）→ 按 generation 作废缓存 → 重登 → 同方法/URL/body 重发一次。
- 公开面只描述第二次请求/响应；第一次 401 响应体不进公开结果与 Engine 日志。
- 第二次仍 401 → 正常断言，不再重发。
- **单步最长耗时**：最多「2 次业务请求 + 1 次登录」，各用一次 `timeout_seconds`；不另设预算。

## 安全

- 缓存命名值 / 指纹 / HMAC 密钥 / generation 不进 `repr` / 日志 / 公开结果 / 异常消息。
- 缓存命中路径**必须先**把静态登录秘密与缓存命名值加入执行级替换集合，再构造任何快照。
- 日志只记身份名、策略名、`cache=hit|miss|store|invalidate|evict`、`reauth trigger_status=401`、异常类型名。
