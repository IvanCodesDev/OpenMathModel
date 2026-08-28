"""agents/core 引擎 ←→ backend/api 的胶水层（B2 换脑）。

架构（主规划 §5.3 / §13 Phase 2「状态投影器」）：
- ``run_domain_events`` 表是执行事实来源：引擎事件先落表（同事务），再驱动 v1 投影；
- v1 行（task_runs/step_runs/artifacts/approval_requests/agent_events）是投影，
  对外契约与事件流保持与旧模拟推进器兼容；
- 审批行的"解决"侧（option/comment/client_token 与 v1 approval.resolved 事件）归
  动作层 actions.py（它持有这些上下文），投影只负责审批行创建与请求/状态事件；
- 节点装配按运行归属决定：run → project.owner → users.llm_config。配置了
  自定义 API 的用户，六个建模阶段全部走 agents/skills 的真实 LLM 节点
  （llm.EngineLlmPort 出网；实验阶段另经 agents/tools 的 python 沙箱执行
  生成代码，工具调用通过引擎 record_external 留 TOOL_CALLED 事件）；
  未配置或提示词缺失时整条链回落 sim-0.1 模拟节点。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from omm_agent_core import (
    AgentEvent as CoreEvent,
)
from omm_agent_core import (
    ArtifactRef,
    EventType,
    NodeContext,
    NodeResult,
    NodeServices,
    TaskRunEngine,
    TaskRunSnapshot,
    TaskState,
    replay_events,
)
from omm_agent_core.errors import AgentError
from omm_agent_harness import BudgetGovernor, NodeBudget, RunBudget
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
from omm_agent_tools import PythonSandbox, RecordingInvoker, TaskWorkspace, ToolRegistry
from omm_contracts import (
    AgentEventType,
    ApprovalDecisionType,
    ApprovalStatus,
    ArtifactKind,
    ArtifactStatus,
    StepRunStatus,
    TaskRunStatus,
)

from .blobstore import ArtifactBlobStore, LocalContentStore
from .config import get_settings
from .events import append_event
from .ids import new_id
from .llm import EngineLlmPort, config_usable, is_third_party_host, parse_llm_config
from .models import User
from .orm import (
    AgentEventRow,
    ApprovalRequestRow,
    ArtifactRow,
    DomainEventRow,
    ProjectRow,
    RunNoteRow,
    StageOutputRow,
    StepRunRow,
    TaskRunRow,
)
from .serialize import utcnow
from .stage_outputs import REQUIRED_OUTPUT_KEYS, STAGE_OUTPUT_SCHEMA_IDS
from .usage import budget_exhausted, is_free_endpoint, record_usage
from .workflow import NODE_COMPLETED, STAGE_LABELS

logger = logging.getLogger("omm.engine")

CANCELLED_ERROR = "cancelled by user"
REJECT_OPTION_ID = "reject"

_ARTIFACT_NAMES = {
    "figure": "基线实验结果图（模拟）",
    "report": "建模报告草稿（模拟）",
    "paper": "建模论文草稿",
}

FAIL_EXPERIMENT_MARKER = "[fail:experiment]"


# ── 时钟与 ID：注入 API 的约定（32 位 hex 前缀 ID 满足 v1 模式） ────────────


class _ApiClock:
    def now_iso(self) -> str:
        return utcnow().isoformat()


class _ApiIds:
    def new_id(self, prefix: str) -> str:
        return new_id(prefix)


# ── Artifact 存储端口：进程级绑定（create_app 时注入；缺省按配置构建） ──────

_blobstore: ArtifactBlobStore | None = None


def set_blobstore(store: ArtifactBlobStore) -> None:
    global _blobstore
    _blobstore = store


def get_blobstore() -> ArtifactBlobStore:
    global _blobstore
    if _blobstore is None:
        _blobstore = LocalContentStore(get_settings().artifacts_dir)
    return _blobstore


class ApiArtifactStore:
    """实现 omm_agent_core 的 ArtifactStore 端口：内容落 BlobStore，引用带 local:// URI。"""

    def __init__(self, blobs: ArtifactBlobStore) -> None:
        self._blobs = blobs

    def put(
        self,
        run_id: str,
        kind: str,
        name: str,
        content: bytes,
        media_type: str,
        producer_step: str,
    ) -> ArtifactRef:
        sha256, size = self._blobs.put(content)
        return ArtifactRef(
            artifact_id=new_id("art"),
            kind=kind,
            uri=f"local://{sha256}/{name}",
            sha256=sha256,
            size=size,
            media_type=media_type,
            producer_step=producer_step,
        )


# ── sim-0.1 节点：与旧模拟推进器行为等价 ──────────────────────────────────


def _fail_config(inputs: dict[str, Any]) -> tuple[Optional[str], int]:
    params = dict(inputs.get("params") or {})
    fail_at = params.get("fail_at")
    fail_attempts = int(params.get("fail_attempts") or 1)
    if not fail_at and FAIL_EXPERIMENT_MARKER in str(inputs.get("goal") or ""):
        fail_at, fail_attempts = "EXPERIMENTING", 1
    return fail_at, fail_attempts


