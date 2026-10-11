"""基于 httpx 的 HTTP 客户端实现。

提供 HttpClient 抽象接口的 httpx 实现。调用方传入的 ``timeout`` 必须原样交给
``httpx.AsyncClient.request``，由执行引擎在 ``finally`` 中 ``close`` 释放连接池。
"""

import httpx

from testplat_engine.ports.http_client import HttpClient, HttpResponse


class HttpxClient(HttpClient):
    """基于 httpx 的 HTTP 客户端实现

    使用 httpx.AsyncClient 提供高性能的异步 HTTP 请求能力。
    自动处理 JSON 解析,失败时返回纯文本。

    Example:
        >>> client = HttpxClient()
        >>> response = await client.request("GET", "https://api.example.com/users")
        >>> print(response.status_code, response.body)
        >>> await client.close()
    """

    def __init__(self):
        """初始化 httpx AsyncClient"""
        self._client = httpx.AsyncClient()

    async def request(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> HttpResponse:
        """发送 HTTP 请求 (实现父类抽象方法)

        Args:
            method: HTTP 方法 (GET/POST/PUT/DELETE/PATCH 等)
            url: 完整的请求 URL
            **kwargs: httpx 支持的所有参数 (headers, json, data, params, timeout 等)

        Returns:
            HttpResponse: 包含状态码、响应头和响应体的响应对象

        Raises:
            httpx.TimeoutException: 请求超时
            httpx.RequestError: 连接失败或其他请求错误
        """
        response = await self._client.request(method, url, **kwargs)

        # 尝试解析 JSON,失败则返回文本
        try:
            body = response.json()
        except Exception:
            body = response.text

        return HttpResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=body
        )

    async def close(self):
        """关闭客户端并释放资源

        应在使用完毕后调用,确保连接池正确关闭。
        支持异步上下文管理器模式。
        """
        await self._client.aclose()

    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出时自动关闭"""
        await self.close()
