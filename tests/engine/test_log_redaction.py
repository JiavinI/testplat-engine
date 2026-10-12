"""日志脱敏回归：系统异常原文（含 password/token/Cookie 值）不得出现在任何日志里。

规格要求公开结果、日志和持久化记录都不得包含秘密。这里用 caplog 把所有 logger 的
所有 record 取出，逐条检查 message / args / exc_text / exc_info 格式化结果 / 完整格式化文本，
覆盖两层日志点：

- executor 层：``SingleApiEngine.execute_api`` 的 error / warning；
- CaseRunner 层：``_attribute_system_error`` 的 error。

timeout / transport / internal 三类都要覆盖。测试不访问网络，HTTP 客户端全部注入替身。
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from testplat_engine import (
    ApiExecutionRequest,
    CaseExecutionRequest,
    EngineError,
    EngineInternalError,
    EngineTimeoutError,
    EngineTransportError,
    SingleApiEngine,
)
from testplat_engine.domain.case import CaseStep
from testplat_engine.engine.case_runner import CaseRunner
from testplat_engine.ports.http_client import HttpClient, HttpResponse
from testplat_engine.ports.step_executor import StepExecutionContext, StepExecutorRegistry
from tests.engine.case_support import copy_assets, write_yaml
from tests.engine.test_case_runner import make_case

# 故意注入的秘密：password / token / Cookie 值，任何日志文本都不得出现。
PASSWORD = "pw-LOGLEAK-password-9f3a"
TOKEN = "tok-LOGLEAK-token-71bc"
COOKIE = "sid-LOGLEAK-cookie-55de"
LEAKY_TEXT = f"password={PASSWORD} token={TOKEN} Cookie: session={COOKIE}"
SECRET_VALUES = (PASSWORD, TOKEN, COOKIE)

ENV = {"api_hosts": {"local": "http://127.0.0.1:9"}}

# (kind, 注入的底层异常工厂, 期望日志里出现的异常类型名)
_FAILURES: list[tuple[str, Callable[[], Exception], str]] = [
    ("timeout", lambda: httpx.ReadTimeout(LEAKY_TEXT), "ReadTimeout"),
    ("transport", lambda: httpx.ConnectError(LEAKY_TEXT), "ConnectError"),
    ("internal", lambda: RuntimeError(LEAKY_TEXT), "RuntimeError"),
]
_IDS = [item[0] for item in _FAILURES]


def _assert_no_secret_in_logs(caplog: pytest.LogCaptureFixture) -> str:
    """逐 record、逐字段检查秘密；返回合并后的日志文本供正向断言使用。"""
    assert caplog.records, "没有捕获到任何日志，测试将失去意义"
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    texts: list[str] = []
    for record in caplog.records:
        fields = [
            record.getMessage(),
            str(record.msg),
            str(record.args),
            record.exc_text or "",
            formatter.format(record),  # 含 traceback 的完整输出
        ]
        if record.exc_info:
            # 直接格式化 exc_info：即使 exc_text 未被缓存，也要能发现 traceback 泄漏。
            fields.append("".join(traceback.format_exception(*record.exc_info)))
        texts.extend(fields)
        for secret in SECRET_VALUES:
            for text in fields:
                assert secret not in text, f"日志泄漏秘密 {secret!r}: {text!r}"
    assert caplog.text and all(s not in caplog.text for s in SECRET_VALUES)
    return "\n".join(texts)


# ------------------------------------------------------------ executor 层


def _api_request(assets_root: Path) -> ApiExecutionRequest:
    return ApiExecutionRequest(
        api_id="api.common.get_status",
        assets_root=assets_root,
        environment=ENV,
        params={},
        timeout_seconds=5.0,
    )


@pytest.fixture
def assets_root(tmp_path: Path) -> Path:
    return copy_assets(tmp_path)


@pytest.mark.parametrize(("kind", "make_exc", "exc_type"), _FAILURES, ids=_IDS)
async def test_executor_logs_do_not_leak_secrets(
    kind: str,
    make_exc: Callable[[], Exception],
    exc_type: str,
    assets_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class LeakyClient(HttpClient):
        async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
            raise make_exc()

    caplog.set_level(logging.DEBUG)
    engine = SingleApiEngine(http_client=LeakyClient())
    with pytest.raises(EngineError) as exc_info:
        await engine.execute(_api_request(assets_root))
    # 原始原因仍保留在异常对象上供调用方处理，只是不得进日志。
    assert exc_info.value.kind == kind
    assert SECRET_VALUES[0] in (exc_info.value.log_reason or "")

    text = _assert_no_secret_in_logs(caplog)
    # 安全信息仍可定位问题：kind / stage / 异常类型名。
    assert f"kind={kind} stage=execution exc_type={exc_type}" in text


async def test_executor_close_failure_warning_does_not_leak_secrets(
    assets_root: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已分类异常之后 owned client.close 再抛带秘密的异常：warning 也不得泄漏。"""

    class TimeoutThenLeakyClose(HttpClient):
        async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
            raise httpx.ReadTimeout(LEAKY_TEXT)

        async def close(self) -> None:
            raise RuntimeError(LEAKY_TEXT)

    caplog.set_level(logging.DEBUG)
    engine = SingleApiEngine()
    monkeypatch.setattr(engine, "_acquire_client", lambda: (TimeoutThenLeakyClose(), True))
    with pytest.raises(EngineTimeoutError):
        await engine.execute(_api_request(assets_root))

    text = _assert_no_secret_in_logs(caplog)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "exc_type=RuntimeError" in text


