"""API 契约领域模型

该模块定义了 API 契约的核心领域模型，用于描述 API 的基本信息、请求定义和成功判定条件。
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class Api:
    """API 契约领域模型

    表示一个完整的 API 契约定义，包含 API 的标识、访问信息、请求定义和成功判定条件。

    Attributes:
        id: API 的唯一标识符，格式为 {category}.{subcategory}.{name}
            例如: "api.common.health_check"
        name: API 的可读名称，用于显示和日志
            例如: "健康检查"
        method: HTTP 方法，支持 GET/POST/PUT/DELETE/PATCH
            例如: "GET"
        url: API 的相对路径，不包含 host 部分
            例如: "/health"
        host: API 所属的主机标识，对应环境配置中的 host 名称
            例如: "test_server"
        request: 请求定义，包含请求头、请求体、查询参数等
            例如: {"headers": {"Content-Type": "application/json"}, "body": {}}
        success: 成功判定条件，用于判断 API 调用是否成功
            例如: {"status_code": 200, "contains": ["success"]}

    Examples:
        >>> api = Api(
        ...     id="api.common.health_check",
        ...     name="健康检查",
        ...     method="GET",
        ...     url="/health",
        ...     host="test_server",
        ...     request={},
        ...     success={"status_code": 200}
        ... )
        >>> api.id
        'api.common.health_check'
    """

    id: str
    name: str
    method: str
    url: str
    host: str
    request: dict[str, Any]
    success: dict[str, Any]
    # 3.3a：身份声明。None=未声明（走 default_identity）；"none"=显式匿名；其余为身份名。
    # 只存身份名，资产里永远不出现秘密。
    auth: str | None = None

    def __post_init__(self) -> None:
        """初始化后验证

        验证必要字段的有效性。

        Raises:
            ValueError: 当字段值无效时抛出
        """
        if not self.id:
            raise ValueError("API id 不能为空")

        if not self.name:
            raise ValueError("API name 不能为空")

        if not self.method:
            raise ValueError("API method 不能为空")

        valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
        if self.method.upper() not in valid_methods:
            raise ValueError(
                f"无效的 HTTP 方法: {self.method}. "
                f"支持的方法: {', '.join(sorted(valid_methods))}"
            )

        if not self.url:
            raise ValueError("API url 不能为空")

        if not self.host:
            raise ValueError("API host 不能为空")

        if self.auth is not None and (not isinstance(self.auth, str) or not self.auth.strip()):
            # Q6：空串或非字符串不是合法身份声明；消息不回显值。
            raise ValueError("API auth 必须是非空字符串身份名或 none")

        # 规范化 method 为大写
        object.__setattr__(self, 'method', self.method.upper())

    @property
    def full_url(self) -> str:
        """获取完整的 URL 标识

        Returns:
            格式为 "{method} {url}" 的字符串

        Examples:
            >>> api = Api(id="test", name="test", method="GET", url="/health",
            ...           host="server", request={}, success={})
            >>> api.full_url
            'GET /health'
        """
        return f"{self.method} {self.url}"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式

        Returns:
            包含所有字段的字典
        """
        return {
            "id": self.id,
            "name": self.name,
            "method": self.method,
            "url": self.url,
            "host": self.host,
            "request": self.request,
            "success": self.success,
        }
