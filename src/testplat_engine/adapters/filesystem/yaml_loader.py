"""YAML 资产加载器

该模块提供从 YAML 文件加载测试资产的功能,支持加载 API 契约等资产定义。
"""

from pathlib import Path
from typing import Any

import yaml

from testplat_engine.domain.api import Api


def load_yaml(file_path: Path) -> dict[str, Any]:
    """加载 YAML 文件

    Args:
        file_path: YAML 文件路径

    Returns:
        解析后的字典数据

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: YAML 格式错误
    """
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    try:
        with open(file_path, encoding='utf-8') as f:
            data = yaml.safe_load(f)
            if data is None:
                raise ValueError(f"YAML 文件为空: {file_path}")
            if not isinstance(data, dict):
                raise ValueError(f"YAML 根节点必须是字典: {file_path}")
            return data
    except yaml.YAMLError as e:
        raise ValueError(f"YAML 格式错误: {file_path}, {e}") from e


def yaml_to_api(data: dict[str, Any]) -> Api:
    """将 YAML 数据转换为 Api 领域模型

    Args:
        data: YAML 数据字典

    Returns:
        Api 领域模型实例

    Raises:
        ValueError: 数据格式不符合要求
    """
    # 提取必需字段
    try:
        api_id = data['id']
        name = data['name']
        method = data['method']
        url = data['url']
        host = data['host']
    except KeyError as e:
        raise ValueError(f"缺少必需字段: {e}") from e

    # 提取可选字段,使用默认值
    request = data.get('request', {})
    success = data.get('success', {})

    # 验证数据类型
    if not isinstance(request, dict):
        raise ValueError(f"request 必须是字典类型,实际为: {type(request).__name__}")
    if not isinstance(success, dict):
        raise ValueError(f"success 必须是字典类型,实际为: {type(success).__name__}")

    # 创建 Api 实例 (会在 __post_init__ 中进行字段验证)
    return Api(
        id=api_id,
        name=name,
        method=method,
        url=url,
        host=host,
        request=request,
        success=success,
    )
