"""3.3a 身份解析与凭证注入（Engine 内部模块）。

职责：
1. 预检：优先按调用者选择、再按旧资产 ``auth`` 声明查 ``AuthTable``，解析本次身份；
   身份不存在 / 不可用 / 未知 kind / 声明了身份但没有身份表 → ``InvalidEnvironmentError``
   （preflight，422，不保存记录）。``public_message`` 只含身份名（及 Control 提供的
   只含变量名的 ``public_reason``），``log_reason`` 同样不含值。
2. 注入：在 ``client.request`` 调用处把凭证 header 合并进**本次请求**的 headers；
   与资产 header 按不区分大小写判定冲突，凭证优先且只发一份（Q3/M3），不修改任何共享 client。
3. 按值替换集合：原始变量值 + 完整 header 值（Q5），按长度降序，供公开面脱敏。
4. 3.3b：``LoginAuth`` 同样在这里解析；其凭证由会话池登录后经 ``login_render`` 渲染。

不变量：``SecretStr.reveal()`` 全仓只允许在本模块与 ``login_render`` 调用（有 AST 测试约束）；
揭示出的明文只进入发给 HTTP client 的 headers 与内存中的替换集合，绝不进入日志、
异常消息或任何公开结构。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from testplat_engine.domain.api import Api
from testplat_engine.interface import (
    RESERVED_IDENTITY_NONE,
    AuthTable,
    InvalidEnvironmentError,
    JsonObject,
    LoginAuth,
    StaticHeaderAuth,
    UnavailableAuth,
    _material_kind,
)


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedAuth:
    """预检后确定的身份；只在单次 execute_api 调用内存活，不跨请求复用。"""

    identity: str
    material: StaticHeaderAuth | LoginAuth

    @property
    def header_names(self) -> tuple[str, ...]:
        return self.material.header_names

    @property
    def is_login(self) -> bool:
        """3.3b：login 身份需要先经会话池按需登录，再注入渲染后的 header。"""
        return isinstance(self.material, LoginAuth)

    def __repr__(self) -> str:
        return f"ResolvedAuth(identity={self.identity!r}, header_names={list(self.header_names)!r})"


def resolve_identity(
    api: Api, auth: AuthTable | None, identity: str | None = None
) -> ResolvedAuth | None:
    """调用者选择优先，未选择时兼容旧资产身份规则；返回 None 表示匿名。

    1. 调用者提供 ``identity`` 时覆盖资产 ``auth``；``none`` 显式匿名。
    2. 未提供时使用旧资产 ``auth``；其中 ``none`` 同样显式匿名。
    3. 两者均未声明时使用 ``default_identity``；没有则匿名。
    选择具名身份后查表；无身份表或名不存在 → 422。
    """
    # 调用者显式选择覆盖旧资产声明；兼容回退不会改变共享 API 对象。
    declared = api.auth if identity is None else identity
    if declared == RESERVED_IDENTITY_NONE:
        return None
    if declared is not None:
        if auth is None:
            raise InvalidEnvironmentError(
                public_message=f"API 资产声明了身份 {declared}，但项目未配置 auth",
                log_reason=f"api={api.id} identity={declared} auth_table=None",
            )
        name = declared
    else:
        if auth is None or auth.default_identity is None:
            return None
        name = auth.default_identity

    material = auth.identities.get(name)
    if material is None:
        raise InvalidEnvironmentError(
            public_message=f"身份不存在: {name}",
            log_reason=f"api={api.id} identity={name} missing",
        )
    if isinstance(material, UnavailableAuth):
        raise InvalidEnvironmentError(
            public_message=f"身份 {name} 不可用: {material.public_reason}",
            log_reason=f"api={api.id} identity={name} kind=unavailable",
        )
    if isinstance(material, (StaticHeaderAuth, LoginAuth)):
        return ResolvedAuth(identity=name, material=material)
    # 未知 kind（例如更新版本 Control 新增的材料）：拒绝，绝不静默降级为匿名。
    kind = _material_kind(material) or "?"
    raise InvalidEnvironmentError(
        public_message=f"身份 {name} 的凭证类型不受支持: {kind[:64]}",
        log_reason=f"api={api.id} identity={name} unsupported kind",
    )


def merge_headers(
    asset_headers: object,
    resolved: ResolvedAuth | None,
    login_headers: dict[str, str] | None = None,
) -> tuple[dict[str, str] | None, dict[str, object] | None, list[str]]:
    """合并资产 header 与凭证 header。

    Returns:
        (发出的 headers, 公开 headers 模板, overridden_headers)
        - 发出的 headers：含凭证明文，只交给 ``client.request``；
        - 公开模板：凭证值占位为 ``***``，供公开快照使用；
        - overridden_headers：被凭证覆盖的资产 header 原始写法（Q3）。
    无凭证时原样返回资产 headers，保持 v0.2.0 行为。
    login 身份的凭证值由调用方经 ``login_render`` 渲染后以 ``login_headers`` 传入；
    冲突规则与静态 header 相同（不区分大小写、凭证优先、记录 overridden）。
    """
    if resolved is None:
        if isinstance(asset_headers, Mapping):
            return dict(asset_headers), dict(asset_headers), []
        return asset_headers, asset_headers, []  # type: ignore[return-value]

    credential_lower = {name.lower() for name in resolved.header_names}
    send: dict[str, str] = {}
    public: dict[str, object] = {}
    overridden: list[str] = []
    if isinstance(asset_headers, Mapping):
        for key, value in asset_headers.items():
            name = str(key)
            if name.lower() in credential_lower:
                # 不区分大小写冲突：丢弃资产版本，避免同时发出 Authorization 与 authorization。
                overridden.append(name)
                continue
            send[name] = value  # type: ignore[assignment]
            public[name] = value
    if isinstance(resolved.material, LoginAuth):
        for name, text in (login_headers or {}).items():
            send[name] = text
            public[name] = "***"
        return send, public, overridden
    for name, secret in resolved.material.headers.items():
        send[name] = secret.reveal()
        public[name] = "***"
    return send, public, overridden


def secret_values(resolved: ResolvedAuth | None) -> tuple[str, ...]:
    """本次执行的按值替换集合（Q5）：原始变量值 + 完整 header 值，长度降序。

    收集全部非空凭据值。短凭据在公开面仅做完整值匹配，不替换普通文本的子串。
    """
    if resolved is None or not isinstance(resolved.material, StaticHeaderAuth):
        # login 身份的秘密由会话池维护（执行级集合，H1）。
        return ()
    material = resolved.material
    values = {item.reveal() for item in material.secret_values if len(item)}
    for secret in material.headers.values():
        text = secret.reveal()
        if text:
            values.add(text)
    return tuple(sorted(values, key=len, reverse=True))


def public_auth(
    resolved: ResolvedAuth | None,
    overridden: list[str],
    *,
    cookie_names: list[str] | None = None,
    login_ref: int | None = None,
    session_source: str | None = None,
    reauthenticated: bool = False,
) -> JsonObject | None:
    """公开 ``auth`` 字段（0.5 九键）：七键 + ``session_source`` / ``reauthenticated``。

    静态 header 身份：``tier=None``、``cookie_names=[]``、``login_ref=None``、
    ``session_source=None``、``reauthenticated=False``。
    """
    if resolved is None:
        return None
    material = resolved.material
    is_login = isinstance(material, LoginAuth)
    return {
        "identity": resolved.identity,
        "kind": material.kind,
        "tier": material.tier if is_login else None,  # type: ignore[union-attr]
        "header_names": list(resolved.header_names),
        "overridden_headers": list(overridden),
        "cookie_names": list(cookie_names or []) if is_login else [],
        "login_ref": login_ref if is_login else None,
        "session_source": session_source if is_login else None,
        "reauthenticated": reauthenticated if is_login else False,
    }