class SimStageNode:
    """一个工作状态一个节点；失败注入与产物行为对齐旧推进器。产物经 ArtifactStore 端口真实落盘。"""

    def __init__(self, state: TaskState) -> None:
        self._state = state

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        fail_at, fail_attempts = _fail_config(dict(ctx.inputs))
        if fail_at == self._state.value and ctx.attempt <= fail_attempts:
            return NodeResult.failed("失败注入：实验代码在本次尝试中报错（模拟）")

        artifacts: tuple[ArtifactRef, ...] = ()
        if self._state in (TaskState.EXPERIMENTING, TaskState.PAPER_WRITING):
            if services.artifacts is None:
                return NodeResult.failed("artifact 存储端口未装配，无法发布产物")
            if self._state is TaskState.EXPERIMENTING:
                kind, media, filename = "figure", "image/svg+xml", "baseline-metrics.svg"
                content = f"<svg><!-- simulated baseline figure run={ctx.run_id} attempt={ctx.attempt} --></svg>"
            else:
                kind, media, filename = "report", "text/markdown", "report-draft.md"
                content = f"# 建模报告草稿（模拟）\n\nrun: {ctx.run_id}\n"
            artifacts = (
                services.artifacts.put(
                    ctx.run_id,
                    kind,
                    filename,
                    content.encode("utf-8"),
                    media,
                    ctx.step_id,
                ),
            )

        label = STAGE_LABELS.get(self._state.value, self._state.value)
        if self._state is TaskState.MODEL_PLANNING:
            return NodeResult.needs_review(
                "确认建模方案后继续实验", outputs={"label": label}
            )
        return NodeResult.succeeded(outputs={"label": label}, artifacts=artifacts)


SIM_NODES = {state: SimStageNode(state) for state in TaskState if state.name in {
    "PROBLEM_ANALYSIS",
    "DATA_PREPARATION",
    "MODEL_PLANNING",
    "EXPERIMENTING",
    "VALIDATING",
    "PAPER_WRITING",
}}


# ── 真实 LLM 节点（设置中心「自定义 API」已配置时启用） ────────────────────


#: 附件摘要总量与单附件正文上限：控制进入提示词的 token 规模
_ATTACHMENT_SUMMARY_LIMIT = 4000
_ATTACHMENT_EXCERPT_LIMIT = 1200


_REFERENCE_KIND_LABELS = {"problem": "赛题", "paper": "优秀论文", "method": "方法"}


def _attachments_summary(params: dict[str, Any]) -> str:
    """运行参数里的附件与知识库引用 → 提示词附件摘要段。

    真实题面常在附件或 @ 引用的赛题里而不在首句指令里；把 excerpt 交给
    问题分析节点，分析产出（含 title 与 viability 判定）才反映实际要解决
    的问题。附件（attachment_metadata）与知识库引用（reference_metadata，
    首页「添加上下文」挑选的赛题/论文/方法）共用同一份预算。
    """
    parts: list[str] = []
    used = 0

    def push(piece: str) -> bool:
        nonlocal used
        if used + len(piece) > _ATTACHMENT_SUMMARY_LIMIT:
            parts.append("（其余材料从略）")
            return False
        parts.append(piece)
        used += len(piece)
        return True

    entries = params.get("attachment_metadata")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        excerpt = re.sub(r"\s+", " ", str(entry.get("excerpt") or "")).strip()
        piece = (
            f"《{name}》：{excerpt[:_ATTACHMENT_EXCERPT_LIMIT]}"
            if excerpt
            else f"《{name}》（正文尚未提取，仅有文件元数据）"
        )
        if not push(piece):
            return "\n".join(parts)

    references = params.get("reference_metadata")
    for entry in references if isinstance(references, list) else []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        excerpt = re.sub(r"\s+", " ", str(entry.get("excerpt") or "")).strip()
        if not title or not excerpt:
            continue
        label = _REFERENCE_KIND_LABELS.get(str(entry.get("kind") or ""), "资料")
        if not push(f"【引用{label}】《{title}》：{excerpt[:_ATTACHMENT_EXCERPT_LIMIT]}"):
            return "\n".join(parts)

    return "\n".join(parts)


class _GoalProblemAnalysisNode(ProblemAnalysisNode):
    """把运行输入的 goal 映射成提示词需要的 problem_statement。"""

    def build_variables(self, ctx: Any) -> dict[str, Any]:
        statement = ctx.inputs.get("problem_statement") or ctx.inputs.get("goal") or ""
        summary = str(ctx.inputs.get("attachments_summary") or "").strip()
        if not summary:
            summary = _attachments_summary(dict(ctx.inputs.get("params") or {}))
        return {
            "problem_statement": str(statement),
            "attachments_summary": summary or "无",
        }


class _ParamsDataPreparationNode(DataPreparationNode):
    """数据准备节点的 API 侧变量适配：附件摘要从运行参数的附件元数据提取。"""

    def build_variables(self, ctx: Any) -> dict[str, Any]:
        variables = super().build_variables(ctx)
        if variables["attachments_summary"] in ("", "无"):
            summary = _attachments_summary(dict(ctx.inputs.get("params") or {}))
            variables["attachments_summary"] = summary or "无"
        return variables


#: 实验代码允许使用的第三方库候选：沙箱与 API 同一解释器（sys.executable），
#: 在 API 进程探测一次即等于沙箱事实；结果注入实验提示词的 import 白名单，
#: 代码质量随环境升级（有 numpy/pandas 就不必被钉死在纯标准库）。
_SANDBOX_PACKAGE_CANDIDATES = (
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "statsmodels",
    "matplotlib",
    "networkx",
    "sympy",
)


@lru_cache(maxsize=1)
def _sandbox_packages() -> str:
    available = [
        name
        for name in _SANDBOX_PACKAGE_CANDIDATES
        if importlib.util.find_spec(name) is not None
    ]
    return "、".join(available) if available else "无（仅 Python 标准库）"


#: 六个阶段的模板齐套才启用真实链路：缺一个就整链回落模拟，
#: 避免「前两个阶段真实、后四个阶段无声退化」的混合链误导用户。
#: 论文阶段是分章多轮管线（doc/paper-multipass-generation-plan.md）：
#: 总编/章节/统稿三个模板与回退用的整篇模板都必须在场。
_REQUIRED_PROMPTS = frozenset(
    {
        "problem_analysis.default",
        "data_preparation.default",
        "model_planning.default",
        "experiment_code.default",
        "validating.default",
        "paper_outline.default",
        "paper_section.default",
        "paper_finalize.default",
        "paper_writing.default",
    }
)


