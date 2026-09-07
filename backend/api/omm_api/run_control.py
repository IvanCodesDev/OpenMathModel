"""对话即控制面（ADR-0018 / ADR-0019）：任务页聊天框里的一句话可以真正驱动运行。

托管对话轮（ADR-0016）在调用模型**之前**经过这里：读运行状态 → 算出当前状态下
合法的动作集 → 意图判定（本地规则只做短句快路径；其余一律交池里最弱的模型带着
最近几轮对话做一次限时 JSON 判定，模型拿不准再回落规则结果）→ 走
``actions.execute_action`` / ``engine_glue.accept_revision`` / ``engine_glue.redo_run``
/ 运行备注这几条**既有路径**执行 → 产出 ``action`` 事件与注入系统提示词的状态块。

设计红线：

- 判定异常、解析失败、结果不在合法集内，一律回落为普通对话——**绝不误执行**；
- 合法动作集只由状态机决定（与按钮同一套规则），模型只负责把意图归到枚举里；
- 取消、从阶段重做这类不可逆 / 要重新花费的动作先发提案（``proposed``），下一轮
  「确认」才执行；
- 备注保持静默（不发事件），只有真实动作才让页面渲染轨迹行。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from omm_contracts import (
    AgentEventType,
    ApprovalStatus,
    StepRunStatus,
    TaskRunAction,
    TaskRunActionInput,
    TaskRunStatus,
    TERMINAL_TASK_RUN_STATUSES,
)

from .actions import REJECT_OPTION_ID, execute_action
from .engine_glue import (
    MAX_REVISION_ROUNDS,
    REDO_STATUSES,
    accept_revision,
    redo_run,
    revision_rounds,
    suggest_revision_stage,
)
from .errors import ApiError
from .events import append_event
from .ids import new_id
from .llm import ChatOutcome, LlmConfig, complete_once, endpoint_strength, is_third_party_host
from .orm import ApprovalRequestRow, RunNoteRow, StepRunRow, TaskRunRow
from .serialize import utcnow
from .workflow import STAGE_LABELS, STAGES

logger = logging.getLogger("omm.run_control")

#: 对话可触发的动作枚举：/actions 的五个动作 + revision（ADR-0013 修订受理，已完成
#: 运行）+ redo（ADR-0019 任意非完成状态从阶段重做）。
ACTION_KINDS = ("retry", "resume", "pause", "cancel", "approve", "reject", "revision", "redo")
#: 不可逆 / 要重新花费的动作：先发提案，下一轮确认才执行。
CONFIRM_REQUIRED = frozenset({"cancel", "redo"})

JUDGE_READ_TIMEOUT_S = 10.0
JUDGE_MAX_TOKENS = 200
#: 规范化后不超过这么长的句子，本地规则命中即直判、不出网（「继续啊」「用方案 B」
#: 「从数据准备重做」）；更长的话规则命中也只是候选，交判定模型带着上下文拍板。
RULE_FAST_PATH_MAX_CHARS = 12
#: 超过这个长度的消息不送判定：整段粘贴的材料是补充要求，不是命令（只落备注）。
JUDGE_TEXT_LIMIT = 800
#: 判定模型看到的最近对话轮数及每侧摘要长度。
JUDGE_HISTORY_TURNS = 3
JUDGE_HISTORY_USER_CHARS = 200
JUDGE_HISTORY_REPLY_CHARS = 240
#: 备注正文上限（与 RunNoteInput 一致）。
NOTE_TEXT_LIMIT = 2000

_TERMINAL = frozenset(status.value for status in TERMINAL_TASK_RUN_STATUSES)

# ── 词表 ────────────────────────────────────────────────────────────────────

_RETRY_WORDS = (
    "重试", "再试", "重跑", "再跑", "重新跑", "重新执行", "重新运行", "重来", "再来一次",
    "再来一遍", "重做", "重新做", "继续", "接着", "想办法", "修复", "修一下", "再试试",
    "retry", "rerun", "again",
)
_RESUME_WORDS = ("恢复", "继续", "接着", "解除暂停", "resume")
_PAUSE_WORDS = ("暂停", "停一下", "先停", "停下", "先别跑", "pause", "hold")
#: 「继续说 / 接着讲」是对话延续，不是让运行继续：匹配前先剔掉。
_CONVERSATION_CONTINUATIONS = (
    "继续说", "接着说", "继续讲", "接着讲", "继续解释", "继续分析", "接着分析", "继续写",
    "继续聊", "继续回答", "继续讨论", "继续介绍", "接着解释",
)
_CANCEL_WORDS = (
    "取消任务", "取消这个任务", "取消运行", "取消掉", "终止", "中止", "不做了", "放弃",
    "停止任务", "别做了", "结束任务", "不用做了", "不跑了", "别跑了", "cancel", "abort",
)
#: 单独成句的「取消」也算取消任务（「取消」夹在别的话里多半是「取消修改」之类，交判定）。
_BARE_CANCEL = ("取消",)
_APPROVE_WORDS = (
    "同意", "批准", "确认", "通过", "采用", "就这个", "按推荐", "接受", "可以", "好的",
    "没问题", "ok", "okay", "yes", "继续",
)
_REJECT_WORDS = (
    "退回", "拒绝", "不同意", "重新规划", "换个方案", "换方案", "不采用", "驳回", "重做方案",
    "撤回", "不改了", "取消修改", "打回",
)
#: 修改动词：点名了阶段时本地直判（已完成运行 → revision；其它状态 → redo 提案），
#: 没点名阶段的交判定模型连同上下文一起看。
_REVISION_VERBS = (
    "修改", "改成", "换成", "重做", "重跑", "重写", "调整", "优化", "补充", "增加", "加上",
    "删掉", "去掉", "精简", "缩短", "更正", "修正", "替换", "重新", "改一下", "改下",
    "重来", "再来", "回退", "退回到", "从头", "redo",
)
#: 不点名选项的放行词（「同意」「确认」「继续」）只在这么短的消息里才替用户选预选项：
#: 「可以解释一下方案吗」里的「可以」不是放行。
_BARE_APPROVE_MAX_CHARS = 12
#: 问句标记：只有问句、没有命令词时按普通对话处理（模型带着状态块能答）。
_INQUIRY_MARKERS = (
    "为什么", "为啥", "怎么回事", "什么原因", "原因是", "是什么", "什么意思", "怎么办",
    "如何", "能不能", "可不可以", "行不行", "吗", "呢", "?", "？", "解释", "说明一下",
    "介绍", "总结一下", "分析一下", "看看", "讲讲", "告诉我",
)
#: 命令词前的否定：「不要重试」「先别跑」不是命令。
_NEGATIONS = ("不要", "不用", "先不", "暂不", "无需", "不必", "急着", "别", "不", "勿", "莫")
_CONFIRM_WORDS = (
    "确认", "确定", "是的", "是", "好", "好的", "可以", "行", "对", "执行", "同意", "没问题",
    "ok", "okay", "yes", "y", "嗯", "恩", "要", "取消吧", "取消",
)
_DENY_WORDS = ("不要", "算了", "不用", "别", "否", "不", "no", "n", "放弃", "先不", "取消提案", "不取消")

#: 按 id 点名选项的别名。通用放行词（同意 / 确认 / 可以…）不在此列——它们不点名
#: 任何选项，只在 decide_locally 的守卫分支里替用户选**预选项**。
_OPTION_ALIASES: dict[str, tuple[str, ...]] = {
    "adopt_cleaned": ("清洗", "采用清洗", "用清洗"),
    "use_raw": ("原始数据", "原数据", "不清洗", "用原始"),
    "accept": ("接受", "记录局限", "就这样", "先接受"),
    "confirm_delivery": ("交付", "确认交付", "定稿", "可以交付"),
}

_LETTER_PATTERNS = (
    re.compile(r"方案\s*([a-h])(?![a-z])"),
    re.compile(r"(?<![a-z])([a-h])\s*方案"),
    re.compile(r"^(?:选|用|要|就)?\s*([a-h])$"),
)
_ORDINALS = {
    "第一个": 0, "第1个": 0, "第一种": 0, "第一": 0, "一号": 0, "1号": 0,
    "第二个": 1, "第2个": 1, "第二种": 1, "第二": 1, "二号": 1, "2号": 1,
    "第三个": 2, "第3个": 2, "第三种": 2, "第三": 2, "三号": 2, "3号": 2,
}
_LABEL_SPLIT = re.compile(r"[：:·、，,。；;（）()\[\]「」『』“”\"'\s/]+")
_JSON_OBJECT = re.compile(r"\{[^{}]*\}", re.S)


# ── 运行状态快照 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateOption:
    id: str
    label: str
    description: str = ""
    recommended: bool = False


@dataclass
class RunControlContext:
    """一轮对话开始时运行的可判定状态：判定、执行与提示词注入共用这一份。"""

    run_id: str
    status: str
    node: str
    failure_message: str = ""
    approval_id: str = ""
    approval_title: str = ""
    options: list[GateOption] = field(default_factory=list)
    #: 待审批门是否是修订门（ADR-0013）及其轮次；0 = 节点自提的闸门。
    revision_gate_round: int = 0
    #: 本运行已发起过的修订轮数。
    revision_rounds: int = 0
    #: 上一轮对话留下、尚待确认的提案（{"kind": "cancel", ...}）。
    pending_proposal: Optional[dict[str, Any]] = None

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.node, "任务准备" if self.node in ("", "CREATED") else self.node)

    @property
    def legal(self) -> frozenset[str]:
        return legal_actions(
            self.status,
            bool(self.approval_id),
            self.options,
            self.revision_rounds,
            revision_gate=self.revision_gate_round > 0,
        )

    @property
    def stage_index(self) -> int:
        """当前阶段在 STAGES 里的下标；还没进入任何阶段 = -1。"""
        return STAGES.index(self.node) if self.node in STAGES else -1

    @property
    def preferred_option(self) -> Optional[str]:
        """CTA 预选项的同一口径（workspace_view._preferred_option）：唯一推荐或唯一正向。"""
        positive = [option for option in self.options if option.id != REJECT_OPTION_ID]
        recommended = [option for option in positive if option.recommended]
        if len(recommended) == 1:
            return recommended[0].id
        return positive[0].id if len(positive) == 1 else None

    def option(self, option_id: Optional[str]) -> Optional[GateOption]:
        for option in self.options:
            if option.id == option_id:
                return option
        return None


def legal_actions(
    status: str,
    has_approval: bool,
    options: list[GateOption],
    rounds: int,
    *,
    revision_gate: bool = False,
) -> frozenset[str]:
    """当前状态下对话允许触发的动作——判据与 actions.py / ADR-0013 / ADR-0019 完全一致。

    ``redo``（从选定阶段重做）在 FAILED / RUNNING / PAUSED 以及节点自提闸门的
    WAITING_APPROVAL 下合法；修订门（``revision_gate``）上六个起点已经是选项，走
    approve 而不另开 redo。
    """
    if status == TaskRunStatus.FAILED.value:
        return frozenset({"retry", "redo"})
    if status == TaskRunStatus.WAITING_APPROVAL.value and has_approval:
        legal = {"approve", "cancel"}
        if any(option.id == REJECT_OPTION_ID for option in options):
            legal.add("reject")
        if not revision_gate:
            legal.add("redo")
        return frozenset(legal)
    if status == TaskRunStatus.PAUSED.value:
        return frozenset({"resume", "cancel", "redo"})
    if status == TaskRunStatus.RUNNING.value:
        return frozenset({"pause", "cancel", "redo"})
    if status == TaskRunStatus.QUEUED.value:
        return frozenset({"cancel"})
    if status == TaskRunStatus.COMPLETED.value and rounds < MAX_REVISION_ROUNDS:
        return frozenset({"revision"})
    return frozenset()


def load_context(
    session: Session, run: TaskRunRow, previous_actions: Optional[list[dict[str, Any]]] = None
) -> RunControlContext:
    context = RunControlContext(
        run_id=run.id,
        status=run.status,
        node=run.current_node or "",
        failure_message=run.failure_message or "",
        revision_rounds=revision_rounds(session, run.id),
    )
    if run.status == TaskRunStatus.WAITING_APPROVAL.value:
        approval = session.execute(
            select(ApprovalRequestRow)
            .where(
                ApprovalRequestRow.run_id == run.id,
                ApprovalRequestRow.status == ApprovalStatus.PENDING.value,
            )
            .order_by(ApprovalRequestRow.requested_at.desc())
        ).scalars().first()
        if approval is not None:
            context.approval_id = approval.id
            context.approval_title = approval.title
            context.options = [
                GateOption(
                    id=str(option.get("id") or ""),
                    label=str(option.get("label") or ""),
                    description=str(option.get("description") or ""),
                    recommended=option.get("recommended") is True,
                )
                for option in (approval.options or [])
                if option.get("id")
            ]
            try:
                context.revision_gate_round = int((approval.evidence or {}).get("revision_round") or 0)
            except (TypeError, ValueError):
                context.revision_gate_round = 0
    context.pending_proposal = pending_proposal_of(previous_actions)
    return context


def pending_proposal_of(actions: Optional[list[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    """上一轮 ``meta.actions`` 里最后一条是否仍是待确认提案。提案只对紧接着的下一轮有效。"""
    if not actions:
        return None
    last = actions[-1]
    if isinstance(last, dict) and last.get("status") == "proposed" and last.get("kind") in CONFIRM_REQUIRED:
        return last
    return None


# ── 意图判定 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ControlDecision:
    #: ACTION_KINDS 之一，或 "confirm" / "deny"（针对待确认提案），或 "none"。
    kind: str
    option_id: Optional[str] = None
    stage: Optional[str] = None
    #: "rule"（本地规则）/ "judge"（弱模型）/ "proposal"（回应上一轮提案）/ "none"。
    source: str = "none"

    @property
    def actionable(self) -> bool:
        return self.kind in ACTION_KINDS or self.kind in ("confirm", "deny")


NONE_DECISION = ControlDecision(kind="none")


def _normalize(text: str) -> str:
    lowered = text.strip().lower()
    compact = re.sub(r"[\s\u3000]+", "", lowered)
    for phrase in _CONVERSATION_CONTINUATIONS:
        compact = compact.replace(phrase, "")
    return compact


def _negated(normalized: str, word: str) -> bool:
    """该词的每次出现前面都紧跟否定词 → 视为被否定。"""
    occurrences = [match.start() for match in re.finditer(re.escape(word), normalized)]
    if not occurrences:
        return False
    for start in occurrences:
        window = normalized[max(0, start - 3) : start]
        if not any(window.endswith(negation) for negation in _NEGATIONS):
            return False
    return True


def _has_word(normalized: str, words: tuple[str, ...]) -> bool:
    return any(word in normalized and not _negated(normalized, word) for word in words)


def is_inquiry(text: str) -> bool:
    normalized = _normalize(text)
    return any(marker in normalized for marker in _INQUIRY_MARKERS)


def _label_chunks(label: str) -> set[str]:
    normalized = _normalize(label)
    chunks = {normalized} if len(normalized) >= 2 else set()
    for piece in _LABEL_SPLIT.split(label.lower()):
        piece = re.sub(r"\s+", "", piece)
        if len(piece) >= 2:
            chunks.add(piece)
    return chunks


def match_option(text: str, options: list[GateOption]) -> list[GateOption]:
    """按正文点名审批选项：判别性 label 片段 / 字母（方案 B）/ 序数（第二个）/ 已知别名。

    多个选项共有的片段（如修订门七个选项都带的「重做」）不参与匹配，避免一句
    「重做」同时命中六个起点。返回全部命中项——调用方只在**唯一**命中时执行。
    """
    normalized = _normalize(text)
    positive = [option for option in options if option.id != REJECT_OPTION_ID]
    if not positive:
        return []
    # 字母 / 序数最具体，先认：「采用方案 B」不能被推荐项的别名「采用」抢走
    for pattern in _LETTER_PATTERNS:
        found = pattern.search(normalized)
        if found:
            letter = found.group(1)
            by_letter = [
                option
                for option in positive
                if re.search(rf"方案\s*{letter}(?![a-z])", option.label.lower())
                or re.search(rf"(?<![a-z]){letter}\s*方案", option.label.lower())
            ]
            if by_letter:
                return by_letter
    for token, index in _ORDINALS.items():
        if token in normalized and index < len(positive):
            return [positive[index]]
    chunk_sets = {option.id: _label_chunks(option.label) for option in positive}
    shared = {
        chunk
        for option_id, chunks in chunk_sets.items()
        for chunk in chunks
        if any(chunk in other for other_id, other in chunk_sets.items() if other_id != option_id)
    }
    matched: list[GateOption] = []
    for option in positive:
        discriminative = chunk_sets[option.id] - shared
        if any(chunk in normalized for chunk in discriminative):
            matched.append(option)
            continue
        if option.id.startswith("redo:"):
            label = STAGE_LABELS.get(option.id[len("redo:") :], "")
            if label and label in normalized:
                matched.append(option)
    if matched:
        return matched
    for option in positive:
        aliases = _OPTION_ALIASES.get(option.id) or _OPTION_ALIASES.get(option.id.split(":", 1)[0])
        if aliases and _has_word(normalized, aliases):
            matched.append(option)
    return matched


def explicit_stage(text: str) -> Optional[str]:
    """正文点名的阶段（按标签，如「从数据准备重做」）；没点名返回 None。"""
    normalized = _normalize(text)
    hits = [stage for stage in STAGES if STAGE_LABELS[stage] in normalized]
    if len(hits) == 1:
        return hits[0]
    if hits:
        # 一句话点到多个阶段：最靠前的那个才能整段满足要求（下游本来就会重跑）
        return min(hits, key=STAGES.index)
    return None


def _redo_or_retry(stage: str, context: RunControlContext, *, source: str) -> ControlDecision:
    """「从 X 重做」落成哪个动作：失败运行上点名的正是失败阶段 → 就是 retry（直接执行，
    不必再确认）；其它情形是真正的回退重做（提案）。"""
    if context.status == TaskRunStatus.FAILED.value and stage == context.node and "retry" in context.legal:
        return ControlDecision(kind="retry", source=source)
    return ControlDecision(kind="redo", stage=stage, source=source)


def decide_locally(text: str, context: RunControlContext) -> ControlDecision:
    """本地规则判定：确定、不出网。拿不准返回 kind="none"（调用方再决定是否送判定模型）。"""
    normalized = _normalize(text)
    if not normalized:
        return NONE_DECISION

    proposal = context.pending_proposal
    if proposal is not None:
        proposal_kind = str(proposal.get("kind") or "")
        if normalized in _DENY_WORDS:
            return ControlDecision(kind="deny", source="proposal")
        if normalized in _CONFIRM_WORDS or (
            proposal_kind == "cancel" and _has_word(normalized, _CANCEL_WORDS + _BARE_CANCEL)
        ):
            return ControlDecision(kind="confirm", source="proposal")
        # 其它文本：提案作废，按新意图处理

    legal = context.legal
    inquiry = is_inquiry(text)
    candidates: list[ControlDecision] = []

    if "retry" in legal and _has_word(normalized, _RETRY_WORDS):
        candidates.append(ControlDecision(kind="retry", source="rule"))
    if "resume" in legal and _has_word(normalized, _RESUME_WORDS):
        candidates.append(ControlDecision(kind="resume", source="rule"))
    if "pause" in legal and _has_word(normalized, _PAUSE_WORDS):
        candidates.append(ControlDecision(kind="pause", source="rule"))
    if "cancel" in legal and (_has_word(normalized, _CANCEL_WORDS) or normalized in _BARE_CANCEL):
        candidates.append(ControlDecision(kind="cancel", source="rule"))
    if "reject" in legal and _has_word(normalized, _REJECT_WORDS):
        candidates.append(ControlDecision(kind="reject", option_id=REJECT_OPTION_ID, source="rule"))
    if "approve" in legal:
        matched = match_option(text, context.options)
        if len(matched) > 1 and all(option.id.startswith("redo:") for option in matched):
            # 修订门上一句点到多个阶段：最靠前的起点才能整段满足要求（下游本来会重跑）
            matched = [
                min(
                    matched,
                    key=lambda option: STAGES.index(option.id[len("redo:") :])
                    if option.id[len("redo:") :] in STAGES
                    else len(STAGES),
                )
            ]
        if len(matched) == 1:
            candidates.append(ControlDecision(kind="approve", option_id=matched[0].id, source="rule"))
        elif (
            not matched
            and not inquiry
            and len(normalized) <= _BARE_APPROVE_MAX_CHARS
            and _has_word(normalized, _APPROVE_WORDS)
            and context.preferred_option
        ):
            # 「同意 / 确认 / 继续」这类不点名的放行：只有预选项唯一时才替用户选它
            candidates.append(
                ControlDecision(kind="approve", option_id=context.preferred_option, source="rule")
            )
    if "revision" in legal and not inquiry:
        stage = explicit_stage(text)
        if stage is not None and _has_word(normalized, _REVISION_VERBS):
            candidates.append(ControlDecision(kind="revision", stage=stage, source="rule"))
    if "redo" in legal and not inquiry:
        stage = explicit_stage(text)
        if stage is not None and _has_word(normalized, _REVISION_VERBS + _RETRY_WORDS):
            # 点名了阶段的「从数据准备重做」是最具体的信号：比「重做」同时命中的
            # retry（重跑当前阶段）更准，直接以它为准
            return _redo_or_retry(stage, context, source="rule")

    kinds = {candidate.kind for candidate in candidates}
    if len(kinds) != 1:
        # 零命中交判定模型；多种动作同时命中（「退回」+「取消」）本地不拍板
        return NONE_DECISION
    decision = candidates[0]
    if decision.kind == "cancel" and "reject" in legal and normalized in _BARE_CANCEL:
        # 有「退回 / 撤回」选项的门上，一句光秃秃的「取消」更可能是撤回而非取消任务
        return NONE_DECISION
    return decision


#: 判定模型看到的最近对话：``[{"text": 用户原话, "reply": 助手回复}, …]``，旧 → 新。
History = list[dict[str, str]]

_JUDGE_DESCRIPTIONS = {
    "retry": "retry：只是让当前失败的阶段再试一次（重试 / 再来 / 继续 / 想办法），没有指出要改更早阶段的内容",
    "resume": "resume：恢复已暂停的任务",
    "pause": "pause：暂停正在执行的任务",
    "cancel": "cancel：取消整个任务（不可逆；系统会先请用户确认）",
    "approve": "approve：在待确认事项中选定一个选项，必须给出 option_id",
    "reject": "reject：退回 / 撤回待确认事项（option_id 固定为 reject）",
    "revision": "revision：对已完成的成果提出修改要求，会从某个阶段起重做；stage 给出建议起点",
    "redo": (
        "redo：从某个阶段起重做（该阶段及其之后的阶段整段重跑，在途工作停止；系统会先请用户确认）。"
        "用户要求修改的内容属于**已完成或正在执行**的阶段时用它，stage = 最早需要重做的那个阶段"
        "（换模型 / 改目标函数 / 换算法 → MODEL_PLANNING；数据处理口径 / 清洗 / 特征 → DATA_PREPARATION；"
        "题意 / 假设 / 目标理解 → PROBLEM_ANALYSIS；实验代码 / 参数 / 随机种子 → EXPERIMENTING；"
        "验证方法 → VALIDATING；论文文字 → PAPER_WRITING）。要改的内容属于**尚未开始**的阶段 → none"
        "（系统会把这句话作为补充要求在那个阶段执行时提供给智能体）。待确认事项里已有能满足要求的选项时"
        "优先 approve / reject，不用 redo"
    ),
}


def _stage_progress(context: RunControlContext) -> str:
    index = context.stage_index
    if context.status == TaskRunStatus.COMPLETED.value:
        return "全部六个阶段已完成"
    parts = []
    for position, stage in enumerate(STAGES):
        if position < index:
            marker = "已完成"
        elif position == index:
            marker = {
                TaskRunStatus.FAILED.value: "失败于此",
                TaskRunStatus.WAITING_APPROVAL.value: "等待确认",
                TaskRunStatus.PAUSED.value: "暂停于此",
            }.get(context.status, "正在执行")
        else:
            marker = "未开始"
        parts.append(f"{stage}={STAGE_LABELS[stage]}（{marker}）")
    return " → ".join(parts)


def _judge_prompt(text: str, context: RunControlContext, history: Optional[History] = None) -> str:
    legal = context.legal
    lines = [
        "你是数学建模工作台的运行控制判定器。用户在任务页聊天框里发了一句话，"
        "请结合运行状态与最近对话，判断这句话是否是在**要求系统执行**下面某个动作。",
        "只输出一行 JSON：{\"action\": \"<动作或 none>\", \"option_id\": \"\", \"stage\": \"\", \"reason\": \"\"}。",
        "规则：询问原因 / 解释 / 讨论结果 / 表达情绪 / 提供背景信息一律 none；用户要求系统去做、去改、"
        "去重来（不一定用固定词，可以是任何说法，也可以承接上文——「就按你说的那个改」）才返回动作；"
        "动作必须在下面列出的允许范围内；拿不准一律 none（误执行的代价远大于漏判）。",
        f"运行状态：{_STATUS_LABELS.get(context.status, context.status)}（{context.status}），当前阶段：{context.stage_label}。",
        f"阶段顺序与进度：{_stage_progress(context)}",
    ]
    if context.failure_message:
        lines.append(f"失败原因：{context.failure_message[:300]}")
    if context.approval_id:
        lines.append(f"待确认事项：{context.approval_title}")
        for option in context.options:
            flag = "（推荐）" if option.recommended else ""
            lines.append(f"  - option_id={option.id}：{option.label}{flag} {option.description}".rstrip())
    lines.append("当前允许的动作：")
    for kind in ACTION_KINDS:
        if kind in legal:
            lines.append(f"  - {_JUDGE_DESCRIPTIONS[kind]}")
    if "revision" in legal or "redo" in legal:
        lines.append("stage 取值：" + " / ".join(f"{stage}={STAGE_LABELS[stage]}" for stage in STAGES))
    if history:
        lines.append("最近对话（旧 → 新）：")
        for turn in history[-JUDGE_HISTORY_TURNS:]:
            user_text = " ".join(str(turn.get("text") or "").split())[:JUDGE_HISTORY_USER_CHARS]
            reply = " ".join(str(turn.get("reply") or "").split())[:JUDGE_HISTORY_REPLY_CHARS]
            if user_text:
                lines.append(f"  用户：{user_text}")
            if reply:
                lines.append(f"  助手：{reply}")
    lines.append(f"用户这句话：{text[:JUDGE_TEXT_LIMIT]}")
    return "\n".join(lines)


def _parse_judge_reply(text: str, context: RunControlContext, user_text: str = "") -> ControlDecision:
    """只认合法集内的结果；redo / revision 的阶段缺失或不合法时按正文推断。"""
    start, end = text.find("{"), text.rfind("}")
    candidates: list[str] = []
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    candidates.extend(match.group(0) for match in _JSON_OBJECT.finditer(text))
    for raw in candidates:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        kind = str(data.get("action") or "none").strip().strip("\"'").lower()
        if kind not in context.legal:
            return NONE_DECISION
        option_id = str(data.get("option_id") or "").strip() or None
        stage = str(data.get("stage") or "").strip().upper() or None
        if kind == "approve":
            if option_id is None or context.option(option_id) is None or option_id == REJECT_OPTION_ID:
                return NONE_DECISION
        elif kind == "reject":
            option_id = REJECT_OPTION_ID
        elif kind == "revision":
            if stage not in STAGES:
                stage = explicit_stage(user_text or text) or None
        elif kind == "redo":
            if stage not in STAGES and user_text:
                stage = explicit_stage(user_text) or suggest_revision_stage(user_text)
            if stage is None or stage not in STAGES:
                return NONE_DECISION
            if context.stage_index >= 0 and STAGES.index(stage) > context.stage_index:
                # 要改的是还没开始的阶段：没有东西可重做，这句话作为备注等那一阶段执行时读
                return NONE_DECISION
            return _redo_or_retry(stage, context, source="judge")
        else:
            option_id, stage = None, None
        return ControlDecision(kind=kind, option_id=option_id, stage=stage, source="judge")
    return NONE_DECISION


def judge_intent(
    config: LlmConfig,
    text: str,
    context: RunControlContext,
    on_usage: Optional[Callable[[ChatOutcome], None]] = None,
    history: Optional[History] = None,
) -> ControlDecision:
    """弱模型判定：限时、只认合法集内的结果；任何异常返回 none（回落规则 / 对话）。"""
    candidates = [
        endpoint for endpoint in config.endpoints if config.allow_proxy or not is_third_party_host(endpoint.host)
    ]
    if not candidates:
        return NONE_DECISION
    judge = min(candidates, key=endpoint_strength)
    try:
        outcome = complete_once(
            judge,
            [{"role": "user", "content": _judge_prompt(text, context, history)}],
            max_tokens=JUDGE_MAX_TOKENS,
            read_timeout=JUDGE_READ_TIMEOUT_S,
        )
    except Exception as error:  # noqa: BLE001 - 判定挂了只是不执行，对话照常
        logger.warning("run control judge failed on %s: %s", judge.name, error)
        return NONE_DECISION
    if on_usage is not None:
        try:
            on_usage(outcome)
        except Exception:  # noqa: BLE001 - 记账绝不影响对话
            logger.exception("run control judge usage callback failed")
    return _parse_judge_reply(outcome.text, context, user_text=text)


Judge = Callable[[str, RunControlContext, Optional[History]], ControlDecision]


def decide(
    text: str,
    context: RunControlContext,
    judge: Optional[Judge] = None,
    history: Optional[History] = None,
) -> ControlDecision:
    """判定顺序（ADR-0019 §4）：提案回应 → 短句规则直判 → 判定模型 → 回落规则结果。

    规则只是快路径：规范化后不超过 ``RULE_FAST_PATH_MAX_CHARS`` 的句子命中词表才直接
    采用；更长的话即便命中也只算候选，交模型带着最近对话拍板（「换成随机森林重新做」
    里的「重新做」是 retry 词，但用户要的是从建模方案重做）。模型判成 none / 出错 /
    不在合法集内时回落到规则结果——规则命中的仍会执行，什么都没命中才是普通对话。
    超长文本不判定：那是粘贴进来的材料，只落备注。
    """
    local = decide_locally(text, context)
    if local.source == "proposal":
        return local
    normalized = _normalize(text)
    if local.kind != "none" and len(normalized) <= RULE_FAST_PATH_MAX_CHARS:
        return local
    if judge is None or not context.legal:
        return local
    stripped = text.strip()
    if len(stripped) > JUDGE_TEXT_LIMIT:
        return NONE_DECISION
    judged = judge(stripped, context, history)
    if judged.kind != "none":
        return judged
    return local


# ── 执行 ─────────────────────────────────────────────────────────────────────


def record_run_note(session: Session, run: TaskRunRow, text: str, scope: str = "global") -> RunNoteRow:
    """落一条运行备注 + run.log 回执（§11.3 方案 A）。HTTP /notes 与对话控制面共用。"""
    note = RunNoteRow(
        id=new_id("note"),
        run_id=run.id,
        text=text[:NOTE_TEXT_LIMIT],
        scope=scope,
        created_at=utcnow(),
    )
    session.add(note)
    if scope == "global":
        message = "已记录补充要求，将在后续每次节点执行时提供给智能体"
    else:
        label = STAGE_LABELS.get(scope, scope)
        message = f"已记录补充要求，将在「{label}」阶段的节点执行时提供给智能体"
        stage_done = session.execute(
            select(StepRunRow).where(
                StepRunRow.run_id == run.id,
                StepRunRow.node == scope,
                StepRunRow.status == StepRunStatus.SUCCEEDED.value,
            )
        ).scalars().first()
        if stage_done is not None:
            message += (
                "；该阶段已完成，备注不会自动触发重做——"
                "如需按新要求重做，请在时间线对相应阶段发起重试或回退"
            )
    append_event(
        session,
        run.id,
        AgentEventType.run_log.value,
        {
            "kind": "user_note",
            "note_id": note.id,
            "scope": scope,
            "text": text[:500],
            "message": message,
        },
    )
    return note


def _proposal_label(proposal: dict[str, Any]) -> str:
    kind = str(proposal.get("kind") or "")
    if kind == "redo":
        return f"从「{proposal.get('stage_label') or STAGE_LABELS.get(str(proposal.get('stage') or ''), '')}」重做"
    return "取消任务" if kind == "cancel" else kind


def _proposal_for(decision: ControlDecision, text: str, context: RunControlContext) -> dict[str, Any]:
    kind = decision.kind
    if kind == "cancel":
        return {
            "kind": "cancel",
            "status": "proposed",
            "message": "要取消这个任务吗？取消后运行立即结束、不可恢复，已生成的产物仍可查看。",
            "confirm_hint": "回复「确认」或点下方按钮执行；回复其它内容则不取消",
        }
    if kind == "redo":
        stage = decision.stage or context.node
        label = STAGE_LABELS.get(stage, stage)
        in_flight = ""
        if context.status == TaskRunStatus.RUNNING.value:
            in_flight = f"；正在执行的「{context.stage_label}」步骤会在当前调用结束后停止、结果作废"
        elif context.status == TaskRunStatus.WAITING_APPROVAL.value:
            in_flight = f"；待确认事项「{context.approval_title}」会随之作废"
        return {
            "kind": "redo",
            "status": "proposed",
            "stage": stage,
            "stage_label": label,
            # 提案要把原话带过去：下一轮用户只回一个「确认」，重做的节点得读到「要改什么」
            "text": text.strip()[:NOTE_TEXT_LIMIT],
            "message": (
                f"要从「{label}」重做吗？该阶段及其之后的阶段会整段重跑并重新计费{in_flight}；"
                "你的这句话会作为要求提供给重做的智能体，上游阶段的成果保留。"
            ),
            "confirm_hint": "回复「确认」或点下方按钮执行；回复其它内容则不重做",
        }
    return {"kind": kind, "status": "proposed", "message": f"要执行「{kind}」吗？", "confirm_hint": "回复「确认」执行"}


def execute(
    session: Session,
    run: TaskRunRow,
    decision: ControlDecision,
    text: str,
    context: RunControlContext,
    *,
    actor: str,
) -> Optional[dict[str, Any]]:
    """按判定执行；返回 ``action`` 事件正文（None = 本轮没有可见动作）。

    执行走与按钮相同的函数：状态机不允许时得到 409 类错误，这里如实转成
    ``status:"rejected"`` 告知，而不是让对话轮失败。
    """
    kind = decision.kind
    if kind == "confirm":
        proposal = context.pending_proposal or {}
        kind = str(proposal.get("kind") or "")
        if kind not in ACTION_KINDS:
            return None
        decision = ControlDecision(kind=kind, stage=proposal.get("stage") or None, source="proposal")
        if kind == "redo" and proposal.get("text"):
            # 「确认」本身不是要求；落备注的是提案里带过来的那句原话
            text = str(proposal["text"])
    elif kind == "deny":
        proposal = context.pending_proposal or {}
        return {
            "kind": str(proposal.get("kind") or "cancel"),
            "status": "dismissed",
            "message": f"已放弃{_proposal_label(proposal)}，运行状态不变。",
        }
    elif kind in CONFIRM_REQUIRED and decision.source != "proposal":
        return _proposal_for(decision, text, context)

    if kind not in ACTION_KINDS:
        return None
    stage = context.node
    stage_label = context.stage_label
    try:
        if kind == "retry":
            execute_action(session, run.id, TaskRunActionInput(action=TaskRunAction.RETRY), actor=actor)
            note = record_run_note(session, run, text) if text.strip() else None
            return {
                "kind": "retry",
                "status": "executed",
                "stage": stage,
                "stage_label": stage_label,
                "note_id": note.id if note else None,
                "message": (
                    f"已重试「{stage_label}」阶段"
                    + ("，并把你的要求作为备注注入该阶段的执行提示词" if note else "")
                ),
            }
        if kind == "resume":
            execute_action(session, run.id, TaskRunActionInput(action=TaskRunAction.RESUME), actor=actor)
            return {
                "kind": "resume",
                "status": "executed",
                "stage": stage,
                "stage_label": stage_label,
                "message": f"已恢复任务，将从「{stage_label}」阶段的检查点继续执行",
            }
        if kind == "pause":
            execute_action(session, run.id, TaskRunActionInput(action=TaskRunAction.PAUSE), actor=actor)
            return {
                "kind": "pause",
                "status": "executed",
                "stage": stage,
                "stage_label": stage_label,
                "message": f"已暂停任务，「{stage_label}」阶段的检查点已保留；说「恢复」即可继续",
            }
        if kind == "cancel":
            execute_action(session, run.id, TaskRunActionInput(action=TaskRunAction.CANCEL), actor=actor)
            return {
                "kind": "cancel",
                "status": "executed",
                "stage": stage,
                "stage_label": stage_label,
                "message": "已取消任务；已生成的历史记录和产物仍可查看",
            }
        if kind in ("approve", "reject"):
            option_id = REJECT_OPTION_ID if kind == "reject" else decision.option_id
            option = context.option(option_id)
            execute_action(
                session,
                run.id,
                TaskRunActionInput(
                    action=TaskRunAction.APPROVE,
                    approval_id=context.approval_id or None,
                    option_id=option_id,
                    comment=text.strip()[:2000] or None,
                ),
                actor=actor,
            )
            label = option.label if option else (option_id or "")
            return {
                "kind": kind,
                "status": "executed",
                "approval_id": context.approval_id,
                "approval_title": context.approval_title,
                "option_id": option_id,
                "option_label": label,
                "message": f"已在「{context.approval_title}」中选择「{label}」",
            }
        if kind == "revision":
            receipt = accept_revision(session, run, text, stage=decision.stage)
            suggested = str(receipt["suggested_stage"])
            suggested_label = STAGE_LABELS.get(suggested, suggested)
            return {
                "kind": "revision",
                "status": "executed",
                "round": receipt["round"],
                "approval_id": receipt["approval_id"],
                "note_id": receipt["note_id"],
                "stage": suggested,
                "stage_label": suggested_label,
                "message": (
                    f"已受理第 {receipt['round']} 轮修改要求，建议从「{suggested_label}」重做并已预选；"
                    "回复「确认」或在待确认事项中点一下即开始重跑（所选阶段及其之后的阶段会整段重做，另计一份运行配额）"
                ),
            }
        if kind == "redo":
            target = decision.stage or context.node
            receipt = redo_run(session, run, target, text)
            target_label = STAGE_LABELS.get(target, target)
            waiting = (
                "；在途步骤会在当前调用结束后让位"
                if receipt["from_status"] == TaskRunStatus.RUNNING.value
                else ""
            )
            return {
                "kind": "redo",
                "status": "executed",
                "stage": target,
                "stage_label": target_label,
                "note_id": receipt["note_id"],
                "message": (
                    f"已从「{target_label}」重做，该阶段及其之后的阶段会整段重跑{waiting}；"
                    "你的要求已作为备注提供给重做的智能体，上游阶段的成果保留"
                ),
            }
    except ApiError as error:
        return {"kind": kind, "status": "rejected", "code": error.code, "message": error.message}
    return None


# ── 提示词注入 ───────────────────────────────────────────────────────────────

_STATUS_LABELS = {
    "QUEUED": "排队中",
    "RUNNING": "执行中",
    "PAUSED": "已暂停",
    "WAITING_APPROVAL": "等待用户确认",
    "FAILED": "执行失败",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
}
_COMMAND_HELP = {
    "retry": "「重试」重跑当前失败的阶段",
    "resume": "「恢复」继续执行",
    "pause": "「暂停」保留检查点停下",
    "cancel": "「取消任务」终止运行（会先请用户确认）",
    "approve": "直接说选哪个选项（如「用方案 B」「采用清洗结果」「确认」）",
    "reject": "「退回」/「撤回」",
    "revision": "直接说要改什么（如「从数据准备重做」「把论文摘要改短」）",
    "redo": "直接说要改什么或从哪个阶段重做（如「换成随机森林重新做」「从数据准备重做」；会先请用户确认）",
}
#: 把活交回给运行的动作：执行后对话只做交接，不替运行干活（算题 / 建模 / 写论文段落）。
#: pause / cancel 让运行停下，用户接下来多半要讨论，回复沿用默认口径。
_HANDOFF_KINDS = frozenset({"retry", "resume", "redo", "approve", "reject", "revision"})

_REPLY_RULES_DEFAULT = (
    "回复要求：先用一两句话如实告知上面已执行的操作与接下来会发生什么，再回答用户的问题；"
    "不得声称执行了未列出的操作，也不得把「记录备注」说成「已修改」。"
    "如果用户想要的动作当前不允许，说明原因并给出上面列出的可用指令。"
)
_REPLY_RULES_HANDOFF = (
    "回复要求：运行已经接手这件事——用两三句话如实告知上面已执行的操作、接下来运行会做什么、"
    "进度可在下方的执行步骤里看到，然后结束回复。用户这句话里的具体要求（参数取值、方法选择、"
    "修改内容等）已随动作提供给运行里的智能体，由它带着要求完成，不必在这里复述或展开。"
    "绝不要在对话里替运行去做那件事：不要自己算题、建模、给出结果数字或写论文段落——那会与运行"
    "里正在进行的工作重复，且对话里的结果不会进入成果。这句话里若还带着问题（如问失败原因），"
    "用简短几句回答即可。不得声称执行了未列出的操作，也不得把「记录备注」说成「已修改」。"
)
_REPLY_RULES_PROPOSED = (
    "回复要求：上面的提案尚未执行——用一两句话说明将要执行什么、有什么代价，请用户回复「确认」"
    "或点下方按钮；不得声称已经执行，也不要在对话里替运行去做那件事（不要自己算题、建模或写论文段落）。"
    "这句话里若还带着问题，用简短几句回答即可。"
)


def reply_rules(actions: list[dict[str, Any]]) -> str:
    """按本轮动作决定回复口径：交接（运行已接手）/ 提案待确认 / 默认（问答为主）。

    「按典型参数继续」这类话在 FAILED 上会真的 retry；若仍按默认口径「再回答用户的问题」，
    模型会把它当成「把典型参数下的结果算出来」，在对话里与运行并行解同一道题
    （2026-09-07 用户截图：回复气泡里逐项算分，下方执行步骤同时在跑）。
    """
    executed_kinds = {str(action.get("kind") or "") for action in actions if action.get("status") == "executed"}
    if executed_kinds & _HANDOFF_KINDS:
        return _REPLY_RULES_HANDOFF
    if any(action.get("status") == "proposed" for action in actions):
        return _REPLY_RULES_PROPOSED
    return _REPLY_RULES_DEFAULT


def prompt_block(context: RunControlContext, actions: list[dict[str, Any]]) -> str:
    """注入系统提示词的状态块：模型据此知道运行在哪、本轮做了什么、用户还能说什么。"""
    lines = ["【当前运行状态】"]
    status_label = _STATUS_LABELS.get(context.status, context.status)
    if context.status == TaskRunStatus.COMPLETED.value:
        lines.append(f"- 状态：已完成（全部六个阶段已交付）；修订轮数已用 {context.revision_rounds}/{MAX_REVISION_ROUNDS}")
    else:
        lines.append(f"- 状态：{status_label}（{context.status}）；当前阶段：{context.stage_label}")
    if context.failure_message:
        lines.append(f"- 失败原因：{context.failure_message[:400]}")
    if context.approval_id:
        lines.append(f"- 待用户确认：{context.approval_title}")
        for option in context.options:
            flag = "（推荐）" if option.recommended else ""
            description = f"——{option.description}" if option.description else ""
            lines.append(f"  · {option.label}{flag}{description}")
    if context.pending_proposal:
        lines.append(
            f"- 上一轮留有待确认的提案：{_proposal_label(context.pending_proposal)}（用户回复「确认」才执行）"
        )
    help_lines = [_COMMAND_HELP[kind] for kind in ACTION_KINDS if kind in context.legal]
    if help_lines:
        lines.append("- 用户在对话里可直接下达的指令：" + "；".join(help_lines))
    elif context.status == TaskRunStatus.CANCELLED.value:
        lines.append("- 运行已取消，没有可执行的动作；如需继续请基于当前结果新建任务")
    elif context.status == TaskRunStatus.COMPLETED.value:
        lines.append("- 修订轮数已用完，没有可执行的动作；如需继续请基于当前结果新建任务")

    lines.append("【本轮已执行的操作】")
    if actions:
        for action in actions:
            lines.append(f"- {action.get('message') or action.get('kind')}")
    else:
        lines.append("- 无（本轮没有执行任何运行控制动作；不要声称做了）")
    lines.append(reply_rules(actions))
    return "\n".join(lines)


# ── 托管轮接线 ───────────────────────────────────────────────────────────────


@dataclass
class ControlResult:
    actions: list[dict[str, Any]]
    prompt_block: str


def run_control_step(
    session_factory: sessionmaker[Session],
    *,
    run_id: str,
    user_id: str,
    actor: str,
    text: str,
    config: LlmConfig,
    previous_actions: Optional[list[dict[str, Any]]],
    on_judge_usage: Optional[Callable[[ChatOutcome], None]] = None,
    history: Optional[History] = None,
) -> ControlResult:
    """托管轮后台线程里的控制步骤：判定 + 执行在一个事务里完成，返回事件与提示词块。

    任何异常都不允许让对话轮失败：记日志，回落成「没有动作」的普通对话。
    """
    session = session_factory()
    try:
        run = session.get(TaskRunRow, run_id)
        if run is None:
            return ControlResult(actions=[], prompt_block="")
        context = load_context(session, run, previous_actions)

        def judge(
            question: str, ctx: RunControlContext, recent: Optional[History]
        ) -> ControlDecision:
            return judge_intent(config, question, ctx, on_usage=on_judge_usage, history=recent)

        decision = decide(text, context, judge=judge, history=history)
        actions: list[dict[str, Any]] = []
        if decision.actionable:
            outcome = execute(session, run, decision, text, context, actor=actor)
            if outcome is not None:
                actions.append(outcome)
        elif run.status not in _TERMINAL and text.strip() and not is_inquiry(text):
            # 没有动作的普通补充要求：照旧落成备注（静默，不发事件）；问句不进备注——
            # 「为什么这么慢」出现在节点提示词的「用户补充要求」里只会误导智能体
            record_run_note(session, run, text.strip())
        session.commit()
        logger.info(
            "run control run=%s status=%s decision=%s source=%s actions=%s",
            run_id,
            context.status,
            decision.kind,
            decision.source,
            [f"{item.get('kind')}:{item.get('status')}" for item in actions],
        )
        # 执行后状态已变（如 FAILED → RUNNING）：状态块按最新快照写，模型别再说「失败中」
        session.refresh(run)
        context = load_context(session, run, None)
        if actions and actions[-1].get("status") == "proposed":
            context.pending_proposal = actions[-1]
        return ControlResult(actions=actions, prompt_block=prompt_block(context, actions))
    except Exception:  # noqa: BLE001 - 控制面出错不能拖垮对话
        session.rollback()
        logger.exception("run control step failed for %s", run_id)
        return ControlResult(actions=[], prompt_block="")
    finally:
        session.close()


def action_events(result: ControlResult) -> Iterator[dict[str, Any]]:
    for action in result.actions:
        yield {"type": "action", **action}


__all__ = [
    "ACTION_KINDS",
    "CONFIRM_REQUIRED",
    "JUDGE_HISTORY_TURNS",
    "JUDGE_TEXT_LIMIT",
    "RULE_FAST_PATH_MAX_CHARS",
    "ControlDecision",
    "ControlResult",
    "GateOption",
    "History",
    "RunControlContext",
    "action_events",
    "decide",
    "decide_locally",
    "execute",
    "explicit_stage",
    "judge_intent",
    "legal_actions",
    "load_context",
    "match_option",
    "pending_proposal_of",
    "prompt_block",
    "record_run_note",
    "reply_rules",
    "run_control_step",
]
