"""3.3b 登录模板渲染（Engine 内部模块；L3：reveal() 白名单之二）。

职责：
1. 渲染登录步骤 / probe / inject 模板：``SecretStr`` 先 reveal，再替换
   ``${login:名字}``（本身份已产出的命名值）与 ``${login_host}``（登录 host URL）；
2. 收集一个 login 身份的静态秘密（原始 ``${secret:}`` 值、SecretStr 参数、
   ``connections`` 中的 SecretStr），供执行级按值替换集合（H1）。

不变量：``SecretStr.reveal()`` 全仓只允许在 ``auth_injection.py``、本模块与
``login_cache.py`` 调用（AST 测试）；渲染出的明文只交给 HTTP client 或进入内存中的
替换集合，绝不进日志、异常消息或公开结构。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from testplat_engine.interface import (
    LOGIN_HOST_PLACEHOLDER,
    LOGIN_REF_RE,
    MIN_SECRET_LENGTH,
    LoginAuth,
    SecretStr,
)


class MissingLoginValueError(LookupError):
    """模板引用了尚未产出的命名值；消息只含名字（名字不是秘密）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"缺少命名值: {name}")


def render_text(value: object, values: Mapping[str, str], host_url: str) -> str:
    """渲染单个模板为明文。"""
    text = value.reveal() if isinstance(value, SecretStr) else str(value)

    def _sub(match: Any) -> str:
        name = match.group(1)
        if name not in values:
            raise MissingLoginValueError(name)
        return values[name]

    text = LOGIN_REF_RE.sub(_sub, text)
    return text.replace(LOGIN_HOST_PLACEHOLDER, host_url)


def render_value(value: object, values: Mapping[str, str], host_url: str) -> Any:
    """递归渲染请求体字段：字符串 / SecretStr 渲染为明文，其它 JSON 值原样。"""
    if isinstance(value, (str, SecretStr)):
        return render_text(value, values, host_url)
    if isinstance(value, Mapping):
        return {str(k): render_value(v, values, host_url) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [render_value(item, values, host_url) for item in value]
    return value


def render_headers(
    templates: Mapping[str, object] | None, values: Mapping[str, str], host_url: str
) -> dict[str, str]:
    return {name: render_text(item, values, host_url) for name, item in (templates or {}).items()}


def secret_texts_in(value: object) -> set[str]:
    """取嵌套结构里全部 SecretStr 的明文（≥ 8），用于按值替换集合。"""
    out: set[str] = set()
    if isinstance(value, SecretStr):
        text = value.reveal()
        if len(text) >= MIN_SECRET_LENGTH:
            out.add(text)
    elif isinstance(value, Mapping):
        for item in value.values():
            out |= secret_texts_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            out |= secret_texts_in(item)
    return out


def static_login_secrets(
    material: LoginAuth, connections: Mapping[str, Mapping[str, object]]
) -> set[str]:
    """一个 login 身份在登录前即已知的秘密：原始变量值 + 所有 SecretStr 模板 + 连接密码。

    含 ``${login:}`` 的 SecretStr 模板只取其固定部分无意义，因此整体明文加入（≥ 8）；
    原始 ``${secret:}`` 值另由 ``secret_values`` 保证覆盖子串。
    """
    out: set[str] = set()
    for item in material.secret_values:
        out |= secret_texts_in(item)
    out |= secret_texts_in(material.params)
    out |= secret_texts_in(material.inject_headers)
    if material.flow is not None:
        for step in material.flow.steps:
            out |= secret_texts_in(step.headers)
            if step.body is not None:
                out |= secret_texts_in(step.body.fields)
                out |= secret_texts_in(step.body.content)
    if material.probe is not None:
        out |= secret_texts_in(material.probe.headers)
    out |= secret_texts_in(connections)
    return out


def secret_template_refs(templates: Mapping[str, object] | None) -> set[str]:
    """SecretStr 模板内部引用的 ``${login:}`` 名字（预检核对来源用，不外显值）。"""
    refs: set[str] = set()
    for item in (templates or {}).values():
        if isinstance(item, SecretStr):
            refs |= set(LOGIN_REF_RE.findall(item.reveal()))
    return refs
