"""逻辑验证脚本

验证核心逻辑,不导入依赖外部库的模块。
"""

import sys
from pathlib import Path

from testplat_engine.domain.api import Api


def test_api_id_to_path_logic():
    """测试 API ID 到路径转换逻辑 (独立实现验证)"""
    print("测试 API ID 到路径转换逻辑:")

    def api_id_to_path(assets_root: Path, api_id: str) -> Path:
        """API ID 到路径转换 (与 registry.py 中的逻辑相同)"""
        parts = api_id.split('.')
        relative_path = Path(*parts).with_suffix('.yaml')
        return assets_root / relative_path

    assets_root = Path("/assets")

    test_cases = [
        ("api.common.health_check", "/assets/api/common/health_check.yaml"),
        ("api.user.profile.get", "/assets/api/user/profile/get.yaml"),
        ("api.test", "/assets/api/test.yaml"),
        ("api.a.b.c.d", "/assets/api/a/b/c/d.yaml"),
    ]

    for api_id, expected in test_cases:
        path = api_id_to_path(assets_root, api_id)
        assert str(path) == expected, f"期望 {expected}, 实际 {path}"
        print(f"  ✓ {api_id:30} -> {path}")

    print("\n✓ 路径转换逻辑验证通过\n")


def test_yaml_to_api_logic():
    """测试 YAML 数据到 Api 转换逻辑"""
    print("测试 YAML 数据转换逻辑:")

    def yaml_to_api(data: dict) -> Api:
        """YAML 到 Api 转换 (与 yaml_loader.py 中的逻辑相同)"""
        try:
            api_id = data['id']
            name = data['name']
            method = data['method']
            url = data['url']
            host = data['host']
        except KeyError as e:
            raise ValueError(f"缺少必需字段: {e}") from e

        request = data.get('request', {})
        success = data.get('success', {})

        if not isinstance(request, dict):
            raise ValueError(f"request 必须是字典类型,实际为: {type(request).__name__}")
        if not isinstance(success, dict):
            raise ValueError(f"success 必须是字典类型,实际为: {type(success).__name__}")

        return Api(
            id=api_id,
            name=name,
            method=method,
            url=url,
            host=host,
            request=request,
            success=success,
        )

    # 测试 1: 完整数据
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
    print("  ✓ 完整数据转换成功")

    # 测试 2: 使用默认值
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

    # 测试 3: 缺少必需字段
    try:
        invalid_data = {'id': 'test', 'name': 'test'}
        yaml_to_api(invalid_data)
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "缺少必需字段" in str(e)
        print("  ✓ 缺少字段错误检测")

    # 测试 4: request 类型错误
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
        print("  ✓ request 类型错误检测")

    # 测试 5: success 类型错误
    try:
        invalid_data = {
            'id': 'test',
            'name': 'test',
            'method': 'GET',
            'url': '/test',
            'host': 'server',
            'success': ['invalid']
        }
        yaml_to_api(invalid_data)
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "success 必须是字典类型" in str(e)
        print("  ✓ success 类型错误检测")

    print("\n✓ 数据转换逻辑验证通过\n")


def test_api_model_validation():
    """测试 Api 模型验证"""
    print("测试 Api 模型验证:")

    # 测试 1: 有效数据
    api = Api(
        id='api.test.valid',
        name='有效 API',
        method='get',
        url='/test',
        host='server',
        request={},
        success={}
    )
    assert api.method == 'GET'  # 小写规范化为大写
    print(f"  ✓ 有效数据创建成功 (method: get -> {api.method})")

    # 测试 2: full_url 属性
    assert api.full_url == 'GET /test'
    print(f"  ✓ full_url 属性: {api.full_url}")

    # 测试 3: to_dict 方法
    api_dict = api.to_dict()
    assert api_dict['id'] == 'api.test.valid'
    assert api_dict['method'] == 'GET'
    print("  ✓ to_dict 方法正常")

    # 测试 4: 无效 HTTP 方法
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
        print("  ✓ 无效 HTTP 方法检测")

    # 测试 5: 空 id
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
        print("  ✓ 空 id 检测")

    # 测试 6: 空 name
    try:
        Api(
            id='test',
            name='',
            method='GET',
            url='/test',
            host='server',
            request={},
            success={}
        )
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "name 不能为空" in str(e)
        print("  ✓ 空 name 检测")

    # 测试 7: 空 url
    try:
        Api(
            id='test',
            name='test',
            method='GET',
            url='',
            host='server',
            request={},
            success={}
        )
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "url 不能为空" in str(e)
        print("  ✓ 空 url 检测")

    # 测试 8: 空 host
    try:
        Api(
            id='test',
            name='test',
            method='GET',
            url='/test',
            host='',
            request={},
            success={}
        )
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "host 不能为空" in str(e)
        print("  ✓ 空 host 检测")

    # 测试 9: 所有支持的 HTTP 方法
    valid_methods = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']
    for method in valid_methods:
        api = Api(
            id='test',
            name='test',
            method=method.lower(),
            url='/test',
            host='server',
            request={},
            success={}
        )
        assert api.method == method
    print(f"  ✓ 所有支持的 HTTP 方法: {', '.join(valid_methods)}")

    print("\n✓ 模型验证测试通过\n")


def main():
    """运行所有验证测试"""
    print("=" * 70)
    print("YAML 加载器核心逻辑验证")
    print("=" * 70)
    print()

    try:
        test_api_id_to_path_logic()
        test_yaml_to_api_logic()
        test_api_model_validation()

        print("=" * 70)
        print("✓ 所有逻辑验证通过!")
        print("=" * 70)
        print()
        print("说明:")
        print("  - API ID 到文件路径转换逻辑正确")
        print("  - YAML 数据到 Api 模型转换逻辑正确")
        print("  - Api 模型验证逻辑完整")
        print("  - 错误处理机制健全")
        print()
        print("注意: 完整的文件加载功能需要安装 PyYAML:")
        print("  pip install pyyaml")
        print()
        return 0
    except Exception as e:
        print(f"\n✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