@lru_cache(maxsize=1)
def _prompt_registry() -> Optional[PromptRegistry]:
    """agents/prompts 的模板注册表；目录缺失（异常部署）时返回 None 并回落模拟。"""
    try:
        registry = load_default_registry()
    except Exception:  # noqa: BLE001 - 提示词损坏不允许拖垮控制面
        logger.exception("加载提示词模板失败，任务将回落模拟节点")
        return None
    if not _REQUIRED_PROMPTS.issubset(set(registry.ids())):
        logger.warning("提示词模板不完整（%s），任务将回落模拟节点", registry.ids())
        return None
    return registry


# ── 预算治理接线（§4.7 C9）：run/node 级硬停在执行面生效 ────────────────────
#
# 治理器每次装配引擎时重建，账本从 run.log 事件（llm_call 用量、python_run
# 工具审计）持久重建——跨 advance、跨进程重启限额都成立。墙钟预算按执行
# 时间的口径需要跨事件求和（审批等待不计时），本批次明确延后不启用。

#: prompt_id → 预算记账的节点归属（论文分章管线的三个 prompt 同属论文节点）。
_PROMPT_NODE_IDS = {
    "problem_analysis.default": TaskState.PROBLEM_ANALYSIS.value,
    "data_preparation.default": TaskState.DATA_PREPARATION.value,
    "model_planning.default": TaskState.MODEL_PLANNING.value,
    "experiment_code.default": TaskState.EXPERIMENTING.value,
    "validating.default": TaskState.VALIDATING.value,
    "paper_outline.default": TaskState.PAPER_WRITING.value,
    "paper_section.default": TaskState.PAPER_WRITING.value,
    "paper_finalize.default": TaskState.PAPER_WRITING.value,
    "paper_writing.default": TaskState.PAPER_WRITING.value,
}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _run_budget_from_env() -> RunBudget:
    """§4.7 拍板默认值 + 环境变量覆盖（预算追加的临时通道，GB 闸门后续批次）。"""
    defaults = RunBudget()
    return RunBudget(
        max_total_tokens=_env_int("OMM_RUN_MAX_TOKENS", defaults.max_total_tokens),
        max_llm_calls=_env_int("OMM_RUN_MAX_LLM_CALLS", defaults.max_llm_calls),
        max_sandbox_runs=_env_int("OMM_RUN_MAX_SANDBOX_RUNS", defaults.max_sandbox_runs),
        # 墙钟按执行时间计的语义未实现：治理器按进程内时钟计会把审批等待也算进去，
        # 宁可不启用也不误伤（禁用 = 上限无穷大）。
        max_wall_clock_s=float("inf"),
    )


def _build_budget_governor(session: Session, run: TaskRunRow) -> BudgetGovernor:
    """账本从事件重建：llm_call 事件累计次数与 tokens（按 prompt 归属节点），
    python_run 工具审计累计沙箱次数。数据源与断点续写同为 run.log（事件即账本）。"""
    governor = BudgetGovernor(run_budget=_run_budget_from_env())
    node_cap = _env_int("OMM_NODE_MAX_TOKENS", NodeBudget().max_tokens)
    for node_id in set(_PROMPT_NODE_IDS.values()):
        governor.open_node(node_id, NodeBudget(max_tokens=node_cap))

    total_tokens = 0
    llm_calls = 0
    sandbox_runs = 0
    node_tokens: dict[str, int] = {}
    rows = session.execute(
        select(AgentEventRow.payload)
        .where(
            AgentEventRow.run_id == run.id,
            AgentEventRow.type == AgentEventType.run_log.value,
        )
        .order_by(AgentEventRow.sequence.asc())
    ).scalars()
    for payload in rows:
        data = payload or {}
        if data.get("kind") == "llm_call":
            llm_calls += 1
            tokens = int(data.get("prompt_tokens") or 0) + int(data.get("completion_tokens") or 0)
            total_tokens += tokens
            node_id = _PROMPT_NODE_IDS.get(str(data.get("prompt_id") or ""))
            if node_id is not None:
                node_tokens[node_id] = node_tokens.get(node_id, 0) + tokens
        elif data.get("tool") == PythonSandbox.TOOL_NAME:
            sandbox_runs += 1
    governor.seed_usage(
        total_tokens=total_tokens,
        llm_calls=llm_calls,
        sandbox_runs=sandbox_runs,
        node_tokens=node_tokens,
    )
    return governor


def _budget_stop_message(error: AgentError) -> str:
    """预算硬停 → 用户可行动的失败信息（错误码保留在文首供日志与聚合）。"""
    context = error.context or {}
    used = (
        f"已用 tokens {context.get('total_tokens', '?')}、"
        f"LLM 调用 {context.get('llm_calls', '?')} 次、"
        f"沙箱运行 {context.get('sandbox_runs', '?')} 次"
    )
    return (
        f"{error}。{used}。这是失控保护：确需继续，可提高环境变量上限"
        "（OMM_RUN_MAX_TOKENS / OMM_RUN_MAX_LLM_CALLS / OMM_RUN_MAX_SANDBOX_RUNS / "
        "OMM_NODE_MAX_TOKENS）后重试，或取消任务；GB 预算追加闸门在后续批次提供。"
    )


