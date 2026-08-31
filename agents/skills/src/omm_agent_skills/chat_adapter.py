"""文本协议会话适配器：LlmPort 的会话扩展 → 内环 ChatFn。

沙盒 Agent 执行体（harness ``run_sandbox_task``）需要「多轮对话 + 工具调用」
的 ChatFn；控制面的模型出口（EngineLlmPort 及其测试替身）走用户配置的五种
协议，不依赖厂商原生 function calling。两者之间用**文本协议**衔接：

- 端口侧鸭子契约：``chat_text(messages, label=...) -> str``——收 role∈
  {system,user,assistant} 的 dict 消息序列，返回原始回复文本。传输重试、
  预算记账、过程事件全部留在端口内部（与 ``complete`` 同一条出口纪律）。
- 模型侧信封约定：需要调用工具时**只输出**一个 JSON 对象
  ``{"tool": "<工具名>", "arguments": {...}}``；适配器把它解析成合成
  ToolCall 交给内环，工具观察以 user 消息回给模型。终答是不含 ``tool``
  键的 JSON，原样透传给内环的 parser/validator（结构违约走 R1 修复梯）。

协议指令文本（:func:`tool_protocol_note`）由本模块单点持有，节点装配任务卡
时拼进 task_brief——模型看到的协议说明与适配器的解析规则永远同源。
"""

from __future__ import annotations

import itertools
import json
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


#: 沙盒工具目录的协议说明（与 omm_agent_tools 的注册名对齐，装配期契约）。
_TOOL_USAGE_LINES = {
    "python_run": '- python_run：执行完整 Python 脚本。arguments = {"code": "<脚本源码>"}',
    "ws_write": '- ws_write：写工作区 UTF-8 文本文件。arguments = {"path": "相对路径", "text": "内容"}',
    "ws_read": '- ws_read：读工作区文本文件。arguments = {"path": "相对路径"}',
    "ws_list": '- ws_list：列出工作区文件。arguments = {"prefix": "可选路径前缀"}',
    "env_probe": "- env_probe：探测运行环境（可用包清单）。arguments = {}",
}


def tool_protocol_note(tools: Sequence[str]) -> str:
    """给模型看的工具调用协议说明；tools 是本任务允许的工具名清单。"""
    lines = [_TOOL_USAGE_LINES[name] for name in tools if name in _TOOL_USAGE_LINES]
    return (
        "工具调用协议：需要执行动作时，只输出一个 JSON 对象（不要任何其它文字）："
        '{"tool": "<工具名>", "arguments": {...}}。可用工具：\n'
        + "\n".join(lines)
        + "\n工具结果会以下一条消息回给你。全部动作完成并自查达标后，"
        "按「工作方式与终答要求」输出终答 JSON（终答不含 tool 键）。"
    )


_FENCE_CHARS = "`"


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """尽力解析工具信封；不是信封（或根本不是 JSON）返回 None。"""
    candidate = raw.strip()
    if not candidate:
        return None
    if _FENCE_CHARS in candidate or not candidate.startswith("{"):
        # 复用技能层的宽容解析（围栏/前后杂文），失败即视为非信封
        from .nodes import extract_json

        try:
            parsed = extract_json(candidate)
        except (json.JSONDecodeError, ValueError):
            return None
    else:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict) and isinstance(parsed.get("tool"), str) and parsed["tool"].strip():
        return parsed
    return None


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

    def chat(messages: Sequence[Message]) -> Reply:
        if on_call is not None:
            on_call()
        raw = llm.chat_text(to_wire_messages(messages), label=label)
        envelope = _parse_envelope(raw)
        if envelope is not None:
            arguments = envelope.get("arguments")
            call = ToolCall(
                id=f"tp_{next(counter)}",
                name=envelope["tool"].strip(),
                arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
            )
            return Reply(
                content=raw, tool_calls=(call,), usage=Usage(0, 0, 0), model="llm-port"
            )
        return Reply(content=raw, tool_calls=(), usage=Usage(0, 0, 0), model="llm-port")

    return chat
