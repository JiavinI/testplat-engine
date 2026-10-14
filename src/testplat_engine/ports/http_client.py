"""HTTP 客户端抽象接口

提供统一的 HTTP 请求接口,支持不同的底层实现 (httpx, aiohttp 等)。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CookieInfo:
    """一条 Cookie（H2，内部端口）：名字、值、作用域与过期时间（epoch 秒或 None）。

    ``repr`` 不含值：Cookie 值一律视为秘密，避免经调试输出/断言消息外泄。
    """

    name: str
    value: str = field(repr=False)
    domain: str = ""
    path: str = "/"
    expires: float | None = None


@dataclass
class HttpResponse:
    """HTTP 响应数据类

    Attributes:
        status_code: HTTP 状态码 (200, 404, 500 等)
        headers: 响应头字典
        body: 响应体 (JSON 解析后的对象,可以是 dict, list 或基本类型)
    """

    status_code: int
    headers: dict[str, str]
    body: Any  # JSON 解析后的对象
    # 3.3b（H2）：本响应 Set-Cookie 解析出的 Cookie 列表与原始文本；旧调用方可不传。
    cookies: list[CookieInfo] = field(default_factory=list, repr=False)
    text: str | None = field(default=None, repr=False)


class HttpClient(ABC):
    """HTTP 客户端抽象接口

    定义统一的 HTTP 请求方法,具体实现由 Adapter 层提供。
    支持异步操作,适配现代 Python 异步框架。
    """

    @abstractmethod
    async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        """发送 HTTP 请求

        Args:
            method: HTTP 方法,支持 GET/POST/PUT/DELETE/PATCH 等
            url: 完整的请求 URL (包含协议、域名、路径)
            **kwargs: 额外的请求参数,支持:
                - headers: dict[str, str] - 自定义请求头
                - json: Any - JSON 请求体 (自动序列化)
                - data: Any - 表单数据
                - params: dict[str, str] - URL 查询参数
                - timeout: float - 请求超时时间 (秒)
                - 其他实现相关参数

        Returns:
            HttpResponse: 包含状态码、响应头和响应体的响应对象

        Raises:
            TimeoutError: 请求超时
            ConnectionError: 连接失败
            ValueError: 参数错误或响应格式错误
            其他实现相关异常

        Example:
            >>> client = ConcreteHttpClient()
            >>> response = await client.request(
            ...     "POST",
            ...     "https://api.example.com/users",
            ...     json={"name": "Alice"},
            ...     headers={"Authorization": "Bearer token"}
            ... )
            >>> print(response.status_code, response.body)
        """
        pass

    def cookie_jar(self) -> list[CookieInfo]:
        """当前 client cookie jar 的全部 Cookie（H2）；无 jar 的实现返回空列表。

        login 身份的专用 client 靠它判断 Cookie 过期、取 ``cookie_names`` 与按值脱敏。
        """
        return []
