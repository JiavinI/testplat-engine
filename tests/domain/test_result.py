"""ApiResult 领域模型单元测试

测试 ApiResult 领域模型的各项功能，包括创建、验证和属性访问。
"""

import pytest

from testplat_engine.domain.result import ApiResult


class TestApiResultCreation:
    """测试 ApiResult 模型的创建"""

    def test_create_success_result(self):
        """测试创建成功的结果"""
        result = ApiResult(
            status="success",
            response={"status_code": 200, "body": {"message": "ok"}},
        )

        assert result.status == "success"
        assert result.response["status_code"] == 200
        assert result.error is None

    def test_create_failed_result(self):
        """测试创建失败的结果"""
        result = ApiResult(
            status="failed",
            response=None,
            error="连接超时",
        )

        assert result.status == "failed"
        assert result.response is None
        assert result.error == "连接超时"

    def test_create_failed_result_with_response(self):
        """测试创建带响应数据的失败结果"""
        result = ApiResult(
            status="failed",
            response={"status_code": 500, "body": "Internal Server Error"},
            error="服务器内部错误",
        )

        assert result.status == "failed"
        assert result.response["status_code"] == 500
        assert result.error == "服务器内部错误"

    def test_success_factory_method(self):
        """测试 success 工厂方法"""
        response = {"status_code": 200, "data": {"id": 1}}
        result = ApiResult.success(response)

        assert result.status == "success"
        assert result.response == response
        assert result.error is None
        assert result.is_success

    def test_failed_factory_method(self):
        """测试 failed 工厂方法"""
        result = ApiResult.failed("网络错误")

        assert result.status == "failed"
        assert result.response is None
        assert result.error == "网络错误"
        assert result.is_failed

    def test_failed_factory_method_with_response(self):
        """测试带响应的 failed 工厂方法"""
        response = {"status_code": 404}
        result = ApiResult.failed("资源不存在", response=response)

        assert result.status == "failed"
        assert result.response == response
        assert result.error == "资源不存在"


class TestApiResultValidation:
    """测试 ApiResult 模型的验证逻辑"""

    def test_invalid_status_raises_error(self):
        """测试无效的状态值会抛出错误"""
        with pytest.raises(ValueError, match="无效的状态值"):
            ApiResult(
                status="pending",
                response=None,
            )

    def test_failed_status_without_error_raises_error(self):
        """测试 failed 状态但没有错误信息会抛出错误"""
        with pytest.raises(ValueError, match="状态为 'failed' 时必须提供 error 信息"):
            ApiResult(
                status="failed",
                response=None,
            )

    def test_success_status_clears_error(self):
        """测试 success 状态会清空错误信息"""
        # 注意：这个测试验证即使传入了 error，success 状态也会清空它
        result = ApiResult(
            status="success",
            response={"status_code": 200},
            error="这个会被清空",
        )

        assert result.error is None


class TestApiResultProperties:
    """测试 ApiResult 模型的属性和方法"""

    def test_is_success_property_when_success(self):
        """测试成功状态的 is_success 属性"""
        result = ApiResult.success({"status_code": 200})

        assert result.is_success is True
        assert result.is_failed is False

    def test_is_success_property_when_failed(self):
        """测试失败状态的 is_success 属性"""
        result = ApiResult.failed("错误")

        assert result.is_success is False
        assert result.is_failed is True

    def test_is_failed_property_when_success(self):
        """测试成功状态的 is_failed 属性"""
        result = ApiResult.success({"status_code": 200})

        assert result.is_failed is False

    def test_is_failed_property_when_failed(self):
        """测试失败状态的 is_failed 属性"""
        result = ApiResult.failed("错误")

        assert result.is_failed is True

    def test_to_dict_contains_all_fields_success(self):
        """测试成功结果的 to_dict 包含所有字段"""
        result = ApiResult(
            status="success",
            response={
                "status_code": 201,
                "headers": {"Content-Type": "application/json"},
                "body": {"id": 1, "name": "test"},
            },
        )

        result_dict = result.to_dict()

        assert result_dict["status"] == "success"
        assert result_dict["response"]["status_code"] == 201
        assert result_dict["error"] is None

    def test_to_dict_contains_all_fields_failed(self):
        """测试失败结果的 to_dict 包含所有字段"""
        result = ApiResult(
            status="failed",
            response={"status_code": 500},
            error="服务器错误",
        )

        result_dict = result.to_dict()

        assert result_dict["status"] == "failed"
        assert result_dict["response"]["status_code"] == 500
        assert result_dict["error"] == "服务器错误"

    def test_to_dict_returns_dict_type(self):
        """测试 to_dict 返回字典类型"""
        result = ApiResult.success({"data": "test"})

        result_dict = result.to_dict()
        assert isinstance(result_dict, dict)
        assert len(result_dict) == 3  # status, response, error


