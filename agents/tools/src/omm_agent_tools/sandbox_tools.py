"""沙盒 Agent 的工作区工具集（设计 §7.1，H2）。

沙盒 Agent 的全部动作面 = 五个工具：``ws_list / ws_read / ws_write /
python_run(code_run) / env_probe``——写码、跑码、读产物、探环境，除此之外
没有任何触达。四个新工具都经 ``TaskWorkspace`` 的路径安全原语（越界拒绝、
原子写、配额），tier 按最小授权标注（读=readonly、写=workspace_write），
由 RecordingInvoker 统一审计与执行（TOOL_CALLED 事件、超时、崩溃隔离）。

``env_probe`` 产出可复现性指纹（§7.3）：运行时/版本/依赖清单哈希——
SandboxRunReport.env_fingerprint 的数据源；同指纹+同种子+同数据的冷启动
重跑，指标应在浮点容差内一致。H7 切片 1 起它同时是 ExecutorProfile（§13.3）
的数据源：``languages`` 块逐语言报可用 / 版本 / 可执行文件 / 可用包 / 该语言
自己的 deps_hash，``available_languages`` 是能跑的语言顺位表——节点侧按任务卡
language 取对应块拼 SandboxRunReport.env_fingerprint（接线刀）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
from collections.abc import Callable, Mapping
from typing import Any

from omm_agent_core import ToolResult

from .registry import ToolCallContext, ToolHandler, ToolSpec
from .runners import LANGUAGE_SPECS, LanguageProbe, probe_language
from .workspace import TaskWorkspace, WorkspaceViolation

__all__ = [
    "ENV_PROBE_PACKAGE_CANDIDATES",
    "WS_READ_MAX_CHARS",
    "env_fingerprint",
    "language_fingerprint",
    "sandbox_workspace_specs",
]

#: env_probe 输出里逐语言块的形状（探测事实 + 该语言的三键指纹），供接线刀直接取用。
ProbeSource = Callable[[], Mapping[str, LanguageProbe]]

#: ws_read 单次返回的正文上限：观察进内环 prompt，必须有界（§5.2 观察截断
#: 在 loops 层还有一道；这里是工具层的第一道闸）。
WS_READ_MAX_CHARS = 20_000

#: env_probe 探测的第三方包候选：与实验提示词的 import 白名单同一来源口径
#: （engine_glue 侧按 sys.executable 探测注入提示词；此处探测的是工具进程
#: 自身，Tier0 沙箱与调用方共享解释器，两者事实一致）。
ENV_PROBE_PACKAGE_CANDIDATES: tuple[str, ...] = (
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "statsmodels",
    "matplotlib",
    "networkx",
    "sympy",
    "torch",
)


def _deps_hash(packages: list[str]) -> str:
    canonical = json.dumps(sorted(packages), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def language_fingerprint(probe: LanguageProbe) -> dict[str, Any]:
    """一种语言的 sandbox-run-report.v1 三键指纹 + 探测事实（接线刀按 language 取用）。

    不可用的语言 version 为空串、deps_hash 对空清单取哈希——形状齐、值如实，
    消费方看 ``available`` 决定要不要用。
    """
    return {
        "runtime": probe.language,
        "version": probe.version or "",
        "deps_hash": _deps_hash(list(probe.packages)),
        "available": probe.available,
        "executable": probe.executable,
        "available_packages": sorted(probe.packages),
        "detail": probe.detail,
    }


def _default_probes() -> Mapping[str, LanguageProbe]:
    """未注入 code_run 时按各语言缺省候选探测（缓存在 runners 里，进程内只跑一次）。"""
    return {language: probe_language(spec) for language, spec in LANGUAGE_SPECS.items()}


def env_fingerprint(probes: ProbeSource | None = None) -> dict[str, Any]:
    """运行环境指纹：SandboxRunReport.env_fingerprint 的生成器。

    顶层 ``runtime / version / deps_hash / available_packages`` 是 Python 的、在
    工具进程内探测（Tier0 沙箱与调用方共享解释器，两者事实一致），形状与值
    自 H2 起不变。deps_hash 只对「候选包是否可用」的清单取哈希（不含具体小版本
    ——清单变化才是复现风险的主信号）。

    ``languages`` 块按语言给 :func:`language_fingerprint`（含在 ``-I`` / ``--vanilla``
    与清洗环境下子进程探到的版本与包，与 code_run 同条件——那才是「生成的代码
    能不能 import」的诚实答案）；``available_languages`` 按 §7.4 顺位列能跑的。
    """
    available = sorted(
        name
        for name in ENV_PROBE_PACKAGE_CANDIDATES
        if importlib.util.find_spec(name) is not None
    )
    language_blocks = {
        language: language_fingerprint(probe)
        for language, probe in (probes or _default_probes)().items()
    }
    return {
        "runtime": "python",
        "version": platform.python_version(),
        "deps_hash": _deps_hash(available),
        "available_packages": available,
        "languages": language_blocks,
        "available_languages": [
            language for language, block in language_blocks.items() if block["available"]
        ],
    }


def _ws_list(workspace: TaskWorkspace):
    def handler(arguments: dict[str, Any], _ctx: ToolCallContext) -> ToolResult:
        prefix = str(arguments.get("prefix") or "").strip()
        files = workspace.list_files()
        if prefix:
            files = [name for name in files if name.startswith(prefix)]
        return ToolResult(status="succeeded", output={"files": files})

    return handler


def _ws_read(workspace: TaskWorkspace):
    def handler(arguments: dict[str, Any], _ctx: ToolCallContext) -> ToolResult:
        path = str(arguments.get("path") or "")
        try:
            text = workspace.read_text(path)
        except WorkspaceViolation as exc:
            return ToolResult(status="failed", error=str(exc))
        except FileNotFoundError:
            return ToolResult(status="failed", error=f"文件不存在：{path}")
        except UnicodeDecodeError:
            return ToolResult(
                status="failed",
                error=f"文件不是 UTF-8 文本（二进制产物请经 artifact 通道读取）：{path}",
            )
        truncated = len(text) > WS_READ_MAX_CHARS
        return ToolResult(
            status="succeeded",
            output={
                "path": path,
                "text": text[:WS_READ_MAX_CHARS],
                "truncated": truncated,
                "total_chars": len(text),
            },
        )

    return handler


def _ws_write(workspace: TaskWorkspace):
    def handler(arguments: dict[str, Any], _ctx: ToolCallContext) -> ToolResult:
        path = str(arguments.get("path") or "")
        text = str(arguments.get("text") or "")
        try:
            workspace.write_text(path, text)
        except WorkspaceViolation as exc:
            return ToolResult(status="failed", error=str(exc))
        return ToolResult(
            status="succeeded",
            output={"path": path, "bytes": len(text.encode("utf-8"))},
        )

    return handler


def _env_probe(probes: ProbeSource | None) -> ToolHandler:
    def handler(_arguments: dict[str, Any], _ctx: ToolCallContext) -> ToolResult:
        return ToolResult(status="succeeded", output=env_fingerprint(probes))

    return handler


def sandbox_workspace_specs(
    workspace: TaskWorkspace, probes: ProbeSource | None = None
) -> list[ToolSpec]:
    """沙盒 Agent 工具集中除执行工具外的四件（执行工具 = ``python_run`` 与 / 或
    ``code_run``，由装配方一并注册；``python_run`` 保留为过渡别名，事件与预算
    账本连续）。

    ``probes`` 让 env_probe 报装配方真正启用的语言（传 ``code_run.probes``：含
    注入的可执行文件路径）；不传就按各语言缺省候选探测。探测惰性发生在第一次
    env_probe 调用、进程内缓存，注册本身不起子进程。
    """
    return [
        ToolSpec(
            name="ws_list",
            description="列出工作区内的文件（可选 prefix 过滤）；只读。",
            handler=_ws_list(workspace),
            risk="low",
            timeout_s=10.0,
            tier="readonly",
        ),
        ToolSpec(
            name="ws_read",
            description=f"读取工作区内的 UTF-8 文本文件（单次至多 {WS_READ_MAX_CHARS} 字符，超长截断并标注）；只读。",
            handler=_ws_read(workspace),
            risk="low",
            timeout_s=15.0,
            required_args=("path",),
            tier="readonly",
        ),
        ToolSpec(
            name="ws_write",
            description="向工作区写入 UTF-8 文本文件（原子写、路径越界拒绝、配额受限）。",
            handler=_ws_write(workspace),
            risk="medium",
            timeout_s=15.0,
            required_args=("path", "text"),
            tier="workspace_write",
        ),
        ToolSpec(
            name="env_probe",
            description=(
                "探测执行环境：运行时/版本/可用第三方包与依赖指纹（可复现性记录用），"
                "以及各实现语言（python / r …）是否可用、版本与可用包；只读。"
            ),
            handler=_env_probe(probes),
            risk="low",
            # 第一次调用会为每种语言各起一个探测子进程（R 冷启动约 1 s），之后进程内缓存。
            timeout_s=90.0,
            tier="readonly",
        ),
    ]
