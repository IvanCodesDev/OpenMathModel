"""``code_run``：多语言统一执行入口（设计 §7.4，H7 切片 1）。

arguments = ``{"code": "<脚本源码>", "language": "python" | "r" | …, "timeout_s"?: 秒}``
——按任务卡 ``language`` 分发到对应 :class:`~omm_agent_tools.runners.SubprocessRunner`，
缺省 python（与今天 ``python_run`` 的行为一致，账本连续）。

路由的三条硬纪律（§7.4「无匹配执行器显式失败，不静默换语言」）：

1. 别名归一（python3 / py → python、rscript → r、北太天元 → baltamatica）；
2. 语言认得但本部署没启用 / 尚无执行器 / 运行时没装 → ``failed`` + 可行动文案
   （列出当前真的能跑的语言），**绝不**悄悄改用别的语言跑；
3. 认不出的语言名 → ``[E210]`` 参数错误（观察，可修复）。

输出 dict 在执行核的 ``exit_code / stdout / stderr / files[/ skipped_files]`` 之上
多一个 ``language``（归一后的标识），事件账本能按语言分账。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omm_agent_core import ToolResult
from omm_agent_core.ports import ArtifactStore

from .registry import ToolCallContext, ToolSpec
from .runners import (
    LANGUAGE_SPECS,
    PLANNED_LANGUAGES,
    LanguageProbe,
    SubprocessRunner,
    describe_available,
    normalize_language,
)
from .workspace import TaskWorkspace

__all__ = ["CODE_RUN_TOOL_NAME", "DEFAULT_LANGUAGE", "CodeRunSandbox"]

CODE_RUN_TOOL_NAME = "code_run"

#: 未给 language 时的缺省：python_run 时代的唯一语言；方案卡 language 缺省也是它。
DEFAULT_LANGUAGE = "python"


class CodeRunSandbox:
    """统一入口：一个工具名、按 ``language`` 分发到各语言的执行核。

    ``languages`` 决定本部署启用哪些语言（缺省 = 本刀有执行器的全部：python、r）；
    ``executables`` 按语言指定可执行文件（测试注入替身、ExecutorProfile 指定解释器）。
    启用但运行时没装的语言留在表里——调用时显式失败并说明装什么，探测时报
    ``available=False``，好让 §13.3 的能力路由和 §7.4 的 ``IMPLEMENTATION_LANGUAGES``
    解锁都以真实探测为准。
    """

    TOOL_NAME = CODE_RUN_TOOL_NAME

    def __init__(
        self,
        workspace: TaskWorkspace,
        *,
        timeout_s: float = 60.0,
        store: ArtifactStore | None = None,
        languages: Sequence[str] | None = None,
        executables: Mapping[str, str] | None = None,
        default_language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self._workspace = workspace
        self.timeout_s = timeout_s
        self.default_language = normalize_language(default_language) or DEFAULT_LANGUAGE
        overrides = {normalize_language(key): value for key, value in (executables or {}).items()}
        wanted = (
            [normalize_language(item) for item in languages]
            if languages is not None
            else list(LANGUAGE_SPECS)
        )
        self._runners: dict[str, SubprocessRunner] = {}
        for language in wanted:
            if not language:
                continue
            spec = LANGUAGE_SPECS.get(language)
            if spec is None:
                raise ValueError(
                    f"language {language!r} has no runner in this build; "
                    f"runners exist for: {', '.join(LANGUAGE_SPECS)}"
                )
            self._runners[language] = SubprocessRunner(
                spec,
                workspace,
                executable=overrides.get(language),
                timeout_s=timeout_s,
                store=store,
            )
        if self.default_language not in self._runners:
            raise ValueError(
                f"default language {self.default_language!r} is not among the enabled "
                f"languages {tuple(self._runners)}"
            )

    # -- 能力面 ---------------------------------------------------------------

    @property
    def runners(self) -> Mapping[str, SubprocessRunner]:
        return dict(self._runners)

    def runner_for(self, language: str) -> SubprocessRunner | None:
        return self._runners.get(normalize_language(language))

    def enabled_languages(self) -> tuple[str, ...]:
        return tuple(self._runners)

    def available_languages(self) -> tuple[str, ...]:
        """真的能跑的语言（可执行文件解析得到），按启用顺序。"""
        return tuple(name for name, runner in self._runners.items() if runner.available())

    def probes(self, *, use_cache: bool = True) -> dict[str, LanguageProbe]:
        return {name: runner.probe(use_cache=use_cache) for name, runner in self._runners.items()}

    # -- 工具面 ---------------------------------------------------------------

    def spec(self) -> ToolSpec:
        enabled = "、".join(self._runners)
        return ToolSpec(
            name=self.TOOL_NAME,
            description=(
                f"在隔离的运行工作区里执行一段脚本。arguments = {{code, language}}；"
                f"language 可选 {enabled}（缺省 {self.default_language}），"
                "必须与任务卡确认的实现语言一致，不会自动换语言。"
            ),
            handler=self._handle,
            risk="high",
            # 与 python_run 同理：真正的杀进程在执行核里，调用器的线程守卫只是后手。
            timeout_s=self.timeout_s + 10.0,
            required_args=("code",),
            tier="execute",
        )

    def _handle(self, arguments: dict[str, Any], ctx: ToolCallContext) -> ToolResult:
        code = arguments.get("code")
        if not isinstance(code, str) or not code.strip():
            return ToolResult(status="failed", error="'code' must be a non-empty string")

        raw_language = arguments.get("language")
        language = normalize_language(raw_language) or self.default_language
        runner = self._runners.get(language)
        if runner is None:
            return ToolResult(status="failed", error=self._routing_error(language, raw_language))

        timeout_s = min(float(arguments.get("timeout_s", self.timeout_s)), self.timeout_s)
        result = runner.run(code, ctx, timeout_s)
        output = dict(result.output)
        output["language"] = language
        return ToolResult(
            status=result.status,
            output=output,
            error=result.error,
            duration_ms=result.duration_ms,
            artifacts=result.artifacts,
        )

    def _routing_error(self, language: str, raw_language: Any) -> str:
        available = describe_available(list(self._runners.values()))
        shown = str(raw_language).strip() if raw_language is not None else language
        if language in PLANNED_LANGUAGES:
            return (
                f"实现语言 {shown} 尚无执行器：{PLANNED_LANGUAGES[language]}；"
                f"当前可用：{available}。不会自动换用其它语言运行——请在方案阶段选择可用语言。"
            )
        if language in LANGUAGE_SPECS:
            return (
                f"实现语言 {shown} 未在本执行器启用（已启用：{'、'.join(self._runners)}；"
                f"当前可用：{available}）。不会自动换用其它语言运行。"
            )
        return (
            f"[E210] 不认识的实现语言 {shown!r}；可选：{'、'.join(self._runners)}"
            f"（当前可用：{available}）。"
        )
