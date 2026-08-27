"""发送前的接待判定：对话优先，任务化是显式升级（Codex/opencode 式门控）。

首页发送 → 本判定 → "modeling_task" 才创建 Project/TaskRun 启动六阶段；
"needs_info"/"chat" 由前端在首页原地回应，不建任务、不产生垃圾项目、
不烧整条链路的模型调用。

判定本身是一次限时的轻量调用（选池中最弱模型，与 Auto 路由的难度判定
同策略）；启发式短路能不出网就不出网。任何判定异常一律放行成
"modeling_task"——门控挂了绝不能挡住真实用户；题面实际无效的兜底由
问题分析节点的 viability 准入门负责（第二层，engine_glue/skills）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

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
#: 不超过该长度的输入不可能是题面（如「你好」「在吗」），直接引导补题
_TRIVIAL_GOAL_CHARS = 4

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
    #: 判定来源："heuristic"（启发式短路）/ "judge"（模型判定）/ "fallback"（未配置或判定失败放行）
    source: str = "fallback"


def _judge_prompt(goal: str) -> str:
    return (
        "你是数学建模工作台的接待员，判断用户输入应如何处理。"
        '只输出一行 JSON，形如 {"intent": "...", "reply": "..."}：\n'
        '- "modeling_task"：输入包含可着手建模的实质题面（有问题对象与求解目标，'
        "即使信息不完整）；reply 给空字符串。\n"
        '- "needs_info"：想发起建模但没有给出题面（如「帮我做个建模」「解决这道题」）；'
        "reply 用一两句中文告知需要提供的内容（题目正文、数据附件）。\n"
        '- "chat"：闲聊、寒暄或与建模无关的一般提问；reply 用一两句中文友好回应，'
        "并说明提供完整赛题即可开始建模。\n\n"
        f"用户输入：\n{goal[:_GOAL_EXCERPT_CHARS]}"
    )


def _parse_judge_reply(text: str) -> tuple[Optional[str], str]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None, ""
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    intent = str(data.get("intent") or "")
    if intent not in INTENTS:
        return None, ""
    return intent, str(data.get("reply") or "").strip()


def _default_reply(intent: str) -> str:
    return _NEEDS_INFO_FALLBACK_REPLY if intent == "needs_info" else _CHAT_FALLBACK_REPLY


def decide_intake(
    config: LlmConfig,
    goal: str,
    has_attachments: bool,
    on_usage: Optional[Callable[[ChatOutcome], None]] = None,
) -> IntakeDecision:
    """判定一次发送应当启动任务还是原地回应。绝不抛异常。"""
    goal = goal.strip()
    if not config_usable(config):
        # 未配置自定义 API：保持演示/模拟链路的现状（发送即建任务）
        return IntakeDecision("modeling_task", source="fallback")
    if has_attachments:
        # 带附件的发送几乎总是真题（题面常在附件里）；解析后若仍无效，
        # 由问题分析节点的 viability 门兜底
        return IntakeDecision("modeling_task", source="heuristic")
    if len(goal) >= _LONG_GOAL_CHARS:
        return IntakeDecision("modeling_task", source="heuristic")
    if len(goal) <= _TRIVIAL_GOAL_CHARS:
        return IntakeDecision(
            "needs_info", reply=_NEEDS_INFO_FALLBACK_REPLY, source="heuristic"
        )

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
            [{"role": "user", "content": _judge_prompt(goal)}],
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
    if intent != "modeling_task" and not reply:
        reply = _default_reply(intent)
    return IntakeDecision(intent, reply=reply, source="judge")
