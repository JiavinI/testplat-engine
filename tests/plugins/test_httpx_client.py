"""HttpxClient 单元测试

测试基于 httpx 的 HTTP 客户端实现的核心功能。
"""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from testplat_engine.plugins.httpx_client import HttpxClient


@pytest.fixture
def client():
    """创建 HttpxClient 实例"""
    return HttpxClient()


@pytest.fixture
def mock_response():
    """创建 mock httpx.Response"""
    response = Mock(spec=httpx.Response)
    response.status_code = 200
    response.headers = httpx.Headers({"content-type": "application/json"})
    return response


class TestHttpxClient:
    """HttpxClient 测试套件"""

    @pytest.mark.asyncio
    async def test_request_json_success(self, client):
        """测试 JSON 响应解析成功"""
        mock_response = Mock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.json.return_value = {"message": "success", "data": [1, 2, 3]}
        mock_response.text = '{"message": "success", "data": [1, 2, 3]}'

        with patch.object(
            client._client, "request", new_callable=AsyncMock, return_value=mock_response
        ):
            response = await client.request("GET", "https://api.example.com/test")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.body == {"message": "success", "data": [1, 2, 3]}

    @pytest.mark.asyncio
    async def test_request_json_parse_failure(self, client):
        """测试 JSON 解析失败时返回文本"""
        mock_response = Mock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "text/html"})
        mock_response.json.side_effect = ValueError("Invalid JSON")
        mock_response.text = "<html>Not JSON</html>"

        with patch.object(
            client._client, "request", new_callable=AsyncMock, return_value=mock_response
        ):
            response = await client.request("GET", "https://api.example.com/html")

        assert response.status_code == 200
        assert response.body == "<html>Not JSON</html>"

    @pytest.mark.asyncio
    async def test_request_with_headers(self, client):
        """测试自定义请求头"""
        mock_response = Mock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({})
        mock_response.json.return_value = {}

        with patch.object(
            client._client, "request", new_callable=AsyncMock, return_value=mock_response
        ) as mock_request:
            await client.request(
                "GET", "https://api.example.com/test", headers={"Authorization": "Bearer token123"}
            )

        mock_request.assert_called_once_with(
            "GET", "https://api.example.com/test", headers={"Authorization": "Bearer token123"}
        )

    @pytest.mark.asyncio
    async def test_request_post_with_json(self, client):
        """测试 POST 请求携带 JSON 数据"""
        mock_response = Mock(spec=httpx.Response)
        mock_response.status_code = 201
        mock_response.headers = httpx.Headers({})
        mock_response.json.return_value = {"id": 42}

        with patch.object(
            client._client, "request", new_callable=AsyncMock, return_value=mock_response
        ) as mock_request:
            response = await client.request(
                "POST", "https://api.example.com/users", json={"name": "Alice", "age": 30}
            )

        assert response.status_code == 201
        assert response.body == {"id": 42}
        mock_request.assert_called_once_with(
            "POST", "https://api.example.com/users", json={"name": "Alice", "age": 30}
        )

    @pytest.mark.asyncio
    async def test_request_with_params(self, client):
        """测试 URL 查询参数"""
        mock_response = Mock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({})
        mock_response.json.return_value = []

        with patch.object(
            client._client, "request", new_callable=AsyncMock, return_value=mock_response
        ) as mock_request:
            await client.request(
                "GET", "https://api.example.com/search", params={"q": "test", "limit": "10"}
            )

        mock_request.assert_called_once_with(
            "GET", "https://api.example.com/search", params={"q": "test", "limit": "10"}
        )

    @pytest.mark.asyncio
    async def test_request_timeout_error(self, client):
        """测试请求超时异常"""
        with patch.object(
            client._client,
            "request",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("Timeout"),
        ):
            with pytest.raises(httpx.TimeoutException):
                await client.request("GET", "https://api.example.com/slow")

    @pytest.mark.asyncio
    async def test_request_connection_error(self, client):
        """测试连接失败异常"""
        with patch.object(
            client._client,
            "request",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("Connection failed"),
        ):
            with pytest.raises(httpx.ConnectError):
                await client.request("GET", "https://invalid.example.com")

    @pytest.mark.asyncio
    async def test_close(self, client):
        """测试客户端关闭"""
        with patch.object(client._client, "aclose", new_callable=AsyncMock) as mock_close:
            await client.close()

        mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """测试异步上下文管理器"""
        async with HttpxClient() as client:
            mock_response = Mock(spec=httpx.Response)
            mock_response.status_code = 200
            mock_response.headers = httpx.Headers({})
            mock_response.json.return_value = {"status": "ok"}

            with patch.object(
                client._client, "request", new_callable=AsyncMock, return_value=mock_response
            ):
                response = await client.request("GET", "https://api.example.com/health")
                assert response.status_code == 200

        # 验证退出时自动关闭
        assert client._client.is_closed

    @pytest.mark.asyncio
    async def test_different_http_methods(self, client):
        """测试不同的 HTTP 方法"""
        methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]

        for method in methods:
            mock_response = Mock(spec=httpx.Response)
            mock_response.status_code = 200
            mock_response.headers = httpx.Headers({})
            mock_response.json.return_value = {}

            with patch.object(
                client._client, "request", new_callable=AsyncMock, return_value=mock_response
            ) as mock_request:
                await client.request(method, "https://api.example.com/resource")
                mock_request.assert_called_once_with(method, "https://api.example.com/resource")

    @pytest.mark.asyncio
    async def test_response_headers_dict_conversion(self, client):
        """测试响应头转换为字典"""
        mock_response = Mock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers(
            {"content-type": "application/json", "x-custom-header": "custom-value"}
        )
        mock_response.json.return_value = {}

        with patch.object(
            client._client, "request", new_callable=AsyncMock, return_value=mock_response
        ):
            response = await client.request("GET", "https://api.example.com/test")

        assert isinstance(response.headers, dict)
        assert response.headers["content-type"] == "application/json"
        assert response.headers["x-custom-header"] == "custom-value"
