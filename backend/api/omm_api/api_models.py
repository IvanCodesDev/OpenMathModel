"""列表响应包装模型（与 OpenAPI components 对齐）。"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from omm_contracts import (
    AgentEvent,
    ApprovalRequest,
    Artifact,
    DatasetProfile,
    DeliveryManifest,
    DocumentDraft,
    ExperimentSummary,
    PlanProposal,
    ProblemFrame,
    Project,
    StepRun,
    TaskRun,
)


class ProjectList(BaseModel):
    items: list[Project]
    total: int


class ProjectUpdateInput(BaseModel):
    """项目维护输入（侧栏「最近任务」的重命名与归档）；两项都可省略。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    #: true = 归档（从默认列表消失），false = 取消归档；None = 不改动。
    archived: Optional[bool] = None


class TaskRunList(BaseModel):
    items: list[TaskRun]
    total: int


class StepRunList(BaseModel):
    items: list[StepRun]


class ApprovalList(BaseModel):
    items: list[ApprovalRequest]


class AgentEventList(BaseModel):
    items: list[AgentEvent]


class ArtifactList(BaseModel):
    items: list[Artifact]
    total: int


#: run_notes 的合法 scope：global=后续全部阶段；某阶段值=只作用于该阶段的调用。
RunNoteScope = Literal[
    "global",
    "PROBLEM_ANALYSIS",
    "DATA_PREPARATION",
    "MODEL_PLANNING",
    "EXPERIMENTING",
    "VALIDATING",
    "PAPER_WRITING",
]


class RunNoteInput(BaseModel):
    """运行中补充要求（§11.3 方案 A）：POST /v1/task-runs/{run_id}/notes。"""

    text: str = Field(min_length=1, max_length=2000)
    scope: RunNoteScope = "global"


class RunNote(BaseModel):
    id: str
    run_id: str
    text: str
    scope: str
    created_at: str


class RunRevisionInput(BaseModel):
    """已完成运行的修改要求（ADR-0013）：POST /v1/task-runs/{run_id}/revisions。

    只收要求正文，不收重做起点：起点由服务端按正文给出建议、再经审批门由用户
    拍板（同一句「结论不够有力」既可能只重写论文、也可能要重做实验，差一个
    数量级的花费，不能替用户默选）。
    """

    text: str = Field(min_length=1, max_length=2000)


class RunRevision(BaseModel):
    """修订受理回执：这一轮的轮次、待确认的审批、服务端建议的重做起点。"""

    run_id: str
    round: int
    approval_id: str
    suggested_stage: str
    note_id: str


class StageOutputs(BaseModel):
    """六类页面正文投影的聚合响应：GET /v1/task-runs/{run_id}/stage-outputs。

    每个字段是对应阶段（PROBLEM_ANALYSIS/DATA_PREPARATION/MODEL_PLANNING/
    EXPERIMENTING+VALIDATING/PAPER_WRITING）真实节点的最新成功输出投影；阶段
    尚未成功完成时对应字段为 null（不是 404——运行本身存在，只是该阶段的正文
    还没有）。delivery_manifest 在运行尚无任何可交付内容时也为 null。
    """

    run_id: str
    problem_frame: Optional[ProblemFrame] = None
    dataset_profile: Optional[DatasetProfile] = None
    plan_proposal: Optional[PlanProposal] = None
    experiment_summary: Optional[ExperimentSummary] = None
    document_draft: Optional[DocumentDraft] = None
    delivery_manifest: Optional[DeliveryManifest] = None


class RunMetricsReport(BaseModel):
    """E6 看板运行级报告：GET /v1/task-runs/{run_id}/metrics（harness ``aggregate_run`` 的输出）。

    顶层 13 键固定；各节内部随聚合器演进（加键不升 version，删键 / 改语义才升），看板页面落地前
    不把节内形状冻进公开契约，这里按开放对象透传。``code``（各步骤执行进程的代码版本）是后加的
    键，旧客户端忽略即可。
    """

    version: int
    run: dict[str, Any]
    stages: dict[str, Any]
    gates: dict[str, Any]
    iterations: dict[str, Any]
    failures: dict[str, Any]
    tools: dict[str, Any]
    reviews: dict[str, Any]
    robustness: dict[str, Any]
    audit: dict[str, Any]
    llm: dict[str, Any]
    process: dict[str, Any]
    code: dict[str, Any]


class TaskIntakeAttachment(BaseModel):
    """接待判定可见的附件证据：文件名与浏览器已解析出的正文摘录。"""

    name: str = Field(min_length=1, max_length=255)
    excerpt: str = Field(default="", max_length=2000)
    characters: int = Field(default=0, ge=0)


class TaskIntakeTurn(BaseModel):
    """首页对话里的一轮原话（不含前端注入的任务 / 附件 / 模式指令块）。"""

    role: Literal["user", "assistant"]
    text: str = Field(default="", max_length=4000)


