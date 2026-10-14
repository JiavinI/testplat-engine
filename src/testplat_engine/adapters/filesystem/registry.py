"""测试资产注册表。

提供从文件系统加载 API / Case 契约的统一入口。路径解析必须限制在
``assets_root`` 之内，防止 id 通过 ``..`` 或分隔符逃逸到资产目录外。
"""

from pathlib import Path

from testplat_engine.adapters.filesystem.case_loader import load_case as _load_case
from testplat_engine.adapters.filesystem.yaml_loader import load_yaml, yaml_to_api
from testplat_engine.domain.api import Api
from testplat_engine.domain.case import Case


class Registry:
    """测试资产注册表。

    Attributes:
        assets_root: 资产根目录路径；所有加载结果必须落在此目录树内。
    """

    def __init__(self, assets_root: Path):
        """初始化资产注册表。

        Args:
            assets_root: 资产根目录路径
        """
        self.assets_root = Path(assets_root)

    def load_api(self, api_id: str) -> Api:
        """加载 API 契约。

        路径映射规则:
            api_id: ``api.common.health_check``
            文件路径: ``{assets_root}/api/common/health_check.yaml``

        Raises:
            FileNotFoundError: API 文件不存在
            ValueError: api_id 非法、YAML 无效、或文件内 id 与请求不一致
        """
        file_path = self._api_id_to_path(api_id)
        data = load_yaml(file_path)
        api = yaml_to_api(data)

        if api.id != api_id:
            raise ValueError(f"API 文件中的 id ({api.id}) 与请求的 id ({api_id}) 不匹配")

        return api

    def load_case(self, case_id: str) -> Case:
        """加载 Case 资产；委托 case_loader，抛出公开 EngineError 族。"""
        return _load_case(case_id, self.assets_root)

    def _api_id_to_path(self, api_id: str) -> Path:
        """将 API id 转为文件路径，并强制结果位于 assets_root 内。

        拒绝含路径分隔符或 ``..`` 段的 api_id，避免目录穿越。
        """
        if not api_id or not isinstance(api_id, str):
            raise ValueError("api_id 不能为空")
        if "/" in api_id or "\\" in api_id:
            raise ValueError(f"api_id 不得包含路径分隔符: {api_id}")

        parts = api_id.split(".")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"api_id 含有非法路径段: {api_id}")

        relative_path = Path(*parts).with_suffix(".yaml")
        candidate = (self.assets_root / relative_path).resolve()
        assets_root = self.assets_root.resolve()

        # Python 3.11+ Path.is_relative_to；显式校验防止符号链接脱出根目录。
        if not candidate.is_relative_to(assets_root):
            raise ValueError(f"api_id 解析后超出 assets_root: {api_id} -> {candidate}")

        return candidate
