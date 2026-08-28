"""沙盒 Agent 的工作区工具集（设计 §7.1，H2）。

沙盒 Agent 的全部动作面 = 五个工具：``ws_list / ws_read / ws_write /
python_run(code_run) / env_probe``——写码、跑码、读产物、探环境，除此之外
没有任何触达。四个新工具都经 ``TaskWorkspace`` 的路径安全原语（越界拒绝、
原子写、配额），tier 按最小授权标注（读=readonly、写=workspace_write），
由 RecordingInvoker 统一审计与执行（TOOL_CALLED 事件、超时、崩溃隔离）。

``env_probe`` 产出可复现性指纹（§7.3）：运行时/版本/依赖清单哈希——
SandboxRunReport.env_fingerprint 的数据源；同指纹+同种子+同数据的冷启动
重跑，指标应在浮点容差内一致。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
from typing import Any

from omm_agent_core import ToolResult

from .registry import ToolCallContext, ToolSpec
from .workspace import TaskWorkspace, WorkspaceViolation

__all__ = [
    "ENV_PROBE_PACKAGE_CANDIDATES",
    "WS_READ_MAX_CHARS",
    "env_fingerprint",
    "sandbox_workspace_specs",
]

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
)


def env_fingerprint() -> dict[str, Any]:
    """运行环境指纹：SandboxRunReport.env_fingerprint 的生成器。

    deps_hash 只对「候选包是否可用」的清单取哈希（不含具体小版本——Tier0
    共享解释器下版本随环境走，清单变化才是复现风险的主信号；逐包版本随
    Tier-L 的 env_probe 多语言化再精化）。
    """
    available = sorted(
        name
        for name in ENV_PROBE_PACKAGE_CANDIDATES
        if importlib.util.find_spec(name) is not None
    )
    canonical = json.dumps(available, ensure_ascii=False)
    return {
        "runtime": "python",
        "version": platform.python_version(),
        "deps_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "available_packages": available,
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


def _env_probe():
    def handler(_arguments: dict[str, Any], _ctx: ToolCallContext) -> ToolResult:
        return ToolResult(status="succeeded", output=env_fingerprint())

    return handler


def sandbox_workspace_specs(workspace: TaskWorkspace) -> list[ToolSpec]:
    """沙盒 Agent 工具集中除 code_run 外的四件（code_run=既有 PythonSandbox，
    由装配方一并注册；§7.4 统一命名为 code_run 随多语言 Runner 落地，现名
    python_run 保持事件与预算账本的连续性）。"""
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
            description="探测执行环境：运行时/版本/可用第三方包与依赖指纹（可复现性记录用）；只读。",
            handler=_env_probe(),
            risk="low",
            timeout_s=10.0,
            tier="readonly",
        ),
    ]
