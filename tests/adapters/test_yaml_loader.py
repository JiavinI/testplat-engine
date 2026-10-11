"""YAML 加载器测试

测试 YAML 资产加载器的各项功能,包括文件加载、数据转换和错误处理。
"""

from pathlib import Path

import pytest

from testplat_engine.adapters.filesystem.registry import Registry
from testplat_engine.adapters.filesystem.yaml_loader import load_yaml, yaml_to_api
from testplat_engine.domain.api import Api


class TestLoadYaml:
    """测试 load_yaml 函数"""

    def test_load_valid_yaml(self, tmp_path: Path):
        """测试加载有效的 YAML 文件"""
        yaml_content = """
id: api.test.example
name: 测试 API
method: GET
url: /test
host: test_server
request:
  headers:
    Content-Type: application/json
success:
  status_code: 200
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content, encoding='utf-8')

        data = load_yaml(yaml_file)

        assert data['id'] == 'api.test.example'
        assert data['name'] == '测试 API'
        assert data['method'] == 'GET'
        assert data['url'] == '/test'
        assert data['host'] == 'test_server'
        assert data['request']['headers']['Content-Type'] == 'application/json'
        assert data['success']['status_code'] == 200

    def test_load_nonexistent_file(self, tmp_path: Path):
        """测试加载不存在的文件"""
        yaml_file = tmp_path / "nonexistent.yaml"

        with pytest.raises(FileNotFoundError, match="文件不存在"):
            load_yaml(yaml_file)

    def test_load_empty_yaml(self, tmp_path: Path):
        """测试加载空 YAML 文件"""
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("", encoding='utf-8')

        with pytest.raises(ValueError, match="YAML 文件为空"):
            load_yaml(yaml_file)

    def test_load_invalid_yaml_format(self, tmp_path: Path):
        """测试加载格式错误的 YAML 文件"""
        yaml_content = """
id: test
  invalid indentation:
name: test
"""
        yaml_file = tmp_path / "invalid.yaml"
        yaml_file.write_text(yaml_content, encoding='utf-8')

        with pytest.raises(ValueError, match="YAML 格式错误"):
            load_yaml(yaml_file)

    def test_load_yaml_with_list_root(self, tmp_path: Path):
        """测试加载根节点为列表的 YAML 文件"""
        yaml_content = """
- item1
- item2
"""
        yaml_file = tmp_path / "list.yaml"
        yaml_file.write_text(yaml_content, encoding='utf-8')

        with pytest.raises(ValueError, match="YAML 根节点必须是字典"):
            load_yaml(yaml_file)


class TestYamlToApi:
    """测试 yaml_to_api 函数"""

    def test_convert_valid_data(self):
        """测试转换有效的 YAML 数据"""
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

    def test_convert_with_default_values(self):
        """测试转换使用默认值的数据"""
        data = {
            'id': 'api.test.minimal',
            'name': '最小 API',
            'method': 'GET',
            'url': '/minimal',
            'host': 'server'
        }

        api = yaml_to_api(data)

        assert api.request == {}
        assert api.success == {}

    def test_convert_missing_required_field(self):
        """测试转换缺少必需字段的数据"""
        data = {
            'id': 'api.test.incomplete',
            'name': '不完整 API',
            'method': 'GET'
            # 缺少 url 和 host
        }

        with pytest.raises(ValueError, match="缺少必需字段"):
            yaml_to_api(data)

    def test_convert_invalid_request_type(self):
        """测试转换 request 字段类型错误"""
        data = {
            'id': 'api.test.invalid',
            'name': '无效 API',
            'method': 'GET',
            'url': '/test',
            'host': 'server',
            'request': 'invalid'  # 应该是字典
        }

        with pytest.raises(ValueError, match="request 必须是字典类型"):
            yaml_to_api(data)

    def test_convert_invalid_success_type(self):
        """测试转换 success 字段类型错误"""
        data = {
            'id': 'api.test.invalid',
            'name': '无效 API',
            'method': 'GET',
            'url': '/test',
            'host': 'server',
            'success': ['invalid']  # 应该是字典
        }

        with pytest.raises(ValueError, match="success 必须是字典类型"):
            yaml_to_api(data)


class TestRegistry:
    """测试 Registry 类"""

    @pytest.fixture
    def assets_root(self, tmp_path: Path) -> Path:
        """创建测试资产目录结构"""
        assets = tmp_path / "assets"
        assets.mkdir()

        # 创建 api/common 目录
        api_common = assets / "api" / "common"
        api_common.mkdir(parents=True)

        # 创建健康检查 API 文件
        health_check_yaml = """
id: api.common.health_check
name: 健康检查
method: GET
url: /health
host: test_server
request: {}
success:
  status_code: 200
"""
        (api_common / "health_check.yaml").write_text(health_check_yaml, encoding='utf-8')

        # 创建嵌套的 API 文件
        user_api = assets / "api" / "user" / "profile"
        user_api.mkdir(parents=True)
        profile_yaml = """
id: api.user.profile.get
name: 获取用户资料
method: GET
url: /user/profile
host: user_service
request:
  headers:
    Authorization: Bearer token
success:
  status_code: 200
  contains:
    - user_id
"""
        (user_api / "get.yaml").write_text(profile_yaml, encoding='utf-8')

        return assets

    def test_load_api_success(self, assets_root: Path):
        """测试成功加载 API"""
        registry = Registry(assets_root)

        api = registry.load_api("api.common.health_check")

        assert api.id == "api.common.health_check"
        assert api.name == "健康检查"
        assert api.method == "GET"
        assert api.url == "/health"
        assert api.host == "test_server"
        assert api.request == {}
        assert api.success == {"status_code": 200}

    def test_load_nested_api(self, assets_root: Path):
        """测试加载嵌套路径的 API"""
        registry = Registry(assets_root)

        api = registry.load_api("api.user.profile.get")

        assert api.id == "api.user.profile.get"
        assert api.name == "获取用户资料"
        assert api.method == "GET"
        assert api.url == "/user/profile"
        assert api.host == "user_service"
        assert "Authorization" in api.request['headers']
        assert api.success['status_code'] == 200
        assert "user_id" in api.success['contains']

    def test_load_nonexistent_api(self, assets_root: Path):
        """测试加载不存在的 API"""
        registry = Registry(assets_root)

        with pytest.raises(FileNotFoundError, match="文件不存在"):
            registry.load_api("api.nonexistent.api")

    def test_load_api_with_mismatched_id(self, assets_root: Path):
        """测试加载 ID 不匹配的 API"""
        # 创建一个 ID 不匹配的 API 文件
        mismatched_yaml = """
id: api.wrong.id
name: 错误 ID
method: GET
url: /test
host: server
"""
        api_dir = assets_root / "api" / "test"
        api_dir.mkdir(parents=True)
        (api_dir / "file.yaml").write_text(mismatched_yaml, encoding='utf-8')

        registry = Registry(assets_root)

        with pytest.raises(ValueError, match="API 文件中的 id .* 与请求的 id .* 不匹配"):
            registry.load_api("api.test.file")

    def test_api_id_to_path(self, assets_root: Path):
        """测试 API ID 到文件路径的转换"""
        registry = Registry(assets_root)

        path = registry._api_id_to_path("api.common.health_check")

        expected = assets_root / "api" / "common" / "health_check.yaml"
        assert path == expected
