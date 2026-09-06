"""SubagentSupervisor（设计 §4.8/§8.3，D1.3，H2 最小版）。

子代理协议的执行监督者：SpawnSpec 校验（E510，缺陷 fail fast）、深度=1 强制
（E540，缺陷）、全局并发 ≤3、超时收割（E530，收割为 timeout envelope 由父
处置）、Envelope 输出校验（E520，违约输出不下发）、spawn/结果双审计。

边界（保持薄，§8.5 成本核算的立足点）：业务执行体是注入的 ``runner``
（沙盒子代理=包 run_sandbox_task；Proposer/Reviewer/Auditor=包 run_inner_loop
的各自装配），Supervisor 只管协议与治理；output_schema_id 的解析与校验器
由调用方给（schema 归 skills/contracts，harness 不 import）。

上下文隔离（§8.3 第一条）在机制层的落点：context_slice 必须是结构化 dict
且序列化尺寸有上限——把父对话全文塞进切片（反模式清单 §5.6 最后一条）会
直接被 E510 拒绝，而不是靠评审自觉。
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from omm_agent_core.errors import AgentError, ErrorCode
from omm_agent_core.models import ArtifactRef

from .budget import RunBudget
from .gateway import Usage

__all__ = [
    "CONTEXT_SLICE_MAX_CHARS",
    "MAX_SUBAGENT_CONCURRENCY",
    "ResultEnvelope",
    "SpawnSpec",
    "SubagentRunner",
    "SubagentSupervisor",
]

#: §4.8：全局同时在跑的子代理数上限。
MAX_SUBAGENT_CONCURRENCY = 3

#: 上下文切片的序列化尺寸上限：超过即视为"父对话转录"嫌疑（E510）。
#: 结构化切片（题面片段/方案卡/断言清单）远小于此；整段对话史则轻松超出。
CONTEXT_SLICE_MAX_CHARS = 32_000

#: 合法 kind：固定名或带视角/类型后缀的两类（§8.2 角色目录）。
_KIND_EXACT = frozenset({"sandbox", "reviewer"})
_KIND_PREFIXES = ("proposer:", "auditor:")

#: 工具权限档位的顺序（与 omm_agent_tools.TIERS 一致；装配期契约而非 import，
#: 避免 harness→tools 的这条边只为一个元组存在）。
_TIER_ORDER = ("readonly", "workspace_write", "execute", "spawn")


@dataclass(frozen=True)
class SpawnSpec:
    """一次子代理派发的完整说明（D1.3）。"""

    kind: str
    goal: str
    context_slice: Mapping[str, Any]  # 结构化输入，绝非父对话转录
    toolset: tuple[str, ...]
    tool_tier: str  # 必须 ≤ 父 tier（§8.3）
    budgets: RunBudget  # 父剩余预算的切片（BudgetGovernor.subagent_slice 给上限）
    output_schema_id: str


@dataclass(frozen=True)
class ResultEnvelope:
    """子代理的唯一回传形状（D1.3）；不互聊——产出经工件与父的合并代码汇流。"""

    status: str  # "done" | "failed" | "exhausted" | "timeout"
    output: dict[str, Any] | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    usage: Usage = field(default_factory=lambda: Usage(0, 0, 0))
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "done"


#: 业务执行体：拿到校验通过的 SpawnSpec，返回 Envelope。抛出的异常由
#: Supervisor 收割为 failed envelope（子代理崩溃不允许炸穿父节点）。
SubagentRunner = Callable[[SpawnSpec], ResultEnvelope]

#: 审计回调：spawn 与结果各一条（TOOL_CALLED{tool:"subagent:<kind>"}，§8.3）。
AuditListener = Callable[[dict[str, Any]], None]


def _tier_rank(tier: str) -> int:
    try:
        return _TIER_ORDER.index(tier)
    except ValueError:
        raise AgentError(
            ErrorCode.SUBAGENT_SPAWN_INVALID,
            f"未知工具档位 {tier!r}（合法：{_TIER_ORDER}）",
        ) from None


def validate_spec(spec: SpawnSpec, *, parent_tier: str) -> None:
    """SpawnSpec 校验（E510=装配/调用缺陷，fail fast）。"""
    kind_ok = spec.kind in _KIND_EXACT or any(
        spec.kind.startswith(prefix) and len(spec.kind) > len(prefix)
        for prefix in _KIND_PREFIXES
    )
    if not kind_ok:
        raise AgentError(
            ErrorCode.SUBAGENT_SPAWN_INVALID,
            f"kind {spec.kind!r} 不在角色目录（sandbox/proposer:<view>/reviewer/auditor:<type>）",
        )
    if not spec.goal.strip():
        raise AgentError(ErrorCode.SUBAGENT_SPAWN_INVALID, "goal 不得为空")
    if not spec.output_schema_id.strip():
        raise AgentError(ErrorCode.SUBAGENT_SPAWN_INVALID, "output_schema_id 不得为空")
    if not isinstance(spec.context_slice, Mapping):
        raise AgentError(
            ErrorCode.SUBAGENT_SPAWN_INVALID, "context_slice 必须是结构化对象"
        )
    serialized = json.dumps(dict(spec.context_slice), ensure_ascii=False, default=repr)
    if len(serialized) > CONTEXT_SLICE_MAX_CHARS:
        raise AgentError(
            ErrorCode.SUBAGENT_SPAWN_INVALID,
            f"context_slice 序列化 {len(serialized)} 字符，超过上限 "
            f"{CONTEXT_SLICE_MAX_CHARS}——切片应是结构化摘要，不是父对话转录（§5.6）",
        )
    if _tier_rank(spec.tool_tier) > _tier_rank(parent_tier):
        raise AgentError(
            ErrorCode.SUBAGENT_SPAWN_INVALID,
            f"子代理 tier {spec.tool_tier!r} 超过父 tier {parent_tier!r}（§8.3：子 ≤ 父）",
        )


class SubagentSupervisor:
    """spawn 的唯一入口：校验 → 并发闸 → 执行/收割 → Envelope 校验 → 审计。"""

    def __init__(
        self,
        *,
        max_concurrency: int = MAX_SUBAGENT_CONCURRENCY,
        audit: AuditListener | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gate = threading.Semaphore(max_concurrency)
        self._audit = audit
        self._clock = clock

    def spawn(
        self,
        spec: SpawnSpec,
        runner: SubagentRunner,
        *,
        parent_tier: str,
        caller_depth: int = 0,
        output_validator: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> ResultEnvelope:
        """派发一个子代理并等待其收束（同步语义；父的 fan-out 用线程并行）。

        - ``caller_depth``：0=阶段主导 Agent；>0 即子代理试图再 spawn → E540
          （缺陷 fail fast，深度=1 是章程不是配置，§1.3 原则 12）。
        - ``output_validator``：output_schema_id 对应的校验器（调用方解析
          schema 后注入）；done 且违约 → 改写为 failed(E520)，不可信输出不下发。
        """
        if caller_depth > 0:
            raise AgentError(
                ErrorCode.SUBAGENT_DEPTH_VIOLATION,
                f"深度=1 强制：子代理（depth={caller_depth}）不得再 spawn",
            )
        validate_spec(spec, parent_tier=parent_tier)

        self._emit_audit({
            "tool": f"subagent:{spec.kind}",
            "phase": "spawn",
            "goal": spec.goal[:200],
            "tool_tier": spec.tool_tier,
            # 子代理拿到了哪些工具（空 = 纯推理）：审计「提议人能不能自己检索」
            "toolset": list(spec.toolset),
            "output_schema_id": spec.output_schema_id,
            "budget_tokens": spec.budgets.max_total_tokens,
        })

        started = self._clock()
        with self._gate:
            envelope = self._run_reaped(spec, runner)
        duration_ms = int((self._clock() - started) * 1000)

        if envelope.ok and output_validator is not None and envelope.output is not None:
            problems = output_validator(envelope.output)
            if problems:
                envelope = ResultEnvelope(
                    status="failed",
                    output=None,  # 违约输出不下发：宁缺勿污染父节点（E520）
                    artifacts=envelope.artifacts,
                    usage=envelope.usage,
                    error_code=ErrorCode.SUBAGENT_ENVELOPE_INVALID.value,
                )

        self._emit_audit({
            "tool": f"subagent:{spec.kind}",
            "phase": "result",
            "envelope_status": envelope.status,
            "error_code": envelope.error_code,
            "duration_ms": duration_ms,
            "prompt_tokens": envelope.usage.prompt_tokens,
            "completion_tokens": envelope.usage.completion_tokens,
            "artifact_count": len(envelope.artifacts),
        })
        return envelope

    # -- internals ------------------------------------------------------------

    def _run_reaped(self, spec: SpawnSpec, runner: SubagentRunner) -> ResultEnvelope:
        """在工作线程执行 runner，超时收割为 timeout envelope（E530）。

        与 ToolBus 的超时语义一致且如实声明：Python 线程无法被安全杀死，
        超时后线程被放弃（daemon），父拿回控制权按"重试一次→降级→上闸门"
        处置（§8.3）。墙钟额度来自预算切片。
        """
        outcome: dict[str, ResultEnvelope] = {}

        def target() -> None:
            try:
                outcome["envelope"] = runner(spec)
            except AgentError as exc:
                outcome["envelope"] = ResultEnvelope(
                    status="exhausted" if exc.code.value.startswith("E3") else "failed",
                    error_code=exc.code.value,
                )
            except Exception as exc:  # noqa: BLE001 - 子代理崩溃不得炸穿父节点
                outcome["envelope"] = ResultEnvelope(
                    status="failed",
                    error_code=None if not isinstance(exc, AgentError) else exc.code.value,
                )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        # 墙钟额度未启用（如控制面按审批等待语义禁用 = inf）时按无超时等待：
        # thread.join(inf) 在 CPython 会 OverflowError，非有限值必须转 None。
        timeout = spec.budgets.max_wall_clock_s
        thread.join(timeout if math.isfinite(timeout) else None)
        if thread.is_alive():
            return ResultEnvelope(
                status="timeout",
                error_code=ErrorCode.SUBAGENT_TIMEOUT_REAPED.value,
            )
        return outcome.get(
            "envelope",
            ResultEnvelope(status="failed", error_code=None),
        )

    def _emit_audit(self, payload: dict[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            self._audit(dict(payload))
        except Exception:  # noqa: BLE001 - 审计回调故障绝不影响执行
            pass
