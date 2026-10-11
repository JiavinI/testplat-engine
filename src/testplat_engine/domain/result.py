"""API 执行结果领域模型

该模块定义了 API 执行结果的核心领域模型，用于描述 API 调用的执行状态、响应数据和错误信息。
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ApiResult:
    """API 执行结果领域模型

    表示一个 API 调用的执行结果，包含执行状态、响应数据和可选的错误信息。

    Attributes:
        status: 执行状态，可选值:
            - "success": 执行成功
            - "failed": 执行失败
        response: HTTP 响应数据，可以是任意类型
            通常包含 status_code, headers, body 等字段
        error: 错误信息，仅在 status 为 "failed" 时有值
            包含失败原因的描述性文本

    Examples:
        >>> # 成功的结果
        >>> result = ApiResult(
        ...     status="success",
        ...     response={"status_code": 200, "body": {"message": "ok"}}
        ... )
        >>> result.is_success
        True

        >>> # 失败的结果
        >>> result = ApiResult(
        ...     status="failed",
        ...     response=None,
        ...     error="连接超时"
        ... )
        >>> result.is_success
        False
        >>> result.error
        '连接超时'
    """

    status: str
    response: Any
    error: str | None = None

    def __post_init__(self) -> None:
        """初始化后验证

        验证状态字段的有效性。

        Raises:
            ValueError: 当状态值无效时抛出
        """
        valid_statuses = {"success", "failed"}
        if self.status not in valid_statuses:
            raise ValueError(
                f"无效的状态值: {self.status}. "
                f"支持的状态: {', '.join(sorted(valid_statuses))}"
            )

        # 如果状态是 failed，确保有错误信息
        if self.status == "failed" and not self.error:
            raise ValueError("状态为 'failed' 时必须提供 error 信息")

        # 如果状态是 success，清空错误信息
        if self.status == "success":
            object.__setattr__(self, 'error', None)

    @property
    def is_success(self) -> bool:
        """判断是否执行成功

        Returns:
            True 表示成功，False 表示失败
        """
        return self.status == "success"

    @property
    def is_failed(self) -> bool:
        """判断是否执行失败

        Returns:
            True 表示失败，False 表示成功
        """
        return self.status == "failed"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式

        Returns:
            包含所有字段的字典
        """
        return {
            "status": self.status,
            "response": self.response,
            "error": self.error,
        }

    @classmethod
    def success(cls, response: Any) -> "ApiResult":
        """创建成功的结果

        Args:
            response: 响应数据

        Returns:
            状态为 success 的 ApiResult 实例

        Examples:
            >>> result = ApiResult.success({"status_code": 200})
            >>> result.is_success
            True
        """
        return cls(status="success", response=response, error=None)

    @classmethod
    def failed(cls, error: str, response: Any = None) -> "ApiResult":
        """创建失败的结果

        Args:
            error: 错误信息
            response: 可选的响应数据

        Returns:
            状态为 failed 的 ApiResult 实例

        Examples:
            >>> result = ApiResult.failed("连接超时")
            >>> result.is_failed
            True
            >>> result.error
            '连接超时'
        """
        return cls(status="failed", response=response, error=error)
