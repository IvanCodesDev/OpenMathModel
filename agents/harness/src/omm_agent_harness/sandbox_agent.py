"""沙盒 Agent 执行体（设计 §7.1，H2）：任务卡 → 写码/跑码 → 断言验收 → 报告。

角色定位（§8.2）：编码执行子代理。领一张实现任务卡（目标、任务说明、显式
种子、**验收断言列表**），在隔离工作区里驱动「写码 → 运行 → 读产物 → 修复」
直到断言全部通过或 R2 运行预算耗尽（§5.4：6 次运行/任务），产出与
sandbox-run-report.v1 同构的报告 dict。

三条硬纪律：

1. **验收以断言为准，不接受模型自述成功**——模型的终答只是"我认为做完了"
   的信号，触发一轮确定性断言评估；未过的断言差异作为反馈进入下一波修复。
2. **修复梯子不跨级（§5.4）**：单波内环里的 R1（终答结构修复）仍归
   run_inner_loop；本执行体管理的是 R2（执行修复）——按"波次"推进，每波
   是一次独立装配的内环（结构化反馈接续，不转录全对话，上下文纪律 §10.1）。
   内环结构违约 / 无进展 / 工具连败 / 取消就收束；回合用完只是这一波的配额尽了，断言没过、
   R2 还有余额就照常开下一波，反馈开头写明上一波为什么结束（一次没运行时要它先去运行）。
   每波的回合数写进波次提示词，本波还没运行过、只剩两个回合时观察里再提醒一次。
3. **运行预算按次预付**：沙箱运行（python_run / code_run）超过 max_runs 的那一次
   不会执行（§4.7 "a started run is spent money" 的镜像），预算尽即收束报告。
   每次运行的观察都附「已用 k / N」，失败的运行另附 stderr / stdout 尾部（内环对失败
   结果只渲染 error，回溯不并进来模型就得再花一次运行把它打出来）。
   **收尾运行**：波末最后一次运行没过验收、而更早某次运行的结果能全过时，执行体把那次
   的代码原样再跑一遍——不占 R2 预算、不计 usage.runs（与节点复跑核对同口径），经同一个
   执行器，所以工作区、产物采集、发布代码与指标都回到那一版，下游「最后一次运行」口径不变。
   调用方给了 ``discard_files`` 时，晚于来源运行、失败版新建且复跑没有重新写出的文件在验收
   时当作不存在，复跑通过后交给它删掉——工作区与节点采集里都不再留失败版的文件。
   收尾通过时终答还是波末那份（写在没过的运行之后），执行体再开一个不带工具的内环，把采用的
   代码、复跑输出与验收结果交给模型重写终答；没写成就沿用原终答。
4. **实现语言由任务卡定，模型无权换（§7.4，H7）**：``SandboxTask.language`` 是 G1
   确认下来的语言；执行体把 ``code_run`` 的 ``language`` 固定成它——模型漏传就补上，
   传了别的语言就退回一条观察（不执行、不计预算）；语言不是 python 时 ``python_run``
   同样被退回。语言路由的对面（执行器没有该语言 → 显式失败）在 tools 层。

依赖形状与 loops 一致：chat / 工具执行器 / 代码产物发布回调全部注入；
harness 不 import skills（prompt 文本由调用方给）、不 import tools（语言别名归一
由调用方注入 ``normalize_language``，缺省只做小写去空白）、不 import contracts
（报告是形状对齐的 dict，校验与序列化归调用方）。
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from omm_agent_core.models import ArtifactRef, ToolResult

from .budget import LoopBudget
from .context import ContextAssembler, Section
from .gateway import Message, ToolCall, Usage
from .loops import ChatFn, LoopOutcome, LoopTask, ToolExecutor, run_inner_loop

__all__ = [
    "CODE_RUN_TOOL_NAME",
    "DEFAULT_LANGUAGE",
    "PYTHON_TOOL_NAME",
    "RUN_TOOL_NAMES",
    "SandboxAssertion",
    "SandboxEvidence",
    "SandboxTask",
    "run_sandbox_task",
]

#: 沙箱执行工具名（装配期契约，与 omm_agent_tools.PythonSandbox.TOOL_NAME 一致）。
#: H7 切片 1 起它是 ``code_run`` 的过渡别名，只跑 Python；事件与预算账本按名字连续。
PYTHON_TOOL_NAME = "python_run"

#: 多语言统一执行入口（与 omm_agent_tools.CodeRunSandbox.TOOL_NAME 一致）：
#: arguments = {code, language}，按任务卡语言分发到对应 Runner。
CODE_RUN_TOOL_NAME = "code_run"

#: 两个名字都算「一次沙箱运行」：R2 预算、证据采集、最终代码回收一视同仁。
RUN_TOOL_NAMES = frozenset({PYTHON_TOOL_NAME, CODE_RUN_TOOL_NAME})

#: 工作区写文件工具名（与 omm_agent_tools 的 ws_write 一致）：收尾清理要知道模型在哪次运行
#: 之后新建过哪些文件。
_WS_WRITE_TOOL = "ws_write"

#: 任务卡缺省实现语言（python_run 时代的唯一语言；方案卡 language 缺省也是它）。
DEFAULT_LANGUAGE = "python"

#: 指标标记行（与实验节点同一约定）：脚本打印
#: ``OMM_METRICS_JSON: {...}``，取最后一条。
_METRICS_LINE = re.compile(r"^OMM_METRICS_JSON:\s*(\{.*\})\s*$", re.MULTILINE)

#: 断言反馈里上一波代码的截断长度。
_FEEDBACK_CODE_CHARS = 3000

#: 失败运行回给模型的 stderr / stdout 尾部长度（内环观察整段上限 4000 字符，两段加
#: 报错与预算行要装得下）。
_OBSERVED_STDERR_CHARS = 1500
_OBSERVED_STDOUT_CHARS = 800

#: 收尾后重写叙事时交给模型的采用版代码（保头）与复跑 stdout（保尾）长度，以及列出的工作区
#: 文件数上限（图件说明只能写现有的图）。
_NARRATIVE_CODE_CHARS = 6000
_NARRATIVE_STDOUT_CHARS = 2000
_NARRATIVE_LISTED_FILES = 80

#: 重写叙事的内环轮数：一轮交终答，多一轮兜住误调工具被退回（结构修复另有一次 R1）。
_NARRATIVE_MAX_TURNS = 2

#: 内环回合用完的退出原因（LoopOutcome.exit_reason）：这种出口断言没过、R2 还有余额就再开一波。
_TURNS_EXHAUSTED = "max_turns"

#: 本波还没运行过代码、只剩这么多回合时，在观察里提醒模型直接去运行。
_TURN_HINT_REMAINING = 2

_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```$", re.MULTILINE)


@dataclass(frozen=True)
class SandboxEvidence:
    """断言可见的执行证据：确定性校验的唯一输入面。"""

    files: tuple[str, ...]  # 工作区文件清单（相对路径）
    read_text: Callable[[str], str]  # 读工作区文本文件（不存在则抛异常）
    last_run: ToolResult | None  # 最后一次沙箱运行（python_run / code_run）的结果
    stdout: str  # 最后一次运行的标准输出
    metrics: Mapping[str, Any]  # 标记行解析出的指标（无则空 dict）


@dataclass(frozen=True)
class SandboxAssertion:
    """一条验收断言：description 给模型看，check 是父节点给定的确定性校验。"""

    id: str
    description: str
    check: Callable[[SandboxEvidence], tuple[bool, str]]  # (passed, detail)


@dataclass(frozen=True)
class SandboxTask:
    """实现任务卡（§7.1）：沙盒 Agent 的全部输入。"""

    task_id: str
    goal: str
    system_prompt: str  # 角色卡与纪律（调用方通常取自 prompts 模板）
    task_brief: str  # 数据接口/输出约定等任务说明
    assertions: tuple[SandboxAssertion, ...]
    seeds: Mapping[str, Any] = field(default_factory=dict)
    max_runs: int = 6  # R2 预算（§5.4 单一出处的拍板值）
    max_turns_per_wave: int = 8  # 单波内环轮数（§4.7 沙盒档位）
    max_waves: int = 3  # 断言修复波次上限（回合用完后开的下一波也算一波）
    #: 终答除 summary 外要求的叙事键：(键名, 给模型看的一句话说明)。父节点
    #: 需要沙盒 Agent 的叙事产出（如实验节点的 approach_summary/progress_note）
    #: 时在此声明；校验与提示词由执行体统一生成，经 on_final_answer 回传。
    extra_final_keys: tuple[tuple[str, str], ...] = ()
    #: 终答里**可选**的叙事键：同样写进终答示例让模型照着填，但缺席 / 留空不算
    #: 结构问题（不触发 R1 修复）。用于只在特定情形才有内容的键——如实验节点的
    #: figure_notes（没画图就没得说），做成必填会逼模型编一句凑数。
    optional_final_keys: tuple[tuple[str, str], ...] = ()
    #: 实现语言（§7.4：随 G1 确认、决定执行器路由），契约小写标识（python / r / …）。
    #: 执行体据此固定 code_run 的 language、退回模型自行换语言的调用。
    language: str = DEFAULT_LANGUAGE
    #: 模型应调用的执行工具：``python_run``（过渡别名，缺省，只跑 Python）或
    #: ``code_run``（多语言统一入口）。非 Python 语言必须走 code_run。
    run_tool: str = PYTHON_TOOL_NAME

    def __post_init__(self) -> None:
        if self.run_tool not in RUN_TOOL_NAMES:
            raise ValueError(
                f"run_tool must be one of {sorted(RUN_TOOL_NAMES)}, got {self.run_tool!r}"
            )
        if not self.language.strip():
            raise ValueError("language must be a non-empty contract id (e.g. 'python')")
        if self.language != DEFAULT_LANGUAGE and self.run_tool == PYTHON_TOOL_NAME:
            raise ValueError(
                f"language {self.language!r} cannot run through {PYTHON_TOOL_NAME}; "
                f"use run_tool={CODE_RUN_TOOL_NAME!r}"
            )


def _lenient_parse(raw: str) -> Any:
    """终答解析：容忍 markdown 围栏与前后杂文（与技能层 extract_json 同纪律）。"""
    candidate = raw.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    candidate = _FENCE.sub("", candidate).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        return json.loads(candidate[start : end + 1])
    raise json.JSONDecodeError("no JSON object found", raw, 0)


def _final_answer_validator(task: SandboxTask) -> Callable[[Any], list[str]]:
    def validate(value: Any) -> list[str]:
        if not isinstance(value, dict):
            return ["终答必须是 JSON 对象"]
        problems: list[str] = []
        if not str(value.get("summary") or "").strip():
            problems.append("missing required key: summary（一句话说明做了什么与结果）")
        for key, hint in task.extra_final_keys:
            if not str(value.get(key) or "").strip():
                problems.append(f"missing required key: {key}（{hint}）")
        return problems

    return validate


def _extract_metrics(stdout: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for match in _METRICS_LINE.finditer(stdout):
        try:
            candidate = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            metrics = candidate  # 取脚本打印的最后一条标记行
    return metrics


def _default_normalize_language(value: Any) -> str:
    return str(value or "").strip().lower()


def _budget_note(used: int, total: int) -> str:
    left = max(total - used, 0)
    if left == 0:
        return f"已用 {used} / {total} 次运行，预算已用尽：之后的运行不会执行，请直接输出终答"
    return f"已用 {used} / {total} 次运行，剩 {left} 次（读文件用 ws_read，不占运行次数）"


def _failure_observation(result: ToolResult) -> str:
    """失败运行的观察正文：报错 + stderr 尾部（回溯在最后）+ stdout 尾部（跑到了哪一步）。"""
    output = result.output or {}
    parts = [str(result.error or result.status)]
    stderr = str(output.get("stderr") or "").strip()
    if stderr:
        parts.append("stderr（尾部）：\n" + stderr[-_OBSERVED_STDERR_CHARS:])
    stdout = str(output.get("stdout") or "").strip()
    if stdout:
        parts.append("stdout（尾部）：\n" + stdout[-_OBSERVED_STDOUT_CHARS:])
    return "\n".join(parts)


@dataclass(frozen=True)
class _RunRecord:
    """一次真执行的模型运行：钉好语言的调用 + 原始结果（收尾运行据此重评、重跑）。"""

    call: ToolCall
    result: ToolResult


@dataclass(frozen=True)
class _Writes:
    """一次真执行（模型运行 / 收尾复跑）或一次 ws_write 在工作区里留下的痕迹：新建、写过（含新建）、
    删掉的工作区相对路径，以及运行报上来的产物。"""

    created: frozenset[str] = frozenset()
    written: frozenset[str] = frozenset()
    deleted: frozenset[str] = frozenset()
    artifacts: tuple[ArtifactRef, ...] = ()


def _workspace_path(value: Any) -> str:
    """工作区相对路径归一：反斜杠换成 /、去掉 ./ 与重复分隔（ws_write 的 path 是模型原样给的）。"""
    text = str(value or "").strip().replace("\\", "/")
    normalized = posixpath.normpath(text) if text else ""
    return "" if normalized in {"", "."} else normalized


def _path_set(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple)):
        return frozenset()
    return frozenset(path for path in map(_workspace_path, value) if path)


def _run_writes(result: ToolResult) -> _Writes:
    """执行器报上来的本次新建 / 改写 / 删掉的路径（tools 的 created_files 等，缺席当作没有）。"""
    output = result.output or {}
    created = _path_set(output.get("created_files"))
    return _Writes(
        created=created,
        written=created | _path_set(output.get("modified_files")),
        deleted=_path_set(output.get("deleted_files")),
        artifacts=tuple(result.artifacts),
    )


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _hiding(read_text: Callable[[str], str], gone: frozenset[str]) -> Callable[[str], str]:
    """读文件时把 ``gone`` 里的路径当作不存在（收尾验收不许靠失败版留下的文件）。"""

    def read(path: str) -> str:
        if _workspace_path(path) in gone:
            raise FileNotFoundError(path)
        return read_text(path)

    return read


class _RunTracker:
    """沙箱运行（python_run / code_run）的预算、语言路由与证据跟踪：包装注入的工具执行器。

    - 预算按次预付：超过 max_runs 的调用不执行，返回失败观察并置 exhausted；
    - 语言固定：code_run 缺 language 就补任务卡的语言；传了别的语言、或语言不是
      python 却调 python_run → 退回观察（不执行、不计预算）；
    - 证据采集：记录最后一次运行的代码/结果、每次模型运行的记录与全部产物 id；
    - 观察加注：回给模型的那份附运行预算，失败的另附 stderr / stdout 尾部（原始结果照存）；
    - 收尾运行（:meth:`finalize`）：原样重跑某次模型运行，不计 ``runs``；
    - 收尾清理：按执行顺序记下每次真执行与 ws_write 新建 / 写过 / 删掉的路径，
      :meth:`leftovers` 据此找出晚于来源运行、失败版留下的文件；
    - 按波计数（:meth:`begin_wave` 清零）：内环每个工具回合调一次执行器，调用次数就是本波已用的
      回合数；本波还没运行过、只剩不多的回合时，这一回合的观察里加一句回合提醒。
    """

    def __init__(
        self,
        inner: ToolExecutor,
        max_runs: int,
        *,
        language: str = DEFAULT_LANGUAGE,
        normalize_language: Callable[[Any], str] | None = None,
        run_tool: str = PYTHON_TOOL_NAME,
    ) -> None:
        self._inner = inner
        self._max = max_runs
        self._normalize = normalize_language or _default_normalize_language
        self.language = self._normalize(language) or DEFAULT_LANGUAGE
        self.run_tool = run_tool
        self.runs = 0
        self._wave_max_turns = 0
        self.wave_turns = 0
        self.wave_runs = 0
        #: 本波各工具的调用次数（按首次调用的先后）
        self.wave_tools: dict[str, int] = {}
        self.exhausted = False
        self.rejected_language_calls = 0
        self.last_code: str = ""
        self.last_result: ToolResult | None = None
        self.artifact_ids: list[str] = []
        self.artifact_names: dict[str, str] = {}
        self.history: list[_RunRecord] = []
        #: 已被拿来收尾过的模型运行（history 下标）：每次运行最多收尾一次
        self.finalized_from: set[int] = set()
        #: 执行时间线（模型运行、收尾复跑、ws_write 按发生顺序）与模型运行在其中的位置
        self._timeline: list[_Writes] = []
        self._positions: list[int] = []

    def _route(self, call: ToolCall) -> ToolCall | ToolResult:
        """把一次运行调用钉在任务卡语言上；违约返回失败观察而不是改写模型的意图。"""
        if call.name == PYTHON_TOOL_NAME:
            if self.language == DEFAULT_LANGUAGE:
                return call
            return ToolResult(
                status="failed",
                error=(
                    f"本任务的实现语言是 {self.language}（方案阶段已确认），{PYTHON_TOOL_NAME} "
                    f"只运行 Python；请用 {CODE_RUN_TOOL_NAME} 工具并传 "
                    f'language="{self.language}"。本次未执行。'
                ),
            )
        wanted = self._normalize(call.arguments.get("language"))
        if not wanted:
            return replace(call, arguments={**call.arguments, "language": self.language})
        if wanted != self.language:
            return ToolResult(
                status="failed",
                error=(
                    f"本任务的实现语言固定为 {self.language}（方案阶段已确认），"
                    f"不接受 language={call.arguments.get('language')!r}；"
                    f"请用 {self.language} 实现并以 {CODE_RUN_TOOL_NAME}"
                    f'(language="{self.language}") 运行。本次未执行。'
                ),
            )
        if wanted != call.arguments.get("language"):
            return replace(call, arguments={**call.arguments, "language": self.language})
        return call

    def begin_wave(self, max_turns: int) -> None:
        """新一波开始：本波的回合、运行与各工具调用次数清零。"""
        self._wave_max_turns = max_turns
        self.wave_turns = 0
        self.wave_runs = 0
        self.wave_tools = {}

    def __call__(self, calls: Sequence[ToolCall]) -> Sequence[ToolResult]:
        self.wave_turns += 1
        results: list[ToolResult] = []
        for call in calls:
            self.wave_tools[call.name] = self.wave_tools.get(call.name, 0) + 1
            if call.name not in RUN_TOOL_NAMES:
                passed_through = list(self._inner([call]))
                if call.name == _WS_WRITE_TOOL:
                    for result in passed_through:
                        self._note_ws_write(call, result)
                results.extend(passed_through)
                continue
            routed = self._route(call)
            if isinstance(routed, ToolResult):
                self.rejected_language_calls += 1
                results.append(routed)
                continue
            if self.runs + 1 > self._max:
                self.exhausted = True
                results.append(
                    ToolResult(
                        status="failed",
                        error=f"[E330] R2 运行预算已尽（max_runs={self._max}），本次运行未执行",
                    )
                )
                continue
            self.runs += 1
            self.wave_runs += 1
            outcome = self._execute(routed)
            self._positions.append(len(self._timeline) - 1)
            self.history.append(_RunRecord(call=routed, result=outcome))
            results.append(self._observed(outcome))
        return self._turn_hinted(results)

    def _turn_hinted(self, results: list[ToolResult]) -> list[ToolResult]:
        """本波还没运行过、只剩不多的回合：这一回合最后一条观察前面加回合提醒（原始结果照存）。"""
        left = self._wave_max_turns - self.wave_turns
        if self.wave_runs or not results or not 0 < left <= _TURN_HINT_REMAINING:
            return results
        hint = (
            f"[回合提醒] 本轮 {self._wave_max_turns} 个回合已用 {self.wave_turns} 个，"
            "还没有运行过代码；回合用完还没交终答这一轮就到此结束——下一回合请直接用 "
            f"{self.run_tool} 运行完整脚本（源码作为 code 传入，不必先用 ws_write 落盘）"
        )
        last = results[-1]
        results[-1] = (
            replace(last, output={"turn_budget": hint, **(last.output or {})})
            if last.ok
            else replace(last, error=f"{last.error or last.status}\n{hint}")
        )
        return results

    def finalize(self, index: int) -> ToolResult:
        """收尾运行：``history[index]`` 那次模型运行的调用原样再跑一遍（不计 R2 预算）。"""
        self.finalized_from.add(index)
        return self._execute(self.history[index].call)

    def leftovers(self, source: int, *, rerun_done: bool = True) -> dict[str, list[str]]:
        """晚于 ``history[source]`` 的执行新建、收尾复跑没有重新写出的路径 → 它们报上来的产物 id。

        ``rerun_done`` 为 False 是挑来源时的预估：复跑还没跑，按那次运行自己写过的路径算。
        来源运行（含）之前写过的路径不算——那时它就在工作区里；来源运行之后被删过的路径也不算——
        可能是原本就在、被失败运行删掉又重建的文件，宁可留着。
        """
        position = self._positions[source]
        if rerun_done:
            later, rerun = self._timeline[position + 1 : -1], self._timeline[-1].written
        else:
            later, rerun = self._timeline[position + 1 :], self._timeline[position].written
        keep = set(rerun)
        for entry in self._timeline[: position + 1]:
            keep |= entry.written
        for entry in later:
            keep |= entry.deleted
        paths = sorted({path for entry in later for path in entry.created} - keep)
        return {
            path: [
                ref.artifact_id
                for entry in later
                if path in entry.written
                for ref in entry.artifacts
                if _basename(ref.uri) == _basename(path)
            ]
            for path in paths
        }

    def forget(self, artifact_ids: Iterable[str]) -> None:
        """删掉的文件报过的产物不再参与「最后一次运行的那份」（指标来源）；报告里的全量产物照列。"""
        for artifact_id in artifact_ids:
            self.artifact_names.pop(artifact_id, None)

    def _note_ws_write(self, call: ToolCall, result: ToolResult) -> None:
        output = result.output or {}
        path = _workspace_path(output.get("path") or call.arguments.get("path"))
        if not result.ok or not path:
            return
        created = frozenset({path}) if output.get("created") else frozenset()
        self._timeline.append(_Writes(created=created, written=frozenset({path})))

    def _execute(self, call: ToolCall) -> ToolResult:
        self.last_code = str(call.arguments.get("code") or "")
        outcome = list(self._inner([call]))[0]
        self.last_result = outcome
        self._timeline.append(_run_writes(outcome))
        for ref in outcome.artifacts:
            self.artifact_ids.append(ref.artifact_id)
            # 先删后插：同一 id 再报一次也排到最后（「最后一次运行的那份」按插入序取）
            self.artifact_names.pop(ref.artifact_id, None)
            self.artifact_names[ref.artifact_id] = ref.uri.rstrip("/").rsplit("/", 1)[-1]
        return outcome

    def _observed(self, outcome: ToolResult) -> ToolResult:
        note = _budget_note(self.runs, self._max)
        if outcome.ok:
            return replace(outcome, output={"run_budget": note, **(outcome.output or {})})
        return replace(outcome, error=f"{_failure_observation(outcome)}\n[运行预算] {note}")


def _assemble_wave_prompt(
    task: SandboxTask, feedback: str | None, runs_left: int | None = None
) -> tuple[Message, ...]:
    """一波内环的任务卡 prompt：分节装配（§4.2），反馈段只在修复波出现。

    ``runs_left`` 是本波开始时还剩的 R2 运行次数（缺省 = 整张任务卡的 max_runs）。
    """
    sections = [
        Section(name="system", content=task.system_prompt),
        Section(name="task_frame", heading="任务目标", content=task.goal),
        Section(name="task_frame_brief", heading="任务说明", content=task.task_brief),
        Section(
            name="seeds",
            heading="随机种子（必须显式使用）",
            content=json.dumps(dict(task.seeds), ensure_ascii=False) if task.seeds else "",
        ),
        Section(
            name="acceptance",
            heading="验收标准（以确定性校验为准，自述完成无效）",
            content="\n".join(
                f"- [{item.id}] {item.description}" for item in task.assertions
            ),
        ),
        Section(
            name="repair_feedback",
            heading="上一轮未通过验收（修复后重新运行）",
            content=feedback or "",
        ),
        Section(
            name="output_spec",
            heading="工作方式与终答要求",
            content=(
                _run_tool_instruction(task)
                + _run_budget_instruction(task, task.max_runs if runs_left is None else runs_left)
                + _turn_budget_instruction(task)
                + "运行成功并自查达标后，只输出一个 JSON 对象作为终答："
                + _final_answer_example(task)
                + "。终答会触发验收断言评估，未通过会把差异反馈给你继续修复。"
            ),
        ),
    ]
    return ContextAssembler.build(sections).messages


def _run_tool_instruction(task: SandboxTask) -> str:
    """告诉模型用哪个工具跑码。python_run 任务卡的措辞与 H2 以来逐字相同；
    code_run 任务卡点明语言固定、不许换（§7.4）。"""
    if task.run_tool == PYTHON_TOOL_NAME:
        return "用 python_run 工具执行代码（需要留档的辅助文件用 ws_write）；"
    return (
        f"用 {task.run_tool} 工具执行代码，参数 language 固定为 \"{task.language}\""
        f"（实现语言已在方案阶段确认，不要换用其它语言；需要留档的辅助文件用 ws_write）；"
    )


def _run_budget_instruction(task: SandboxTask, runs_left: int) -> str:
    return (
        f"本任务还剩 {runs_left} 次运行（每调一次 {task.run_tool} 计 1 次，用尽后不再执行），"
        "读文件用 ws_read、不占运行次数，运行留给完整脚本；"
    )


def _turn_budget_instruction(task: SandboxTask) -> str:
    return (
        f"每一轮最多 {task.max_turns_per_wave} 个回合（调一次工具或交一次终答各算 1 个），"
        "回合用完还没交终答这一轮就到此结束，脚本写好就直接运行；"
    )


def _turns_exhausted_note(task: SandboxTask, runs: int, tools: Mapping[str, int]) -> str:
    """回合用完、断言没过的那一波之后，下一波反馈的开头：上一波为什么结束、这一波先做什么。"""
    head = f"上一轮 {task.max_turns_per_wave} 个回合用完也没有交出终答"
    if runs:
        return (
            f"{head}（期间运行了 {runs} 次）。本轮跑通、自查达标后就交终答，"
            "不要把回合耗在重复的读写上。"
        )
    called = "、".join(f"{name} ×{count}" for name, count in tools.items())
    return (
        f"{head}，期间一次都没有运行代码{f'（调用了 {called}）' if called else ''}。"
        f"本轮请先把完整脚本直接交给 {task.run_tool} 运行——源码作为 code 传入，不必先用 ws_write "
        "落盘（ws_write 整文件覆盖、不是追加），也不要把回合花在分段重读整份旧文件上；"
        "跑通、自查达标后再交终答。"
    )


def _final_answer_example(task: SandboxTask) -> str:
    pairs = ['"summary": "一句话说明做了什么与关键结果"']
    pairs += [f'"{key}": "{hint}"' for key, hint in task.extra_final_keys]
    pairs += [f'"{key}": "（可选）{hint}"' for key, hint in task.optional_final_keys]
    return "{" + ", ".join(pairs) + "}"


def _assemble_narrative_prompt(
    task: SandboxTask,
    *,
    source_run: int,
    code: str,
    result: ToolResult | None,
    assertion_results: Sequence[Mapping[str, Any]],
    files: Sequence[str],
    previous: Mapping[str, Any] | None,
) -> tuple[Message, ...]:
    """收尾通过后重写叙事的 prompt：只给采用的那一版（代码、复跑输出、验收结果、现有文件）与旧终答。

    不带任务说明（工具协议、审稿意见）与运行预算：这一轮不许再跑任何东西。
    """
    stdout = str((result.output or {}).get("stdout") or "").strip() if result is not None else ""
    listed = [f"- {path}" for path in files[:_NARRATIVE_LISTED_FILES]]
    if len(files) > _NARRATIVE_LISTED_FILES:
        listed.append(f"- ……另有 {len(files) - _NARRATIVE_LISTED_FILES} 个文件")
    sections = [
        Section(name="system", content=task.system_prompt),
        Section(name="task_frame", heading="任务目标", content=task.goal),
        Section(
            name="finalize_frame",
            heading="收尾说明",
            content=(
                f"本任务已通过验收，采用的是第 {source_run} 次运行：最后一次运行没有通过验收，"
                f"执行体收尾时把第 {source_run} 次运行的代码原样复跑了一遍，复跑通过，代码、产物与"
                "指标都以这一版为准，之后的改动没有被采用。你之前的终答写在那些没通过的运行之后，"
                "叙事可能描述的是没被采用的版本，现在按采用的这一版重写终答。"
            ),
        ),
        Section(
            name="adopted_code",
            heading=f"采用的代码（第 {source_run} 次运行）",
            content=code,
            max_chars=_NARRATIVE_CODE_CHARS,
        ),
        Section(
            name="adopted_stdout",
            heading="复跑输出（尾部）",
            content=stdout,
            max_chars=_NARRATIVE_STDOUT_CHARS,
            overflow="truncate_head",
        ),
        Section(
            name="acceptance",
            heading="复跑的验收结果",
            content="\n".join(f"- [{item['id']}] {item['detail']}" for item in assertion_results),
        ),
        Section(name="workspace_files", heading="工作区现有文件", content="\n".join(listed)),
        Section(
            name="previous_answer",
            heading="你之前的终答（写在没通过的运行之后）",
            content=(
                json.dumps(dict(previous), ensure_ascii=False)
                if previous
                else "（没有交出合法终答）"
            ),
        ),
        Section(
            name="output_spec",
            heading="重写要求",
            content=(
                "不要调用任何工具，也不要再运行代码，只依据上面的材料重写。叙事只写采用的这一版：方法、"
                "参数与指标以上面的代码、复跑输出与验收结果为准；之后没被采用的尝试不要写成结果，需要"
                "交代时一句带过（如「后续调参没有通过验收，已回退到这一版」）；图件说明只写工作区里"
                "现有的图。只输出一个 JSON 对象作为终答："
                + _final_answer_example(task)
                + "。"
            ),
        ),
    ]
    return ContextAssembler.build(sections).messages


def _evaluate(
    task: SandboxTask, evidence: SandboxEvidence
) -> tuple[list[dict[str, Any]], bool]:
    results: list[dict[str, Any]] = []
    all_passed = True
    for assertion in task.assertions:
        try:
            passed, detail = assertion.check(evidence)
        except Exception as exc:  # noqa: BLE001 - 断言代码缺陷按未通过处理并留痕
            passed, detail = False, f"断言执行异常：{type(exc).__name__}: {exc}"
        results.append({"id": assertion.id, "passed": bool(passed), "detail": str(detail)})
        all_passed = all_passed and bool(passed)
    return results, all_passed


def _feedback_from(
    assertion_results: Sequence[Mapping[str, Any]], last_code: str, note: str = ""
) -> str:
    failed = [item for item in assertion_results if not item["passed"]]
    lines = [f"- [{item['id']}] {item['detail']}" for item in failed]
    code_part = (
        f"\n\n上一轮最后执行的代码（节选）：\n{last_code[:_FEEDBACK_CODE_CHARS]}"
        if last_code
        else ""
    )
    head = f"{note}\n" if note else ""
    return head + "以下验收断言未通过：\n" + "\n".join(lines) + code_part


def run_sandbox_task(
    task: SandboxTask,
    *,
    chat: ChatFn,
    execute_tools: ToolExecutor,
    workspace_files: Callable[[], Sequence[str]],
    read_text: Callable[[str], str],
    env_fingerprint: Mapping[str, Any],
    publish_code: Callable[[str], str] | None = None,
    cancelled: Callable[[], bool] | None = None,
    on_final_answer: Callable[[dict[str, Any]], None] | None = None,
    normalize_language: Callable[[Any], str] | None = None,
    on_loop_exit: Callable[[LoopOutcome], None] | None = None,
    on_finalize: Callable[[int], None] | None = None,
    discard_files: Callable[[Mapping[str, Sequence[str]]], Sequence[str]] | None = None,
    on_narrative_rewrite: Callable[[int, str | None], None] | None = None,
) -> dict[str, Any]:
    """驱动一张任务卡到 sandbox-run-report.v1 形状的报告 dict。

    波次语义：一波 = 一次独立装配的内环（模型写码/跑码/终答）+ 一次断言
    评估；未过则携带断言差异进入下一波。内环以回合用完收束的那一波同样进下一波（反馈开头补
    「上一轮 N 个回合用完也没交终答」与那一波运行 / 调用了什么）；结构违约 / 无进展 / 工具连败 /
    取消则不再开新波。attempts 上报波次数（每波至少一次
    评估）；usage.runs 上报模型发起的沙箱运行次数（python_run 与 code_run 同计，
    即 R2 预算账本；收尾运行不计）。

    收尾运行：波末断言没过时，从新到旧找一次没收尾过、拿它的结果重评能全过的
    模型运行（当前工作区清单 + 那次的 stdout / 指标；断言都是证据的纯函数，事后重评
    是准的），把它的代码原样再跑一遍，再用真实证据重评一次。过了即按通过收束；没过
    就带着「曾通过、复跑未复现」的说明进下一波。取消后不收尾。

    ``on_final_answer`` 在收束前回传最后一个通过结构校验的终答对象（含
    extra_final_keys 声明的叙事键）——报告本身保持 sandbox-run-report.v1
    形状，叙事产出经此旁路交给父节点。收尾通过时回传的是按采用的那次运行重写过的终答。

    叙事重写：收尾通过后（取消了不做）再开一个不带工具的内环，交给模型的只有采用的代码、复跑
    输出、验收结果、现有文件与旧终答，按同一套终答键与校验重写；模型误调工具会被退回、不执行。
    没写成（结构不合格、轮数用尽、调用出错）就沿用原终答——任务已过验收，不会因此失败。
    ``on_narrative_rewrite`` 回传采用的是第几次运行（从 1 数）与没写成的原因（None = 已重写），
    父节点据此补一句执行轨迹旁白。

    ``on_loop_exit`` 每波内环收束后回传其 LoopOutcome：报告契约里没有退出原因，
    父节点据最后一波判定失败码（内环自己的 E120 / E330 / E331 / E332，或 R2 用尽）。

    ``on_finalize`` 在收尾运行开跑前回传被复跑的是第几次模型运行（从 1 数），
    父节点据此给执行轨迹补一句说明（那次运行没有模型旁白）。

    ``discard_files`` 给了就开收尾清理：晚于来源运行、失败版新建（运行写出或模型 ws_write）且
    复跑没有重新写出的文件，在挑来源与复跑后的重评里都当作不存在（通过不能靠它们）；复跑通过后
    以「路径 → 它们报过的产物 id」交给它删，它回传真删掉的路径，这些产物不再当指标来源。复跑
    没过就进下一波，模型还要接着改，文件不动。

    ``normalize_language`` 是语言别名归一（python3 → python、Rscript → r …），由
    调用方注入以与方案卡 / 执行器同一张表；缺省只做小写去空白。
    """
    tracker = _RunTracker(
        execute_tools,
        task.max_runs,
        language=task.language,
        normalize_language=normalize_language,
        run_tool=task.run_tool,
    )
    total_usage = {"tokens": 0, "duration_ms": 0}
    assertion_results: list[dict[str, Any]] = []
    feedback: str | None = None
    waves = 0
    final_answer: dict[str, Any] | None = None

    def evidence_of(
        result: ToolResult | None, files: Sequence[str], hidden: Collection[str] = ()
    ) -> SandboxEvidence:
        stdout = str((result.output or {}).get("stdout") or "") if result is not None else ""
        gone = frozenset(hidden)
        return SandboxEvidence(
            files=tuple(path for path in files if _workspace_path(path) not in gone),
            read_text=_hiding(read_text, gone) if gone else read_text,
            last_run=result,
            stdout=stdout,
            metrics=_extract_metrics(stdout),
        )

    def evidence() -> SandboxEvidence:
        return evidence_of(tracker.last_result, workspace_files())

    def finalize() -> tuple[list[dict[str, Any]], bool, int] | None:
        """收尾运行；找不到能全过的更早运行返回 None，否则 (重评结果, 是否通过, 来源下标)。"""
        files = tuple(workspace_files())
        for index in reversed(range(len(tracker.history))):
            record = tracker.history[index]
            if index in tracker.finalized_from or record.result is tracker.last_result:
                continue
            predicted = (
                tracker.leftovers(index, rerun_done=False) if discard_files is not None else {}
            )
            if _evaluate(task, evidence_of(record.result, files, predicted))[1]:
                break
        else:
            return None
        if on_finalize is not None:
            on_finalize(index + 1)
        tracker.finalize(index)
        stale = tracker.leftovers(index) if discard_files is not None else {}
        results, ok = _evaluate(task, evidence_of(tracker.last_result, workspace_files(), stale))
        if ok and stale and discard_files is not None:
            removed = discard_files(stale)
            tracker.forget(artifact_id for path in removed for artifact_id in stale.get(path, ()))
        return results, ok, index

    def rewrite_narrative(source: int) -> tuple[dict[str, Any] | None, str | None]:
        """按收尾采用的那次运行重写终答：(重写好的终答, None) 或 (None, 没写成的原因)。"""
        messages = _assemble_narrative_prompt(
            task,
            source_run=source + 1,
            code=tracker.last_code,
            result=tracker.last_result,
            assertion_results=assertion_results,
            files=tuple(workspace_files()),
            previous=final_answer,
        )
        try:
            outcome = run_inner_loop(
                LoopTask(
                    task_id=f"{task.task_id}:narrative",
                    messages=messages,
                    validator=_final_answer_validator(task),
                    parser=_lenient_parse,
                    budget=LoopBudget(max_turns=_NARRATIVE_MAX_TURNS),
                ),
                chat=chat,
                cancelled=cancelled,
            )
        except Exception as exc:  # 任务已过验收：重写出错只沿用原终答，不能把它拖成失败
            return None, f"{type(exc).__name__}: {exc}"
        total_usage["tokens"] += outcome.usage.total_tokens
        total_usage["duration_ms"] += outcome.usage.duration_ms
        if outcome.ok and outcome.value is not None:
            return outcome.value, None
        return None, outcome.last_error or outcome.exit_reason

    def is_cancelled() -> bool:
        return cancelled is not None and cancelled()

    passed = False
    adopted: int | None = None  # 收尾通过时采用的模型运行（history 下标）
    while waves < task.max_waves:
        waves += 1
        tracker.begin_wave(task.max_turns_per_wave)
        outcome: LoopOutcome = run_inner_loop(
            LoopTask(
                task_id=f"{task.task_id}:wave{waves}",
                messages=_assemble_wave_prompt(task, feedback, task.max_runs - tracker.runs),
                validator=_final_answer_validator(task),
                parser=_lenient_parse,
                budget=LoopBudget(max_turns=task.max_turns_per_wave),
            ),
            chat=chat,
            execute_tools=tracker,
            cancelled=cancelled,
        )
        total_usage["tokens"] += outcome.usage.total_tokens
        total_usage["duration_ms"] += outcome.usage.duration_ms
        if on_loop_exit is not None:
            on_loop_exit(outcome)
        if outcome.ok and outcome.value is not None:
            final_answer = outcome.value

        assertion_results, passed = _evaluate(task, evidence())
        finalize_note = ""
        if not passed and not is_cancelled():
            finalized = finalize()
            if finalized is not None:
                assertion_results, passed, source = finalized
                if passed:
                    adopted = source
                else:
                    finalize_note = (
                        f"（第 {source + 1} 次运行的结果曾全部通过验收，执行体收尾时把那份代码原样"
                        "复跑了一遍，复跑没能通过——下面是复跑的结果与代码）"
                    )
        if passed:
            break
        if not outcome.ok and (outcome.exit_reason != _TURNS_EXHAUSTED or is_cancelled()):
            # 内环未产出合法终答（结构违约/无进展/工具连败/取消）：不再开新波（修复梯子不跨级，
            # §5.4），断言结果保留为最后事实（失败原因由各断言 detail 承载）。回合用完是每波的
            # 配额用尽、不是任务的预算尽：断言没过就照常凭剩下的 R2 运行预算开下一波。
            break
        if tracker.exhausted or tracker.runs >= task.max_runs:
            break  # R2 运行预算已尽（§5.4）：收束为 failed 报告
        notes = [finalize_note]
        if not outcome.ok:
            notes.insert(0, _turns_exhausted_note(task, tracker.wave_runs, tracker.wave_tools))
        feedback = _feedback_from(
            assertion_results, tracker.last_code, "\n".join(note for note in notes if note)
        )

    if passed and adopted is not None and not is_cancelled():
        rewritten, problem = rewrite_narrative(adopted)
        if rewritten is not None:
            final_answer = rewritten
        if on_narrative_rewrite is not None:
            on_narrative_rewrite(adopted + 1, problem)

    if on_final_answer is not None and final_answer is not None:
        on_final_answer(dict(final_answer))

    final_code_artifact = ""
    if publish_code is not None and tracker.last_code:
        final_code_artifact = publish_code(tracker.last_code)

    # 改写过的文件每次运行都会再报一版：取最后一次运行的那份（工作区里就是它）
    metrics_source = next(
        (
            artifact_id
            for artifact_id, name in reversed(tracker.artifact_names.items())
            if name == "metrics.json"
        ),
        None,
    )

    report: dict[str, Any] = {
        "status": "passed" if passed else "failed",
        "attempts": waves,
        "final_code_artifact": final_code_artifact,
        "produced_artifacts": list(dict.fromkeys(tracker.artifact_ids)),
        "metrics_source_artifact": metrics_source,
        "assertions": assertion_results,
        "seeds": dict(task.seeds),
        "env_fingerprint": {
            "runtime": str(env_fingerprint.get("runtime") or ""),
            "version": str(env_fingerprint.get("version") or ""),
            "deps_hash": str(env_fingerprint.get("deps_hash") or ""),
        },
        "usage": {
            "runs": tracker.runs,
            "tokens": total_usage["tokens"],
            "duration_ms": total_usage["duration_ms"],
        },
    }
    return report
