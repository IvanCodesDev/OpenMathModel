"""发送前的接待判定：对话优先，任务化是显式升级（Codex/opencode 式门控）。

首页发送 → 本判定 → "modeling_task" 才创建 Project/TaskRun 启动六阶段；
"needs_info"/"chat" 由前端在首页原地回应，不建任务、不产生垃圾项目、
不烧整条链路的模型调用。

判定本身是一次限时的轻量调用（选池中最弱模型，与 Auto 路由的难度判定
同策略）；启发式短路能不出网就不出网。任何判定异常一律放行成
"modeling_task"——门控挂了绝不能挡住真实用户；题面实际无效的兜底由
问题分析节点的 viability 准入门负责（第二层，engine_glue/skills）。

判定模型是池中最弱的那个，在「一句话建模需求」上并不可靠，所以模型不是
唯一裁判：本地先按「赛题标识」与「任务型信号（有对象 + 有求解目标）」
识别一遍，前者直接放行不出网，后者在判定模型给出 "needs_info" 时否决它。
否决只针对 "needs_info"——这个意图的语义是「看出你要建模、但嫌信息少」，
而信息少恰恰是本门明令不得拦人的理由，本地已确认有对象与目标时它必是
误判；"chat"（与建模无关）仍尊重模型，垃圾项目的防线不能一起拆掉。

附件的处理分两种：浏览器解析出了正文摘录的附件按内容判定（附件内容
与建模无关时不再盲目放行）；解析不出文字的附件（纯图片、扫描件、
关闭了自动解析）内容不可见，维持放行，由第二层准入门兜底。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from .llm import (
    ChatOutcome,
    LlmConfig,
    complete_once,
    config_usable,
    endpoint_strength,
    is_third_party_host,
)

logger = logging.getLogger("omm.intake")

INTENTS = ("modeling_task", "needs_info", "chat")

INTAKE_READ_TIMEOUT_S = 10.0
INTAKE_MAX_TOKENS = 256

#: 进入判定提示词的输入截断
_GOAL_EXCERPT_CHARS = 800
#: 达到该长度的输入视为题面粘贴，直接放行（不烧判定调用）
_LONG_GOAL_CHARS = 200
#: 不超过该长度且无任何建模信号的输入是寒暄（「你好」「在吗」「你是谁」），
#: 按 chat 友好回应——它们不是「想建模但没说清」，回「请提供赛题正文」答非所问
_TRIVIAL_GOAL_CHARS = 4
#: 每个附件进入判定提示词的正文摘录截断
_ATTACHMENT_EXCERPT_CHARS = 500
#: 进入判定提示词的附件数量上限（更多附件只会稀释信号）
_ATTACHMENT_PROMPT_LIMIT = 5

#: 赛题标识：出现即认定用户带着题面来（哪怕只写「2024 国赛 A 题」），
#: 不必再问判定模型。英文条目服务 COMAP 系赛题。
_PROBLEM_MARKERS = (
    "赛题", "a题", "b题", "c题", "d题", "e题", "f题",
    "高教社杯", "数学建模竞赛", "国赛", "美赛", "研赛", "数模",
    "mcm", "icm", "mathorcup", "华数杯", "深圳杯", "电工杯", "认证杯",
)

#: 任务型信号：出现即说明用户在指派一件有求解目标的活。**刻意不含「建模」
#: 「模型」这类话题词** —— 门控要拦的恰恰是「帮我做个建模」这种只报话题、
#: 不说对象的输入，把话题词算进来会让 needs_info 这一类彻底失效。
#: 英文条目取词根以覆盖动名词变形（optimiz → optimize/optimization）。
_TASK_SIGNALS = (
    "优化", "预测", "求解", "调度", "排产", "排班", "选址", "分配", "规划",
    "分类", "聚类", "拟合", "回归", "仿真", "定价", "配送", "库存", "路径",
    "最小化", "最大化", "最短", "最优", "灵敏度", "评价体系", "综合评价",
    "optimiz", "predict", "forecast", "schedul", "allocat", "minimiz",
    "maximiz", "cluster", "classif", "regression", "simulat", "routing",
    "assignment", "inventory", "pricing", "sensitivity",
)

#: 咨询式问句标记：命中说明用户在「问建模」而不是「派建模任务」
#: （如「怎么入门优化算法」），任务型信号一律不生效，交回判定模型。
_INQUIRY_MARKERS = (
    "怎么", "如何", "为什么", "什么是", "能不能", "可不可以", "是不是",
    "有哪些", "推荐", "教我", "学习", "入门", "区别", "吗", "呢",
)

#: 命中任务型信号还需要的最短长度：更短的输入即便命中也没有具体对象
#: （「帮我优化一下」6 字命中「优化」，却没说优化什么）。
_TASK_SIGNAL_MIN_CHARS = 10

#: 弱模型常把意图写成同义词或省略后缀。判定语义已经明确时不该因为字面
#: 对不上就白烧一次调用，统一归一到三个合法值。
_INTENT_ALIASES = {
    "modeling": "modeling_task",
    "modelling_task": "modeling_task",
    "modeling-task": "modeling_task",
    "modeling task": "modeling_task",
    "task": "modeling_task",
    "建模": "modeling_task",
    "建模任务": "modeling_task",
    "need_info": "needs_info",
    "needs-info": "needs_info",
    "needs info": "needs_info",
    "needsinfo": "needs_info",
    "缺少信息": "needs_info",
    "补充信息": "needs_info",
    "闲聊": "chat",
    "对话": "chat",
}

#: 判定回复里的候选 JSON 片段（不含嵌套）：弱模型爱在 JSON 前后写解释、
#: 套 Markdown 围栏、或一口气输出多个对象，逐个候选试解析比取最外层
#: 「{ … }」跨度稳——后者会被解释文字里的花括号带偏。
_JSON_OBJECT = re.compile(r"\{[^{}]*\}", re.S)

_NEEDS_INFO_FALLBACK_REPLY = (
    "请提供完整的赛题正文（可附题目文档与数据文件），我才能为你启动建模任务。"
)
_CHAT_FALLBACK_REPLY = (
    "我是数学建模工作台的智能体，负责从读题到论文的完整建模流程；"
    "把完整赛题（正文与数据附件）发给我即可开始。"
)


@dataclass(frozen=True)
class IntakeDecision:
    intent: str  # "modeling_task" | "needs_info" | "chat"
    reply: str = ""
    #: 判定来源："heuristic"（启发式短路或本地否决）/ "judge"（模型判定）/
    #: "fallback"（未配置或判定失败放行）。本地否决计入 heuristic：最终结论
    #: 确实由本地信号做出，而 source 是对外契约的三值枚举，不为此扩容。
    source: str = "fallback"
    #: 被本地否决改写掉的模型原始意图，仅供日志回溯，不出接口。
    overridden_from: str = ""


@dataclass(frozen=True)
class IntakeAttachment:
    """判定可见的附件证据：文件名与浏览器解析出的正文摘录（可为空）。"""

    name: str
    excerpt: str = ""
    characters: int = 0


def _modeling_signal(goal: str) -> str:
    """本地识别输入的建模信号：``"problem"`` / ``"task"`` / ``""``。

    - ``"problem"``：带赛题标识，用户显然是带着题面来的 → 直接放行，不出网。
    - ``"task"``：有任务型动词、长度够、不是咨询式问句 → 有对象也有求解目标，
      仍交判定模型看一眼（附件与语境可能改变结论），但模型判 "needs_info"
      时以本地结论为准。
    - ``""``：没有可据以放行的信号，完全交给判定模型。

    赛题标识优先于问句判断：「2024 国赛 A 题怎么做」虽是问句，题面就在手上。
    """
    lowered = goal.lower()
    if any(marker in lowered for marker in _PROBLEM_MARKERS):
        return "problem"
    if any(marker in lowered for marker in _INQUIRY_MARKERS):
        return ""
    if len(goal) >= _TASK_SIGNAL_MIN_CHARS and any(
        word in lowered for word in _TASK_SIGNALS
    ):
        return "task"
    return ""


def _judge_prompt(goal: str, attachments: Sequence[IntakeAttachment] = ()) -> str:
    parts = [
        "你是数学建模工作台的接待员，判断用户输入应如何处理。"
        "这道门只拦明显不该建任务的输入，拿不准一律判 modeling_task："
        "误拦真实用户会把人直接挡在建模流程外，而误放的无效题面由后续的准入门兜底。\n"
        '只输出一行 JSON，形如 {"intent": "...", "reply": "..."}：\n'
        '- "modeling_task"：能看出要解决的对象与求解/优化/预测目标就算，哪怕只有一句话、'
        "没有数据、没有完整题面；不要因为「描述太简略」或「缺少数据附件」改判其他两类。"
        "例：「帮我做一个共享单车调度优化模型」有对象（共享单车调度）有目标（优化），属此类。"
        "reply 给空字符串。\n"
        '- "needs_info"：想发起建模但完全没说要解决什么，对象与目标都缺到无从下手'
        "（如「帮我做个建模」「解决这道题」这类指代不明的）；"
        "reply 用一两句中文告知需要提供的内容（题目正文、数据附件）。\n"
        '- "chat"：闲聊、寒暄或与建模无关的一般提问；reply 用一两句中文友好回应，'
        "并说明提供完整赛题即可开始建模。\n"
    ]
    if attachments:
        parts.append(
            "\n用户上传了附件，正文摘录见下。附件内容与输入正文同权判断：附件里"
            "含题面或数据说明即为 modeling_task；若正文与附件都不构成建模题面，"
            "按上述规则回应，并在 reply 中说明附件内容与建模无关。\n"
        )
        for attachment in attachments[:_ATTACHMENT_PROMPT_LIMIT]:
            parts.append(
                f"\n附件「{attachment.name}」内容摘录：\n"
                f"{attachment.excerpt[:_ATTACHMENT_EXCERPT_CHARS]}\n"
            )
    parts.append(f"\n用户输入：\n{goal[:_GOAL_EXCERPT_CHARS]}")
    return "".join(parts)


def _normalize_intent(raw: object) -> str | None:
    value = str(raw or "").strip().strip("\"'").lower()
    if value in INTENTS:
        return value
    return _INTENT_ALIASES.get(value)


def _intent_from_object(data: object) -> tuple[str, str] | None:
    if not isinstance(data, dict):
        return None
    intent = _normalize_intent(data.get("intent"))
    if intent is None:
        return None
    return intent, str(data.get("reply") or "").strip()


def _parse_judge_reply(text: str) -> tuple[str | None, str]:
    """从判定回复里取出意图与回应；解析不出返回 ``(None, "")`` 交调用方放行。

    先按「最外层跨度」解析（模型规矩输出一行 JSON 的常态路径，也能吃下
    reply 正文里带花括号的情况），失败再逐个扫描不含嵌套的候选片段——
    模型在 JSON 前后写解释、套围栏或连发多个对象时靠它兜住。
    """
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = _intent_from_object(json.loads(text[start : end + 1]))
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            return parsed
    for match in _JSON_OBJECT.finditer(text):
        try:
            parsed = _intent_from_object(json.loads(match.group(0)))
        except json.JSONDecodeError:
            continue
        if parsed is not None:
            return parsed
    return None, ""


def _default_reply(intent: str) -> str:
    return _NEEDS_INFO_FALLBACK_REPLY if intent == "needs_info" else _CHAT_FALLBACK_REPLY


def decide_intake(
    config: LlmConfig,
    goal: str,
    has_attachments: bool,
    attachments: Sequence[IntakeAttachment] = (),
    on_usage: Callable[[ChatOutcome], None] | None = None,
) -> IntakeDecision:
    """判定一次发送应当启动任务还是原地回应。绝不抛异常。

    每次判定都留一行日志：这道门拦错人时用户只会说「进不去建模」，没有
    intent/source 就无从回溯是启发式短路还是判定模型误判。signal 与 overrode
    是本地信号层的观察窗——弱模型判定被否决了多少次，只能从这里看出来。
    """
    decision = _decide_intake(config, goal, has_attachments, attachments, on_usage)
    logger.info(
        "task intake intent=%s source=%s signal=%s overrode=%s goal_chars=%d attachments=%d",
        decision.intent,
        decision.source,
        _modeling_signal(goal.strip()) or "none",
        decision.overridden_from or "none",
        len(goal.strip()),
        len(attachments),
    )
    return decision


def _decide_intake(
    config: LlmConfig,
    goal: str,
    has_attachments: bool,
    attachments: Sequence[IntakeAttachment],
    on_usage: Callable[[ChatOutcome], None] | None,
) -> IntakeDecision:
    goal = goal.strip()
    if not config_usable(config):
        # 未配置自定义 API：保持演示/模拟链路的现状（发送即建任务）
        return IntakeDecision("modeling_task", source="fallback")
    if len(goal) >= _LONG_GOAL_CHARS:
        # 长输入视为题面粘贴，直接放行（附件另说也不影响：正文本身已是实质证据）
        return IntakeDecision("modeling_task", source="heuristic")
    evidence = [item for item in attachments if item.excerpt.strip()]
    if has_attachments and not evidence:
        # 附件内容不可见（未解析出文字/纯图片/关闭了自动解析、或确认页只有
        # 元数据）：维持放行，无效题面由问题分析节点的 viability 门兜底
        return IntakeDecision("modeling_task", source="heuristic")
    signal = _modeling_signal(goal)
    if not evidence and len(goal) <= _TRIVIAL_GOAL_CHARS:
        # 极短输入本地处理，不值得出网：带赛题标识的（「A题」「美赛」）是想
        # 做题但没给题面 → 引导补题；其余是寒暄（「你好」「在吗」「你是谁」）
        # → 友好回应，对着一句寒暄索要「完整的赛题正文」属答非所问。
        trivial = "needs_info" if signal == "problem" else "chat"
        return IntakeDecision(trivial, reply=_default_reply(trivial), source="heuristic")
    if signal == "problem":
        # 带赛题标识 = 用户带着题面来的，再问一遍判定模型只会引入误判风险
        return IntakeDecision("modeling_task", source="heuristic")

    candidates = [
        endpoint
        for endpoint in config.endpoints
        if config.allow_proxy or not is_third_party_host(endpoint.host)
    ]
    if not candidates:
        return IntakeDecision("modeling_task", source="fallback")
    judge = min(candidates, key=endpoint_strength)
    try:
        outcome = complete_once(
            judge,
            [{"role": "user", "content": _judge_prompt(goal, evidence)}],
            max_tokens=INTAKE_MAX_TOKENS,
            read_timeout=INTAKE_READ_TIMEOUT_S,
        )
    except Exception as error:  # noqa: BLE001 - 门控失败必须放行，绝不挡真实用户
        logger.warning("task intake judge failed on %s: %s", judge.name, error)
        return IntakeDecision("modeling_task", source="fallback")

    if on_usage is not None:
        try:
            on_usage(outcome)
        except Exception:  # noqa: BLE001 - 用量记账绝不允许影响接待
            logger.exception("task intake usage callback failed")

    intent, reply = _parse_judge_reply(outcome.text)
    if intent is None:
        return IntakeDecision("modeling_task", source="fallback")
    if intent == "needs_info" and signal == "task":
        # 本地已确认输入里有对象也有求解目标，判定模型却说「信息不足」——
        # 而「描述太简略/缺数据附件」正是本门明令不得拦人的理由，这一票只能
        # 是弱模型的误判，以本地结论为准放行（误放行由第二层准入门兜底）。
        return IntakeDecision("modeling_task", source="heuristic", overridden_from=intent)
    if intent != "modeling_task" and not reply:
        reply = _default_reply(intent)
    return IntakeDecision(intent, reply=reply, source="judge")
