"""真实节点装配：worker 侧的六阶段建模执行链。

与 API 侧 ``backend/api/omm_api/engine_glue.py`` 的节点装配同构（六个
agents/skills 节点 + 默认提示词注册表 + 规划阶段审批门），但有两个刻意差异：

- worker 不 import ``omm_api``，也没有 sim 模拟链可回落——执行面装配不完整
  （提示词缺失）是部署缺陷，必须在构造期失败，而不是在任务跑到一半时才暴露；
- API 侧针对前端附件契约的变量适配（``attachment_metadata`` 摘要）不在这里
  复制：Phase 2 接线时由控制面把算好的 ``attachments_summary`` 放进 run
  inputs，worker 只消费标准化后的输入。

沙箱工具、产物存储与事件汇的每-run 绑定在 ``runtime.WorkerRuntime._open_run``
（与 engine_glue 的 ``_build_tool_invoker`` 同构：allowlist 只放 python_run、
caller_max_tier="execute"、recorder 绑定引擎 record_external）。
"""

from __future__ import annotations

from typing import Any

from omm_agent_core import LlmPort, NodeContext, StepNode, TaskState
from omm_agent_skills import (
    DataPreparationNode,
    ExperimentExecutionNode,
    ModelPlanningNode,
    PaperWritingNode,
    ProblemAnalysisNode,
    PromptRegistry,
    ValidationNode,
    load_default_registry,
)

from .runtime import WorkerConfig, WorkerRuntime

#: 六阶段模板必须齐套才允许装配（engine_glue._REQUIRED_PROMPTS 的执行面子集：
#: 论文分章三件套 API 侧另有强制，worker 装配只查节点直接引用的模板）。
#: H3 前置刀后实验与清洗都走沙盒会话模板（experiment_code.sandbox /
#: data_cleaning.sandbox），experiment_code.default 退役；验证阶段的稳健性
#: 复跑用 validating.sandbox。方案阶段（H3）三视角并行提议 + 归约用
#: model_planning.proposer / model_planning.reduce，default 是无监督者时的回落。
REQUIRED_PROMPT_IDS = frozenset(
    {
        "problem_analysis.default",
        "data_preparation.default",
        "data_cleaning.sandbox",
        "model_planning.default",
        "model_planning.proposer",
        "model_planning.reduce",
        "experiment_code.sandbox",
        "validating.default",
        "validating.sandbox",
        "paper_writing.default",
    }
)


class GoalProblemAnalysisNode(ProblemAnalysisNode):
    """把 run 输入的 goal 映射成提示词需要的 problem_statement。

    控制面发布的任务输入形状是 ``{"goal": ..., "params": ...}``（见
    engine_glue.create_run_events 播种的 RUN_CREATED），而分析提示词的变量名
    是 problem_statement；显式给出的 problem_statement 优先。
    """

    def build_variables(self, ctx: NodeContext) -> dict[str, Any]:
        statement = ctx.inputs.get("problem_statement") or ctx.inputs.get("goal") or ""
        summary = str(ctx.inputs.get("attachments_summary") or "").strip()
        return {
            "problem_statement": str(statement),
            "attachments_summary": summary or "无",
        }


def build_real_nodes(
    *,
    unattended: bool = False,
    prompts: PromptRegistry | None = None,
) -> dict[TaskState, StepNode]:
    """六阶段真实节点注册表。

    ``unattended=True`` 关闭两个必停的人工审批门（方案确认 G1、定稿交付 G4，
    require_confirmation=False），供无人值守评测整链直跑；默认保持产品语义：
    方案产出后停在 REVIEW_REQUESTED 等待确认，论文发布后再停一次等确认交付。
    """
    registry = prompts or load_default_registry()
    missing = REQUIRED_PROMPT_IDS - set(registry.ids())
    if missing:
        raise ValueError(
            "提示词模板不齐套，真实节点装配失败（worker 无模拟链可回落）："
            + ", ".join(sorted(missing))
        )
    return {
        TaskState.PROBLEM_ANALYSIS: GoalProblemAnalysisNode(registry),
        TaskState.DATA_PREPARATION: DataPreparationNode(registry),
        TaskState.MODEL_PLANNING: ModelPlanningNode(
            registry, require_confirmation=not unattended
        ),
        TaskState.EXPERIMENTING: ExperimentExecutionNode(registry),
        TaskState.VALIDATING: ValidationNode(registry),
        TaskState.PAPER_WRITING: PaperWritingNode(
            registry, require_confirmation=not unattended
        ),
    }


def create_real_runtime(
    config: WorkerConfig,
    llm: LlmPort,
    *,
    unattended: bool = False,
    worker_id: str | None = None,
    clock: Any = None,
    ids: Any = None,
    prompts: PromptRegistry | None = None,
) -> WorkerRuntime:
    """装配跑真实六阶段链的 WorkerRuntime。

    这是 Phase 2「API→Worker 移交」的执行面入口：控制面进程只需提供 LLM 端口
    与文件系统布局（WorkerConfig），随后通过队列投递 advance 任务、通过
    ``apply_action`` 转发审批/重试等控制动作。
    """
    return WorkerRuntime(
        config,
        nodes=build_real_nodes(unattended=unattended, prompts=prompts),
        llm=llm,
        worker_id=worker_id,
        clock=clock,
        ids=ids,
    )