class TestApiResultEdgeCases:
    """测试 ApiResult 模型的边界情况"""

    def test_response_can_be_none_for_success(self):
        """测试成功结果的响应可以为 None"""
        result = ApiResult.success(None)

        assert result.is_success
        assert result.response is None

    def test_response_can_be_any_type(self):
        """测试响应可以是任意类型"""
        # 字符串响应
        result1 = ApiResult.success("OK")
        assert result1.response == "OK"

        # 数字响应
        result2 = ApiResult.success(200)
        assert result2.response == 200

        # 列表响应
        result3 = ApiResult.success([1, 2, 3])
        assert result3.response == [1, 2, 3]

        # 复杂对象响应
        result4 = ApiResult.success(
            {
                "status_code": 200,
                "headers": {"Content-Type": "application/json"},
                "body": {"users": [{"id": 1}, {"id": 2}]},
            }
        )
        assert result4.response["body"]["users"][0]["id"] == 1

    def test_error_message_can_be_multiline(self):
        """测试错误信息可以是多行文本"""
        error_message = """连接失败:
        - 原因: 网络超时
        - 重试次数: 3
        - 建议: 检查网络连接"""

        result = ApiResult.failed(error_message)

        assert "连接失败" in result.error
        assert "网络超时" in result.error

    def test_complex_response_structure(self):
        """测试复杂的响应结构"""
        complex_response = {
            "status_code": 200,
            "headers": {
                "Content-Type": "application/json",
                "X-Request-Id": "abc123",
            },
            "body": {
                "data": {
                    "users": [
                        {"id": 1, "name": "Alice", "roles": ["admin", "user"]},
                        {"id": 2, "name": "Bob", "roles": ["user"]},
                    ],
                    "pagination": {
                        "page": 1,
                        "page_size": 10,
                        "total": 2,
                    },
                },
                "meta": {"timestamp": "2024-01-01T00:00:00Z"},
            },
        }

        result = ApiResult.success(complex_response)

        assert result.response["body"]["data"]["users"][0]["name"] == "Alice"
        assert result.response["body"]["data"]["pagination"]["total"] == 2

    def test_failed_result_with_partial_response(self):
        """测试失败结果带部分响应数据"""
        # 模拟请求部分成功但最终失败的场景
        partial_response = {
            "status_code": 422,
            "headers": {"Content-Type": "application/json"},
            "body": {
                "errors": [
                    {"field": "email", "message": "无效的邮箱格式"},
                    {"field": "password", "message": "密码长度不足"},
                ]
            },
        }

        result = ApiResult.failed("请求验证失败", response=partial_response)

        assert result.is_failed
        assert result.response["status_code"] == 422
        assert len(result.response["body"]["errors"]) == 2
        assert "验证失败" in result.error


class TestApiResultUsagePatterns:
    """测试 ApiResult 的常见使用模式"""

    def test_pattern_check_result_and_process(self):
        """测试检查结果并处理的模式"""
        # 成功场景
        success_result = ApiResult.success({"data": {"user_id": 123}})

        if success_result.is_success:
            user_id = success_result.response["data"]["user_id"]
            assert user_id == 123

        # 失败场景
        failed_result = ApiResult.failed("数据库错误")

        if failed_result.is_failed:
            assert "错误" in failed_result.error

    def test_pattern_convert_to_dict_for_serialization(self):
        """测试转换为字典用于序列化的模式"""
        result = ApiResult.success({"status_code": 200})

        # 模拟序列化场景
        serialized = result.to_dict()

        assert serialized["status"] == "success"
        assert "response" in serialized
        assert "error" in serialized

    def test_pattern_factory_methods_for_clean_api(self):
        """测试使用工厂方法提供清晰 API 的模式"""
        # 模拟 API 调用函数返回 ApiResult
        def mock_api_call(should_succeed: bool) -> ApiResult:
            if should_succeed:
                return ApiResult.success({"data": "ok"})
            else:
                return ApiResult.failed("模拟错误")

        success = mock_api_call(True)
        assert success.is_success

        failure = mock_api_call(False)
        assert failure.is_failed