# ------------------------------------------------------------ CaseRunner 层


class _RaisingExecutor:
    """按脚本抛出异常的假 executor：模拟 executor 层已分类 / 未分类的各种系统异常。"""

    def __init__(self, make_exc: Callable[[], BaseException]) -> None:
        self._make_exc = make_exc

    async def execute_step(self, step: CaseStep, context: StepExecutionContext) -> Any:
        raise self._make_exc()


def _classified(kind: str, cause: Exception) -> EngineError:
    """复刻真实 executor 的产物：带 log_reason 的已分类异常，且 ``__cause__`` 是原始异常。"""
    reason = f"{type(cause).__name__}: {cause}"
    err: EngineError
    if kind == "timeout":
        err = EngineTimeoutError(log_reason=reason)
    elif kind == "transport":
        err = EngineTransportError(log_reason=reason)
    else:
        err = EngineInternalError("execution", log_reason=reason)
    err.__cause__ = cause
    return err


async def _run_case_runner(make_exc: Callable[[], BaseException]) -> EngineError:
    registry = StepExecutorRegistry()
    registry.register("api", _RaisingExecutor(make_exc))
    runner = CaseRunner(registry)
    case = make_case({"step_id": "s1"})
    with pytest.raises(EngineError) as exc_info:
        await runner.run(
            case,
            assets_root=Path("/unused"),
            environment=ENV,
            case_params={},
            timeout_seconds=5.0,
        )
    return exc_info.value


@pytest.mark.parametrize(("kind", "make_exc", "exc_type"), _FAILURES, ids=_IDS)
async def test_case_runner_logs_do_not_leak_secrets_for_classified_errors(
    kind: str,
    make_exc: Callable[[], Exception],
    exc_type: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    err = await _run_case_runner(lambda: _classified(kind, make_exc()))
    assert err.kind == kind

    text = _assert_no_secret_in_logs(caplog)
    assert f"case=case.demo.unit step=s1 kind={kind} stage=execution exc_type={exc_type}" in text


async def test_case_runner_logs_do_not_leak_secrets_for_unclassified_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """executor 直接抛出未归类的原始异常：CaseRunner 兜底为 internal，日志同样不得泄漏。"""
    caplog.set_level(logging.DEBUG)
    err = await _run_case_runner(lambda: RuntimeError(LEAKY_TEXT))
    assert err.kind == "internal"

    text = _assert_no_secret_in_logs(caplog)
    assert "case=case.demo.unit step=s1 kind=internal stage=execution exc_type=RuntimeError" in text


# ------------------------------------------------------------ 端到端（两层同时在场）


@pytest.mark.parametrize(("kind", "make_exc", "exc_type"), _FAILURES, ids=_IDS)
async def test_case_end_to_end_logs_do_not_leak_secrets(
    kind: str,
    make_exc: Callable[[], Exception],
    exc_type: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真实 SingleApiEngine + CaseRunner + ApiStepExecutor：executor 与 CaseRunner 日志一并检查。"""
    assets_root = copy_assets(tmp_path)
    write_yaml(
        assets_root / "scenarios" / "demo" / "log_leak.case.yaml",
        'schema_version: "0.2"\n'
        "id: case.demo.log_leak\n"
        "name: 日志泄漏回归\n"
        "steps:\n"
        "  - step_id: boom\n"
        "    type: api\n"
        "    api_id: api.demo.transport_target\n",
    )

    class LeakyClient(HttpClient):
        async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
            raise make_exc()

    caplog.set_level(logging.DEBUG)
    engine = SingleApiEngine(http_client=LeakyClient())
    with pytest.raises(EngineError) as exc_info:
        await engine.execute(
            CaseExecutionRequest(
                case_id="case.demo.log_leak",
                assets_root=assets_root,
                environment=ENV,
                params={},
                timeout_seconds=5.0,
            )
        )
    assert exc_info.value.kind == kind

    text = _assert_no_secret_in_logs(caplog)
    loggers = {r.name for r in caplog.records}
    assert "testplat_engine.engine.executor" in loggers
    assert "testplat_engine.engine.case_runner" in loggers
    assert f"case=case.demo.log_leak step=boom kind={kind} stage=execution" in text
    # 公开 payload 同样不含秘密（既有保证，这里顺带守住）。
    assert all(s not in str(exc_info.value.to_dict()) for s in SECRET_VALUES)
