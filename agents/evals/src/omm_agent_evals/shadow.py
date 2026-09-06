"""影子等价评测（设计文档 §6.5）：Graph v1 驱动 vs 现引擎线性推进。

§6.5 的口径：**影子等价 = 控制流事件序列等价，不比内容**——事件类型序列、状态
转移序列、步骤 / attempt 计数、审批点位置逐一相等；payload 里的内容性字段
（outputs 正文、时间戳、id）不参与比对。本模块把这个口径落成两个可复用的
纯函数（``control_flow_trace`` / ``snapshot_control_flow``）和一组把全链会话
开到底的场景脚本（``SHADOW_SCENARIOS``）：审批门放行 / 拒绝重试 / 回退重做 /
失败恢复 / 修订回合 / 暂停取消——现引擎能走到的每条控制流分叉都走一遍，
两种调度器各跑一趟后逐一比对，再在 shadow 档位下确认零分歧。

只在脚本 LLM + 脚本沙箱下做全序列断言（§6.5 第 2 条：真实 LLM 的非确定性使
其天然不适用于全序列断言，这是边界不是缺陷）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from omm_agent_core import AdvanceOutcome, AgentEvent, EventType, TaskRunSnapshot, TaskState
from omm_agent_skills import G3_ACCEPT_OPTION_ID, G4_CONFIRM_OPTION_ID

from .full_chain import (
    FULL_CHAIN_ROBUSTNESS_CHECKS,
    FullChainSession,
    build_full_chain_session,
    robustness_success,
    sandbox_failure,
    sandbox_success,
)

#: 各事件类型参与比对的 payload 键（§6.5：只取控制流字段，不取内容字段）。
#: 没列出的事件只比类型（STEP_SUCCEEDED / STEP_FAILED / ARTIFACT_PRODUCED /
#: TOOL_CALLED 的载荷是内容或 id）。
CONTROL_FLOW_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.STATE_CHANGED: ("from", "to"),
    EventType.STEP_STARTED: ("state", "attempt"),
    EventType.REVIEW_REQUESTED: ("resume_state",),
    EventType.REVIEW_RESOLVED: ("approved", "resume_state", "rerun", "revision_round"),
    EventType.REVISION_REQUESTED: ("target_state", "round"),
    EventType.RUN_RETRIED: ("target_state",),
    EventType.RUN_FAILED: ("failed_state",),
}


def control_flow_trace(events: Iterable[AgentEvent]) -> list[tuple[Any, ...]]:
    """事件日志 → 控制流轨迹：``(类型, (键, 值)…)``，审批点额外带闸门号。"""
    trace: list[tuple[Any, ...]] = []
    for event in events:
        entry: tuple[Any, ...] = (event.event_type.value,)
        for name in CONTROL_FLOW_FIELDS.get(event.event_type, ()):
            entry += ((name, event.payload.get(name)),)
        if event.event_type is EventType.REVIEW_REQUESTED:
            entry += (("gate", (event.payload.get("gate") or {}).get("gate")),)
        trace.append(entry)
    return trace


def snapshot_control_flow(snapshot: TaskRunSnapshot) -> dict[str, Any]:
    """快照的控制流面：终态、逐步骤 (状态, attempt, 结果)、闸门台账键、修订轮数。"""
    return {
        "state": snapshot.state.value,
        "steps": [(step.state.value, step.attempt, step.status.value) for step in snapshot.steps],
        "review": snapshot.review.resume_state.value if snapshot.review else None,
        "failure": snapshot.failure.state.value if snapshot.failure else None,
        "review_decisions": sorted(snapshot.review_decisions),
        "revision_round": snapshot.revision_round,
    }


# -- 场景脚本 ------------------------------------------------------------------------


def failing_robustness_run():
    """三项检查中 bootstrap 稳定性未过：验证阶段上 G3。"""
    checks = [dict(check) for check in FULL_CHAIN_ROBUSTNESS_CHECKS]
    checks[1].update(passed=False, value=0.42, detail="重采样 RMSE 波动 42%，超出阈值")
    return robustness_success(checks=checks)


def _run(session: FullChainSession) -> AdvanceOutcome:
    return session.engine.run_until_blocked(session.snapshot)


def _expect_gate(session: FullChainSession, outcome: AdvanceOutcome, state: TaskState) -> None:
    assert outcome.status == AdvanceOutcome.REVIEW_REQUESTED, outcome.status
    assert session.snapshot.review is not None
    assert session.snapshot.review.resume_state is state, session.snapshot.review.resume_state


def approve_plan(session: FullChainSession) -> AdvanceOutcome:
    """跑到 G1 → 批准方案 A → 继续跑。"""
    _expect_gate(session, _run(session), TaskState.MODEL_PLANNING)
    session.engine.resolve_review(session.snapshot, approved=True, reason="采用方案 A")
    return _run(session)


def confirm_delivery(session: FullChainSession, outcome: AdvanceOutcome) -> AdvanceOutcome:
    """G4 定稿交付闸门（必停）→ 确认交付 → 跑完。"""
    _expect_gate(session, outcome, TaskState.PAPER_WRITING)
    session.engine.resolve_review(session.snapshot, approved=True, reason=G4_CONFIRM_OPTION_ID)
    return _run(session)


def _expect_completed(session: FullChainSession, outcome: AdvanceOutcome) -> None:
    assert outcome.status == AdvanceOutcome.COMPLETED, outcome.status
    assert session.snapshot.state is TaskState.COMPLETED


def drive_happy_path(session: FullChainSession) -> None:
    _expect_completed(session, confirm_delivery(session, approve_plan(session)))


def drive_reject_then_retry(session: FullChainSession) -> None:
    engine, snapshot = session.engine, session.snapshot
    _expect_gate(session, _run(session), TaskState.MODEL_PLANNING)
    engine.resolve_review(snapshot, approved=False, reason="预算约束未满足，重新规划")
    assert snapshot.state is TaskState.FAILED
    engine.retry(snapshot)
    _expect_gate(session, _run(session), TaskState.MODEL_PLANNING)
    engine.resolve_review(snapshot, approved=True, reason="第二版方案通过")
    _expect_completed(session, confirm_delivery(session, _run(session)))


def drive_rounds_exhausted_then_retry(session: FullChainSession) -> None:
    engine, snapshot = session.engine, session.snapshot
    outcome = approve_plan(session)
    assert outcome.status == AdvanceOutcome.FAILED, outcome.status
    assert snapshot.failure is not None and snapshot.failure.state is TaskState.EXPERIMENTING
    engine.retry(snapshot)
    _expect_completed(session, confirm_delivery(session, _run(session)))


def drive_g3_accept(session: FullChainSession) -> None:
    engine, snapshot = session.engine, session.snapshot
    _expect_gate(session, approve_plan(session), TaskState.VALIDATING)
    engine.resolve_review(snapshot, approved=True, reason=G3_ACCEPT_OPTION_ID)
    _expect_completed(session, confirm_delivery(session, _run(session)))


def drive_g3_redo_experiment(session: FullChainSession) -> None:
    engine, snapshot = session.engine, session.snapshot
    _expect_gate(session, approve_plan(session), TaskState.VALIDATING)
    engine.resolve_review(
        snapshot,
        approved=True,
        reason="redo:EXPERIMENTING",
        resume_state=TaskState.EXPERIMENTING,
    )
    _expect_gate(session, _run(session), TaskState.VALIDATING)
    engine.resolve_review(snapshot, approved=True, reason=G3_ACCEPT_OPTION_ID)
    _expect_completed(session, confirm_delivery(session, _run(session)))


def drive_g4_redo_paper(session: FullChainSession) -> None:
    engine, snapshot = session.engine, session.snapshot
    _expect_gate(session, approve_plan(session), TaskState.PAPER_WRITING)
    engine.resolve_review(
        snapshot,
        approved=True,
        reason="redo:PAPER_WRITING",
        resume_state=TaskState.PAPER_WRITING,
        rerun=True,
    )
    _expect_completed(session, confirm_delivery(session, _run(session)))


def drive_unattended(session: FullChainSession) -> None:
    _expect_completed(session, _run(session))


def drive_revision_round(session: FullChainSession) -> None:
    """跑完 → 提修订（从建模方案重做）→ 批准 → G1 再弹 → 放行 → G4 → 完成。"""
    engine, snapshot = session.engine, session.snapshot
    drive_happy_path(session)
    engine.request_revision(
        snapshot, TaskState.MODEL_PLANNING, reason="目标函数改成加权总成本", note_id="note_1"
    )
    engine.resolve_review(snapshot, approved=True)
    _expect_gate(session, _run(session), TaskState.MODEL_PLANNING)
    engine.resolve_review(snapshot, approved=True, reason="采用方案 A")
    _expect_completed(session, confirm_delivery(session, _run(session)))
    assert snapshot.revision_round == 1


def drive_revision_declined(session: FullChainSession) -> None:
    engine, snapshot = session.engine, session.snapshot
    drive_happy_path(session)
    engine.request_revision(snapshot, TaskState.PAPER_WRITING, reason="想改措辞")
    engine.resolve_review(snapshot, approved=False, reason="算了")
    assert snapshot.state is TaskState.COMPLETED
    assert engine.advance(snapshot).status == AdvanceOutcome.IDLE


def drive_pause_resume(session: FullChainSession) -> None:
    engine, snapshot = session.engine, session.snapshot
    assert engine.advance(snapshot).status == AdvanceOutcome.ADVANCED  # 题意分析
    engine.request_pause(snapshot)
    assert engine.advance(snapshot).status == AdvanceOutcome.IDLE
    engine.resume(snapshot)
    drive_happy_path(session)


def drive_cancel(session: FullChainSession) -> None:
    engine, snapshot = session.engine, session.snapshot
    assert engine.advance(snapshot).status == AdvanceOutcome.ADVANCED
    engine.request_cancel(snapshot)
    assert engine.advance(snapshot).status == AdvanceOutcome.CANCELLED
    assert snapshot.state is TaskState.FAILED
    assert engine.advance(snapshot).status == AdvanceOutcome.IDLE


@dataclass(frozen=True)
class ShadowScenario:
    """一条控制流分叉：装配参数 + 把会话开到底的脚本。"""

    name: str
    drive: Callable[[FullChainSession], None]
    build_kwargs: dict[str, Any] = field(default_factory=dict)
    build_factory: Callable[[], dict[str, Any]] | None = None

    def build(self, graph_mode: str) -> FullChainSession:
        kwargs = dict(self.build_kwargs)
        if self.build_factory is not None:
            kwargs.update(self.build_factory())
        return build_full_chain_session(graph_mode=graph_mode, **kwargs)


def _repair_round_kwargs() -> dict[str, Any]:
    return {"tool_runs": [sandbox_failure(), sandbox_success()]}


def _rounds_exhausted_kwargs() -> dict[str, Any]:
    return {
        "tool_runs": [sandbox_failure(), sandbox_failure(), sandbox_failure(), sandbox_success()]
    }


def _g3_kwargs() -> dict[str, Any]:
    return {"validation_run": failing_robustness_run()}


SHADOW_SCENARIOS: tuple[ShadowScenario, ...] = (
    ShadowScenario("happy_path", drive_happy_path),
    ShadowScenario("experiment_repair_round", drive_happy_path, build_factory=_repair_round_kwargs),
    ShadowScenario("reject_then_retry", drive_reject_then_retry),
    ShadowScenario(
        "rounds_exhausted_then_retry",
        drive_rounds_exhausted_then_retry,
        build_factory=_rounds_exhausted_kwargs,
    ),
    ShadowScenario("g3_accept_limitation", drive_g3_accept, build_factory=_g3_kwargs),
    ShadowScenario("g3_redo_experiment", drive_g3_redo_experiment, build_factory=_g3_kwargs),
    ShadowScenario("g4_redo_paper", drive_g4_redo_paper),
    ShadowScenario("unattended", drive_unattended, {"require_confirmation": False}),
    ShadowScenario("revision_round", drive_revision_round),
    ShadowScenario("revision_declined", drive_revision_declined),
    ShadowScenario("pause_resume", drive_pause_resume),
    ShadowScenario("cancel", drive_cancel),
)


def run_scenario(scenario: ShadowScenario, graph_mode: str) -> FullChainSession:
    """按档位装配并把场景开到底；返回会话供比对。"""
    session = scenario.build(graph_mode)
    scenario.drive(session)
    return session


@dataclass(frozen=True)
class ShadowReport:
    """一条场景在两种调度下的比对结果。"""

    scenario: str
    baseline_trace: list[tuple[Any, ...]]
    graph_trace: list[tuple[Any, ...]]
    baseline_flow: dict[str, Any]
    graph_flow: dict[str, Any]
    divergences: list[dict[str, Any]]

    @property
    def equivalent(self) -> bool:
        return (
            self.baseline_trace == self.graph_trace
            and self.baseline_flow == self.graph_flow
            and not self.divergences
        )


def compare_scenario(scenario: ShadowScenario) -> ShadowReport:
    """现引擎（off）与 Graph v1 驱动（linear-v1）各跑一趟，按 §6.5 口径比对。

    图驱动那一趟由线性调度器当影子，所以 ``divergences`` 同时是「图 vs 线性」
    逐步决策一致的证据。
    """
    baseline = run_scenario(scenario, "off")
    graph = run_scenario(scenario, "linear-v1")
    return ShadowReport(
        scenario=scenario.name,
        baseline_trace=control_flow_trace(baseline.sink.events),
        graph_trace=control_flow_trace(graph.sink.events),
        baseline_flow=snapshot_control_flow(baseline.snapshot),
        graph_flow=snapshot_control_flow(graph.snapshot),
        divergences=[d.to_dict() for d in graph.engine.shadow_divergences],
    )


def scenario_names(scenarios: Sequence[ShadowScenario] = SHADOW_SCENARIOS) -> list[str]:
    return [scenario.name for scenario in scenarios]


__all__ = [
    "CONTROL_FLOW_FIELDS",
    "SHADOW_SCENARIOS",
    "ShadowReport",
    "ShadowScenario",
    "compare_scenario",
    "control_flow_trace",
    "failing_robustness_run",
    "run_scenario",
    "scenario_names",
    "snapshot_control_flow",
]
