"""3.3d 跨执行登录缓存（进程内存，不落盘）。

``LoginCache`` 由 ``create_engine`` 构造的 Engine 实例持有一份，经 ``SessionPool``
在多次 ``execute`` 间复用命名值。键 = (cache_scope, identity, fingerprint)；
指纹是进程级随机密钥的 HMAC-SHA256，改密码 / 改登录配置自动失效。

不变量（设计稿 §2 / §0.1）：
- 命名值、指纹、HMAC 密钥、generation 全部视为秘密：不进 repr / 日志 / 公开结果 /
  异常消息；本模块不提供任何列出键或值的公开方法；
- ``SecretStr.reveal()`` 只允许出现在本模块的 ``_reveal_for_fingerprint`` 一处
  （加入 AST 白名单）；reveal 出的明文只参与 HMAC 输入，绝不外泄；
- 日志只记 identity 与 ``cache=hit|miss|store|invalidate|evict``。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from testplat_engine.interface import LoginAuth, SecretStr

logger = logging.getLogger(__name__)

# 缓存键：(作用域, 身份名, 指纹十六进制)。指纹本身是秘密，键不进日志。
CacheKey = tuple[str, str, str]


@dataclass(slots=True, repr=False)
class CacheEntry:
    """缓存条目：只存命名值与过期时刻；不存 client / Cookie jar / 登录记录（C5）。"""

    values: dict[str, str]
    expires_at: float
    generation: int

    def __repr__(self) -> str:
        return f"CacheEntry(names={sorted(self.values)!r}, generation={self.generation})"


def _reveal_for_fingerprint(value: object) -> Any:
    """规范化指纹材料时唯一允许 reveal 的函数；明文只进 HMAC，绝不返回给调用方日志。"""
    if isinstance(value, SecretStr):
        return value.reveal()
    if isinstance(value, Mapping):
        return {str(k): _reveal_for_fingerprint(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_reveal_for_fingerprint(item) for item in value]
    return value


def _flow_payload(material: LoginAuth) -> Any:
    if material.flow is None:
        return None
    steps = []
    for step in material.flow.steps:
        body: Any = None
        if step.body is not None:
            body = {
                "type": step.body.type,
                "fields": _reveal_for_fingerprint(step.body.fields),
                "content": _reveal_for_fingerprint(step.body.content),
                "content_type": step.body.content_type,
            }
        steps.append(
            {
                "name": step.name,
                "method": step.method,
                "path": step.path,
                "headers": _reveal_for_fingerprint(step.headers),
                "body": body,
                "success": dict(step.success) if step.success else None,
                "extract": [
                    {
                        "source": r.source,
                        "as": r.as_name,
                        "path": r.path,
                        "name": r.name,
                        "format": r.format,
                        "optional": r.optional,
                    }
                    for r in step.extract
                ],
            }
        )
    return {"steps": steps}


def _validity_payload(material: LoginAuth) -> Any:
    validity = material.validity
    if validity is None:
        return None
    expires = validity.expires_from
    return {
        "ttl_seconds": validity.ttl_seconds,
        "expires_from": None
        if expires is None
        else {
            "path": expires.path,
            "unit": expires.unit,
            "source": expires.source,
            "step": expires.step,
        },
    }


def _probe_payload(material: LoginAuth) -> Any:
    probe = material.probe
    if probe is None:
        return None
    return {
        "method": probe.method,
        "path": probe.path,
        "headers": _reveal_for_fingerprint(probe.headers),
        "success": dict(probe.success) if probe.success else None,
    }


def fingerprint_material(
    material: LoginAuth,
    host_url: str,
    connections: Mapping[str, Mapping[str, object]],
) -> str:
    """规范化登录材料为稳定 JSON 字符串（供 HMAC）。含 SecretStr 明文（改密码即失效）。"""
    # 插件档位指纹含全部 connections；其它档位不含（设计稿 §2.3）。
    conn_payload = _reveal_for_fingerprint(connections) if material.tier == "plugin" else None
    payload = {
        "strategy": material.strategy,
        "tier": material.tier,
        "host_url": host_url,
        "flow": _flow_payload(material),
        "params": _reveal_for_fingerprint(material.params),
        "inject_headers": _reveal_for_fingerprint(material.inject_headers),
        "validity": _validity_payload(material),
        "probe": _probe_payload(material),
        "session_cookie": material.session_cookie,
        "reauth_on_401": material.reauth_on_401,
        "connections": conn_payload,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class LoginCache:
    """进程内 LRU 登录缓存；不可 pickle/copy；repr 只含条目数。"""

    def __init__(self, max_entries: int = 256) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool):
            raise TypeError("login_cache_max_entries 必须是整数")
        if max_entries < 0:
            raise ValueError("login_cache_max_entries 不能为负")
        self._max_entries = max_entries
        # 进程级随机密钥：只在内存，不可读出；重启即失效（C4）。
        self._hmac_key = secrets.token_bytes(32)
        self._entries: OrderedDict[CacheKey, CacheEntry] = OrderedDict()
        self._locks: dict[CacheKey, asyncio.Lock] = {}
        self._generation = 0

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def enabled(self) -> bool:
        return self._max_entries > 0

    def __repr__(self) -> str:
        return f"LoginCache(entries={len(self._entries)})"

    def __str__(self) -> str:
        return self.__repr__()

    def __reduce__(self) -> object:
        raise TypeError("LoginCache 不可 pickle/copy")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("LoginCache 不可 pickle/copy")

    def fingerprint(
        self,
        material: LoginAuth,
        host_url: str,
        connections: Mapping[str, Mapping[str, object]],
    ) -> str:
        """HMAC-SHA256 十六进制指纹；材料变更（含密码）→ 新指纹 → 旧条目不可命中。"""
        canonical = fingerprint_material(material, host_url, connections)
        return hmac.new(self._hmac_key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

    def make_key(self, scope: str, identity: str, fingerprint: str) -> CacheKey:
        return (scope, identity, fingerprint)

    def lock_for(self, key: CacheKey) -> asyncio.Lock:
        """每键一把 asyncio.Lock（single-flight，C7）。"""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def get(self, key: CacheKey) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        # LRU：命中移到末尾。
        self._entries.move_to_end(key)
        return entry

    def store(self, key: CacheKey, values: Mapping[str, str], expires_at: float) -> int:
        """写入命名值副本与过期时刻；返回新 generation。容量满 → LRU 淘汰。"""
        self._generation += 1
        generation = self._generation
        if key in self._entries:
            del self._entries[key]
        while len(self._entries) >= self._max_entries > 0:
            old_key, _old = self._entries.popitem(last=False)
            self._locks.pop(old_key, None)
            # 日志只记身份名与动作，不记键/指纹。
            logger.info("cache=evict identity=%s", old_key[1])
        self._entries[key] = CacheEntry(
            values=dict(values), expires_at=expires_at, generation=generation
        )
        return generation

    def delete(self, key: CacheKey) -> None:
        if key in self._entries:
            del self._entries[key]
        self._locks.pop(key, None)

    def invalidate(self, key: CacheKey, generation: int) -> bool:
        """按 generation 作废：仅当条目仍是那一代时删除，避免误删并发写入的新 token（§2.7）。"""
        entry = self._entries.get(key)
        if entry is None:
            return False
        if entry.generation != generation:
            return False
        del self._entries[key]
        self._locks.pop(key, None)
        return True


__all__ = ["CacheEntry", "CacheKey", "LoginCache", "fingerprint_material"]
