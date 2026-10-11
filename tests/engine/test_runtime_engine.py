"""SingleApiEngine（EnginePort）真实执行矩阵测试。

全部使用本地 HTTP 服务或注入的 HttpClient，不访问公网。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import httpx
import pytest

from testplat_engine import (
    ApiExecutionOutcome,
    ApiExecutionRequest,
    AssetNotFoundError,
    EngineInternalError,
    EngineTimeoutError,
    EngineTransportError,
    InvalidAssetError,
    InvalidEnvironmentError,
    SingleApiEngine,
)
from testplat_engine.ports.http_client import HttpClient, HttpResponse

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "assets"


class _MatrixHandler(BaseHTTPRequestHandler):
    """为矩阵用例提供确定性本地响应。"""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/status/200":
            self._send(200, {"status": "ok"}, content_type="application/json")
        elif self.path == "/plain":
            self._send(200, "hello-plain", content_type="text/plain")
        elif self.path == "/slow":
            # 配合极短 timeout，验证超时真正传到 HTTP client。
            import time

            time.sleep(2.0)
            self._send(200, {"slow": True}, content_type="application/json")
        elif self.path == "/assert-fail":
            self._send(200, {"status": "bad"}, content_type="application/json")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        self._send(200, {"json": payload}, content_type="application/json")

    def _send(self, status: int, payload: object, *, content_type: str) -> None:
        if content_type == "application/json":
            body = json.dumps(payload).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MatrixHandler)
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
def assets_root(tmp_path: Path) -> Path:
    """复制 fixture 资产到临时目录，并追加矩阵专用 YAML。"""
    root = tmp_path / "assets"
    shutil.copytree(FIXTURE_ROOT, root)
    api_dir = root / "api" / "common"

    (api_dir / "assert_fail.yaml").write_text(
        "id: api.common.assert_fail\n"
        "name: 断言失败样例\n"
        "host: local\n"
        "method: GET\n"
        "url: /assert-fail\n"
        "success:\n  status_code: 200\n  path: $.status\n  value: ok\n",
        encoding="utf-8",
    )
    (api_dir / "plain_text.yaml").write_text(
        "id: api.common.plain_text\n"
        "name: 非 JSON 响应\n"
        "host: local\n"
        "method: GET\n"
        "url: /plain\n"
        "success:\n  status_code: 200\n",
        encoding="utf-8",
    )
    (api_dir / "slow.yaml").write_text(
        "id: api.common.slow\n"
        "name: 慢请求\n"
        "host: local\n"
        "method: GET\n"
        "url: /slow\n"
        "success:\n  status_code: 200\n",
        encoding="utf-8",
    )
    (api_dir / "with_secret_header.yaml").write_text(
        "id: api.common.with_secret_header\n"
        "name: 含敏感头\n"
        "host: local\n"
        "method: GET\n"
        "url: /status/200\n"
        "request:\n"
        "  headers:\n"
        "    Authorization: Bearer secret-token\n"
        "    Cookie: session=abc\n"
        "    X-Trace-Id: public-trace\n"
        "success:\n  status_code: 200\n",
        encoding="utf-8",
    )
    (api_dir / "invalid_missing_fields.yaml").write_text(
        "id: api.common.invalid_missing_fields\n"
        "name: 缺字段\n"
        "method: GET\n",
        encoding="utf-8",
    )
    return root


def _env(base_url: str) -> dict[str, Any]:
    """构造 Control 应传入的已解析环境；Engine 不得再读磁盘配置。"""
    return {"api_hosts": {"local": base_url}}


def _request(
    assets_root: Path,
    api_id: str,
    environment: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
    timeout_seconds: float = 5.0,
) -> ApiExecutionRequest:
    return ApiExecutionRequest(
        api_id=api_id,
        assets_root=assets_root,
        environment=environment,
        params=params or {},
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.asyncio
async def test_success_get(assets_root: Path, local_server: str) -> None:
    engine = SingleApiEngine()
    outcome = await engine.execute(
        _request(assets_root, "api.common.get_status", _env(local_server))
    )

    assert outcome.status == "success"
    assert outcome.error is None
    assert outcome.request is not None
    assert outcome.request["method"] == "GET"
    assert outcome.response is not None
    assert outcome.response["status_code"] == 200
    assert json.loads(json.dumps(outcome.to_dict()))["status"] == "success"


@pytest.mark.asyncio
async def test_success_post_resolves_params(assets_root: Path, local_server: str) -> None:
    engine = SingleApiEngine()
    outcome = await engine.execute(
        _request(
            assets_root,
            "api.common.post_json",
            _env(local_server),
            params={"username": "alice", "email": "a@example.com"},
        )
    )

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response["body"]["json"]["username"] == "alice"


@pytest.mark.asyncio
async def test_assertion_failure_is_business_outcome(
    assets_root: Path, local_server: str
) -> None:
    engine = SingleApiEngine()
    outcome = await engine.execute(
        _request(assets_root, "api.common.assert_fail", _env(local_server))
    )

    assert isinstance(outcome, ApiExecutionOutcome)
    assert outcome.status == "failed"
    assert outcome.error is not None
    assert outcome.error["kind"] == "assertion"
    assert outcome.response is not None
    assert outcome.response["body"]["status"] == "bad"


@pytest.mark.asyncio
async def test_asset_not_found(assets_root: Path, local_server: str) -> None:
    engine = SingleApiEngine()
    with pytest.raises(AssetNotFoundError) as exc_info:
        await engine.execute(
            _request(assets_root, "api.common.does_not_exist", _env(local_server))
        )
    err = exc_info.value
    assert err.kind == "asset_not_found"
    assert err.stage == "preflight"
    assert "secret" not in json.dumps(err.to_dict())


@pytest.mark.asyncio
async def test_invalid_asset_missing_fields(assets_root: Path, local_server: str) -> None:
    engine = SingleApiEngine()
    with pytest.raises(InvalidAssetError) as exc_info:
        await engine.execute(
            _request(
                assets_root, "api.common.invalid_missing_fields", _env(local_server)
            )
        )
    assert exc_info.value.kind == "invalid_asset"
    assert exc_info.value.stage == "preflight"


@pytest.mark.asyncio
async def test_path_traversal_api_id_rejected(assets_root: Path, local_server: str) -> None:
    engine = SingleApiEngine()
    with pytest.raises(InvalidAssetError) as exc_info:
        await engine.execute(
            _request(assets_root, "api.common..secret", _env(local_server))
        )
    assert exc_info.value.stage == "preflight"


@pytest.mark.asyncio
async def test_missing_env_host(assets_root: Path) -> None:
    engine = SingleApiEngine()
    with pytest.raises(InvalidEnvironmentError) as exc_info:
        await engine.execute(
            _request(assets_root, "api.common.get_status", {"api_hosts": {}})
        )
    err = exc_info.value
    assert err.kind == "invalid_environment"
    assert err.stage == "preflight"
    assert "local" in err.public_message


@pytest.mark.asyncio
async def test_missing_api_hosts_key(assets_root: Path) -> None:
    engine = SingleApiEngine()
    with pytest.raises(InvalidEnvironmentError):
        await engine.execute(
            _request(assets_root, "api.common.get_status", {"other": 1})
        )


@pytest.mark.asyncio
async def test_timeout_reaches_http_client(assets_root: Path, local_server: str) -> None:
    engine = SingleApiEngine()
    with pytest.raises(EngineTimeoutError) as exc_info:
        await engine.execute(
            _request(
                assets_root,
                "api.common.slow",
                _env(local_server),
                timeout_seconds=0.2,
            )
        )
    err = exc_info.value
    assert err.kind == "timeout"
    assert err.stage == "execution"


@pytest.mark.asyncio
async def test_transport_error_with_injected_client(assets_root: Path) -> None:
    class BoomClient(HttpClient):
        async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
            raise httpx.ConnectError("connection refused")

    engine = SingleApiEngine(http_client=BoomClient())
    with pytest.raises(EngineTransportError) as exc_info:
        await engine.execute(
            _request(
                assets_root,
                "api.common.get_status",
                _env("http://127.0.0.1:1"),
            )
        )
    err = exc_info.value
    assert err.kind == "transport"
    assert err.stage == "execution"
    assert err.log_reason is not None


@pytest.mark.asyncio
async def test_unknown_exception_maps_to_internal_execution(
    assets_root: Path,
) -> None:
    class WeirdClient(HttpClient):
        async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
            raise RuntimeError("unexpected boom")

    engine = SingleApiEngine(http_client=WeirdClient())
    with pytest.raises(EngineInternalError) as exc_info:
        await engine.execute(
            _request(
                assets_root,
                "api.common.get_status",
                _env("http://127.0.0.1:9"),
            )
        )
    err = exc_info.value
    assert err.kind == "internal"
    assert err.stage == "execution"
    assert "boom" in (err.log_reason or "")
    assert "boom" not in json.dumps(err.to_dict())


@pytest.mark.asyncio
async def test_non_json_response_is_serializable(
    assets_root: Path, local_server: str
) -> None:
    engine = SingleApiEngine()
    outcome = await engine.execute(
        _request(assets_root, "api.common.plain_text", _env(local_server))
    )

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response["body"] == "hello-plain"
    json.dumps(outcome.to_dict())


@pytest.mark.asyncio
async def test_sensitive_headers_redacted_in_public_result(
    assets_root: Path, local_server: str
) -> None:
    engine = SingleApiEngine()
    outcome = await engine.execute(
        _request(assets_root, "api.common.with_secret_header", _env(local_server))
    )

    assert outcome.status == "success"
    assert outcome.request is not None
    headers = outcome.request["headers"]
    assert headers["Authorization"] == "***"
    assert headers["Cookie"] == "***"
    assert headers["X-Trace-Id"] == "public-trace"
    encoded = json.dumps(outcome.to_dict())
    assert "secret-token" not in encoded
    assert "session=abc" not in encoded


@pytest.mark.asyncio
async def test_timeout_kwarg_passed_to_injected_client(assets_root: Path) -> None:
    captured: dict[str, Any] = {}

    class CaptureClient(HttpClient):
        async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
            captured.update(kwargs)
            return HttpResponse(status_code=200, headers={}, body={"status": "ok"})

    engine = SingleApiEngine(http_client=CaptureClient())
    await engine.execute(
        _request(
            assets_root,
            "api.common.get_status",
            _env("http://example.invalid"),
            timeout_seconds=12.5,
        )
    )
    assert captured.get("timeout") == 12.5


@pytest.mark.asyncio
async def test_injected_client_not_closed_by_engine(assets_root: Path) -> None:
    closed = {"value": False}

    class TrackedClient(HttpClient):
        async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
            return HttpResponse(status_code=200, headers={}, body={"status": "ok"})

        async def close(self) -> None:
            closed["value"] = True

    client = TrackedClient()
    engine = SingleApiEngine(http_client=client)
    await engine.execute(
        _request(
            assets_root,
            "api.common.get_status",
            _env("http://example.invalid"),
        )
    )
    assert closed["value"] is False


@pytest.mark.asyncio
async def test_engine_does_not_read_config_from_disk(
    assets_root: Path, local_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认 EnginePort 路径不再打开 env yaml。"""

    def _forbid_open(*args: Any, **kwargs: Any) -> Any:
        path = str(args[0]) if args else ""
        if "config" in path and path.endswith(".yaml"):
            raise AssertionError(f"Engine 不应读取配置文件: {path}")
        return original_open(*args, **kwargs)

    import builtins

    original_open = builtins.open
    monkeypatch.setattr(builtins, "open", _forbid_open)

    engine = SingleApiEngine()
    outcome = await engine.execute(
        _request(assets_root, "api.common.get_status", _env(local_server))
    )
    assert outcome.status == "success"


def test_engine_port_structural_typing() -> None:
    engine: Any = SingleApiEngine()
    assert asyncio.iscoroutinefunction(engine.execute)
