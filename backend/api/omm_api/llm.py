"""自定义模型接口的服务端实现：协议适配、流式解析、备用回退与中转站门控。

设置中心「自定义 API」保存的接口配置存在 users.llm_config（本机后端数据库），
对话回复（/api/chat）与任务执行（engine_glue 的 LLM 节点）都经由本模块调用，
密钥不下发到浏览器、请求不经过前端跨域。

协议映射（apiProtocol 下拉的五个选项）：
- openai / ollama / custom → OpenAI Chat Completions 形状（Ollama 的 /v1 与
  自定义 REST 网关同构，仅 base_url 与路径前缀不同）；
- anthropic → Messages API（x-api-key + anthropic-version，SSE 事件流）；
- gemini → generateContent / streamGenerateContent?alt=sse（key 走查询串）。

回退语义与设置面板文案一致：超时、网络层失败、HTTP 429 限流或 HTTP 402
余额不足时切到下一个已保存接口——余额是接口各自独立的资产，主接口欠费不代表
备用接口不可用；其余模型侧 4xx/5xx 属于配置或内容问题，换接口大概率同样失败，
直接报错。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .errors import ApiError

logger = logging.getLogger("omm.llm")

PROTOCOLS = ("openai", "anthropic", "gemini", "ollama", "custom")

#: 厂商官方域名；不在名单内且非本机地址的一律视为第三方中转站。
OFFICIAL_HOSTS = frozenset(
    {
        "api.openai.com",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
        "api.deepseek.com",
        "dashscope.aliyuncs.com",
        "open.bigmodel.cn",
        # 智谱国内站与 Z.ai 是同一家的两个官方入口，模型 ID 一致
        "api.z.ai",
        "api.moonshot.cn",
        "api.moonshot.ai",
        "api.x.ai",
        "api.mistral.ai",
        "api.minimax.chat",
        "ark.cn-beijing.volces.com",
        "qianfan.baidubce.com",
        "api.hunyuan.cloud.tencent.com",
        "api.stepfun.com",
        "api.siliconflow.cn",
        "openrouter.ai",
    }
)

#: 官网/聊天页域名 ≠ API 域名：新手最常见的填错。命中时直接指出正确地址，
#: 免得用户对着 CDN 的 HTML 错误页猜原因。
_WEBSITE_TO_API = {
    "deepseek.com": "https://api.deepseek.com",
    "www.deepseek.com": "https://api.deepseek.com",
    "chat.deepseek.com": "https://api.deepseek.com",
    "openai.com": "https://api.openai.com",
    "www.openai.com": "https://api.openai.com",
    "chat.openai.com": "https://api.openai.com",
    "chatgpt.com": "https://api.openai.com",
    "anthropic.com": "https://api.anthropic.com",
    "www.anthropic.com": "https://api.anthropic.com",
    "claude.ai": "https://api.anthropic.com",
    "gemini.google.com": "https://generativelanguage.googleapis.com",
    "kimi.moonshot.cn": "https://api.moonshot.cn/v1",
    "www.moonshot.cn": "https://api.moonshot.cn/v1",
    "kimi.com": "https://api.moonshot.cn/v1",
    "www.kimi.com": "https://api.moonshot.cn/v1",
    "tongyi.aliyun.com": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "chatglm.cn": "https://open.bigmodel.cn/api/paas/v4",
    "bigmodel.cn": "https://open.bigmodel.cn/api/paas/v4",
    "www.bigmodel.cn": "https://open.bigmodel.cn/api/paas/v4",
    "www.zhipuai.cn": "https://open.bigmodel.cn/api/paas/v4",
    "z.ai": "https://api.z.ai/api/paas/v4",
    "www.z.ai": "https://api.z.ai/api/paas/v4",
    "chat.z.ai": "https://api.z.ai/api/paas/v4",
    "x.ai": "https://api.x.ai/v1",
    "www.x.ai": "https://api.x.ai/v1",
    "grok.com": "https://api.x.ai/v1",
}

#: Anthropic 要求显式 max_tokens；其余协议不传则由服务端决定。
# Anthropic 协议必须显式 max_tokens。8192 是 Claude 3.5+ 全系支持的输出上限：
# 论文撰写等长产出节点（4500-6000 字 JSON）在 4096 下会被拦腰截断导致解析失败。
ANTHROPIC_DEFAULT_MAX_TOKENS = 8192

CONNECT_TIMEOUT_S = 10.0
CHAT_READ_TIMEOUT_S = 120.0
# 推理型模型即使只回一个 OK 也可能先思考十几秒，测试超时不能太紧
TEST_READ_TIMEOUT_S = 45.0

#: 测试注入缝：单测把它换成返回 httpx.MockTransport 的工厂，避免真实出网。
_transport_factory: Callable[[], Optional[httpx.BaseTransport]] = lambda: None


@dataclass(frozen=True)
class LlmEndpoint:
    """一条已保存接口；字段与设置面板一一对应。"""

    id: str
    name: str
    protocol: str
    base_url: str
    api_key: str = ""
    model: str = ""
    organization: str = ""
    headers: str = ""
    path_prefix: str = ""
    #: 模型能力权重（1-10）；0 = 用户未设置，按模型名推断。
    weight: int = 0

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""


@dataclass(frozen=True)
class LlmConfig:
    endpoints: tuple[LlmEndpoint, ...] = ()
    active_endpoint_id: str = ""
    allow_proxy: bool = True
    stream: bool = True
    fallback: bool = True

    def active(self) -> Optional[LlmEndpoint]:
        for endpoint in self.endpoints:
            if endpoint.id == self.active_endpoint_id:
                return endpoint
        return self.endpoints[0] if self.endpoints else None

    def chain(self) -> list[LlmEndpoint]:
        """调用顺序：主接口在前；开启回退时其余已保存接口按序作为备用。"""
        primary = self.active()
        if primary is None:
            return []
        return self.chain_from(primary)

    def chain_from(self, primary: LlmEndpoint) -> list[LlmEndpoint]:
        """以指定接口为主的调用链（Auto 路由与手动指定接口共用）。"""
        if not self.fallback:
            return [primary]
        return [primary, *[e for e in self.endpoints if e.id != primary.id]]

    def find(self, endpoint_id: str) -> Optional[LlmEndpoint]:
        for endpoint in self.endpoints:
            if endpoint.id == endpoint_id:
                return endpoint
        return None


def parse_llm_config(raw: object) -> LlmConfig:
    """把 users.llm_config 的 JSON 解析成结构；容忍缺字段与历史脏数据。"""
    if not isinstance(raw, dict):
        return LlmConfig()
    endpoints: list[LlmEndpoint] = []
    for item in raw.get("endpoints") or []:
        if not isinstance(item, dict):
            continue
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            continue
        try:
            weight = min(10, max(0, int(item.get("weight") or 0)))
        except (TypeError, ValueError):
            weight = 0
        endpoints.append(
            LlmEndpoint(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or "未命名接口"),
                protocol=str(item.get("protocol") or "openai"),
                base_url=base_url,
                api_key=str(item.get("api_key") or ""),
                model=str(item.get("model") or ""),
                organization=str(item.get("organization") or ""),
                headers=str(item.get("headers") or ""),
                path_prefix=str(item.get("path_prefix") or ""),
                weight=weight,
            )
        )
    return LlmConfig(
        endpoints=tuple(endpoints),
        active_endpoint_id=str(raw.get("active_endpoint_id") or ""),
        allow_proxy=bool(raw.get("allow_proxy", True)),
        stream=bool(raw.get("stream", True)),
        fallback=bool(raw.get("fallback", True)),
    )


def config_usable(config: LlmConfig) -> bool:
    endpoint = config.active()
    return endpoint is not None and bool(endpoint.base_url) and bool(endpoint.model)


# ── 中转站门控 ──────────────────────────────────────────────────────────────


def _is_local_host(host: str) -> bool:
    if host in {"localhost", "::1"}:
        return True
    return host.startswith(("127.", "10.", "192.168."))


def is_third_party_host(host: str) -> bool:
    return bool(host) and not _is_local_host(host) and host not in OFFICIAL_HOSTS


def ensure_proxy_allowed(endpoint: LlmEndpoint, allow_proxy: bool) -> None:
    """「允许使用第三方中转站」关闭时，仅放行官方域名与本机地址。"""
    if is_third_party_host(endpoint.host) and not allow_proxy:
        raise ApiError(
            403,
            "PROXY_DISABLED",
            f"接口 {endpoint.host} 属于第三方中转站，已被设置中心「允许使用第三方中转站」开关阻止",
        )


# ── Auto 模式：能力权重与难度路由 ──────────────────────────────────────────
#
# 模型池 = 用户已保存的全部接口。每条接口的强弱由「模型能力权重」（1-10，
# 设置面板可自定义）决定；未设置时按模型命名习惯推断（旗舰/推理型强，
# flash/mini 等轻量型弱）。Auto 模式先让池中最轻量的模型给问题难度打分
# （1-5），再把问题路由到强弱合适的接口。

#: 模型名中的旗舰/推理型信号与轻量型信号；两类都命中时相互抵消。
#: 各家的档位记号会随命名习惯变化（OpenAI 5.6 的 sol/terra/luna、Anthropic 的
#: fable 都是无先例的新词），带连字符的条目是为了不误伤名字里恰好含该词根的
#: 模型（如 "sol" 会命中 solar 系列）。命中不了的模型按中位 5 处理，用户随时
#: 可以在接口上填「模型能力权重」直接覆盖推断结果。
_STRONG_MODEL_HINTS = (
    "opus", "reasoner", "thinking", "max", "pro", "ultra", "sonnet", "fable", "-sol",
)
_LIGHT_MODEL_HINTS = (
    "flash", "mini", "lite", "nano", "haiku", "turbo", "air", "tiny", "small", "-luna",
)

#: 判定挂了有规则估计兜底，不值得让用户对着空白气泡等 30 秒。
JUDGE_READ_TIMEOUT_S = 10.0
#: 判定输出只要一行 JSON；不设上限时推理型裁判可能烧掉几千思考 token。
#: 个别网关不接受 max_tokens 时判定失败，同样走规则兜底，不影响对话。
JUDGE_MAX_TOKENS = 512
#: 判定提示词里问题的截断：头部保留题面主体，尾部保留「第 N 问/具体要求」
#: （长题的落点常在结尾，只截头会丢掉真正要回答的部分）。
JUDGE_QUESTION_HEAD_CHARS = 1500
JUDGE_QUESTION_TAIL_CHARS = 500

#: 不超过该长度且无难度关键词的消息直接按难度 2 处理，不花判定调用。
TRIVIAL_QUESTION_CHARS = 40
#: 不超过该长度且无新难度信号的追问继承上一轮判定（省一次判定调用）。
FOLLOWUP_QUESTION_CHARS = 200
#: 追问继承的轮数预算：连续继承这么多轮后强制重判一次，防话题漂移。
REJUDGE_EVERY_TURNS = 5

#: 规则估计用的建模类关键词：命中越多视为越难。英文条目服务 COMAP 等
#: 英文赛题（在小写文本上做包含匹配，"optimiz"/"simulat" 覆盖动名词变形）。
_HARD_KEYWORDS = (
    "建模", "优化", "证明", "微分方程", "偏微分", "规划", "仿真", "预测",
    "算法", "机器学习", "神经网络", "论文", "灵敏度", "蒙特卡洛", "马尔可夫",
    "求解", "启发式", "多目标", "约束",
    "optimiz", "prove", "proof", "differential equation", "pde", "simulat",
    "forecast", "predict", "algorithm", "machine learning", "neural network",
    "sensitivity", "monte carlo", "markov", "heuristic", "multi-objective",
    "constraint", "regression", "clustering",
)


def endpoint_strength(endpoint: LlmEndpoint) -> int:
    """接口能力评分（1-10）：用户自定义权重优先，未设置时按模型名推断。"""
    if endpoint.weight:
        return endpoint.weight
    name = endpoint.model.lower()
    score = 5
    if any(hint in name for hint in _STRONG_MODEL_HINTS):
        score += 3
    if any(hint in name for hint in _LIGHT_MODEL_HINTS):
        score -= 3
    return min(10, max(1, score))


def _has_difficulty_signal(question: str) -> bool:
    lowered = question.lower()
    return any(word in lowered for word in _HARD_KEYWORDS)


def heuristic_difficulty(question: str) -> int:
    """判定模型不可用时的规则估计：按问题长度与建模类关键词粗分。

    中文按字符数、英文按词数分档：同一信息量下英文字符数是中文的数倍，
    单一字符阈值会系统性低估英文题面。
    """
    lowered = question.lower()
    hits = sum(1 for word in _HARD_KEYWORDS if word in lowered)
    words = len(re.findall(r"[a-zA-Z]+", question))
    if len(question) > 1200 or words > 250 or hits >= 3:
        return 5
    if len(question) > 400 or words > 80 or hits >= 1:
        return 3
    return 2


def _judge_excerpt(question: str) -> str:
    """判定输入截断：保头部题面主体 + 尾部具体要求，中间省略。"""
    limit = JUDGE_QUESTION_HEAD_CHARS + JUDGE_QUESTION_TAIL_CHARS
    if len(question) <= limit:
        return question
    return (
        question[:JUDGE_QUESTION_HEAD_CHARS]
        + "\n…（中间已省略）…\n"
        + question[-JUDGE_QUESTION_TAIL_CHARS:]
    )


def _difficulty_from_text(text: str) -> Optional[int]:
    """从判定回复提取 1-5：优先 JSON 的 difficulty 字段，退而找独立的 1-5。

    独立 = 前后都不是数字、小数点或连字符：避免把 "3.14"、"12345" 或裁判
    回显的量表 "1-5" 误当难度。"""
    match = re.search(r'"difficulty"\s*:\s*"?([1-5])', text)
    if match is not None:
        return int(match.group(1))
    match = re.search(r"(?<![\d.-])([1-5])(?![\d.-])", text)
    return int(match.group(1)) if match else None


def judge_difficulty(
    judge: LlmEndpoint,
    question: str,
    context: str = "",
    on_usage: Optional[Callable[["ChatOutcome"], None]] = None,
) -> tuple[Optional[int], str, str]:
    """让判定接口为问题难度打分 → (难度, 理由, 实际判定模型)。

    判定失败（超时、限流、输出不含 1-5）返回 (None, "", "")，由调用方退回
    规则估计；判定只是路由参考，绝不能因为它挂了就阻塞对话本身。
    on_usage 在判定调用成功后收到 ChatOutcome（用量监控记账），异常不外抛。
    context 是可选的对话背景（如上一轮回复摘要）：追问文本本身往往很短，
    没有背景时会被误判成闲聊。
    """
    background = f"对话背景（仅供参考）：{context[:500]}\n\n" if context else ""
    prompt = (
        "你是模型路由器，需要评估用户问题的难度来选择合适的模型。难度为 1-5 的整数：\n"
        "1-2 = 闲聊、简单问答或事实查询；3 = 常规分析、普通代码或数学计算；\n"
        "4-5 = 复杂数学建模、多步推理、长文档分析或高精度要求。\n"
        "若问题是对先前对话的简短追问，按对话背景的主题难度评估。\n"
        '只回复一行 JSON，例如 {"difficulty": 3, "reason": "常规数学计算"}，reason 不超过 20 字。\n\n'
        f"{background}用户问题：{_judge_excerpt(question)}"
    )
    try:
        outcome = complete_once(
            judge,
            [{"role": "user", "content": prompt}],
            max_tokens=JUDGE_MAX_TOKENS,
            read_timeout=JUDGE_READ_TIMEOUT_S,
        )
    except Exception as error:  # noqa: BLE001 - 判定失败退回规则估计
        logger.warning("llm route judge failed on %s: %s", judge.name, error)
        return None, "", ""
    if on_usage is not None:
        try:
            on_usage(outcome)
        except Exception:  # noqa: BLE001 - 用量记账绝不允许影响路由本身
            logger.exception("llm route judge usage callback failed")
    difficulty = _difficulty_from_text(outcome.text)
    if difficulty is None:
        return None, "", ""
    reason = re.search(r'"reason"\s*:\s*"([^"]{1,60})"', outcome.text)
    return difficulty, reason.group(1) if reason else "", outcome.model


#: 难度 → 目标能力强度：与候选池构成解耦。旧实现按「最弱/中间/最强」的
#: 位置取接口：池里全是强模型时难度 1 也拿旗舰，弱模型多时中难度反而落到
#: 弱档；按绝对强度找最近的没有这两类失真。
_DIFFICULTY_TARGET_STRENGTH = {1: 2, 2: 3, 3: 5, 4: 8, 5: 10}


def pick_by_difficulty(candidates: list[LlmEndpoint], difficulty: int) -> LlmEndpoint:
    """难度 → 接口：选能力强度最接近目标档的。

    同距时难度 ≥3 偏强（答不好引发的重问比强模型差价更贵），≤2 偏弱（省钱），
    再同则保持保存顺序。
    """
    target = _DIFFICULTY_TARGET_STRENGTH.get(difficulty, 5)
    prefer_strong = difficulty >= 3

    def rank(pair: tuple[int, LlmEndpoint]) -> tuple[int, int, int]:
        index, endpoint = pair
        strength = endpoint_strength(endpoint)
        return (abs(strength - target), -strength if prefer_strong else strength, index)

    return min(enumerate(candidates), key=rank)[1]


@dataclass(frozen=True)
class RouteDecision:
    """一次 Auto 路由的结果：选中的接口、回退链与判定信息。"""

    endpoint: LlmEndpoint
    chain: tuple[LlmEndpoint, ...]
    difficulty: int
    reason: str
    #: 空串 = 判定接口不可用或输出无法解析，难度来自规则估计/继承。
    judge_model: str = ""
    #: 本次是否真的花了一次判定调用；前端据此维护重判轮数计数。
    judged: bool = False
    #: True = 难度未跳档，沿用上一轮接口（保住供应商侧 prompt cache）。
    sticky: bool = False

    def meta(self) -> dict[str, Any]:
        return {
            "mode": "auto",
            "difficulty": self.difficulty,
            "reason": self.reason,
            "judge_model": self.judge_model,
            "endpoint_id": self.endpoint.id,
            "judged": self.judged,
            "sticky": self.sticky,
        }


def auto_route(
    config: LlmConfig,
    question: str,
    context: str = "",
    last_difficulty: Optional[int] = None,
    last_endpoint_id: Optional[str] = None,
    turns_since_judge: int = 0,
    on_usage: Optional[Callable[["ChatOutcome"], None]] = None,
) -> RouteDecision:
    """Auto 模式入口：难度判定 + 按权重路由。

    候选池排除被中转站开关挡下的接口；回退链按能力从强到弱排（选中的在前），
    保证降级后仍是「还能用的里面最强的」。on_usage 透传给难度判定调用记账。

    判定调用本身也是成本，token 纪律按优先级递减：
    1. 短追问继承上一轮难度（last_* 由前端回传 route_state），连续继承
       REJUDGE_EVERY_TURNS 轮或输入出现新难度信号时才重判；
    2. 极短且无建模关键词的消息直接按难度 2，不花判定调用；
    3. 其余照常判定，失败退规则估计。
    选定后若与上一轮难度相差 ≤1 档则沿用上一轮接口：换接口会让供应商侧
    prompt cache 全部失效，长对话下这笔隐性成本远大于相邻档位的能力差。
    """
    candidates = [
        endpoint
        for endpoint in config.endpoints
        if config.allow_proxy or not is_third_party_host(endpoint.host)
    ]
    if not candidates:
        raise ApiError(
            400,
            "LLM_NOT_CONFIGURED",
            "没有可用的模型接口：请检查已保存接口或「允许使用第三方中转站」开关",
        )
    if len(candidates) == 1:
        only = candidates[0]
        return RouteDecision(only, (only,), heuristic_difficulty(question), "仅一个接口可用")

    question = question.strip()
    has_signal = _has_difficulty_signal(question)
    last = (
        next((e for e in candidates if e.id == last_endpoint_id), None)
        if last_endpoint_id
        else None
    )

    judged = False
    judge_model = ""
    if (
        last_difficulty is not None
        and len(question) <= FOLLOWUP_QUESTION_CHARS
        and not has_signal
        and turns_since_judge < REJUDGE_EVERY_TURNS
    ):
        difficulty: Optional[int] = int(last_difficulty)
        reason = "短追问继承上次判定"
    elif (
        last_difficulty is None
        and len(question) <= TRIVIAL_QUESTION_CHARS
        and not has_signal
    ):
        # 只在没有会话状态时短路：会话中途的短消息要么走上面的继承，要么
        # （继承轮数耗尽后）带上下文重判——直接按 2 会把硬题续聊踢到弱模型。
        difficulty, reason = 2, "短消息按简单处理"
    else:
        judge = min(candidates, key=endpoint_strength)
        difficulty, reason, judge_model = judge_difficulty(
            judge, question, context=context, on_usage=on_usage
        )
        judged = True
        if difficulty is None:
            difficulty = heuristic_difficulty(question)
            reason = "按问题长度与关键词估计"
            judge_model = ""

    chosen = pick_by_difficulty(candidates, difficulty)
    sticky = False
    if (
        last is not None
        and last_difficulty is not None
        and abs(difficulty - int(last_difficulty)) <= 1
        and last.id != chosen.id
    ):
        chosen, sticky = last, True
    backups = sorted(
        [endpoint for endpoint in candidates if endpoint.id != chosen.id],
        key=endpoint_strength,
        reverse=True,
    )
    chain = (chosen, *backups) if config.fallback else (chosen,)
    logger.info(
        "llm route: difficulty=%d judge=%s endpoint=%s model=%s strength=%d judged=%s sticky=%s",
        difficulty,
        judge_model or ("heuristic" if judged else "rule"),
        chosen.name,
        chosen.model,
        endpoint_strength(chosen),
        judged,
        sticky,
    )
    return RouteDecision(chosen, chain, difficulty, reason, judge_model, judged, sticky)


# ── 请求构造与响应解析（按协议） ────────────────────────────────────────────


def parse_custom_headers(raw: str) -> dict[str, str]:
    """自定义请求头：支持换行或分号分隔的 `Name: value` 列表。"""
    headers: dict[str, str] = {}
    for piece in raw.replace(";", "\n").splitlines():
        name, sep, value = piece.partition(":")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    return headers


def _openai_family(protocol: str) -> bool:
    return protocol in {"openai", "ollama", "custom"}


def _last_user_index(messages: list[dict[str, Any]]) -> Optional[int]:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return None


def build_chat_request(
    endpoint: LlmEndpoint,
    messages: list[dict[str, str]],
    stream: bool,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    images: Optional[list[dict[str, str]]] = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """按协议组装 (url, headers, body)。

    images（{media_type, data(base64), name}）挂到最后一条 user 消息上，
    按各协议的多模态格式转换（ADR-0010 直通阶梯）；输入 messages 不被改写，
    回退链里每个接口各自重新组装。图片顺序遵循各家文档的推荐：OpenAI 文前图后，
    Anthropic 与 Gemini 图前文后。
    """
    api_base = _WEBSITE_TO_API.get(endpoint.host)
    if api_base:
        raise ApiError(
            400,
            "LLM_WEBSITE_URL",
            f"{endpoint.host} 是官网地址而非 API 地址，请把 Base URL 改为 {api_base}",
        )
    resolved_model = (model or endpoint.model).strip()
    if not resolved_model:
        raise ApiError(400, "LLM_MODEL_MISSING", f"接口「{endpoint.name}」未填写默认模型 ID")
    headers = {"Content-Type": "application/json", **parse_custom_headers(endpoint.headers)}
    base = endpoint.base_url
    prefix = endpoint.path_prefix.strip()

    if _openai_family(endpoint.protocol):
        # 裸域名（无路径）默认补 /v1：OpenAI 兼容网关与 Ollama 的标准路径都是
        # /v1/chat/completions；Base URL 已带路径（如 …/v1、…/api）则原样拼接。
        # 显式「路径前缀」永远优先。
        default_prefix = "/chat/completions" if urlsplit(base).path.strip("/") else "/v1/chat/completions"
        url = base + (prefix or default_prefix)
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        if endpoint.organization:
            headers["OpenAI-Organization"] = endpoint.organization
        payload_messages: list[dict[str, Any]] = list(messages)
        target = _last_user_index(payload_messages) if images else None
        if images and target is not None:
            payload_messages[target] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": messages[target]["content"]},
                    *(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"},
                        }
                        for img in images
                    ),
                ],
            }
        body: dict[str, Any] = {"model": resolved_model, "messages": payload_messages, "stream": stream}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return url, headers, body

    if endpoint.protocol == "anthropic":
        url = base + (prefix or "/v1/messages")
        headers["x-api-key"] = endpoint.api_key
        headers["anthropic-version"] = "2023-06-01"
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        chat_messages: list[dict[str, Any]] = [m for m in messages if m["role"] != "system"]
        anthropic_target = _last_user_index(chat_messages) if images else None
        if images and anthropic_target is not None:
            anthropic_text = chat_messages[anthropic_target]["content"]
            chat_messages[anthropic_target] = {
                "role": "user",
                "content": [
                    *(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img["media_type"],
                                "data": img["data"],
                            },
                        }
                        for img in images
                    ),
                    {"type": "text", "text": anthropic_text},
                ],
            }
        body = {
            "model": resolved_model,
            "max_tokens": max_tokens or ANTHROPIC_DEFAULT_MAX_TOKENS,
            "messages": chat_messages,
            "stream": stream,
        }
        if system:
            body["system"] = system
        return url, headers, body

    if endpoint.protocol == "gemini":
        action = "streamGenerateContent" if stream else "generateContent"
        query = f"?key={endpoint.api_key}" + ("&alt=sse" if stream else "")
        url = f"{base}{prefix or '/v1beta'}/models/{resolved_model}:{action}{query}"
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        contents: list[dict[str, Any]] = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
            if m["role"] != "system"
        ]
        if images:
            for index in range(len(contents) - 1, -1, -1):
                if contents[index]["role"] != "user":
                    continue
                contents[index] = {
                    "role": "user",
                    "parts": [
                        *(
                            {"inlineData": {"mimeType": img["media_type"], "data": img["data"]}}
                            for img in images
                        ),
                        *contents[index]["parts"],
                    ],
                }
                break
        body = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if max_tokens is not None:
            body["generationConfig"] = {"maxOutputTokens": max_tokens}
        return url, headers, body

    raise ApiError(400, "LLM_PROTOCOL_UNSUPPORTED", f"不支持的接口协议：{endpoint.protocol}")


def _normalize_usage(protocol: str, usage: object) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    if _openai_family(protocol):
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    elif protocol == "anthropic":
        prompt, completion = usage.get("input_tokens"), usage.get("output_tokens")
    else:  # gemini usageMetadata
        prompt, completion = usage.get("promptTokenCount"), usage.get("candidatesTokenCount")
    result = {}
    if isinstance(prompt, int):
        result["prompt_tokens"] = prompt
    if isinstance(completion, int):
        result["completion_tokens"] = completion
    return result


def parse_chat_response(protocol: str, data: dict[str, Any]) -> tuple[str, str, dict[str, int], str]:
    """非流式响应 → (回答文本, 思考过程, 用量, 实际模型)。结构异常时给出可执行的报错。

    思考过程按各协议的推理字段提取：OpenAI 系是 DeepSeek 开创、各家中转站
    普遍跟随的 ``reasoning_content``（部分网关用 ``reasoning``）；Anthropic 是
    ``thinking`` 内容块；Gemini 是带 ``thought: true`` 的 part。没有就返回空串。
    """
    try:
        if _openai_family(protocol):
            message = data["choices"][0]["message"]
            text = message.get("content") or ""
            reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
            return text, reasoning, _normalize_usage(protocol, data.get("usage")), str(data.get("model") or "")
        if protocol == "anthropic":
            blocks = data.get("content", [])
            text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
            reasoning = "".join(
                block.get("thinking", "") for block in blocks if block.get("type") == "thinking"
            )
            return text, reasoning, _normalize_usage(protocol, data.get("usage")), str(data.get("model") or "")
        if protocol == "gemini":
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
            reasoning = "".join(part.get("text", "") for part in parts if part.get("thought"))
            return text, reasoning, _normalize_usage(protocol, data.get("usageMetadata")), str(data.get("modelVersion") or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError(502, "LLM_BAD_RESPONSE", f"模型接口返回了无法解析的结构：{exc!r}") from exc
    raise ApiError(400, "LLM_PROTOCOL_UNSUPPORTED", f"不支持的接口协议：{protocol}")


def parse_stream_data(protocol: str, payload: str) -> tuple[str, str, bool, dict[str, int]]:
    """一行 SSE data → (回答增量, 思考增量, 是否结束, 用量增量)。无法解析的行按空增量跳过。"""
    if _openai_family(protocol) and payload.strip() == "[DONE]":
        return "", "", True, {}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return "", "", False, {}
    if _openai_family(protocol):
        choices = data.get("choices") or []
        delta_obj = (choices[0].get("delta") or {}) if choices else {}
        delta = delta_obj.get("content") or ""
        reasoning = str(delta_obj.get("reasoning_content") or delta_obj.get("reasoning") or "")
        done = bool(choices and choices[0].get("finish_reason"))
        return delta, reasoning, done, _normalize_usage(protocol, data.get("usage"))
    if protocol == "anthropic":
        kind = data.get("type")
        if kind == "content_block_delta":
            delta_obj = data.get("delta") or {}
            if delta_obj.get("type") == "thinking_delta":
                return "", str(delta_obj.get("thinking") or ""), False, {}
            return str(delta_obj.get("text") or ""), "", False, {}
        if kind == "message_start":
            # 输入 tokens 只出现在 message_start：不读它流式调用会丢 prompt 用量
            message = data.get("message") or {}
            return "", "", False, _normalize_usage(protocol, message.get("usage"))
        if kind == "message_delta":
            return "", "", False, _normalize_usage(protocol, data.get("usage"))
        return "", "", kind == "message_stop", {}
    if protocol == "gemini":
        candidates = data.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        delta = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        reasoning = "".join(part.get("text", "") for part in parts if part.get("thought"))
        done = bool(candidates and candidates[0].get("finishReason"))
        return delta, reasoning, done, _normalize_usage(protocol, data.get("usageMetadata"))
    return "", "", False, {}


# ── HTTP 执行与备用回退 ─────────────────────────────────────────────────────


def bypasses_http_proxy(host: str) -> bool:
    """本机 / 私网 / 链路本地地址：经系统代理转发没有意义，必须直连。

    httpx 的 trust_env 只认 NO_PROXY 环境变量，不读 Windows 注册表的
    ProxyOverride——即便系统自己的 proxy_bypass('127.0.0.1') 返回 True、
    绕过列表里明明写着 127.*，httpx 照样把请求塞进系统代理。真实后果：
    开着系统代理时，本机 Ollama / vLLM / 自建网关的调用被送进代理，用户
    拿到的是代理返回的「接口 X 返回 HTTP 502」，与接口本身毫无关系。
    """
    host = host.strip().strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _direct_mounts(url: str) -> dict[str, Optional[httpx.BaseTransport]]:
    """只把该直连的目标摘出系统代理，而不是整体关掉 trust_env。

    境内访问官方厂商多半依赖系统代理，一刀切关掉会把能用的接口打死；按目标
    主机挂一条更具体的空代理规则，httpx 取最具体匹配，官方域名照走代理。
    """
    host = urlsplit(url).hostname or ""
    if not bypasses_http_proxy(host):
        return {}
    pattern_host = f"[{host}]" if ":" in host else host
    return {f"all://{pattern_host}": None}


def _client(read_timeout: float, url: str = "") -> httpx.Client:
    timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT_S)
    return httpx.Client(
        timeout=timeout,
        transport=_transport_factory(),
        follow_redirects=False,
        mounts=_direct_mounts(url),
    )


def _upstream_error(endpoint: LlmEndpoint, response: httpx.Response) -> ApiError:
    snippet = response.text[:300]
    try:
        payload = response.json()
        detail = payload.get("error", {})
        if isinstance(detail, dict) and detail.get("message"):
            snippet = str(detail["message"])[:300]
        elif isinstance(payload.get("message"), str):
            snippet = payload["message"][:300]
    except Exception:  # noqa: BLE001 - 上游错误体可以是任何东西
        pass
    if response.status_code == 402:
        # 余额不足单独归码：换一个有余额的接口就能继续（进 _FALLBACK_CODES），
        # 失败指引也按「充值」方向给（engine_glue 按 code 分型），不与其它
        # 上游错误混在 LLM_UPSTREAM_ERROR 里。
        return ApiError(
            402,
            "LLM_NO_BALANCE",
            f"接口「{endpoint.name}」余额不足（HTTP 402）：{snippet}",
        )
    return ApiError(
        502,
        "LLM_UPSTREAM_ERROR",
        f"接口「{endpoint.name}」返回 HTTP {response.status_code}：{snippet}",
    )


#: 换一个接口就可能成功的错误：限流 / 超时 / 网络层失败，以及 402 余额不足——
#: 余额是接口各自独立的资产（真实事故：DeepSeek 402 时链里的 GLM 余额充足，
#: 却因 402 不回退导致整个任务失败）。
_FALLBACK_CODES = frozenset(
    {"LLM_RATE_LIMITED", "LLM_TIMEOUT", "LLM_UNREACHABLE", "LLM_NO_BALANCE"}
)

#: 同一条调用链稍后重来就可能自愈的瞬态类（EngineLlmPort 整次调用重试用）。
#: 402 不在其中：余额不足是确定性失败，链内回退已试过备用接口，原样重试
#: 只会再撞一次。
_TRANSIENT_CODES = frozenset({"LLM_RATE_LIMITED", "LLM_TIMEOUT", "LLM_UNREACHABLE"})


def _should_fall_back(error: Exception) -> bool:
    """与面板文案对齐：超时、网络层失败、限流与余额不足触发备用切换。"""
    if isinstance(error, httpx.TimeoutException | httpx.TransportError):
        return True
    return isinstance(error, ApiError) and error.code in _FALLBACK_CODES


def _is_transient(error: Exception) -> bool:
    """整次调用重试的判定：只认换个时间重来可能自愈的瞬态类。"""
    if isinstance(error, httpx.TimeoutException | httpx.TransportError):
        return True
    return isinstance(error, ApiError) and error.code in _TRANSIENT_CODES


def _post(endpoint: LlmEndpoint, url: str, headers: dict[str, str], body: dict[str, Any], read_timeout: float) -> httpx.Response:
    """一次出网调用：网络层异常归一成可执行的错误信封，绝不以 500 裸露给页面。"""
    try:
        with _client(read_timeout, url) as client:
            return client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as error:
        raise ApiError(
            504,
            "LLM_TIMEOUT",
            f"接口「{endpoint.name}」超过 {int(read_timeout)} 秒未响应（{endpoint.host}），"
            "可能是模型思考时间过长或网关排队，请重试或换用响应更快的模型",
        ) from error
    except httpx.TransportError as error:
        raise ApiError(
            502,
            "LLM_UNREACHABLE",
            f"无法连接接口「{endpoint.name}」（{endpoint.host}）：{error}",
        ) from error


def _response_json(endpoint: LlmEndpoint, response: httpx.Response) -> dict[str, Any]:
    if 300 <= response.status_code < 400:
        location = response.headers.get("Location", "未提供 Location")
        raise ApiError(
            502,
            "LLM_REDIRECTED",
            f"接口「{endpoint.name}」返回重定向（HTTP {response.status_code} → {location}），"
            "请把 Base URL 改为最终地址",
        )
    try:
        data = response.json()
    except ValueError as error:
        raise ApiError(
            502,
            "LLM_BAD_RESPONSE",
            f"接口「{endpoint.name}」返回了非 JSON 内容（HTTP {response.status_code}）："
            f"{response.text[:160]}",
        ) from error
    if not isinstance(data, dict):
        raise ApiError(502, "LLM_BAD_RESPONSE", f"接口「{endpoint.name}」返回了意外的 JSON 结构")
    return data


@dataclass
class ChatOutcome:
    text: str
    model: str
    endpoint: LlmEndpoint
    reasoning: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0
    fallback_used: bool = False


def _log_call(endpoint: LlmEndpoint, model: str, status: str, elapsed_ms: int, usage: dict[str, int]) -> None:
    """「记录接口用量」：每次调用输出一行结构化日志，可与请求 ID 对照。"""
    logger.info(
        "llm call endpoint=%s host=%s model=%s status=%s elapsed_ms=%d prompt_tokens=%s completion_tokens=%s",
        endpoint.name,
        endpoint.host,
        model,
        status,
        elapsed_ms,
        usage.get("prompt_tokens", "-"),
        usage.get("completion_tokens", "-"),
    )


def complete_once(
    endpoint: LlmEndpoint,
    messages: list[dict[str, str]],
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    read_timeout: float = CHAT_READ_TIMEOUT_S,
    images: Optional[list[dict[str, str]]] = None,
) -> ChatOutcome:
    """对单个接口做一次非流式补全；429 归一为 LLM_RATE_LIMITED 供回退判断。"""
    url, headers, body = build_chat_request(
        endpoint, messages, stream=False, model=model, max_tokens=max_tokens, images=images
    )
    started = time.monotonic()
    try:
        response = _post(endpoint, url, headers, body, read_timeout)
    except ApiError as error:
        _log_call(endpoint, model or endpoint.model, error.code, int((time.monotonic() - started) * 1000), {})
        raise
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if response.status_code == 429:
        _log_call(endpoint, model or endpoint.model, "429", elapsed_ms, {})
        raise ApiError(429, "LLM_RATE_LIMITED", f"接口「{endpoint.name}」触发限流（HTTP 429）")
    if response.status_code >= 400:
        _log_call(endpoint, model or endpoint.model, str(response.status_code), elapsed_ms, {})
        raise _upstream_error(endpoint, response)
    text, reasoning, usage, actual_model = parse_chat_response(
        endpoint.protocol, _response_json(endpoint, response)
    )
    outcome = ChatOutcome(
        text=text,
        model=actual_model or (model or endpoint.model),
        endpoint=endpoint,
        reasoning=reasoning,
        usage=usage,
        elapsed_ms=elapsed_ms,
    )
    _log_call(endpoint, outcome.model, "ok", elapsed_ms, usage)
    return outcome


def complete_with_fallback(
    config: LlmConfig,
    messages: list[dict[str, str]],
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    chain: Optional[list[LlmEndpoint]] = None,
    images: Optional[list[dict[str, str]]] = None,
) -> ChatOutcome:
    """按主接口 → 备用接口的顺序尝试非流式补全；chain 可由 Auto 路由指定。"""
    chain = list(chain) if chain is not None else config.chain()
    if not chain:
        raise ApiError(400, "LLM_NOT_CONFIGURED", "尚未配置模型接口，请在设置中心「自定义 API」中保存")
    last_error: Exception | None = None
    for index, endpoint in enumerate(chain):
        ensure_proxy_allowed(endpoint, config.allow_proxy)
        try:
            outcome = complete_once(endpoint, messages, model=model, max_tokens=max_tokens, images=images)
            outcome.fallback_used = index > 0
            return outcome
        except Exception as error:  # noqa: BLE001 - 统一走回退判定
            last_error = error
            if index + 1 < len(chain) and _should_fall_back(error):
                logger.warning("llm fallback: %s -> %s (%s)", endpoint.name, chain[index + 1].name, error)
                continue
            raise
    raise last_error if last_error else AssertionError("unreachable")


def stream_complete_with_fallback(
    config: LlmConfig,
    messages: list[dict[str, str]],
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    chain: Optional[list[LlmEndpoint]] = None,
    on_delta: Optional[Callable[[str, str], None]] = None,
) -> ChatOutcome:
    """流式补全并聚合成 ChatOutcome（任务引擎的 LLM 节点用）。

    与 complete_with_fallback 同一回退语义（仅在还没吐出任何增量之前切换备用），
    额外通过 on_delta(channel, text) 逐块回调增量：channel 为 "reasoning"（思考）
    或 "text"（正文）。流式的读超时按块计而不是按整个响应计——长产出（论文、
    实验代码）不会再因为总时长超过读超时而被误判为超时。
    """
    chain = list(chain) if chain is not None else config.chain()
    if not chain:
        raise ApiError(400, "LLM_NOT_CONFIGURED", "尚未配置模型接口，请在设置中心「自定义 API」中保存")
    last_error: Exception | None = None
    for index, endpoint in enumerate(chain):
        ensure_proxy_allowed(endpoint, config.allow_proxy)
        url, headers, body = build_chat_request(
            endpoint, messages, stream=True, model=model, max_tokens=max_tokens
        )
        started = time.monotonic()
        emitted = False
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, int] = {}
        plain_lines: list[str] = []
        actual_model = ""
        try:
            with _client(CHAT_READ_TIMEOUT_S, url) as client, client.stream(
                "POST", url, headers=headers, json=body
            ) as response:
                if response.status_code == 429:
                    raise ApiError(429, "LLM_RATE_LIMITED", f"接口「{endpoint.name}」触发限流（HTTP 429）")
                if response.status_code >= 400:
                    response.read()
                    raise _upstream_error(endpoint, response)
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        # 个别网关忽略 stream 标志直接回整段 JSON：攒下来按非流式解析
                        if line.strip():
                            plain_lines.append(line)
                        continue
                    delta, reasoning, done, usage_update = parse_stream_data(
                        endpoint.protocol, line[5:].strip()
                    )
                    usage.update(usage_update)
                    if reasoning:
                        emitted = True
                        reasoning_parts.append(reasoning)
                        if on_delta is not None:
                            on_delta("reasoning", reasoning)
                    if delta:
                        emitted = True
                        text_parts.append(delta)
                        if on_delta is not None:
                            on_delta("text", delta)
                    if done:
                        break
            if not emitted and plain_lines:
                # 非流式兜底：网关无视 stream=true 时按普通响应解析，不让整次调用报废
                try:
                    data = json.loads("\n".join(plain_lines))
                except json.JSONDecodeError:
                    data = None
                if isinstance(data, dict):
                    text, reasoning, usage, actual_model = parse_chat_response(endpoint.protocol, data)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                        if on_delta is not None:
                            on_delta("reasoning", reasoning)
                    if text:
                        text_parts.append(text)
                        if on_delta is not None:
                            on_delta("text", text)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            outcome = ChatOutcome(
                text="".join(text_parts),
                model=actual_model or (model or endpoint.model),
                endpoint=endpoint,
                reasoning="".join(reasoning_parts),
                usage=usage,
                elapsed_ms=elapsed_ms,
                fallback_used=index > 0,
            )
            _log_call(endpoint, outcome.model, "ok", elapsed_ms, usage)
            return outcome
        except Exception as error:  # noqa: BLE001 - 统一走回退判定
            elapsed_ms = int((time.monotonic() - started) * 1000)
            _log_call(endpoint, model or endpoint.model, "error", elapsed_ms, usage)
            should_fall_back = not emitted and index + 1 < len(chain) and _should_fall_back(error)
            # 网络层异常归一成可执行的错误信封（与 _post 同语义），不裸露 traceback
            if isinstance(error, httpx.TimeoutException):
                error = ApiError(
                    504,
                    "LLM_TIMEOUT",
                    f"接口「{endpoint.name}」流式响应中断超过 {int(CHAT_READ_TIMEOUT_S)} 秒（{endpoint.host}），请重试或换用更稳定的接口",
                )
            elif isinstance(error, httpx.TransportError):
                error = ApiError(
                    502,
                    "LLM_UNREACHABLE",
                    f"无法连接接口「{endpoint.name}」（{endpoint.host}）：{error}",
                )
            last_error = error
            if should_fall_back:
                logger.warning("llm fallback: %s -> %s (%s)", endpoint.name, chain[index + 1].name, error)
                continue
            raise error
    raise last_error if last_error else AssertionError("unreachable")


def stream_events(
    config: LlmConfig,
    messages: list[dict[str, str]],
    model: Optional[str] = None,
    chain: Optional[list[LlmEndpoint]] = None,
    extra_meta: Optional[dict[str, Any]] = None,
    images: Optional[list[dict[str, str]]] = None,
) -> Iterator[dict[str, Any]]:
    """流式对话事件：meta → delta* → done；失败时产出 error 事件。

    chain 可由 Auto 路由指定；extra_meta（如路由判定结果）并入 meta 事件。
    回退只发生在「还没吐出任何增量」之前；已经开始输出后中断只能如实报错，
    换接口重放会造成内容重复或前后不一致。
    """
    chain = list(chain) if chain is not None else config.chain()
    if not chain:
        yield {"type": "error", "code": "LLM_NOT_CONFIGURED", "message": "尚未配置模型接口，请在设置中心「自定义 API」中保存"}
        return
    for index, endpoint in enumerate(chain):
        try:
            ensure_proxy_allowed(endpoint, config.allow_proxy)
        except ApiError as error:
            yield {"type": "error", "code": error.code, "message": error.message}
            return
        url, headers, body = build_chat_request(endpoint, messages, stream=True, model=model, images=images)
        started = time.monotonic()
        emitted = False
        usage: dict[str, int] = {}
        try:
            with _client(CHAT_READ_TIMEOUT_S, url) as client, client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code == 429:
                    raise ApiError(429, "LLM_RATE_LIMITED", f"接口「{endpoint.name}」触发限流（HTTP 429）")
                if response.status_code >= 400:
                    response.read()
                    raise _upstream_error(endpoint, response)
                yield {
                    "type": "meta",
                    "endpoint": endpoint.name,
                    "host": endpoint.host,
                    "model": model or endpoint.model,
                    "third_party": is_third_party_host(endpoint.host),
                    "fallback_used": index > 0,
                    **(extra_meta or {}),
                }
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    delta, reasoning, done, usage_update = parse_stream_data(endpoint.protocol, line[5:].strip())
                    usage.update(usage_update)
                    if reasoning:
                        emitted = True
                        yield {"type": "reasoning", "text": reasoning}
                    if delta:
                        emitted = True
                        yield {"type": "delta", "text": delta}
                    if done:
                        break
            elapsed_ms = int((time.monotonic() - started) * 1000)
            _log_call(endpoint, model or endpoint.model, "ok", elapsed_ms, usage)
            yield {"type": "done", "usage": usage, "elapsed_ms": elapsed_ms}
            return
        except Exception as error:  # noqa: BLE001 - 统一走回退判定
            elapsed_ms = int((time.monotonic() - started) * 1000)
            _log_call(endpoint, model or endpoint.model, "error", elapsed_ms, usage)
            if not emitted and index + 1 < len(chain) and _should_fall_back(error):
                logger.warning("llm fallback: %s -> %s (%s)", endpoint.name, chain[index + 1].name, error)
                continue
            message = error.message if isinstance(error, ApiError) else f"接口「{endpoint.name}」请求失败：{error}"
            code = error.code if isinstance(error, ApiError) else "LLM_REQUEST_FAILED"
            yield {"type": "error", "code": code, "message": message}
            return


def test_endpoint(endpoint: LlmEndpoint, allow_proxy: bool) -> ChatOutcome:
    """「测试连接」：最小补全验证连通性、密钥与模型 ID 全链路可用。

    不设 max_tokens：推理型模型的思考预算可能远超小额上限，强行设小值
    会让本来可用的接口在测试里报错或返回空内容。
    """
    ensure_proxy_allowed(endpoint, allow_proxy)
    return complete_once(
        endpoint,
        [{"role": "user", "content": "连接测试，请仅回复：OK"}],
        read_timeout=TEST_READ_TIMEOUT_S,
    )


# ── 模型列表：让「默认模型 ID」跟上厂商上新 ────────────────────────────────
#
# 厂商预设表是快照，写下来那天就开始过期。三种协议都提供了「列出当前可用
# 模型」的接口，直接问接口本身拿，新模型上线当天就能在补全里选到。

#: 只是拉一张表，不该按对话的耐心等；网关不实现该接口时也能快速落到提示。
MODELS_READ_TIMEOUT_S = 20.0
#: 中转站动辄聚合上千个模型，补全列表塞不下也没有意义，截断即可。
MODELS_MAX_ITEMS = 300


def build_models_request(endpoint: LlmEndpoint) -> tuple[str, dict[str, str]]:
    """按协议组装模型列表接口的 (url, headers)。

    不复用「路径前缀」：那一项是给对话补全路径用的，模型列表在各协议下都是
    另一条固定路径，套上去只会 404。
    """
    headers = {"Accept": "application/json", **parse_custom_headers(endpoint.headers)}
    base = endpoint.base_url

    if _openai_family(endpoint.protocol):
        # 与对话补全同一套判断：裸域名补 /v1，已带路径的原样拼接。
        url = base + ("/models" if urlsplit(base).path.strip("/") else "/v1/models")
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        if endpoint.organization:
            headers["OpenAI-Organization"] = endpoint.organization
        return url, headers

    if endpoint.protocol == "anthropic":
        headers["x-api-key"] = endpoint.api_key
        headers["anthropic-version"] = "2023-06-01"
        return base + "/v1/models", headers

    if endpoint.protocol == "gemini":
        # 默认只回一页，显式要大页避免旧型号占满、新型号被截在后面。
        return f"{base}/v1beta/models?pageSize=200&key={endpoint.api_key}", headers

    raise ApiError(400, "LLM_PROTOCOL_UNSUPPORTED", f"不支持的接口协议：{endpoint.protocol}")


def parse_models_response(protocol: str, data: dict[str, Any]) -> list[str]:
    """模型列表响应 → 去重后的模型 ID（保持服务端返回顺序，通常新在前）。"""
    if protocol == "gemini":
        # Gemini 的条目名带 "models/" 前缀，填进模型 ID 输入框要去掉。
        raw = [
            str(item.get("name") or "").removeprefix("models/")
            for item in data.get("models") or []
            if isinstance(item, dict)
        ]
    else:
        raw = [
            str(item.get("id") or "")
            for item in data.get("data") or []
            if isinstance(item, dict)
        ]
    seen: dict[str, None] = {}
    for name in raw:
        if name:
            seen.setdefault(name, None)
    return list(seen)[:MODELS_MAX_ITEMS]


def list_models(endpoint: LlmEndpoint, allow_proxy: bool) -> list[str]:
    """拉取该接口实际提供的模型 ID。

    这是补全建议而非必要能力：不少自建网关和中转站没有实现模型列表接口，
    命中 404/405 时给出「手填模型 ID」的明确指引，不要让用户对着通用错误
    以为整条接口坏了。
    """
    ensure_proxy_allowed(endpoint, allow_proxy)
    url, headers = build_models_request(endpoint)
    try:
        with _client(MODELS_READ_TIMEOUT_S, url) as client:
            response = client.get(url, headers=headers)
    except httpx.TimeoutException as error:
        raise ApiError(
            504,
            "LLM_TIMEOUT",
            f"接口「{endpoint.name}」拉取模型列表超时（{endpoint.host}）",
        ) from error
    except httpx.TransportError as error:
        raise ApiError(
            502,
            "LLM_UNREACHABLE",
            f"无法连接接口「{endpoint.name}」（{endpoint.host}）：{error}",
        ) from error
    if response.status_code in (404, 405):
        raise ApiError(
            502,
            "LLM_MODELS_UNSUPPORTED",
            f"接口「{endpoint.name}」没有提供模型列表（HTTP {response.status_code}），请直接手填模型 ID",
        )
    if response.status_code >= 400:
        raise _upstream_error(endpoint, response)
    return parse_models_response(endpoint.protocol, _response_json(endpoint, response))


# ── 任务引擎的 LlmPort 适配 ────────────────────────────────────────────────


#: 思考内容进事件流的截断上限。取值要能装下推理模型一次调用的完整思考链
#: （论文写作、方案规划这类长任务的 reasoning 常有数万字），超出才截断并在
#: 末尾留可见标记——静默砍断会让工作台的「深度思考」盒子看起来是坏的。
THINKING_EVENT_MAX_CHARS = 200_000

#: llm_delta 过程事件的节流与总量上限：事件表是过程观测通道，增量按秒级
#: 批量下发（工作台实时可见）。上限只作为异常输出（模型进入重复循环等）的
#: 兜底闸门，正常长文生成不该碰到它；触顶时补发一条 notice 增量说明原因，
#: 而不是让实时盒子无声停住。结尾的 thinking/llm_call 摘要事件不受影响。
DELTA_EVENT_FLUSH_SECONDS = 1.0
DELTA_EVENT_MAX_TOTAL_CHARS = 800_000

#: 触顶时补发的说明文案（channel=notice，前端原样追加到实时盒子末尾）。
DELTA_EVENT_TRUNCATED_NOTICE = (
    "\n\n[本次生成的实时增量已达 {limit:,} 字上限，后续内容不再逐字推送；"
    "完整结果以本步骤的最终产出为准。]"
)

#: 任务引擎一次 LLM 调用的最大尝试数（1 次正常 + 至多 2 次瞬态故障重试）。
#: 只重试 _is_transient 认定的瞬态类（断连 / 超时 / 限流）；402 余额不足是
#: 确定性失败（链内回退已试过备用接口），不烧重试预算。输出结构校验失败的
#: 修复重试是 harness 内环的另一套预算，与这里无关。
ENGINE_CALL_MAX_ATTEMPTS = 3
#: 第 1、2 次重试前的退避秒数：给上游网关一点恢复时间，也避免限流下连环撞墙。
ENGINE_CALL_RETRY_BACKOFF_S = (2.0, 5.0)


class _DeltaEventBuffer:
    """把逐 token 的流式增量攒成秒级 llm_delta 事件（顺序保留、总量封顶）。"""

    def __init__(self, emit: Callable[[dict[str, Any]], None], prompt_id: str) -> None:
        self._emit = emit
        self._prompt_id = prompt_id
        self._chunks: list[tuple[str, str]] = []
        self._last_flush = time.monotonic()
        self._emitted_chars = 0
        self._notice_sent = False

    def push(self, channel: str, text: str) -> None:
        if self._emitted_chars >= DELTA_EVENT_MAX_TOTAL_CHARS:
            self._notify_truncated()
            return
        self._chunks.append((channel, text))
        if time.monotonic() - self._last_flush >= DELTA_EVENT_FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        if not self._chunks:
            self._last_flush = time.monotonic()
            return
        # 合并相邻同通道块：一次 flush 通常只产出一两条事件
        merged: list[tuple[str, list[str]]] = []
        for channel, text in self._chunks:
            if merged and merged[-1][0] == channel:
                merged[-1][1].append(text)
            else:
                merged.append((channel, [text]))
        self._chunks = []
        self._last_flush = time.monotonic()
        for channel, texts in merged:
            budget = DELTA_EVENT_MAX_TOTAL_CHARS - self._emitted_chars
            if budget <= 0:
                self._notify_truncated()
                return
            joined = "".join(texts)
            text = joined[:budget]
            self._emitted_chars += len(text)
            self._emit({
                "kind": "llm_delta",
                "prompt_id": self._prompt_id,
                "channel": channel,
                "text": text,
            })
            if len(text) < len(joined):
                self._notify_truncated()
                return

    def _notify_truncated(self) -> None:
        """触顶只说明一次：让实时盒子有明确结尾，而不是无声定格。"""
        if self._notice_sent:
            return
        self._notice_sent = True
        self._emit({
            "kind": "llm_delta",
            "prompt_id": self._prompt_id,
            "channel": "notice",
            "text": DELTA_EVENT_TRUNCATED_NOTICE.format(
                limit=DELTA_EVENT_MAX_TOTAL_CHARS
            ),
        })


def _clip_thinking(reasoning: str) -> str:
    """思考内容进事件流前的封顶：正常长度原样保留，触顶才截断并标明，
    让工作台展开后能分清「思考就这么长」和「被平台截断了」。"""
    if len(reasoning) <= THINKING_EVENT_MAX_CHARS:
        return reasoning
    return (
        reasoning[:THINKING_EVENT_MAX_CHARS]
        + f"\n\n[思考内容超过 {THINKING_EVENT_MAX_CHARS:,} 字上限，"
        f"此处已截断（原文共 {len(reasoning):,} 字）。]"
    )


def _estimate_tokens(text: str) -> int:
    """无用量数据时的粗估（中英混排按 2 字符 ≈ 1 token）：预算治理与用量
    监控需要一个量级正确的数字，0 会让 token 上限形同虚设。"""
    return max(1, len(text) // 2)


def notes_prompt_block(
    notes: Sequence[tuple[str, str]], node_id: Optional[str]
) -> str:
    """运行中用户备注 → 提示词「用户补充要求」段（设计 §11.3 方案 A 注入点）。

    scope=global 对全部节点生效；scope=某阶段只对该阶段的调用生效。备注是
    append-only 的累积补充（全部有效，按时间序展示）；无匹配备注返回空串。
    注入发生在模板渲染之后、修复反馈之前——它属于任务上下文而非本轮对话。
    ContextAssembler 接线生产后，本逻辑迁移为 TaskFrame 的「用户补充要求」节。
    """
    selected = [text for scope, text in notes if scope == "global" or scope == node_id]
    if not selected:
        return ""
    lines = "\n".join(f"- {text}" for text in selected)
    return f"\n\n## 用户补充要求（运行中追加，必须遵守）\n{lines}"


class EngineLlmPort:
    """omm_agent_core.LlmPort 实现：渲染提示词模板后走同一条回退链。

    技能节点自带 JSON 解析与一次修复重试，这里只负责把渲染好的提示词发出去。
    on_event 是过程事件回调（进 run.log）：每次模型调用**开始**时产出一条
    llm_call_started（工作台立即显示走秒中的思考行，调用期间不再静默），
    结束时产出一条 llm_call 摘要（模型/接口/耗时/用量），推理模型另有一条
    thinking（思考内容），供工作台把「智能体正在做什么」逐条展示出来
    （设计文档 §12.4 微技能级文案）。
    """

    def __init__(
        self,
        config: LlmConfig,
        registry: Any,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
        on_usage: Optional[Callable[[ChatOutcome], None]] = None,
        budget: Optional[Any] = None,
        node_for_prompt: Optional[dict[str, str]] = None,
        user_notes: Sequence[tuple[str, str]] = (),
    ) -> None:
        self._config = config
        self._registry = registry
        self._on_event = on_event
        self._on_usage = on_usage
        # 预算治理（鸭子类型，避免本模块硬依赖 harness）：调用前 check_llm_call
        # 预检、拿到回复后 charge_llm 记账；超限由治理器抛 AgentError 硬停。
        self._budget = budget
        self._node_for_prompt = dict(node_for_prompt or {})
        # 运行中用户备注（(scope, text) 时间序）：端口按 tick 重建，新备注在
        # 下一次节点执行自然生效（§11.3「下一次节点执行注入」的落点）。
        self._user_notes = tuple(user_notes)

    def _emit(self, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(payload)
        except Exception:  # noqa: BLE001 - 过程展示事件绝不允许影响执行本身
            logger.exception("llm 过程事件回调失败")

    def _complete_with_transport_retry(
        self,
        prompt_id: str,
        node_id: Optional[str],
        messages: list[dict[str, str]],
        repair: bool,
    ) -> ChatOutcome:
        """整次调用粒度的瞬态故障自愈：断连 / 超时 / 限流时重来一次调用。

        流式生成中途上游断连（peer closed connection 等）在传输层不做接口
        回退——增量已经吐出，换接口续写会内容重复；但在任务引擎里一次调用
        失败会让整个任务直接 FAILED（真实案例：论文章节写到一半 DeepSeek 断
        连，任务死亡、页面实时盒子定格在半截）。这里按「整次调用」重试：
        部分增量作废、从头重新生成。前端对重复的 llm_call_started 会复用同
        一行、清空实时区并把标题标成「第 N 次尝试」，事件语义现成。
        """
        last_error: Exception | None = None
        for attempt in range(1, ENGINE_CALL_MAX_ATTEMPTS + 1):
            # 预算预检（失控保护）：超限在花钱之前拦下（重试轮也要过这道门，
            # 墙钟 / 调用数达限时绝不靠重试续命），AgentError 上抛由节点层
            # 转成带 E31x/E32x 错误码的干净失败信息。
            if self._budget is not None:
                self._budget.check_llm_call(node_id)
            # 调用开始即发过程事件：模型一次调用动辄一两分钟，没有这条事件的话
            # 工作台在整个阶段里收不到任何东西，结束时才一次性收到全部过程行。
            self._emit({
                "kind": "llm_call_started",
                "prompt_id": prompt_id,
                "repair": repair,
            })
            try:
                if self._config.stream:
                    # 流式：思考与正文增量按秒级批量进事件流，工作台实时可见；
                    # 读超时按块计，长产出不再被总时长误杀。每次尝试用全新的
                    # 增量缓冲，截断预算随尝试重置。
                    deltas = _DeltaEventBuffer(self._emit, prompt_id)
                    outcome = stream_complete_with_fallback(
                        self._config, messages, on_delta=deltas.push
                    )
                    deltas.flush()
                    return outcome
                return complete_with_fallback(self._config, messages)
            except Exception as error:
                # 失败也要给事件流一个收尾：没有这条事件，工作台的走秒思考行会
                # 永远悬挂（页面重进时还会堆出一排同秒走时的僵尸行）。
                message = error.message if isinstance(error, ApiError) else str(error)
                self._emit({
                    "kind": "llm_call_failed",
                    "prompt_id": prompt_id,
                    "error": message[:300],
                })
                last_error = error
                if attempt < ENGINE_CALL_MAX_ATTEMPTS and _is_transient(error):
                    backoff = ENGINE_CALL_RETRY_BACKOFF_S[
                        min(attempt - 1, len(ENGINE_CALL_RETRY_BACKOFF_S) - 1)
                    ]
                    logger.warning(
                        "llm engine call retry: prompt=%s attempt=%d/%d backoff=%.0fs (%s)",
                        prompt_id, attempt, ENGINE_CALL_MAX_ATTEMPTS, backoff, message,
                    )
                    time.sleep(backoff)
                    continue
                raise
        raise last_error if last_error else AssertionError("unreachable")

    def complete(self, prompt_id: str, variables: dict[str, Any]) -> str:
        template = self._registry.get(prompt_id)
        prompt = template.render(variables)
        node_id = self._node_for_prompt.get(prompt_id)
        # 运行中用户备注（§11.3）：属任务上下文，拼在修复反馈之前。
        prompt += notes_prompt_block(self._user_notes, node_id)
        # 技能节点的一次修复重试通过 __repair_error/__previous_output 传入；
        # 模板正文没有这两个占位符，必须在这里显式拼接，否则「把错误反馈给
        # 模型」退化成盲目原样重发。
        repair_error = str(variables.get("__repair_error") or "")
        if repair_error:
            previous = str(variables.get("__previous_output") or "")
            prompt += (
                "\n\n## 上次输出未通过校验\n\n"
                f"校验错误：{repair_error}\n\n"
                f"上次输出（节选）：\n{previous[:2000]}\n\n"
                "请修正以上问题，重新只输出一个符合输出要求的 JSON 对象。"
            )
        messages = [{"role": "user", "content": prompt}]
        outcome = self._complete_with_transport_retry(
            prompt_id, node_id, messages, repair=bool(repair_error)
        )
        return self._account_and_emit(prompt_id, node_id, outcome, prompt_text=prompt)

    def chat_text(self, messages: list[dict[str, str]], *, label: str) -> str:
        """会话式调用（skills 侧 chat_adapter 的鸭子契约）：沙盒执行体的多轮
        写码/跑码会话经此出网。

        与 ``complete`` 同一条出口纪律：传输层瞬态重试、预算预检与记账、过程
        事件（llm_call_started/thinking/llm_call）、用量监控全部在端口内完成。
        ``label`` 即提示词 id（experiment_code.sandbox / data_cleaning.sandbox），
        事件展示与预算记账按 ``node_for_prompt`` 归属到节点。运行中用户备注
        （§11.3）拼进系统消息——实验阶段迁到沙盒会话后该特性不随 complete
        路径退场；沙盒任务卡属任务上下文，备注拼接位置与模板渲染路径同义。
        """
        node_id = self._node_for_prompt.get(label)
        wire = [dict(message) for message in messages]
        notes = notes_prompt_block(self._user_notes, node_id)
        if notes:
            for message in wire:
                if message.get("role") == "system":
                    message["content"] = str(message.get("content") or "") + notes
                    break
            else:
                wire.insert(0, {"role": "system", "content": notes.strip()})
        outcome = self._complete_with_transport_retry(label, node_id, wire, repair=False)
        # 会话没有单条 prompt 文本：指纹与用量粗估都以整个消息序列的规范化
        # 序列化为准（与 ContextAssembler 的 prompt_hash 同一构造思路）。
        canonical = json.dumps(
            [[str(m.get("role") or ""), str(m.get("content") or "")] for m in wire],
            ensure_ascii=False,
        )
        return self._account_and_emit(label, node_id, outcome, prompt_text=canonical)

    def _account_and_emit(
        self,
        prompt_id: str,
        node_id: Optional[str],
        outcome: ChatOutcome,
        prompt_text: str,
    ) -> str:
        """调用成功后的统一出口（complete 与 chat_text 共用）：用量补齐与
        记账、思考/调用摘要事件、prompt 指纹。"""
        # 部分网关的流式响应不带用量：按字符粗估补齐，预算硬停与用量监控
        # 需要量级正确的数字（0 会让 token 上限形同虚设）。
        estimated = False
        if not outcome.usage.get("prompt_tokens"):
            outcome.usage["prompt_tokens"] = _estimate_tokens(prompt_text)
            estimated = True
        if not outcome.usage.get("completion_tokens"):
            outcome.usage["completion_tokens"] = _estimate_tokens(
                outcome.text + outcome.reasoning
            )
            estimated = True
        if self._budget is not None:
            tokens = int(outcome.usage.get("prompt_tokens") or 0) + int(
                outcome.usage.get("completion_tokens") or 0
            )
            self._budget.charge_llm(tokens, node_id)
        if self._on_usage is not None:
            try:
                self._on_usage(outcome)
            except Exception:  # noqa: BLE001 - 用量记账绝不允许影响任务执行
                logger.exception("llm 用量记账回调失败")
        if outcome.reasoning:
            self._emit({
                "kind": "thinking",
                "prompt_id": prompt_id,
                "elapsed_ms": outcome.elapsed_ms,
                "text": _clip_thinking(outcome.reasoning),
            })
        self._emit({
            "kind": "llm_call",
            "prompt_id": prompt_id,
            "model": outcome.model,
            "endpoint": outcome.endpoint.name,
            "elapsed_ms": outcome.elapsed_ms,
            "prompt_tokens": outcome.usage.get("prompt_tokens"),
            "completion_tokens": outcome.usage.get("completion_tokens"),
            # D2.2 审计规格：最终发出 prompt 的指纹（渲染+备注+修复拼接之后）。
            # 「同输入同 prompt」的纯函数纪律由此可在事件日志层面被 evals 断言。
            "prompt_hash": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
            **({"tokens_estimated": True} if estimated else {}),
        })
        return outcome.text
