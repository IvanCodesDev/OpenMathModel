"""自定义模型接口的服务端实现：协议适配、流式解析、备用回退与中转站门控。

设置中心「自定义 API」保存的接口配置存在 users.llm_config（本机后端数据库），
对话回复（/api/chat）与任务执行（engine_glue 的 LLM 节点）都经由本模块调用，
密钥不下发到浏览器、请求不经过前端跨域。

协议映射（apiProtocol 下拉的五个选项）：
- openai / ollama / custom → OpenAI Chat Completions 形状（Ollama 的 /v1 与
  自定义 REST 网关同构，仅 base_url 与路径前缀不同）；
- anthropic → Messages API（x-api-key + anthropic-version，SSE 事件流）；
- gemini → generateContent / streamGenerateContent?alt=sse（key 走查询串）。

回退语义与设置面板文案一致：仅在超时、网络层失败或 HTTP 429 时切到下一个
已保存接口；模型侧 4xx/5xx 属于配置或内容问题，换接口大概率同样失败，直接报错。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator
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
        "api.moonshot.cn",
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
    "www.zhipuai.cn": "https://open.bigmodel.cn/api/paas/v4",
    "x.ai": "https://api.x.ai/v1",
    "www.x.ai": "https://api.x.ai/v1",
    "grok.com": "https://api.x.ai/v1",
}

#: Anthropic 要求显式 max_tokens；其余协议不传则由服务端决定。
ANTHROPIC_DEFAULT_MAX_TOKENS = 4096

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
_STRONG_MODEL_HINTS = ("opus", "reasoner", "thinking", "max", "pro", "ultra", "sonnet")
_LIGHT_MODEL_HINTS = ("flash", "mini", "lite", "nano", "haiku", "turbo", "air", "tiny", "small")

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


def build_chat_request(
    endpoint: LlmEndpoint,
    messages: list[dict[str, str]],
    stream: bool,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """按协议组装 (url, headers, body)。"""
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
        body: dict[str, Any] = {"model": resolved_model, "messages": messages, "stream": stream}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return url, headers, body

    if endpoint.protocol == "anthropic":
        url = base + (prefix or "/v1/messages")
        headers["x-api-key"] = endpoint.api_key
        headers["anthropic-version"] = "2023-06-01"
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        body = {
            "model": resolved_model,
            "max_tokens": max_tokens or ANTHROPIC_DEFAULT_MAX_TOKENS,
            "messages": [m for m in messages if m["role"] != "system"],
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
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
            if m["role"] != "system"
        ]
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


def _client(read_timeout: float) -> httpx.Client:
    timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT_S)
    return httpx.Client(timeout=timeout, transport=_transport_factory(), follow_redirects=False)


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
    return ApiError(
        502,
        "LLM_UPSTREAM_ERROR",
        f"接口「{endpoint.name}」返回 HTTP {response.status_code}：{snippet}",
    )


_FALLBACK_CODES = frozenset({"LLM_RATE_LIMITED", "LLM_TIMEOUT", "LLM_UNREACHABLE"})


def _should_fall_back(error: Exception) -> bool:
    """与面板文案对齐：仅超时、网络层失败与限流触发备用切换。"""
    if isinstance(error, httpx.TimeoutException | httpx.TransportError):
        return True
    return isinstance(error, ApiError) and error.code in _FALLBACK_CODES


def _post(endpoint: LlmEndpoint, url: str, headers: dict[str, str], body: dict[str, Any], read_timeout: float) -> httpx.Response:
    """一次出网调用：网络层异常归一成可执行的错误信封，绝不以 500 裸露给页面。"""
    try:
        with _client(read_timeout) as client:
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
) -> ChatOutcome:
    """对单个接口做一次非流式补全；429 归一为 LLM_RATE_LIMITED 供回退判断。"""
    url, headers, body = build_chat_request(endpoint, messages, stream=False, model=model, max_tokens=max_tokens)
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
) -> ChatOutcome:
    """按主接口 → 备用接口的顺序尝试非流式补全；chain 可由 Auto 路由指定。"""
    chain = list(chain) if chain is not None else config.chain()
    if not chain:
        raise ApiError(400, "LLM_NOT_CONFIGURED", "尚未配置模型接口，请在设置中心「自定义 API」中保存")
    last_error: Exception | None = None
    for index, endpoint in enumerate(chain):
        ensure_proxy_allowed(endpoint, config.allow_proxy)
        try:
            outcome = complete_once(endpoint, messages, model=model, max_tokens=max_tokens)
            outcome.fallback_used = index > 0
            return outcome
        except Exception as error:  # noqa: BLE001 - 统一走回退判定
            last_error = error
            if index + 1 < len(chain) and _should_fall_back(error):
                logger.warning("llm fallback: %s -> %s (%s)", endpoint.name, chain[index + 1].name, error)
                continue
            raise
    raise last_error if last_error else AssertionError("unreachable")


def stream_events(
    config: LlmConfig,
    messages: list[dict[str, str]],
    model: Optional[str] = None,
    chain: Optional[list[LlmEndpoint]] = None,
    extra_meta: Optional[dict[str, Any]] = None,
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
        url, headers, body = build_chat_request(endpoint, messages, stream=True, model=model)
        started = time.monotonic()
        emitted = False
        usage: dict[str, int] = {}
        try:
            with _client(CHAT_READ_TIMEOUT_S) as client, client.stream("POST", url, headers=headers, json=body) as response:
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


# ── 任务引擎的 LlmPort 适配 ────────────────────────────────────────────────


#: 思考内容进事件流的截断上限：事件是过程展示不是正文存储，防止巨型 payload。
THINKING_EVENT_MAX_CHARS = 6000

class EngineLlmPort:
    """omm_agent_core.LlmPort 实现：渲染提示词模板后走同一条回退链。

    技能节点自带 JSON 解析与一次修复重试，这里只负责把渲染好的提示词发出去。
    on_event 是过程事件回调（进 run.log）：每次模型调用产出一条 llm_call
    摘要（模型/接口/耗时/用量），推理模型另有一条 thinking（思考内容），
    供工作台把「智能体正在做什么」逐条展示出来（设计文档 §12.4 微技能级文案）。
    """

    def __init__(
        self,
        config: LlmConfig,
        registry: Any,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
        on_usage: Optional[Callable[[ChatOutcome], None]] = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._on_event = on_event
        self._on_usage = on_usage

    def _emit(self, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(payload)
        except Exception:  # noqa: BLE001 - 过程展示事件绝不允许影响执行本身
            logger.exception("llm 过程事件回调失败")

    def complete(self, prompt_id: str, variables: dict[str, Any]) -> str:
        template = self._registry.get(prompt_id)
        prompt = template.render(variables)
        outcome = complete_with_fallback(
            self._config,
            [{"role": "user", "content": prompt}],
        )
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
                "text": outcome.reasoning[:THINKING_EVENT_MAX_CHARS],
            })
        self._emit({
            "kind": "llm_call",
            "prompt_id": prompt_id,
            "model": outcome.model,
            "endpoint": outcome.endpoint.name,
            "elapsed_ms": outcome.elapsed_ms,
            "prompt_tokens": outcome.usage.get("prompt_tokens"),
            "completion_tokens": outcome.usage.get("completion_tokens"),
        })
        return outcome.text
