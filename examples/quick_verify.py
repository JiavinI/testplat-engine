"""快速验证不需要网络的核心模块。"""

from testplat_engine.domain.api import Api
from testplat_engine.domain.result import ApiResult
from testplat_engine.engine.assert_engine import check_success
from testplat_engine.engine.resolver import resolve_variables
from testplat_engine.ports.http_client import HttpResponse


def main() -> int:
    """按领域模型、端口、变量解析和断言顺序执行最小验证。"""
    print("✅ 1. 验证 domain 层...")
    test_api = Api("api.test", "test", "GET", "/test", "host", {}, {})
    test_result = ApiResult.success({"ok": True})
    print(f"   Api 模型: {test_api.id}")
    print(f"   ApiResult 模型: {test_result.status}")

    print("\n✅ 2. 验证 ports 层...")
    print("   HttpClient 接口已定义")

    print("\n✅ 3. 验证 resolver (不依赖外部)...")
    resolved = resolve_variables(
        Api("api.t", "t", "POST", "/api", "h", {"body": {"user": "${name}"}}, {}),
        {"name": "alice"},
    )
    assert resolved["body"]["user"] == "alice"
    print(f"   变量解析正常: {resolved}")

    print("\n✅ 4. 验证 assert_engine...")
    response = HttpResponse(200, {}, {"code": "0"})
    assert check_success(response, {"path": "$.code", "value": "0"})
    print("   断言检查正常")
    print("\n🎉 核心模块验证通过!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
