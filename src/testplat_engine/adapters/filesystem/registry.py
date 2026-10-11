"""测试资产注册表

该模块提供测试资产的统一加载和访问入口,支持从文件系统加载各类测试资产。
"""

from pathlib import Path

from testplat_engine.adapters.filesystem.yaml_loader import load_yaml, yaml_to_api
from testplat_engine.domain.api import Api


class Registry:
    """测试资产注册表

    提供统一的测试资产加载接口,支持从文件系统加载 API 契约等资产。

    Attributes:
        assets_root: 资产根目录路径

    Examples:
        >>> registry = Registry(Path("/path/to/assets"))
        >>> api = registry.load_api("api.common.health_check")
        >>> api.id
        'api.common.health_check'
    """

    def __init__(self, assets_root: Path):
        """初始化资产注册表

        Args:
            assets_root: 资产根目录路径
        """
        self.assets_root = Path(assets_root)

    def load_api(self, api_id: str) -> Api:
        """加载 API 契约

        根据 API 标识加载对应的 YAML 文件并转换为 Api 领域模型。

        路径映射规则:
            api_id: "api.common.health_check"
            文件路径: {assets_root}/api/common/health_check.yaml

        Args:
            api_id: API 标识,格式为 "api.{category}.{name}",各部分用点号分隔

        Returns:
            Api 领域模型实例

        Raises:
            FileNotFoundError: API 文件不存在
            ValueError: YAML 格式错误或数据不符合 Api 模型要求

        Examples:
            >>> registry = Registry(Path("/assets"))
            >>> api = registry.load_api("api.common.health_check")
            >>> api.method
            'GET'
        """
        # 将 api_id 转换为文件路径
        # "api.common.health_check" -> "api/common/health_check.yaml"
        file_path = self._api_id_to_path(api_id)

        # 加载 YAML 文件
        data = load_yaml(file_path)

        # 转换为 Api 领域模型
        api = yaml_to_api(data)

        # 验证加载的 API id 与请求的 id 一致
        if api.id != api_id:
            raise ValueError(
                f"API 文件中的 id ({api.id}) 与请求的 id ({api_id}) 不匹配"
            )

        return api

    def _api_id_to_path(self, api_id: str) -> Path:
        """将 API id 转换为文件路径

        Args:
            api_id: API 标识,如 "api.common.health_check"

        Returns:
            完整的文件路径

        Examples:
            >>> registry = Registry(Path("/assets"))
            >>> path = registry._api_id_to_path("api.common.health_check")
            >>> str(path)
            '/assets/api/common/health_check.yaml'
        """
        # 将点号替换为路径分隔符
        parts = api_id.split('.')
        # 添加 .yaml 扩展名
        relative_path = Path(*parts).with_suffix('.yaml')
        # 拼接完整路径
        return self.assets_root / relative_path
