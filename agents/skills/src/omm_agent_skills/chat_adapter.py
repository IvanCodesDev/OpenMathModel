"""文本协议会话适配器：LlmPort 的会话扩展 → 内环 ChatFn。

沙盒 Agent 执行体（harness ``run_sandbox_task``）需要「多轮对话 + 工具调用」
的 ChatFn；控制面的模型出口（EngineLlmPort 及其测试替身）走用户配置的五种
协议，不依赖厂商原生 function calling。两者之间用**文本协议**衔接：

- 端口侧鸭子契约：``chat_text(messages, label=...) -> str``——收 role∈
  {system,user,assistant} 的 dict 消息序列，返回原始回复文本。传输重试、
  预算记账、过程事件全部留在端口内部（与 ``complete`` 同一条出口纪律）。
- 模型侧信封约定：需要调用工具时先写一句中文说明（端口把它发成执行轨迹里的
  agent_note 旁白），紧接着一个 JSON 对象 ``{"tool": "<工具名>", "arguments": {...}}``；
  适配器把它解析成合成 ToolCall 交给内环，工具观察以 user 消息回给模型。终答是不含
  ``tool`` 键的 JSON，原样透传给内环的 parser/validator（结构违约走 R1 修复梯）。
- 信封走样的宽容：参数与 ``tool`` 平级的扁平信封、``arguments`` 写成 JSON 字符串，都还原成
  参数；一条回复写了几个信封只执行第一个，下一条工具结果末尾告知其余没执行；工具报缺参时，
  在那条工具结果末尾附该工具的正确信封写法。
- 模型原生工具调用标记漏进正文（DeepSeek 的 ``<｜｜DSML｜｜ invoke …>`` 块）：没有 JSON 信封
  时按第一个 invoke 还原成信封执行，下一条工具结果末尾提示改回信封写法；与 JSON 信封同时
  出现时只执行信封，并告知原生标记里的调用没有执行。

协议指令文本（:func:`tool_protocol_note`）由本模块单点持有，节点装配任务卡
时拼进 task_brief——模型看到的协议说明与适配器的解析规则永远同源。
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from omm_agent_harness import Message, Reply, ToolCall, Usage

__all__ = [
    "ChatTextPort",
    "supports_chat",
    "text_protocol_chat",
    "tool_protocol_note",
]


@runtime_checkable
class ChatTextPort(Protocol):
    """LlmPort 的会话扩展（鸭子契约；core 协议保持只有 complete）。"""

    def chat_text(self, messages: list[dict[str, str]], *, label: str) -> str: ...


def supports_chat(llm: Any) -> bool:
    return callable(getattr(llm, "chat_text", None))


#: 工具目录的协议说明（与 omm_agent_tools 的注册名对齐，装配期契约）：沙盒
#: 五件套 + 卡片知识库两个只读工具（方案阶段提议人自主检索，§10.3 切片二）。
#: code_run 是多语言统一入口（§7.4，H7）：language 由任务卡固定，执行体会退回换语言的调用。
#: 每项 = (用途说明, arguments 写法)；协议说明与缺参提示的信封示例都从这里取。
_TOOL_USAGE: dict[str, tuple[str, str]] = {
    "python_run": ("执行完整 Python 脚本。", '{"code": "<脚本源码>"}'),
    "code_run": (
        "按任务卡实现语言执行完整脚本。",
        '{"code": "<脚本源码>", "language": "<任务卡给定的语言，如 python / r>"}',
    ),
    "ws_write": ("写工作区 UTF-8 文本文件。", '{"path": "相对路径", "text": "内容"}'),
    "ws_read": (
        "读工作区文本文件，一次一段（约 3000 字符）；结果 truncated 为 true 时"
        "带返回的 next_offset 作 offset 再读下一段。",
        '{"path": "相对路径", "offset": 可选整数（从第几个字符起读，缺省 0）}',
    ),
    "ws_list": ("列出工作区文件。", '{"prefix": "可选路径前缀"}'),
    "env_probe": ("探测运行环境（可用包清单）。", "{}"),
    "knowledge_search": (
        "检索赛题与获奖论文卡片库，返回带出处的命中列表。",
        '{"query": "关键词", "kind": "可选 problem / paper", '
        '"task_type": "可选题型或建模方向子串", "limit": 可选整数}',
    ),
    "knowledge_read": (
        "按卡片 id 读全卡（赛题含正文与挂接论文，论文含奖项 / 模型）。",
        '{"card_id": "如 problem:cumcm-2021-c"}',
    ),
}

#: 沙盒任务卡的终答落点（模板里的章节名）；其它消费者按各自模板传 final_hint。
_DEFAULT_FINAL_HINT = "按「工作方式与终答要求」输出终答 JSON（终答不含 tool 键）"


def tool_protocol_note(tools: Sequence[str], final_hint: str | None = None) -> str:
    """给模型看的工具调用协议说明；tools 是本任务允许的工具名清单。

    ``final_hint`` 指向终答要求所在的章节（缺省是沙盒任务卡的「工作方式与终答
    要求」），让协议说明与调用方模板的章节名对得上。
    """
    lines = [
        f"- {name}：{_TOOL_USAGE[name][0]}arguments = {_TOOL_USAGE[name][1]}"
        for name in tools
        if name in _TOOL_USAGE
    ]
    return (
        "工具调用协议：需要执行动作时，先用一句简短的中文说明这一步要做什么、为什么"
        "（这句话会原样展示给用户，不要使用花括号），紧接着输出一个 JSON 对象，除此之外不要其它文字："
        '{"tool": "<工具名>", "arguments": {...}}。可用工具：\n'
        + "\n".join(lines)
        + "\n工具结果会以下一条消息回给你。全部动作完成并自查达标后，"
        + (final_hint or _DEFAULT_FINAL_HINT)
        + "。"
    )


_FENCE_CHARS = "`"
#: 整段解析失败后逐个 ``{`` 试解时最多试几处（说明文字里误写的花括号、坏信封里代码的花括号）。
_MAX_SCAN_STARTS = 8
#: omm_agent_tools 注册表缺参报错的原文（``ToolRegistry.validate_args``）。
_MISSING_ARGUMENTS_MARK = "missing required arguments"

#: DeepSeek 原生工具调用标记（真库所见：``<`` + 两个全角竖线 + ``DSML`` + 两个全角竖线 + 空格 +
#: 标签名）；竖线个数与空白放宽。``parameter`` 的 ``string="false"`` 表示取值是 JSON。
_DSML_BAR = "[\uff5c|]+"
_DSML_OPEN = rf"<\s*{_DSML_BAR}\s*DSML\s*{_DSML_BAR}\s*"
_DSML_CLOSE = rf"<\s*/\s*{_DSML_BAR}\s*DSML\s*{_DSML_BAR}\s*"
_DSML_INVOKE = re.compile(
    rf'{_DSML_OPEN}invoke\s+name\s*=\s*"([^"]*)"\s*>(.*?){_DSML_CLOSE}invoke\s*>', re.DOTALL
)
_DSML_PARAMETER = re.compile(
    rf'{_DSML_OPEN}parameter\s+name\s*=\s*"([^"]*)"(?:\s+string\s*=\s*"(true|false)")?\s*>'
    rf"(.*?){_DSML_CLOSE}parameter\s*>",
    re.DOTALL,
)


def _is_envelope(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("tool"), str)
        and bool(value["tool"].strip())
    )


def _parse_envelope(raw: str) -> tuple[dict[str, Any], bool] | None:
    """尽力解析工具信封，返回 (信封, 其后是否还有信封)；不是信封（或根本不是 JSON）返回 None。"""
    candidate = raw.strip()
    if not candidate:
        return None
    parsed: Any = None
    if _FENCE_CHARS in candidate or not candidate.startswith("{"):
        # 复用技能层的宽容解析（围栏/前后杂文）
        from .nodes import extract_json

        try:
            parsed = extract_json(candidate)
        except (json.JSONDecodeError, ValueError):
            parsed = None
    else:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = None
    if parsed is None:
        # 整段不是一个 JSON：一条回复写了几个信封（首 { 到末 } 会 Extra data），或说明文字里
        # 误写了花括号——取第一个能解出来的对象，它是信封才算
        return _first_envelope(candidate)
    return (parsed, False) if _is_envelope(parsed) else None


def _first_envelope(text: str) -> tuple[dict[str, Any], bool] | None:
    decoder = json.JSONDecoder()
    start = text.find("{")
    for _ in range(_MAX_SCAN_STARTS):
        if start == -1:
            return None
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if not _is_envelope(value):
            return None
        return value, '"tool"' in text[end:]
    return None


def _envelope_arguments(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """信封 → 工具参数：``arguments`` 对象原样；是 JSON 字符串就解开；没有可用的
    ``arguments``（扁平信封）就把 ``tool`` 以外的顶层键当参数。"""
    arguments = envelope.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = None
    if isinstance(arguments, Mapping):
        return dict(arguments)
    return {key: value for key, value in envelope.items() if key not in ("tool", "arguments")}


def _dsml_value(raw: str, string_flag: str) -> Any:
    if string_flag != "false":
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _native_envelopes(text: str) -> list[dict[str, Any]]:
    """正文里漏出的原生（DSML）工具调用 → 信封，按出现顺序；没有就是空列表。"""
    envelopes: list[dict[str, Any]] = []
    for name, body in _DSML_INVOKE.findall(text):
        if not name.strip():
            continue
        parameters = {
            key: _dsml_value(value, string_flag)
            for key, string_flag, value in _DSML_PARAMETER.findall(body)
        }
        # 整个参数对象塞进一个名为 arguments 的 parameter（真库沙盒里的常见写法）
        arguments: Any = parameters["arguments"] if set(parameters) == {"arguments"} else parameters
        envelopes.append({"tool": name.strip(), "arguments": arguments})
    return envelopes


def _missing_arguments_hint(tool: str) -> str:
    usage = _TOOL_USAGE.get(tool)
    example = (
        f'{{"tool": "{tool}", "arguments": {usage[1]}}}'
        if usage
        else '{"tool": "<工具名>", "arguments": {...}}'
    )
    return (
        "[协议提示] 参数要放进 arguments 对象、键名照工具说明写，不能与 tool 平级。"
        f"正确写法：{example}"
    )


def _extra_envelopes_hint(tool: str) -> str:
    return (
        f"[协议提示] 上一条回复写了不止一个工具信封，只执行了第一个（{tool}），后面的都没有执行；"
        "每条回复只写一个信封，等这条结果回来再发下一个。"
    )


def _native_markup_hint(tool: str, skipped: Sequence[str]) -> str:
    rest = f"，后面的 {'、'.join(skipped)} 没有执行" if skipped else ""
    return (
        "[协议提示] 上一条回复用的是模型原生的工具调用标记（DSML），"
        f"已当作 JSON 信封执行了第一个（{tool}）{rest}；之后请按工具说明直接写 "
        '{"tool": "<工具名>", "arguments": {...}}，每条回复只写一个。'
    )


def _native_skipped_hint(tools: Sequence[str]) -> str:
    return (
        f"[协议提示] 上一条回复里还有用模型原生标记（DSML）写的工具调用（{'、'.join(tools)}），"
        "没有执行——只认 JSON 信封；每条回复只写一个信封，等这条结果回来再发下一个。"
    )


def to_wire_messages(messages: Sequence[Message]) -> list[dict[str, str]]:
    """harness Message → 端口 dict 消息；tool 观察折叠成 user 文本。

    文本协议下厂商侧没有 tool 角色的合法上下文（没有原生 tool_calls 配对），
    观察必须以 user 消息回传；前缀标注让模型区分「用户话语」与「工具结果」。
    """
    wire: list[dict[str, str]] = []
    for message in messages:
        if message.role == "tool":
            wire.append({
                "role": "user",
                "content": f"[工具执行结果]\n{message.content}",
            })
        else:
            wire.append({"role": message.role, "content": message.content})
    return wire


def text_protocol_chat(llm: ChatTextPort, *, label: str, on_call=None):
    """把 ``chat_text`` 端口包成内环 ChatFn。

    ``on_call`` 是每次模型调用的计数回调（节点统计 llm_attempts 用）；
    回复的 usage 恒为零——用量记账在端口内部完成（与 ``_port_chat`` 同一
    纪律，内环的 tally 不是计费出处）。
    """
    counter = itertools.count(1)
    # 追加在工具结果末尾的协议提示：消息序号 → (上一条回复原文, 工具结果原文, 提示)。同一个
    # chat 会被多波沙盒复用（每波都是新会话），两段原文都对上才贴，新会话里同序号的消息不会误贴。
    hints: dict[int, tuple[str, str, str]] = {}
    state: dict[str, Any] = {"tool": "", "pending": []}

    def chat(messages: Sequence[Message]) -> Reply:
        if on_call is not None:
            on_call()
        if len(messages) > 1 and messages[-1].role == "tool":
            notes = list(state["pending"])
            if _MISSING_ARGUMENTS_MARK in messages[-1].content:
                notes.append(_missing_arguments_hint(state["tool"]))
            if notes:
                hints[len(messages) - 1] = (
                    messages[-2].content, messages[-1].content, "\n".join(notes)
                )
        state["pending"] = []
        wire = to_wire_messages(messages)
        for index, (reply_text, result_text, note) in hints.items():
            if (
                0 < index < len(messages)
                and messages[index].content == result_text
                and messages[index - 1].content == reply_text
            ):
                wire[index]["content"] += "\n\n" + note
        raw = llm.chat_text(wire, label=label)
        parsed = _parse_envelope(raw)
        native = _native_envelopes(raw)
        follow_ups: list[str] = []
        alongside: list[dict[str, Any]] = []
        if parsed is None and native:
            parsed = (native[0], False)
            follow_ups.append(
                _native_markup_hint(native[0]["tool"], [item["tool"] for item in native[1:]])
            )
        else:
            alongside = native
        if parsed is None:
            return Reply(content=raw, tool_calls=(), usage=Usage(0, 0, 0), model="llm-port")
        envelope, more = parsed
        name = envelope["tool"].strip()
        arguments = _envelope_arguments(envelope)
        # 原生标记只是把同一个调用又写了一遍的不算「没执行」
        missed = [
            item["tool"]
            for item in alongside
            if (item["tool"], _envelope_arguments(item)) != (name, arguments)
        ]
        if missed:
            follow_ups.append(_native_skipped_hint(missed))
        if more:
            follow_ups.append(_extra_envelopes_hint(name))
        state["tool"] = name
        state["pending"] = follow_ups
        call = ToolCall(id=f"tp_{next(counter)}", name=name, arguments=arguments)
        return Reply(content=raw, tool_calls=(call,), usage=Usage(0, 0, 0), model="llm-port")

    return chat