class _BudgetGuardedNode:
    """把节点内抛出的预算硬停（AgentError E31x/E32x）转成干净的步骤失败信息，
    避免引擎兜底把整段 traceback 当失败原因展示给用户。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        try:
            return self._inner.run(ctx, services)
        except AgentError as error:
            return NodeResult.failed(_budget_stop_message(error))


class _BudgetedInvoker:
    """沙箱运行按次预付计费（§4.7：started run is spent money），其余委托原样。"""

    def __init__(self, inner: Any, governor: BudgetGovernor) -> None:
        self._inner = inner
        self._governor = governor

    def invoke(self, run_id: str, step_id: str, tool_name: str, arguments: dict) -> Any:
        if tool_name == PythonSandbox.TOOL_NAME:
            self._governor.charge_sandbox_run()
        return self._inner.invoke(run_id, step_id, tool_name, arguments)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _paper_resume_reader(session: Session, run_id: str) -> Callable[[], Optional[dict]]:
    """论文分章管线的断点数据：从 run.log 事件重建「最新骨架 + 其后已完成章节」。

    检查点就是事件日志本身（paper_outline 事件带 inputs_hash 与完整骨架，
    paper_section 事件带全文与摘要）：零新增表、零新端口，持久性与事件同级。
    数据一致性（哈希匹配、章节前缀连续、标题对得上）由节点侧校验。
    """

    def read() -> Optional[dict]:
        rows = session.execute(
            select(AgentEventRow.payload)
            .where(
                AgentEventRow.run_id == run_id,
                AgentEventRow.type == AgentEventType.run_log.value,
            )
            .order_by(AgentEventRow.sequence.asc())
        ).scalars()
        outline_payload: Optional[dict] = None
        sections: dict[int, dict] = {}
        for payload in rows:
            data = payload or {}
            kind = data.get("kind")
            if kind == "paper_outline" and isinstance(data.get("outline"), dict):
                # 新一轮骨架出现（重试且输入变化时）：旧章节随旧骨架一起作废
                outline_payload = data
                sections = {}
            elif kind == "paper_section" and outline_payload is not None:
                index = data.get("index")
                if isinstance(index, int):
                    sections[index] = data
        if outline_payload is None:
            return None
        return {
            "inputs_hash": outline_payload.get("inputs_hash"),
            "outline": outline_payload.get("outline"),
            "sections": [sections[index] for index in sorted(sections)],
        }

    return read


# ── 装配档位（§4.9 profiles）：OMM_AGENT_NODES=sim|real|mixed ────────────────

_NODES_MODE_ENV = "OMM_AGENT_NODES"
_VALID_NODE_MODES = frozenset({"sim", "real", "mixed"})


def _nodes_mode() -> str:
    """节点档位开关；默认 mixed=按运行归属自动装配（现状行为）。

    非法值按 mixed 处理并留警告日志：环境变量没有"启动即报错"的落点
    （per-run 装配发生在每次 tick），拼写错误不允许把行为静默切到别的档位。
    """
    raw = os.environ.get(_NODES_MODE_ENV, "mixed").strip().lower() or "mixed"
    if raw not in _VALID_NODE_MODES:
        logger.warning(
            "%s=%r 不是合法档位（sim|real|mixed），按 mixed 处理", _NODES_MODE_ENV, raw
        )
        return "mixed"
    return raw


class _RealModeUnavailableNode:
    """`OMM_AGENT_NODES=real` 而真实链路不可用时的显式失败节点。

    §4.9：real 档位绝不静默回落模拟——静默降级正是"半真实磨损信任"（R-C）
    的温床。失败走既有 STEP_FAILED→RUN_FAILED 路径，UI 提供 retry。
    """

    def run(self, ctx: NodeContext, services: NodeServices) -> NodeResult:
        return NodeResult.failed(
            "OMM_AGENT_NODES=real 要求全真实节点，但本次运行无可用 LLM 链路"
            "（未配置自定义 API、配置不可用、预算受限且无免费接口，或提示词模板不齐）。"
            "请在设置中心配置可用的模型接口后重试，或移除该环境变量按运行归属自动装配。"
        )


def _llm_wiring(
    session: Session, run: TaskRunRow, checkpoint: bool = False
) -> tuple[Optional[EngineLlmPort], dict, dict[str, Any]]:
    """档位开关包装：sim 强制整链模拟；real 禁止静默回落；mixed=自动装配。"""
    mode = _nodes_mode()
    if mode == "sim":
        # 强制模拟：SimStageNode 的产出自带"（模拟）"标注（原则 10），
        # 与 mixed 档位"配置不可用回落"走完全相同的展示路径。
        return None, {}, {}
    port, overrides, extras = _llm_wiring_impl(session, run, checkpoint=checkpoint)
    if mode == "real" and port is None:
        failure = _RealModeUnavailableNode()
        return None, {state: failure for state in SIM_NODES}, {}
    return port, overrides, extras


def _llm_wiring_impl(
    session: Session, run: TaskRunRow, checkpoint: bool = False
) -> tuple[Optional[EngineLlmPort], dict, dict[str, Any]]:
    """按运行归属解析自定义 API 配置：可用则换上真实 LLM 节点。

    第三个返回值是 NodeServices.extras：节点内进度事件的落库回调（progress，
    run.log 旁路观测通道）与论文断点续写的读取器（paper_resume）。
    """
    owner = session.execute(
        select(ProjectRow.owner).where(ProjectRow.id == run.project_id)
    ).scalar_one_or_none()
    user = session.get(User, owner) if owner else None
    if user is None:
        return None, {}, {}
    config = parse_llm_config(user.llm_config)
    registry = _prompt_registry()
    if not config_usable(config) or registry is None:
        return None, {}, {}
    # 预算硬限制（设置中心「用量监控」）：达标后只留本地/免费接口；
    # 一个不剩则整条链回落模拟节点，并在 run.log 留痕说明原因。
    if budget_exhausted(session, user):
        free = tuple(endpoint for endpoint in config.endpoints if is_free_endpoint(endpoint))
        if not free:
            append_event(
                session,
                run.id,
                AgentEventType.run_log.value,
                {
                    "kind": "budget_limit",
                    "message": "本月预估费用已达预算上限，付费模型已暂停，"
                    "本次任务改用模拟节点推进；可在设置中心「用量监控」调整预算或关闭硬限制",
                },
            )
            return None, {}, {}
        config = replace(config, endpoints=free)
    # 模型调用的过程事件（调用开始/思考内容/调用摘要）进 run.log，SSE 转发给
    # 工作台的执行轨迹逐条展示。推进线程（checkpoint 模式）下每条过程事件在
    # 发生的当下就独立提交：SSE 是「轮询已提交行」的模式，不提交就要等节点结束
    # 的下一次 checkpoint，一个阶段几分钟的模型调用期间工作台会完全静默、结束时
    # 一次性闪现整批过程行。HTTP 动作路径（checkpoint=False）保持整请求一个
    # 事务，不在中途提交。每次成功调用同时记入 llm_usage_records（用量监控）。
    def _process_event(payload: dict) -> None:
        append_event(session, run.id, AgentEventType.run_log.value, payload)
        if checkpoint:
            session.commit()

    # 预算治理（C9 接线）：账本从事件重建，run/node 级硬停在真实链路生效
    governor = _build_budget_governor(session, run)
    # 运行中用户备注（§11.3 方案 A）：端口按 tick 重建，构造时快照本表——
    # 新备注在下一次节点执行自然生效，正在执行中的节点不被打断。
    user_notes = tuple(
        (row.scope, row.text)
        for row in session.execute(
            select(RunNoteRow)
            .where(RunNoteRow.run_id == run.id)
            .order_by(RunNoteRow.created_at.asc(), RunNoteRow.id.asc())
        ).scalars()
    )
    port = EngineLlmPort(
        config,
        registry,
        on_event=_process_event,
        on_usage=lambda outcome: record_usage(
            session,
            user_id=user.id,
            source="agent",
            outcome=outcome,
            third_party=is_third_party_host(outcome.endpoint.host),
            run_id=run.id,
        ),
        budget=governor,
        node_for_prompt=_PROMPT_NODE_IDS,
        user_notes=user_notes,
    )
    overrides = {
        state: _BudgetGuardedNode(node)
        for state, node in {
            TaskState.PROBLEM_ANALYSIS: _GoalProblemAnalysisNode(registry),
            TaskState.DATA_PREPARATION: _ParamsDataPreparationNode(registry),
            TaskState.MODEL_PLANNING: ModelPlanningNode(registry),
            TaskState.EXPERIMENTING: ExperimentExecutionNode(
                registry, available_packages=_sandbox_packages()
            ),
            TaskState.VALIDATING: ValidationNode(registry),
            TaskState.PAPER_WRITING: PaperWritingNode(registry),
        }.items()
    }
    extras: dict[str, Any] = {
        # 节点内进度事件（论文分章管线逐章上报）与模型过程事件同路落 run.log
        "progress": _process_event,
        # 论文断点续写：重试时从事件日志重建已完成章节，跳过总编与已写章节
        "paper_resume": _paper_resume_reader(session, run.id),
        # 沙箱计费与运行报告共用同一治理器（open_engine 里包装工具端口）
        "budget_governor": governor,
    }
    return port, overrides, extras


# ── 领域事件 → v1 投影 ─────────────────────────────────────────────────────


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _project_status(
    session: Session, run: TaskRunRow, to_status: str, reason: str
) -> None:
    from_status = run.status
    run.status = to_status
    run.updated_at = utcnow()
    append_event(
        session,
        run.id,
        AgentEventType.run_status_changed.value,
        {"from": from_status, "to": to_status, "reason": reason},
    )


def _project_node(session: Session, run: TaskRunRow, to_node: str, reason: str) -> None:
    from_node = run.current_node
    run.current_node = to_node
    run.updated_at = utcnow()
    append_event(
        session,
        run.id,
        AgentEventType.run_node_changed.value,
        {
            "from": from_node,
            "to": to_node,
            "reason": reason,
            "label": STAGE_LABELS.get(to_node, to_node),
        },
    )


_GOAL_NAME_PREFIX = re.compile(r"^(?:请帮我|请|帮我|我想(?:要)?)")
_GOAL_NAME_SPLIT = re.compile(r"[。！？!?；;\n]")


def derive_name_from_goal(goal: str) -> str:
    """复刻 apps/web task-start-state.ts 的 deriveProjectName：识别「仍是自动名」。

    两端算法必须一致，才能判断项目名是否还是创建时从首句截取的默认值；
    不一致时的后果是安全的——只会跳过自动重命名，绝不覆盖用户手动改的名。
    """
    compact = re.sub(r"\s+", " ", goal.replace("\r\n", "\n").replace("\r", "\n")).strip()
    compact = _GOAL_NAME_PREFIX.sub("", compact).strip()
    first = _GOAL_NAME_SPLIT.split(compact, maxsplit=1)[0].strip() or "未命名建模任务"
    characters = list(first)
    return "".join(characters[:24]) + "…" if len(characters) > 24 else first


def _maybe_rename_project(session: Session, run: TaskRunRow, outputs: dict[str, Any]) -> None:
    """问题分析产出 title 后，把「最近任务」的名字换成实际讨论的问题。

    仅当项目名仍等于按 goal 自动截取的默认名时才替换：名字对不上说明用户
    已手动命名，视为显式意图不覆盖。sim 节点的 outputs 没有 title，自然跳过。
    """
    title = re.sub(r"\s+", " ", str(outputs.get("title") or "")).strip()[:60]
    if not title:
        return
    project = session.get(ProjectRow, run.project_id)
    if project is None or project.name == title:
        return
    if project.name != derive_name_from_goal(run.goal or ""):
        return
    previous = project.name
    project.name = title
    project.updated_at = utcnow()
    append_event(
        session,
        run.id,
        AgentEventType.run_log.value,
        {
            "kind": "task_renamed",
            "message": f"已按题意将任务命名为「{title}」",
            "from": previous,
            "to": title,
        },
    )


def _input_hash(run: TaskRunRow, node: str, attempt: int) -> str:
    canonical = json.dumps(
        {"run_id": run.id, "node": node, "attempt": attempt, "params": run.params or {}},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_stage_output(
    session: Session,
    run: TaskRunRow,
    step: StepRunRow,
    outputs: dict[str, Any],
    at: datetime,
) -> None:
    """STEP_SUCCEEDED → stage_outputs 版本化落行（设计 §10.2，H1）。

    只落有契约实质内容的输出（与读侧空投影同一门槛，模拟节点的 {"label"}
    不落行）；同节点旧 current 置 superseded，version 跨重试单调递增——
    「重做产生 v2 且 v1 superseded」由此成为持久事实。
    """
    required_key = REQUIRED_OUTPUT_KEYS.get(step.node)
    if required_key is None or required_key not in outputs:
        return
    latest_version = 0
    for row in session.execute(
        select(StageOutputRow).where(
            StageOutputRow.run_id == run.id,
            StageOutputRow.node == step.node,
        )
    ).scalars():
        latest_version = max(latest_version, int(row.version))
        if row.status == "current":
            row.status = "superseded"
    canonical = json.dumps(outputs, ensure_ascii=False, sort_keys=True)
    session.add(
        StageOutputRow(
            id=new_id("sout"),
            run_id=run.id,
            node=step.node,
            lane_id=None,
            version=latest_version + 1,
            schema_id=STAGE_OUTPUT_SCHEMA_IDS[step.node],
            content=outputs,
            content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            producer_step_id=step.id,
            status="current",
            created_at=at,
        )
    )


def _project(session: Session, run: TaskRunRow, event: CoreEvent) -> None:
    """把一条领域事件翻译成 v1 行与 v1 事件（agent_events 自己维护 sequence）。"""
    kind = event.event_type
    payload = event.payload
    at = _dt(event.created_at)

    if kind is EventType.RUN_CREATED:
        inputs = dict(payload.get("inputs") or {})
        append_event(
            session,
            run.id,
            AgentEventType.run_created.value,
            {"goal": inputs.get("goal"), "auto_start": bool(inputs.get("auto_start", True))},
        )
        return

    if kind is EventType.STATE_CHANGED:
        to_node = str(payload["to"])
        if run.status == TaskRunStatus.QUEUED.value:
            run.started_at = at
            _project_status(session, run, TaskRunStatus.RUNNING.value, "任务开始")
        _project_node(session, run, to_node, "进入阶段")
        return

    if kind is EventType.STEP_STARTED:
        node = str(payload["state"])
        attempt = int(payload["attempt"])
        if run.current_node != node:
            _project_node(session, run, node, "阶段开始")
        session.add(
            StepRunRow(
                id=str(payload["step_id"]),
                run_id=run.id,
                node=node,
                attempt=attempt,
                status=StepRunStatus.RUNNING.value,
                input_hash=_input_hash(run, node, attempt),
                detail=f"{STAGE_LABELS.get(node, node)}（第 {attempt} 次尝试）",
                created_at=at,
                started_at=at,
            )
        )
        append_event(
            session,
            run.id,
            AgentEventType.step_started.value,
            {"node": node, "attempt": attempt, "label": STAGE_LABELS.get(node, node)},
        )
        return

    if kind is EventType.STEP_SUCCEEDED:
        step = session.get(StepRunRow, str(payload["step_id"]))
        if step is not None:
            step.status = StepRunStatus.SUCCEEDED.value
            step.ended_at = at
            outputs = dict(payload.get("outputs") or {})
            _record_stage_output(session, run, step, outputs, at)
            append_event(
                session,
                run.id,
                AgentEventType.step_succeeded.value,
                # outputs 随事件下发：工作台执行轨迹的「阶段产出」可展开行
                # 的数据源（v1 契约 payload 为自由对象，消费方容忍未知字段）。
                {"node": step.node, "attempt": step.attempt, "outputs": outputs},
            )
            if step.node == TaskState.PROBLEM_ANALYSIS.value:
                _maybe_rename_project(session, run, outputs)
        return

    if kind is EventType.STEP_FAILED:
        step = session.get(StepRunRow, str(payload["step_id"]))
        if step is not None:
            step.status = StepRunStatus.FAILED.value
            step.failure_class = "CODE_DEFECT"
            step.detail = str(payload.get("error") or "step failed")
            step.ended_at = at
            append_event(
                session,
                run.id,
                AgentEventType.step_failed.value,
                {"node": step.node, "attempt": step.attempt, "failure_class": "CODE_DEFECT"},
            )
        return

    if kind is EventType.ARTIFACT_PRODUCED:
        ref = dict(payload["artifact"])
        # kind 映射优先（模拟链路的中文名不变）；未登记的 kind 取 URI 尾部的
        # 真实文件名（实验代码产出的 table/figure/code 等按文件名展示）。
        uri_tail = str(ref.get("uri") or "").rstrip("/").rsplit("/", 1)[-1]
        name = _ARTIFACT_NAMES.get(str(ref.get("kind"))) or uri_tail or str(ref.get("kind"))
        # v1 契约把 kind 约束为枚举；节点/工具产生的词不在表内时归入 other，
        # 一个新产物类型绝不允许把序列化层打成 500。
        valid_kinds = {member.value for member in ArtifactKind}
        artifact_kind = str(ref["kind"]) if str(ref["kind"]) in valid_kinds else "other"
        session.add(
            ArtifactRow(
                id=str(ref["artifact_id"]),
                project_id=run.project_id,
                run_id=run.id,
                kind=artifact_kind,
                name=name,
                uri=str(ref["uri"]),
                sha256=str(ref["sha256"]),
                size_bytes=int(ref["size"]),
                media_type=str(ref["media_type"]),
                producer_step=str(ref["producer_step"]),
                status=ArtifactStatus.READY.value,
                created_at=at,
            )
        )
        append_event(
            session,
            run.id,
            AgentEventType.artifact_published.value,
            {
                "artifact_id": ref["artifact_id"],
                "kind": ref["kind"],
                "name": name,
                "uri": ref["uri"],
            },
        )
        return

    if kind is EventType.REVIEW_REQUESTED:
        approval = ApprovalRequestRow(
            id=new_id("appr"),
            run_id=run.id,
            decision_type=ApprovalDecisionType.confirm_plan.value,
            title=str(payload.get("reason") or "确认建模方案后继续实验"),
            options=[
                {"id": "approve", "label": "采用当前方案", "description": "确认方案并进入实验阶段"},
                {"id": "reject", "label": "退回重做方案", "description": "重新执行建模方案阶段并再次确认"},
            ],
            evidence={
                "note": str(payload.get("reason") or "建模方案确认请求"),
                "requested_by_step": str(payload.get("requested_by_step") or ""),
            },
            status=ApprovalStatus.PENDING.value,
            requested_at=at,
        )
        session.add(approval)
        append_event(
            session,
            run.id,
            AgentEventType.approval_requested.value,
            {
                "approval_id": approval.id,
                "decision_type": approval.decision_type,
                "title": approval.title,
            },
        )
        _project_status(session, run, TaskRunStatus.WAITING_APPROVAL.value, "等待方案确认")
        return

    if kind is EventType.REVIEW_RESOLVED:
        # 审批行更新与 v1 approval.resolved 事件由动作层完成（它持有 option/comment）
        if payload.get("approved"):
            _project_status(session, run, TaskRunStatus.RUNNING.value, "方案已确认")
        return

    if kind is EventType.RUN_RETRIED:
        run.failure_class = None
        run.failure_message = None
        if run.status == TaskRunStatus.WAITING_APPROVAL.value:
            _project_status(session, run, TaskRunStatus.RUNNING.value, "方案退回重做")
        elif run.status != TaskRunStatus.RUNNING.value:
            _project_status(session, run, TaskRunStatus.RUNNING.value, "重试失败阶段")
        return

    if kind is EventType.RUN_PAUSED:
        run.paused_from_status = run.status
        _project_status(session, run, TaskRunStatus.PAUSED.value, "用户暂停")
        return

    if kind is EventType.RUN_RESUMED:
        run.paused_from_status = None
        _project_status(session, run, TaskRunStatus.RUNNING.value, "用户恢复")
        return

    if kind is EventType.RUN_CANCELLED:
        return  # 只是意图标志；终态由随后的 advance 落定

    if kind is EventType.RUN_COMPLETED:
        run.ended_at = at
        _project_node(session, run, NODE_COMPLETED, "全部阶段完成")
        _project_status(session, run, TaskRunStatus.COMPLETED.value, "全部阶段完成")
        return

    if kind is EventType.RUN_FAILED:
        error = str(payload.get("error") or "unknown failure")
        if error == CANCELLED_ERROR:
            now = utcnow()
            running_steps = session.execute(
                select(StepRunRow).where(
                    StepRunRow.run_id == run.id,
                    StepRunRow.status == StepRunStatus.RUNNING.value,
                )
            ).scalars()
            for step in running_steps:
                step.status = StepRunStatus.CANCELLED.value
                step.ended_at = now
            pending = session.execute(
                select(ApprovalRequestRow).where(
                    ApprovalRequestRow.run_id == run.id,
                    ApprovalRequestRow.status == ApprovalStatus.PENDING.value,
                )
            ).scalars()
            for approval in pending:
                approval.status = ApprovalStatus.CANCELLED.value
            run.paused_from_status = None
            run.ended_at = now
            _project_status(session, run, TaskRunStatus.CANCELLED.value, "用户取消")
            return
        run.failure_class = "CODE_DEFECT"
        run.failure_message = error
        _project_status(session, run, TaskRunStatus.FAILED.value, "实验步骤失败")
        return

    if kind is EventType.TOOL_CALLED:
        append_event(session, run.id, AgentEventType.run_log.value, dict(payload))
        return


class _ProjectingSink:
    """引擎事件汇：先落 run_domain_events（执行真相），再做 v1 投影。

    领域事件与它的 v1 投影始终在同一个事务里，两者不会脱节。``checkpoint``
    决定这个事务有多大：

    - 关闭（HTTP 动作路径）：整个请求一个事务，动作要么整体生效要么整体回滚；
    - 打开（推进器 tick）：每条事件单独提交。真实节点是分钟级的（LLM 调用、
      沙箱执行），把整个 tick 包成一个事务会让 SQLite 的单写锁被占满节点执行
      全程，并发请求等满 busy_timeout 后以 "database is locked" 失败（页面
      表现为对话 500）。逐条提交同时满足引擎的持久化契约——事件在应用到快照
      前就已落盘，进程中途死亡由重放与 heal 修复，而不是靠回滚整个 tick。
    """

    def __init__(self, session: Session, run: TaskRunRow, checkpoint: bool = False) -> None:
        self._session = session
        self._run = run
        self._checkpoint = checkpoint

    def emit(self, event: CoreEvent) -> None:
        self._session.add(
            DomainEventRow(
                run_id=event.run_id,
                seq=event.seq,
                event_type=event.event_type.value,
                payload=event.payload,
                created_at=event.created_at,
            )
        )
        _project(self._session, self._run, event)
        if self._checkpoint:
            self._session.commit()


# ── 适配器：runner / actions / router 的统一入口 ──────────────────────────


def _load_core_events(session: Session, run_id: str) -> list[CoreEvent]:
    rows = session.execute(
        select(DomainEventRow)
        .where(DomainEventRow.run_id == run_id)
        .order_by(DomainEventRow.seq.asc())
    ).scalars()
    return [
        CoreEvent(
            run_id=row.run_id,
            seq=row.seq,
            event_type=EventType(row.event_type),
            payload=dict(row.payload or {}),
            created_at=row.created_at,
        )
        for row in rows
    ]


def _build_engine(
    session: Session, run: TaskRunRow, checkpoint: bool = False
) -> tuple[TaskRunEngine, NodeServices]:
    llm_port, node_overrides, extras = _llm_wiring(session, run, checkpoint=checkpoint)
    services = NodeServices(
        clock=_ApiClock(),
        ids=_ApiIds(),
        artifacts=ApiArtifactStore(get_blobstore()),
        llm=llm_port,
        extras=extras,
    )
    engine = TaskRunEngine(
        sink=_ProjectingSink(session, run, checkpoint=checkpoint),
        clock=_ApiClock(),
        ids=_ApiIds(),
        nodes={**SIM_NODES, **node_overrides},
        services=services,
    )
    return engine, services


def _build_tool_invoker(
    engine: TaskRunEngine, snapshot: TaskRunSnapshot, run: TaskRunRow
) -> RecordingInvoker:
    """实验节点的工具端口：python 沙箱 + 允许列表 + execute 最小授权。

    产物存储注入 ApiArtifactStore：实验代码创建的文件直接进内容寻址存储，
    与其他产物同一条下载链路；工作区目录只是执行暂存。
    工具事件走引擎 record_external（序列分配必须留在引擎单路径上），
    随 _ProjectingSink 投影成 v1 run.log，工作台执行轨迹可见每次调用。
    """
    settings = get_settings()
    workspace = TaskWorkspace(settings.workspaces_dir, run.id)
    sandbox = PythonSandbox(
        workspace,
        timeout_s=settings.experiment_timeout_seconds,
        store=ApiArtifactStore(get_blobstore()),
    )
    registry = ToolRegistry()
    registry.register(sandbox.spec())

    def record(event_type: EventType, payload: dict[str, Any]) -> CoreEvent:
        return engine.record_external(snapshot, event_type, payload)

    return RecordingInvoker(
        registry.with_allowlist({PythonSandbox.TOOL_NAME}),
        record,
        caller_max_tier="execute",
    )


def open_engine(
    session: Session, run: TaskRunRow, checkpoint: bool = False
) -> tuple[TaskRunEngine, TaskRunSnapshot]:
    engine, services = _build_engine(session, run, checkpoint=checkpoint)
    events = _load_core_events(session, run.id)
    snapshot = replay_events(run.id, run.project_id, events)
    if services.llm is not None:
        # 工具事件要挂在当前快照的事件序列上，因此在快照重放之后绑定。
        invoker = _build_tool_invoker(engine, snapshot, run)
        governor = services.extras.get("budget_governor")
        # 沙箱运行按次预付计费：越线的那次运行根本不会启动（§4.7）
        services.tools = _BudgetedInvoker(invoker, governor) if governor else invoker
    return engine, snapshot


def create_run_events(session: Session, run: TaskRunRow, goal: str, auto_start: bool) -> None:
    """为新建的 v1 运行行播种领域日志（RUN_CREATED → v1 run.created 投影）。

    先 flush：run 行此刻可能仍在 pending，而事件表与 task_runs 之间只有外键、
    没有 ORM relationship，unit-of-work 不保证跨 mapper 的插入顺序——
    PostgreSQL 会以外键违规拒绝先插入的事件行（SQLite 默认不查外键，掩盖此序）。
    """
    session.flush()
    engine, _ = _build_engine(session, run)
    engine.create_run(
        project_id=run.project_id,
        inputs={"goal": goal, "params": run.params or {}, "auto_start": auto_start},
        run_id=run.id,
    )


def advance_run(session: Session, run: TaskRunRow) -> None:
    """一次 tick：终态/等待态直接返回，否则引擎推进一步（含取消落定）。

    唯一开 checkpoint 的入口：节点执行是这里最慢的一段，写锁不能跨过它。
    """
    idle = {
        TaskRunStatus.PAUSED.value,
        TaskRunStatus.WAITING_APPROVAL.value,
        TaskRunStatus.FAILED.value,
        TaskRunStatus.COMPLETED.value,
        TaskRunStatus.CANCELLED.value,
    }
    if run.status in idle:
        return
    engine, snapshot = open_engine(session, run, checkpoint=True)
    # 进程中途死亡（开发期热重载、崩溃）会留下悬挂 RUNNING 的步骤：先按引擎的
    # 修复语义把它们落定为 STEP_FAILED（"executor lost"），事件日志与 step_runs
    # 才是闭合的；随后的 advance 以 attempt+1 重跑该阶段。进程内互斥由唯一的
    # 推进线程保证（本函数是 checkpoint 模式的唯一入口）。
    engine.heal_interrupted(snapshot)
    engine.advance(snapshot)


def pause_run(session: Session, run: TaskRunRow) -> None:
    engine, snapshot = open_engine(session, run)
    engine.request_pause(snapshot)


def resume_run(session: Session, run: TaskRunRow) -> None:
    engine, snapshot = open_engine(session, run)
    engine.resume(snapshot)


def cancel_run(session: Session, run: TaskRunRow) -> None:
    engine, snapshot = open_engine(session, run)
    engine.request_cancel(snapshot)
    engine.advance(snapshot)  # 立即落定 CANCELLED（同步等价于 worker 的 finalize enqueue）


def retry_run(session: Session, run: TaskRunRow) -> None:
    engine, snapshot = open_engine(session, run)
    engine.retry(snapshot)


def resolve_approval(session: Session, run: TaskRunRow, option_id: str) -> None:
    """approve 动作的引擎侧：批准=继续下一阶段；拒绝=重做 MODEL_PLANNING 并再次请求确认。

    两个分支都只落「审批已解决」的状态（投影把 run 置回 RUNNING），实际推进
    交给 RunnerThread 的下一个 tick：真实节点是分钟级长任务（LLM 调用 +
    实验代码执行），不允许在 HTTP 动作请求里同步执行。
    """
    engine, snapshot = open_engine(session, run)
    if option_id == REJECT_OPTION_ID:
        engine.resolve_review(snapshot, approved=False, reason="方案退回重做")
        engine.retry(snapshot)  # RUN_RETRIED 投影置回 RUNNING；下个 tick 重跑 MODEL_PLANNING
        return
    engine.resolve_review(snapshot, approved=True, reason=option_id)  # 下个 tick 进入 EXPERIMENTING
