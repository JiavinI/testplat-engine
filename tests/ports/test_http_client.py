"""HTTP 客户端抽象接口测试

测试 HttpResponse 数据类和 HttpClient 抽象接口的定义。
使用 Mock 实现验证接口契约。
"""

from typing import Any

import pytest

from testplat_engine.ports.http_client import HttpClient, HttpResponse


class MockHttpClient(HttpClient):
    """Mock HTTP 客户端实现

    用于测试接口定义,返回预设的响应数据。
    """

    def __init__(self, mock_response: HttpResponse | None = None):
        """初始化 Mock 客户端

        Args:
            mock_response: 预设的响应对象,如果为 None 则返回默认响应
        """
        self._mock_response = mock_response or HttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body={"message": "success"}
        )
        self.last_request: dict[str, Any] = {}

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: Any
    ) -> HttpResponse:
        """Mock 请求实现,记录请求参数并返回预设响应"""
        self.last_request = {
            "method": method,
            "url": url,
            **kwargs
        }
        return self._mock_response


class TestHttpResponse:
    """HttpResponse 数据类测试"""

    def test_create_response(self):
        """测试创建响应对象"""
        response = HttpResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body={"id": 1, "name": "Alice"}
        )

        assert response.status_code == 200
        assert response.headers == {"content-type": "application/json"}
        assert response.body == {"id": 1, "name": "Alice"}

    def test_response_with_list_body(self):
        """测试响应体为列表的情况"""
        response = HttpResponse(
            status_code=200,
            headers={},
            body=[{"id": 1}, {"id": 2}]
        )

        assert isinstance(response.body, list)
        assert len(response.body) == 2

    def test_response_with_primitive_body(self):
        """测试响应体为基本类型的情况"""
        response = HttpResponse(
            status_code=200,
            headers={},
            body="plain text"
        )

        assert response.body == "plain text"

    def test_response_error_status(self):
        """测试错误状态码的响应"""
        response = HttpResponse(
            status_code=404,
            headers={},
            body={"error": "Not Found"}
        )

        assert response.status_code == 404
        assert response.body["error"] == "Not Found"


class TestHttpClient:
    """HttpClient 抽象接口测试"""

    @pytest.mark.asyncio
    async def test_get_request(self):
        """测试 GET 请求"""
        client = MockHttpClient()

        response = await client.request("GET", "https://api.example.com/users")

        assert response.status_code == 200
        assert client.last_request["method"] == "GET"
        assert client.last_request["url"] == "https://api.example.com/users"

    @pytest.mark.asyncio
    async def test_post_request_with_json(self):
        """测试 POST 请求携带 JSON 数据"""
        mock_response = HttpResponse(
            status_code=201,
            headers={"location": "/users/123"},
            body={"id": 123, "name": "Bob"}
        )
        client = MockHttpClient(mock_response)

        response = await client.request(
            "POST",
            "https://api.example.com/users",
            json={"name": "Bob"}
        )

        assert response.status_code == 201
        assert response.body["id"] == 123
        assert client.last_request["json"] == {"name": "Bob"}

    @pytest.mark.asyncio
    async def test_request_with_headers(self):
        """测试携带自定义请求头"""
        client = MockHttpClient()

        await client.request(
            "GET",
            "https://api.example.com/protected",
            headers={"Authorization": "Bearer token123"}
        )

        assert client.last_request["headers"]["Authorization"] == "Bearer token123"

    @pytest.mark.asyncio
    async def test_request_with_params(self):
        """测试携带 URL 查询参数"""
        client = MockHttpClient()

        await client.request(
            "GET",
            "https://api.example.com/users",
            params={"page": "2", "limit": "10"}
        )

        assert client.last_request["params"] == {"page": "2", "limit": "10"}

    @pytest.mark.asyncio
    async def test_request_with_timeout(self):
        """测试携带超时参数"""
        client = MockHttpClient()

        await client.request(
            "GET",
            "https://api.example.com/slow",
            timeout=30.0
        )

        assert client.last_request["timeout"] == 30.0

    @pytest.mark.asyncio
    async def test_put_request(self):
        """测试 PUT 请求"""
        client = MockHttpClient()

        await client.request(
            "PUT",
            "https://api.example.com/users/1",
            json={"name": "Updated"}
        )

        assert client.last_request["method"] == "PUT"

    @pytest.mark.asyncio
    async def test_delete_request(self):
        """测试 DELETE 请求"""
        mock_response = HttpResponse(
            status_code=204,
            headers={},
            body=None
        )
        client = MockHttpClient(mock_response)

        response = await client.request(
            "DELETE",
            "https://api.example.com/users/1"
        )

        assert response.status_code == 204
        assert client.last_request["method"] == "DELETE"

    @pytest.mark.asyncio
    async def test_interface_contract(self):
        """测试接口契约:确保返回类型正确"""
        client = MockHttpClient()

        response = await client.request("GET", "https://api.example.com/test")

        # 验证返回类型
        assert isinstance(response, HttpResponse)
        assert isinstance(response.status_code, int)
        assert isinstance(response.headers, dict)
        # body 可以是任意类型

    def test_abstract_class(self):
        """测试无法直接实例化抽象类"""
        with pytest.raises(TypeError):
            HttpClient()  # type: ignore

    def test_subclass_must_implement_request(self):
        """测试子类必须实现 request 方法"""
        class IncompleteClient(HttpClient):
            pass

        with pytest.raises(TypeError):
            IncompleteClient()  # type: ignore