class TaskIntakePendingTask(BaseModel):
    """上一轮接待提议、正等用户确认的建模任务（ADR-0024）。"""

    goal: str = Field(min_length=1, max_length=4000)
    domain: str = Field(default="", max_length=40)


class TaskIntakeInput(BaseModel):
    """发送前接待判定的输入：首页/确认页的任务描述、附件证据与首页对话状态。

    attachments 有正文摘录时，服务端把附件内容纳入判定；附件本身不构成「开始建模」。
    history / pending_task 让「好的，开始吧」「就用刚才那个数据」有所指；confirmed = 确认页
    点了「开始任务」（明确的执行意图）。三者都可省略，省略时按首轮、无提议处理。
    """

    goal: str = Field(min_length=1, max_length=4000)
    has_attachments: bool = False
    attachments: list[TaskIntakeAttachment] = Field(default_factory=list, max_length=20)
    history: list[TaskIntakeTurn] = Field(default_factory=list, max_length=12)
    pending_task: Optional[TaskIntakePendingTask] = None
    confirmed: bool = False


class TaskIntakeUnderstanding(BaseModel):
    """一句话的结构化理解（ADR-0024）：路由守卫的输入，前端只做展示与排障。"""

    kind: Literal["chat", "knowledge", "file_analysis", "modeling_task"]
    speech_act: Literal["greet", "ask", "command", "confirm", "cancel", "modify", "supplement"]
    #: 是否明确要求系统现在就开始执行
    execution: bool
    requires_modeling: bool
    continuation: Literal["new", "continue"]
    domain: Literal["optimization", "prediction", "evaluation", "simulation", "data_analysis", "statistics", "none"]
    missing: list[Literal["problem", "objective", "data"]] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class TaskIntakeResult(BaseModel):
    """接待判定结果：route=start 才创建任务；propose / clarify / reply 由首页对话原地回应。

    intent 保留旧三值并与 route 对齐（start → modeling_task，propose / clarify → needs_info，
    reply → chat），只认 intent 的旧前端照常工作。reply 是模板回应（确认页状态行用）。
    task_goal：start / propose 时的任务题面（确认提议时是提议记下的那份）；context_excerpt：
    start 时的首页对话摘录，前端随任务放进 ``params.conversation_context``。
    """

    intent: Literal["modeling_task", "needs_info", "chat"]
    reply: str = ""
    source: Literal["heuristic", "judge", "fallback"]
    route: Literal["start", "propose", "clarify", "reply"]
    understanding: TaskIntakeUnderstanding
    task_goal: str = ""
    context_excerpt: str = ""
    #: 首页对话模型是否可用（已配置自定义 API）：false 时前端直接展示 reply，不发起对话轮。
    chat_ready: bool = True


class ArtifactText(BaseModel):
    """附件正文抽取结果。

    ``status`` 分五档：ready 完整抽出、partial 触顶截断、empty 文件正常但没有
    文字、unsupported 缺少可选依赖或格式不支持、failed 文件损坏或抽取出错。
    后三档也是正常响应（200）——调用方需要的是原因，而不是一个错误码。
    """

    artifact_id: str
    name: str
    media_type: str
    status: Literal["ready", "partial", "empty", "unsupported", "failed"]
    engine: str
    characters: int
    segments: Optional[int] = None
    #: 文档内嵌图片数（近似值，ADR-0010）；null = 该格式不统计或计数失败。
    images: Optional[int] = None
    detail: Optional[str] = None
    text: str


class ArtifactPreview(BaseModel):
    """表格产物的前 N 行预览（数据页「原始数据 / 清洗后预览」用）。

    只做分隔符文本（csv / tsv）：首行作表头、其后 ``rows`` 行原样字符串（单元格截到
    200 字符）；``row_count`` 是数据行总数（内容 ≤ 10 MB 时全量计数，否则 null）；
    ``truncated`` = 还有没展示的行。内容与下载走同一条归属 + 哈希核验路径。
    """

    artifact_id: str
    name: str
    media_type: str
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    encoding: str
    delimiter: str
    columns: list[str]
    rows: list[list[str]]
    row_count: Optional[int] = None
    truncated: bool


class AttachmentParseResult(BaseModel):
    """对话附件的即席解析结果（ADR-0010 批次三）。

    与 ``ArtifactText`` 同一套状态语义，但不落库、不建产物：对话历史保存在页面
    内存、服务端无状态，附件解析也保持同样的隐私姿态。
    """

    name: str
    media_type: str
    status: Literal["ready", "partial", "empty", "unsupported", "failed"]
    engine: str
    characters: int
    segments: Optional[int] = None
    images: Optional[int] = None
    detail: Optional[str] = None
    text: str
