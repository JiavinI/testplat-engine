"""实现验证脚本

验证 YAML 加载器的核心逻辑,不依赖外部库。
"""

import sys
from pathlib import Path

from testplat_engine.domain.api import Api


def test_api_id_to_path_conversion():
    """测试 API ID 到路径的转换逻辑"""
    from testplat_engine.adapters.filesystem.registry import Registry

    assets_root = Path("/assets")
    registry = Registry(assets_root)

    test_cases = [
        ("api.common.health_check", "/assets/api/common/health_check.yaml"),
        ("api.user.profile.get", "/assets/api/user/profile/get.yaml"),
        ("api.test", "/assets/api/test.yaml"),
    ]

    print("测试 API ID 到路径转换:")
    for api_id, expected in test_cases:
        path = registry._api_id_to_path(api_id)
        assert str(path) == expected, f"期望 {expected}, 实际 {path}"
        print(f"  ✓ {api_id} -> {path}")

    print("\n✓ 路径转换测试通过")


def test_yaml_to_api_conversion():
    """测试 YAML 数据到 Api 模型的转换逻辑"""
    from testplat_engine.adapters.filesystem.yaml_loader import yaml_to_api

    print("\n测试 YAML 数据转换:")

    # 测试完整数据
    data = {
        'id': 'api.test.example',
        'name': '测试 API',
        'method': 'POST',
        'url': '/api/test',
        'host': 'test_server',
        'request': {
            'headers': {'Content-Type': 'application/json'},
            'body': {'key': 'value'}
        },
        'success': {'status_code': 201}
    }

    api = yaml_to_api(data)
    assert isinstance(api, Api)
    assert api.id == 'api.test.example'
    assert api.name == '测试 API'
    assert api.method == 'POST'
    assert api.url == '/api/test'
    assert api.host == 'test_server'
    assert api.request == data['request']
    assert api.success == data['success']
    print("  ✓ 完整数据转换成功")

    # 测试默认值
    minimal_data = {
        'id': 'api.test.minimal',
        'name': '最小 API',
        'method': 'GET',
        'url': '/minimal',
        'host': 'server'
    }

    api = yaml_to_api(minimal_data)
    assert api.request == {}
    assert api.success == {}
    print("  ✓ 默认值处理正确")

    # 测试错误处理: 缺少字段
    try:
        invalid_data = {'id': 'test', 'name': 'test'}
        yaml_to_api(invalid_data)
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "缺少必需字段" in str(e)
        print("  ✓ 缺少字段错误检测正确")

    # 测试错误处理: 类型错误
    try:
        invalid_data = {
            'id': 'test',
            'name': 'test',
            'method': 'GET',
            'url': '/test',
            'host': 'server',
            'request': 'invalid'
        }
        yaml_to_api(invalid_data)
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "request 必须是字典类型" in str(e)
        print("  ✓ 类型错误检测正确")

    print("\n✓ 数据转换测试通过")


def test_api_model_validation():
    """测试 Api 模型的验证逻辑"""
    print("\n测试 Api 模型验证:")

    # 测试有效数据
    api = Api(
        id='api.test.valid',
        name='有效 API',
        method='get',  # 小写会被规范化
        url='/test',
        host='server',
        request={},
        success={}
    )
    assert api.method == 'GET'  # 验证大写规范化
    print("  ✓ 有效数据创建成功")
    print(f"  ✓ HTTP 方法规范化: get -> {api.method}")

    # 测试无效 HTTP 方法
    try:
        Api(
            id='test',
            name='test',
            method='INVALID',
            url='/test',
            host='server',
            request={},
            success={}
        )
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "无效的 HTTP 方法" in str(e)
        print("  ✓ 无效 HTTP 方法检测正确")

    # 测试空字段
    try:
        Api(
            id='',
            name='test',
            method='GET',
            url='/test',
            host='server',
            request={},
            success={}
        )
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "id 不能为空" in str(e)
        print("  ✓ 空字段检测正确")

    print("\n✓ 模型验证测试通过")


def main():
    """运行所有验证测试"""
    print("=" * 60)
    print("YAML 加载器实现验证")
    print("=" * 60)

    try:
        test_api_id_to_path_conversion()
        test_yaml_to_api_conversion()
        test_api_model_validation()

        print("\n" + "=" * 60)
        print("✓ 所有验证测试通过!")
        print("=" * 60)
        return 0
    except Exception as e:
        print(f"\n✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
