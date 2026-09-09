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
3. **运行预算按次预付**：沙箱运行（python_run / code_run）超过 max_runs 的那一次
   不会执行（§4.7 "a started run is spent money" 的镜像），预算尽即收束报告。
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
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from omm_agent_core.models import ToolResult

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

#: 任务卡缺省实现语言（python_run 时代的唯一语言；方案卡 language 缺省也是它）。
DEFAULT_LANGUAGE = "python"

#: 指标标记行（与实验节点同一约定）：脚本打印
#: ``OMM_METRICS_JSON: {...}``，取最后一条。
_METRICS_LINE = re.compile(r"^OMM_METRICS_JSON:\s*(\{.*\})\s*$", re.MULTILINE)

#: 断言反馈里上一波代码的截断长度。
_FEEDBACK_CODE_CHARS = 3000

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
    max_waves: int = 3  # 断言修复波次上限（每波至少消耗一次运行才有意义）
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


class _RunTracker:
    """沙箱运行（python_run / code_run）的预算、语言路由与证据跟踪：包装注入的工具执行器。

    - 预算按次预付：超过 max_runs 的调用不执行，返回失败观察并置 exhausted；
    - 语言固定：code_run 缺 language 就补任务卡的语言；传了别的语言、或语言不是
      python 却调 python_run → 退回观察（不执行、不计预算）；
    - 证据采集：记录最后一次调用的代码/结果与全部产物 id。
    """

    def __init__(
        self,
        inner: ToolExecutor,
        max_runs: int,
        *,
        language: str = DEFAULT_LANGUAGE,
        normalize_language: Callable[[Any], str] | None = None,
    ) -> None:
        self._inner = inner
        self._max = max_runs
        self._normalize = normalize_language or _default_normalize_language
        self.language = self._normalize(language) or DEFAULT_LANGUAGE
        self.runs = 0
        self.exhausted = False
        self.rejected_language_calls = 0
        self.last_code: str = ""
        self.last_result: ToolResult | None = None
        self.artifact_ids: list[str] = []
        self.artifact_names: dict[str, str] = {}

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

    def __call__(self, calls: Sequence[ToolCall]) -> Sequence[ToolResult]:
        results: list[ToolResult] = []
        for call in calls:
            if call.name not in RUN_TOOL_NAMES:
                results.extend(self._inner([call]))
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
            self.last_code = str(routed.arguments.get("code") or "")
            outcome = list(self._inner([routed]))[0]
            self.last_result = outcome
            for ref in outcome.artifacts:
                self.artifact_ids.append(ref.artifact_id)
                self.artifact_names[ref.artifact_id] = ref.uri.rstrip("/").rsplit("/", 1)[-1]
            results.append(outcome)
        return results


def _assemble_wave_prompt(
    task: SandboxTask, feedback: str | None
) -> tuple[Message, ...]:
    """一波内环的任务卡 prompt：分节装配（§4.2），反馈段只在修复波出现。"""
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


def _final_answer_example(task: SandboxTask) -> str:
    pairs = ['"summary": "一句话说明做了什么与关键结果"']
    pairs += [f'"{key}": "{hint}"' for key, hint in task.extra_final_keys]
    pairs += [f'"{key}": "（可选）{hint}"' for key, hint in task.optional_final_keys]
    return "{" + ", ".join(pairs) + "}"


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
    assertion_results: Sequence[Mapping[str, Any]], last_code: str
) -> str:
    failed = [item for item in assertion_results if not item["passed"]]
    lines = [f"- [{item['id']}] {item['detail']}" for item in failed]
    code_part = (
        f"\n\n上一轮最后执行的代码（节选）：\n{last_code[:_FEEDBACK_CODE_CHARS]}"
        if last_code
        else ""
    )
    return "以下验收断言未通过：\n" + "\n".join(lines) + code_part


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
) -> dict[str, Any]:
    """驱动一张任务卡到 sandbox-run-report.v1 形状的报告 dict。

    波次语义：一波 = 一次独立装配的内环（模型写码/跑码/终答）+ 一次断言
    评估；未过则携带断言差异进入下一波。attempts 上报波次数（每波至少一次
    评估）；usage.runs 上报真实沙箱运行次数（python_run 与 code_run 同计）。

    ``on_final_answer`` 在收束前回传最后一个通过结构校验的终答对象（含
    extra_final_keys 声明的叙事键）——报告本身保持 sandbox-run-report.v1
    形状，叙事产出经此旁路交给父节点。

    ``normalize_language`` 是语言别名归一（python3 → python、Rscript → r …），由
    调用方注入以与方案卡 / 执行器同一张表；缺省只做小写去空白。
    """
    tracker = _RunTracker(
        execute_tools,
        task.max_runs,
        language=task.language,
        normalize_language=normalize_language,
    )
    total_usage = {"tokens": 0, "duration_ms": 0}
    assertion_results: list[dict[str, Any]] = []
    feedback: str | None = None
    waves = 0
    final_answer: dict[str, Any] | None = None

    def evidence() -> SandboxEvidence:
        last = tracker.last_result
        stdout = str((last.output or {}).get("stdout") or "") if last is not None else ""
        return SandboxEvidence(
            files=tuple(workspace_files()),
            read_text=read_text,
            last_run=last,
            stdout=stdout,
            metrics=_extract_metrics(stdout),
        )

    passed = False
    while waves < task.max_waves:
        waves += 1
        outcome: LoopOutcome = run_inner_loop(
            LoopTask(
                task_id=f"{task.task_id}:wave{waves}",
                messages=_assemble_wave_prompt(task, feedback),
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
        if outcome.ok and outcome.value is not None:
            final_answer = outcome.value

        assertion_results, passed = _evaluate(task, evidence())
        if passed and outcome.ok:
            break
        if not outcome.ok:
            # 内环未产出合法终答（结构违约/轮数尽/无进展/取消）：不再开新波，
            # 断言结果保留为最后事实（失败原因由各断言 detail 承载）。
            break
        if tracker.exhausted or tracker.runs >= task.max_runs:
            break  # R2 运行预算已尽（§5.4）：收束为 failed 报告
        feedback = _feedback_from(assertion_results, tracker.last_code)

    if on_final_answer is not None and final_answer is not None:
        on_final_answer(dict(final_answer))

    final_code_artifact = ""
    if publish_code is not None and tracker.last_code:
        final_code_artifact = publish_code(tracker.last_code)

    metrics_source = next(
        (
            artifact_id
            for artifact_id, name in tracker.artifact_names.items()
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
