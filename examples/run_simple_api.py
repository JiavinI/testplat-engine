"""在本地 HTTP 服务上验证单 API 执行。

示例使用临时目录保存仓库内定义的最小资产，避免依赖开发者机器上的相邻目录或公网服务。
"""

import asyncio
import json
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from testplat_engine import run_api
from testplat_engine.adapters.filesystem import Registry


class _Handler(BaseHTTPRequestHandler):
    """提供示例所需的两个确定性 HTTP 响应。"""

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP handler API
        if self.path != "/status/200":
            self.send_error(404)
            return
        self._send_json(200, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802 - inherited HTTP handler API
        if self.path != "/post":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self._send_json(200, {"json": payload})

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _LocalServer:
    """在本机随机端口启动测试服务，并在退出时释放资源。"""

    def __enter__(self) -> str:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def _prepare_assets(root: Path, base_url: str) -> Path:
    """创建示例资产；配置布局与 Registry 的当前查找规则一致。"""
    assets = root / "assets"
    api_dir = assets / "api" / "common"
    config_dir = root / "config" / "envs"
    api_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (config_dir / "test.yaml").write_text(
        f"api_hosts:\n  local: {base_url}\n", encoding="utf-8"
    )
    (api_dir / "get_status.yaml").write_text(
        "id: api.common.get_status\n"
        "name: 获取状态码\n"
        "host: local\n"
        "method: GET\n"
        "url: /status/200\n"
        "success:\n  status_code: 200\n",
        encoding="utf-8",
    )
    (api_dir / "post_json.yaml").write_text(
        "id: api.common.post_json\n"
        "name: POST JSON 数据\n"
        "host: local\n"
        "method: POST\n"
        "url: /post\n"
        "request:\n  body:\n    username: \"${username}\"\n"
        "    email: \"${email}\"\n"
        "success:\n  path: $.json.username\n  value: \"${username}\"\n",
        encoding="utf-8",
    )
    return assets


async def main() -> int:
    """执行 GET 和 POST 两个本地示例。"""
    with _LocalServer() as base_url, tempfile.TemporaryDirectory() as temp_dir:
        assets = _prepare_assets(Path(temp_dir), base_url)
        registry = Registry(assets_root=assets)

        get_result = await run_api("api.common.get_status", registry, "test")
        post_result = await run_api(
            "api.common.post_json",
            registry,
            "test",
            {"username": "testuser", "email": "test@example.com"},
        )

        print(f"GET: {get_result.status}")
        print(f"POST: {post_result.status}")
        if post_result.error:
            print(f"POST error: {post_result.error}")
        return 0 if get_result.is_success and post_result.is_success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
