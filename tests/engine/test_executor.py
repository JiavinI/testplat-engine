"""执行器的本地 HTTP 集成测试。"""

import json
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from testplat_engine.adapters.filesystem import Registry
from testplat_engine.engine.executor import run_api

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "assets"


class _Handler(BaseHTTPRequestHandler):
    """为测试提供确定性的本地响应，不触发真实网络。"""

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP handler API
        self._send(200, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802 - inherited HTTP handler API
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self._send(200, {"json": payload})

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def local_server():
    """启动随机本地端口，避免测试依赖公网或固定端口。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def registry(tmp_path: Path, local_server: str) -> Registry:
    """复制仓库 fixture，并只替换环境文件中的本地服务地址。"""
    assets_root = tmp_path / "assets"
    shutil.copytree(FIXTURE_ROOT, assets_root)
    config = assets_root / "config" / "envs" / "test.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("BASE_URL", local_server), encoding="utf-8"
    )
    return Registry(assets_root=assets_root)


@pytest.mark.asyncio
async def test_run_api_get_uses_local_server(registry: Registry):
    result = await run_api("api.common.get_status", registry, "test")

    assert result.is_success
    assert result.response.status_code == 200


@pytest.mark.asyncio
async def test_run_api_post_resolves_params_locally(registry: Registry):
    result = await run_api(
        "api.common.post_json",
        registry,
        "test",
        {"username": "local-user", "email": "local@example.com"},
    )

    assert result.is_success
    assert result.response.body["json"]["username"] == "local-user"
