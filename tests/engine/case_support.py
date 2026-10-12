"""Case 运行期测试的共享支撑：本地 HTTP server 与可控替身（不访问公网）。"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "assets"

# 响应中故意夹带的各类秘密；任何公开结果/序列化文本都不得出现它们。
SECRETS = ("tok-SECRET-1", "pw-SECRET-2", "sid-SECRET-3", "rt-SECRET-4", "Bearer-SECRET-5")


@dataclass
class Recorded:
    """服务端视角的一次请求，用于断言"确实发了/没发/收到了什么类型"。"""

    method: str
    path: str
    headers: dict[str, str]
    body: Any


@dataclass
class ServerState:
    requests: list[Recorded] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)
    release: Event = field(default_factory=Event)

    def paths(self) -> list[str]:
        with self.lock:
            return [r.path for r in self.requests]


def _make_handler(state: ServerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _record(self) -> Any:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            body: Any = json.loads(raw.decode("utf-8")) if raw else None
            with state.lock:
                state.requests.append(
                    Recorded(self.command, self.path, dict(self.headers.items()), body)
                )
            return body

        def _send(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            body = self._record()
            if self.path == "/demo/resources":
                # 嵌套秘密 + Set-Cookie：用于验证脱敏与"秘密不流向下游"。
                self._send(
                    201,
                    {
                        "id": 101,
                        "name": (body or {}).get("name"),
                        "token": SECRETS[0],
                        "profile": {"password": SECRETS[1], "nick": "n1"},
                        "items": [{"refresh_token": SECRETS[3], "ok": True}],
                    },
                    {"Set-Cookie": f"sid={SECRETS[2]}"},
                )
            elif self.path == "/demo/echo":
                self._send(
                    200,
                    {
                        "echo": (body or {}).get("value"),
                        "flag": True,
                        "payload": {"k": "v", "n": 1},
                    },
                )
            else:
                self.send_error(404)

        def do_GET(self) -> None:  # noqa: N802
            self._record()
            if self.path == "/demo/resources/item":
                self._send(200, {"ok": True})
            elif self.path == "/status/200":
                self._send(200, {"status": "ok"})
            elif self.path == "/demo/timeout":
                # 阻塞到测试结束，让客户端的短超时真实触发。
                state.release.wait(timeout=5)
                self._send(200, {"late": True})
            elif self.path == "/demo/transport":
                # 不回任何响应直接断开连接 → httpx RemoteProtocolError（传输错误）。
                self.close_connection = True
                self.connection.close()
            else:
                self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def start_server() -> tuple[ThreadingHTTPServer, Thread, ServerState, str]:
    state = ServerState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, state, f"http://{host}:{port}"


def stop_server(server: ThreadingHTTPServer, thread: Thread, state: ServerState) -> None:
    state.release.set()
    server.shutdown()
    server.server_close()
    thread.join()


def copy_assets(tmp_path: Path) -> Path:
    root = tmp_path / "assets"
    shutil.copytree(FIXTURE_ROOT, root)
    return root


def write_yaml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
