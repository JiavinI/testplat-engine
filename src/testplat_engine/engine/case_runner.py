"""串行 CaseRunner：步骤调度、执行上下文、fail-fast 与 Case 汇总。

CaseRunner 只**编排**：不发 HTTP、不做断言、不渲染 API 资产里的变量；这些都由
注册的 StepExecutor（第一批只有 ``api``）及其复用的单 API 执行 Module 负责。

三条核心约定：

1. 上下文所有权
   alias 只由本类在"步骤成功且声明了 output"之后写入，值取自安全输出模型
   （status_code/headers/body，敏感键已移除）。executor 拿到的是只读快照，
   无法改写，也无法写入未声明的 alias。
2. fail-fast
   第一批固定开启、调用方不可关闭。业务失败（断言失败、变量解析失败）以
   ``failed`` 结果**返回**，后续步骤补 ``skipped``；Case 状态由步骤状态推导。
3. 异常边界
   业务失败是返回值，系统错误（timeout / transport / internal）是异常。
   executor 抛出的 ``EngineError`` 保持原 ``kind``、阶段固定为 ``execution``，
   并被归属到当前步骤、携带安全 ``partial_outcome`` 重新抛出；绝不吞成断言失败。
   ``CancelledError`` 等 ``BaseException`` 不拦截，原样传播。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from testplat_engine.domain.case import Case, CaseStep
from testplat_engine.engine.case_context import (
    AliasShapeError,
    VariableResolutionError,
    bind_alias_response,
    resolve_step_params,
)
from testplat_engine.interface import (
    AuthTable,
    CaseExecutionOutcome,
    EngineError,
    EngineInternalError,
    EngineTimeoutError,
    EngineTransportError,
    JsonObject,
    JsonValue,
    StepOutcome,
    _freeze_json,
)
from testplat_engine.ports.step_executor import (
    PreflightableStepExecutor,
    StepExecutionContext,
    StepExecutor,
    StepExecutorRegistry,
    UnknownStepTypeError,
)

logger = logging.getLogger(__name__)


class CaseRunner:
    """按资产声明顺序串行执行 Case 的所有步骤。"""

    def __init__(
        self,
        registry: StepExecutorRegistry,
        *,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._registry = registry
        # 3.3b：每次 run 调用一次，得到本次执行独占的会话池（D8/D13，不跨执行复用）。
        self._session_factory = session_factory

    async def run(
        self,
        case: Case,
        *,
        assets_root: Path,
        environment: Mapping[str, JsonValue],
        case_params: Mapping[str, JsonValue],
        timeout_seconds: float,
        auth: AuthTable | None = None,
    ) -> CaseExecutionOutcome:
        """执行 Case。

        ``case_params`` 必须是已合并且已通过 preflight 校验的参数；
        ``timeout_seconds`` 是**每个步骤**的 HTTP 超时，不是整个 Case 的预算。
        ``auth`` 是整张身份表，原样下传给每个步骤（预检 + 执行），本类不解析身份。

        Returns:
            业务结果（Case success 或业务 failed）。

        Raises:
            EngineError: preflight 错误（不携带 partial），或步骤执行中的
                timeout / transport / internal（携带 partial_outcome）。
        """
        frozen_params = _freeze_json(dict(case_params))
        frozen_env = _freeze_json(dict(environment))
        assert isinstance(frozen_params, MappingProxyType)
        assert isinstance(frozen_env, MappingProxyType)

        self._preflight(case, assets_root, frozen_env, frozen_params, timeout_seconds, auth)

        started = time.perf_counter()
        # alias 表由本方法独占；executor 只能看到 MappingProxyType 快照。
        aliases: dict[str, Mapping[str, object]] = {}
        outcomes: list[StepOutcome] = []
        pool = self._session_factory() if self._session_factory is not None else None

        try:
            for index, step in enumerate(case.steps):
                step_started = time.perf_counter()
                try:
                    outcome = await self._run_step(
                        step, assets_root, frozen_env, frozen_params, aliases, timeout_seconds,
                        step_started, auth, pool,
                    )
                    if outcome.status == "success" and step.output is not None:
                        # 声明了 output 才写 alias；形状不符是 executor 缺陷，走 internal。
                        aliases[step.output] = bind_alias_response(outcome.response)
                except EngineError as exc:
                    raise self._attribute_system_error(
                        exc, case, index, outcomes, step_started, started,
                        logins=_logins(pool),
                    ) from exc
                except AliasShapeError as exc:
                    wrapped = EngineInternalError(
                        "execution", log_reason=f"alias shape mismatch: {exc}"
                    )
                    raise self._attribute_system_error(
                        wrapped, case, index, outcomes, step_started, started, origin=exc,
                        logins=_logins(pool),
                    ) from exc
                except Exception as exc:
                    # 未预期的缺陷：保持 internal/execution，归属当前步骤。
                    wrapped = EngineInternalError(
                        "execution", log_reason=f"{type(exc).__name__}: {exc}"
                    )
                    raise self._attribute_system_error(
                        wrapped, case, index, outcomes, step_started, started, origin=exc,
                        logins=_logins(pool),
                    ) from exc

                outcomes.append(outcome)
                if outcome.status == "failed":
                    # fail-fast：业务失败后停止；剩余步骤如实标记为 skipped。
                    # D9/H4：登录失败归属当前步骤，后续 skip_reason=login_failed 以示区分。
                    reason = (
                        "login_failed"
                        if (outcome.error or {}).get("kind") == "login_failed"
                        else "fail_fast"
                    )
                    outcomes.extend(self._skipped(case.steps[index + 1 :], reason))
                    break
        finally:
            if pool is not None:
                # 执行结束统一关闭 login 身份的专用 client（成功、失败、异常路径都走这里）。
                await pool.close()

        status = "success" if all(o.status == "success" for o in outcomes) else "failed"
        return CaseExecutionOutcome(
            case_id=case.id,
            status=status,
            duration_seconds=time.perf_counter() - started,
            steps=tuple(outcomes),
            logins=_logins(pool),
        )

    def _executor_for(self, step: CaseStep) -> StepExecutor:
        """按 type 取 executor；未注册属于 Engine 装配缺陷（契约层已拒绝未知 type）。"""
        try:
            return self._registry.get(step.type)
        except UnknownStepTypeError as exc:
            raise EngineInternalError(
                "preflight",
                public_message=f"步骤 {step.step_id} 的 type 没有可用执行器: {step.type}",
                log_reason=f"unregistered step type {step.type!r} (step_id={step.step_id})",
            ) from exc

    def _preflight(
        self,
        case: Case,
        assets_root: Path,
        environment: Mapping[str, JsonValue],
        case_params: Mapping[str, JsonValue],
        timeout_seconds: float,
        auth: AuthTable | None = None,
    ) -> None:
        """在执行任何步骤前，确认每个步骤都有 executor 且静态可执行。

        这样"executor 未注册 / 资产缺失 / 环境缺 host"在零副作用时就以
        preflight 错误暴露，不会出现前几步已发请求、后几步才报 preflight。
        身份不存在/不可用同理：全部步骤的身份在这里一次性检查。
        """
        context = StepExecutionContext(
            assets_root=assets_root,
            environment=environment,
            case_params=case_params,
            aliases=MappingProxyType({}),
            timeout_seconds=timeout_seconds,
            auth=auth,
        )
        for step in case.steps:
            executor = self._executor_for(step)
            if isinstance(executor, PreflightableStepExecutor):
                try:
                    executor.preflight_step(step, context)
                except EngineError:
                    raise
                except Exception as exc:
                    raise EngineInternalError(
                        "preflight", log_reason=f"{type(exc).__name__}: {exc}"
                    ) from exc

    async def _run_step(
        self,
        step: CaseStep,
        assets_root: Path,
        environment: Mapping[str, JsonValue],
        case_params: Mapping[str, JsonValue],
        aliases: Mapping[str, Mapping[str, object]],
        timeout_seconds: float,
        step_started: float,
        auth: AuthTable | None = None,
        pool: Any = None,
    ) -> StepOutcome:
        """解析引用 → 调 executor → 校验返回值；变量解析失败以业务 failed 返回。"""
        alias_view: Mapping[str, object] = MappingProxyType(dict(aliases))
        try:
            resolved = resolve_step_params(
                step.step_id, step.params, case_params=case_params, aliases=alias_view
            )
        except VariableResolutionError as exc:
            # A4：引用在执行前解析；失败属于该步骤的业务失败，request/response 均为 null。
            return StepOutcome(
                step_id=step.step_id,
                type=step.type,
                status="failed",
                duration_seconds=time.perf_counter() - step_started,
                name=step.name,
                error=exc.to_error(),
            )

        context = StepExecutionContext(
            assets_root=assets_root,
            environment=environment,
            case_params=case_params,
            aliases=alias_view,
            timeout_seconds=timeout_seconds,
            auth=auth,
            session_pool=pool,
        )
        # executor 只拿到已解析的 params；原始 CaseStep 对象保持不变。
        resolved_step = CaseStep(
            step_id=step.step_id,
            type=step.type,
            api_id=step.api_id,
            name=step.name,
            params=resolved,
            output=step.output,
        )
        outcome = await self._executor_for(step).execute_step(resolved_step, context)
        return self._validate_outcome(step, outcome)

    @staticmethod
    def _validate_outcome(step: CaseStep, outcome: object) -> StepOutcome:
        """executor 返回值必须属于当前步骤，且不能自行宣布 skipped。"""
        if not isinstance(outcome, StepOutcome):
            raise EngineInternalError(
                "execution", log_reason=f"executor returned {type(outcome).__name__}"
            )
        if outcome.step_id != step.step_id or outcome.type != step.type:
            raise EngineInternalError(
                "execution",
                log_reason=(
                    f"executor outcome mismatch: got {outcome.step_id}/{outcome.type}, "
                    f"expected {step.step_id}/{step.type}"
                ),
            )
        if outcome.status == "skipped":
            raise EngineInternalError(
                "execution", log_reason="executor must not return skipped outcome"
            )
        return outcome

    @staticmethod
    def _skipped(steps: tuple[CaseStep, ...], reason: str = "fail_fast") -> list[StepOutcome]:
        return [
            StepOutcome(
                step_id=step.step_id,
                type=step.type,
                status="skipped",
                duration_seconds=0.0,
                name=step.name,
                skip_reason=reason,
            )
            for step in steps
        ]

    def _attribute_system_error(
        self,
        exc: EngineError,
        case: Case,
        index: int,
        completed: list[StepOutcome],
        step_started: float,
        case_started: float,
        origin: BaseException | None = None,
        logins: list[JsonObject] | None = None,
    ) -> EngineError:
        """把系统错误归属到当前步骤，返回携带安全 partial_outcome 的同 kind 异常。

        - 已完成步骤原样保留；当前步骤 failed；其后全部 skipped。
        - Case 已开始后，preflight 阶段的异常（只可能是 TOCTOU 或 executor 缺陷）
          统一收敛为 internal/execution，保证异常与 partial 的 kind/stage 一致。
        - ``log_reason`` 只保留在异常对象上，不进入 partial_outcome / 外显 payload，
          也不写日志；日志只记异常类型名。
        - ``origin`` 是触发本次归属的原始异常（用于取类型名）；缺省时取
          ``exc.__cause__``（executor 层 ``raise classified from exc`` 留下的原始异常），
          再退回 ``exc`` 自身。
        """
        step = case.steps[index]
        source = origin if origin is not None else (exc.__cause__ or exc)
        origin_type = type(source).__name__
        if exc.stage == "execution" and exc.kind in ("timeout", "transport", "internal"):
            kind, public_message, log_reason = exc.kind, exc.public_message, exc.log_reason
        else:
            kind = "internal"
            public_message = "执行引擎内部错误"
            log_reason = f"{exc.stage}-stage {exc.kind} after case started: {exc.log_reason}"

        error: JsonObject = {"kind": kind, "stage": "execution", "message": public_message}
        failed = StepOutcome(
            step_id=step.step_id,
            type=step.type,
            status="failed",
            duration_seconds=time.perf_counter() - step_started,
            name=step.name,
            error=error,
        )
        partial = CaseExecutionOutcome(
            case_id=case.id,
            status="failed",
            duration_seconds=time.perf_counter() - case_started,
            steps=(*completed, failed, *self._skipped(case.steps[index + 1 :])),
            logins=logins or [],
        )
        # 日志不得包含 log_reason / traceback：它们可能带有 password/token/Cookie 原值。
        # 只保留定位所需的安全信息：case、step、kind、stage、原始异常的类型名。
        logger.error(
            "case step system error case=%s step=%s kind=%s stage=execution exc_type=%s",
            case.id,
            step.step_id,
            kind,
            origin_type,
        )
        if kind == "timeout":
            return EngineTimeoutError(
                public_message, log_reason=log_reason, partial_outcome=partial
            )
        if kind == "transport":
            return EngineTransportError(
                public_message, log_reason=log_reason, partial_outcome=partial
            )
        return EngineInternalError(
            "execution", public_message, log_reason=log_reason, partial_outcome=partial
        )


def _logins(pool: Any) -> list[JsonObject]:
    """会话池的公开登录记录（已脱敏）；无池时为空列表。"""
    return [] if pool is None else pool.public_logins()
