"""API 领域模型单元测试

测试 Api 领域模型的各项功能，包括创建、验证和属性访问。
"""

import pytest

from testplat_engine.domain.api import Api


class TestApiCreation:
    """测试 Api 模型的创建"""

    def test_create_valid_api(self):
        """测试创建有效的 API 模型"""
        api = Api(
            id="api.common.health_check",
            name="健康检查",
            method="GET",
            url="/health",
            host="test_server",
            request={},
            success={"status_code": 200},
        )

        assert api.id == "api.common.health_check"
        assert api.name == "健康检查"
        assert api.method == "GET"
        assert api.url == "/health"
        assert api.host == "test_server"
        assert api.request == {}
        assert api.success == {"status_code": 200}

    def test_create_api_with_request_body(self):
        """测试创建带请求体的 API 模型"""
        api = Api(
            id="api.user.create",
            name="创建用户",
            method="POST",
            url="/users",
            host="api_server",
            request={
                "headers": {"Content-Type": "application/json"},
                "body": {"username": "test", "email": "test@example.com"},
            },
            success={"status_code": 201},
        )

        assert api.method == "POST"
        assert api.request["body"]["username"] == "test"

    def test_method_normalized_to_uppercase(self):
        """测试 HTTP 方法会被规范化为大写"""
        api = Api(
            id="test",
            name="test",
            method="get",
            url="/test",
            host="server",
            request={},
            success={},
        )

        assert api.method == "GET"

    def test_all_http_methods_supported(self):
        """测试支持所有常见的 HTTP 方法"""
        methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

        for method in methods:
            api = Api(
                id=f"test_{method}",
                name=f"test {method}",
                method=method,
                url="/test",
                host="server",
                request={},
                success={},
            )
            assert api.method == method.upper()


class TestApiValidation:
    """测试 Api 模型的验证逻辑"""

    def test_empty_id_raises_error(self):
        """测试空 ID 会抛出错误"""
        with pytest.raises(ValueError, match="API id 不能为空"):
            Api(
                id="",
                name="test",
                method="GET",
                url="/test",
                host="server",
                request={},
                success={},
            )

    def test_empty_name_raises_error(self):
        """测试空名称会抛出错误"""
        with pytest.raises(ValueError, match="API name 不能为空"):
            Api(
                id="test",
                name="",
                method="GET",
                url="/test",
                host="server",
                request={},
                success={},
            )

    def test_empty_method_raises_error(self):
        """测试空方法会抛出错误"""
        with pytest.raises(ValueError, match="API method 不能为空"):
            Api(
                id="test",
                name="test",
                method="",
                url="/test",
                host="server",
                request={},
                success={},
            )

    def test_invalid_method_raises_error(self):
        """测试无效的 HTTP 方法会抛出错误"""
        with pytest.raises(ValueError, match="无效的 HTTP 方法"):
            Api(
                id="test",
                name="test",
                method="INVALID",
                url="/test",
                host="server",
                request={},
                success={},
            )

    def test_empty_url_raises_error(self):
        """测试空 URL 会抛出错误"""
        with pytest.raises(ValueError, match="API url 不能为空"):
            Api(
                id="test",
                name="test",
                method="GET",
                url="",
                host="server",
                request={},
                success={},
            )

    def test_empty_host_raises_error(self):
        """测试空 host 会抛出错误"""
        with pytest.raises(ValueError, match="API host 不能为空"):
            Api(
                id="test",
                name="test",
                method="GET",
                url="/test",
                host="",
                request={},
                success={},
            )


class TestApiProperties:
    """测试 Api 模型的属性和方法"""

    def test_full_url_property(self):
        """测试 full_url 属性返回正确的格式"""
        api = Api(
            id="test",
            name="test",
            method="GET",
            url="/api/v1/users",
            host="server",
            request={},
            success={},
        )

        assert api.full_url == "GET /api/v1/users"

    def test_full_url_with_post_method(self):
        """测试 POST 方法的 full_url"""
        api = Api(
            id="test",
            name="test",
            method="POST",
            url="/api/v1/users",
            host="server",
            request={},
            success={},
        )

        assert api.full_url == "POST /api/v1/users"

    def test_to_dict_contains_all_fields(self):
        """测试 to_dict 方法包含所有字段"""
        api = Api(
            id="api.user.get",
            name="获取用户",
            method="GET",
            url="/users/123",
            host="api_server",
            request={"headers": {"Authorization": "Bearer token"}},
            success={"status_code": 200, "contains": ["id", "username"]},
        )

        result = api.to_dict()

        assert result["id"] == "api.user.get"
        assert result["name"] == "获取用户"
        assert result["method"] == "GET"
        assert result["url"] == "/users/123"
        assert result["host"] == "api_server"
        assert result["request"] == {"headers": {"Authorization": "Bearer token"}}
        assert result["success"] == {"status_code": 200, "contains": ["id", "username"]}

    def test_to_dict_returns_dict_type(self):
        """测试 to_dict 返回字典类型"""
        api = Api(
            id="test",
            name="test",
            method="GET",
            url="/test",
            host="server",
            request={},
            success={},
        )

        result = api.to_dict()
        assert isinstance(result, dict)
        assert len(result) == 7  # 7 个字段


class TestApiEdgeCases:
    """测试 Api 模型的边界情况"""

    def test_complex_nested_request_structure(self):
        """测试复杂的嵌套请求结构"""
        api = Api(
            id="api.order.create",
            name="创建订单",
            method="POST",
            url="/orders",
            host="api_server",
            request={
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer token",
                },
                "body": {
                    "items": [
                        {"product_id": 1, "quantity": 2},
                        {"product_id": 2, "quantity": 1},
                    ],
                    "shipping": {"address": "123 Main St", "city": "Beijing"},
                },
                "query_params": {"discount_code": "SAVE10"},
            },
            success={
                "status_code": 201,
                "response_body": {"contains": ["order_id", "total_price"]},
            },
        )

        assert len(api.request["body"]["items"]) == 2
        assert api.request["body"]["shipping"]["city"] == "Beijing"

    def test_url_with_path_parameters(self):
        """测试带路径参数的 URL"""
        api = Api(
            id="api.user.get_by_id",
            name="根据ID获取用户",
            method="GET",
            url="/users/{user_id}",
            host="api_server",
            request={},
            success={},
        )

        assert "{user_id}" in api.url

    def test_api_with_empty_success_conditions(self):
        """测试空的成功条件"""
        api = Api(
            id="test",
            name="test",
            method="GET",
            url="/test",
            host="server",
            request={},
            success={},
        )

        assert api.success == {}
        assert isinstance(api.success, dict)
