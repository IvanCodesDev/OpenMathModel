"""多语言 Runner（设计 §7.4，H7 切片 1）：统一 ``code_run`` 入口按 ``language`` 分发。

统一执行契约（§7.4，语言中立）：脚本落到 ``steps/<step_id>/<main.ext>``、子进程
cwd = 工作区根、环境白名单、超时杀进程、stdout / stderr 截断、新建文件按后缀
归类采集为产物；指标协议同样语言中立——脚本自己打印 ``OMM_METRICS_JSON: {...}``
行 / 落 ``metrics.json``，执行核不解析（那是执行体与节点的事）。

每语言一个 :class:`LanguageSpec`（启动命令、脚本名、可执行文件候选、额外需要
透传的环境变量、版本 / 包探测），共用同一个 :class:`SubprocessRunner` 执行核。
本刀落地 Python 与 R；MATLAB / Octave / 北太天元 只登记为「已知语言、尚无执行器」，
路由到它们**显式失败、不静默换语言**（§7.4 硬约束）。

沙箱边界与 python_runner 相同（Tier0，§7.2）：进程级隔离，网络与孙进程未拦截，
如实声明、不由调用方默认假设。R 的前端在 POSIX 上是 shell 脚本、在 Windows 上要
从 ``LOCALAPPDATA`` 推算用户库目录，所以 R 额外透传一组**路径类**环境变量
（PATH / HOME / LOCALAPPDATA / R_LIBS_* …）——它们不是秘密；用户密钥类变量
一律不透传。
"""

from __future__ import annotations

import contextlib
import glob
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omm_agent_core import ArtifactRef, ToolResult
from omm_agent_core.ports import ArtifactStore

from .registry import ToolCallContext
from .workspace import TaskWorkspace, WorkspaceArtifactStore

__all__ = [
    "ENV_ALLOWLIST",
    "KIND_BY_SUFFIX",
    "LANGUAGE_ALIASES",
    "LANGUAGE_SPECS",
    "PLANNED_LANGUAGES",
    "PYTHON_SPEC",
    "R_SPEC",
    "CompletedRun",
    "LanguageProbe",
    "LanguageSpec",
    "SubprocessRunner",
    "artifact_kind",
    "base_sandbox_env",
    "clear_probe_cache",
    "clip_output",
    "describe_available",
    "normalize_language",
    "probe_language",
    "resolve_executable",
    "run_process_tree",
    "runner_env",
]

#: 所有语言的子进程都拿到的最小环境（Windows 安全子集 + POSIX 最小值）；不含 PATH、
#: 不含任何用户密钥。语言额外需要的变量在各自 LanguageSpec.env_passthrough 里声明。
ENV_ALLOWLIST: tuple[str, ...] = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    # POSIX minimum for the same code path on CI/containers.
    "LANG",
    "LC_ALL",
)

OUTPUT_LIMIT = 32 * 1024
MAX_ARTIFACTS = 16
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024

#: 版本 / 包探测的子进程预算：R 冷启动约 1 s，MATLAB 级别的运行时后续按需放宽。
PROBE_TIMEOUT_S = 60.0

#: 产物 kind 按后缀（packages/contracts artifact.kind enum），采集的文件不经翻译
#: 直接投影到 v1 契约。`.pdf` 有意不映射为 figure：论文管线把 figure 产物当可内嵌
#: 图片插进正文（figures.py），PDF 进去只会得到一张空图——Rscript 默认图形设备落的
#: `Rplots.pdf` 归 other，模板要求图件显式存 png / svg。
KIND_BY_SUFFIX: Mapping[str, str] = {
    ".svg": "figure",
    ".png": "figure",
    ".jpg": "figure",
    ".jpeg": "figure",
    ".gif": "figure",
    ".csv": "table",
    ".tsv": "table",
    ".py": "code",
    ".r": "code",
    ".m": "code",
    ".jl": "code",
    ".log": "log",
    ".txt": "log",
    ".json": "dataset",
    ".rds": "dataset",
    ".rdata": "dataset",
    ".rda": "dataset",
}

#: 语言别名 → 契约小写标识。与 omm_agent_skills.nodes._LANGUAGE_ALIASES 逐字同口径
#: （skills 不 import tools，两边各存一份；改一处必须改另一处，test_runners 有对照用例
#: 锁住本表的键值）。认不出的原样小写后交给路由判定。
LANGUAGE_ALIASES: Mapping[str, str] = {
    "python": "python", "python3": "python", "py": "python", "cpython": "python",
    "r": "r", "rscript": "r", "r language": "r", "r 语言": "r",
    "matlab": "matlab", "octave": "octave", "gnu octave": "octave",
    "julia": "julia", "baltamatica": "baltamatica", "北太天元": "baltamatica",
}

#: §7.4 v3.42 拍板的后续语言：已登记、本刀无执行器。路由到它们时错误文案说清
#: 「尚无执行器」而不是「未知语言」，好让方案阶段的 G1 确认与执行阶段的失败对得上。
PLANNED_LANGUAGES: Mapping[str, str] = {
    "matlab": "MATLAB（仅本地，随本地执行代理切片 ②）",
    "octave": "Octave（随 MATLAB 同批，CI 替身）",
    "baltamatica": "北太天元（复用 MATLAB 微技能兼容子集）",
    "julia": "Julia（未列入 v3.42 拍板范围）",
}

#: 探测脚本打印的单行标记，探测器只认这一行（前面可能有包加载噪声）。
_PROBE_LINE = re.compile(r"^OMM_PROBE_JSON:\s*(\{.*\})\s*$", re.MULTILINE)
_VERSION_IN_TEXT = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def normalize_language(value: Any) -> str:
    """单个语言值 → 小写标识（空值 → 空串）。"""
    text = str(value or "").strip().lower()
    return LANGUAGE_ALIASES.get(text, text)


@dataclass(frozen=True)
class LanguageSpec:
    """一种实现语言的启动约定：执行核只认这张表，不认语言名。

    ``argv`` / ``probe_argv`` 里的 ``{executable}`` / ``{script}`` 在运行时替换；
    ``executables`` 按序解析——绝对路径直接存在性判定，命令名走 PATH；
    ``search_globs`` 是 PATH 找不到时的兜底安装目录通配（``{ENV}`` 取环境变量，
    缺就跳过），按版本号倒序取最新。
    """

    language: str
    label: str  # 错误文案的主语（python_run 的既有文案用 "python"）
    script_name: str
    argv: tuple[str, ...]
    executables: tuple[str, ...]
    version_argv: tuple[str, ...]
    probe_argv: tuple[str, ...] = ()
    probe_script: str = ""
    package_candidates: tuple[str, ...] = ()
    env_passthrough: tuple[str, ...] = ()
    env_overrides: tuple[tuple[str, str], ...] = ()
    search_globs: tuple[str, ...] = ()

    def command(self, executable: str, script: str) -> list[str]:
        return [
            part.replace("{executable}", executable).replace("{script}", script)
            for part in self.argv
        ]


#: 与 sandbox_tools.ENV_PROBE_PACKAGE_CANDIDATES 同一份候选（那边是顶层指纹的
#: 出处，这边给 languages.python 用同一口径）。
_PYTHON_PACKAGE_CANDIDATES: tuple[str, ...] = (
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

#: 在 ``python -I`` 与清洗环境下探测：与 python_run 同条件才是「生成的代码能不能 import」
#: 的诚实答案（GPU 探针的同一教训：用户 site-packages 里的包在父进程 import 得到、
#: 在 -I 下不行）。
_PYTHON_PROBE_SCRIPT = (
    "import importlib.util, json, platform\n"
    f"cands = {json.dumps(list(_PYTHON_PACKAGE_CANDIDATES))}\n"
    "pk = [n for n in cands if importlib.util.find_spec(n) is not None]\n"
    "info = {'version': platform.python_version(), 'packages': pk}\n"
    "print('OMM_PROBE_JSON: ' + json.dumps(info))\n"
)

PYTHON_SPEC = LanguageSpec(
    language="python",
    label="python",
    script_name="main.py",
    argv=("{executable}", "-I", "{script}"),
    executables=(sys.executable,),
    version_argv=("--version",),
    probe_argv=("{executable}", "-I", "-c", _PYTHON_PROBE_SCRIPT),
    package_candidates=_PYTHON_PACKAGE_CANDIDATES,
    env_overrides=(("PYTHONIOENCODING", "utf-8"), ("PYTHONUTF8", "1")),
)

#: R 侧包候选：jsonlite 是指标协议的首选序列化器（基础 R 不带，缺席时模板退回
#: sprintf 手写 JSON）；其余是建模常用件。MASS / Matrix 随 R 发行版自带，可当探测器
#: 本身是否工作的对照。
_R_PACKAGE_CANDIDATES: tuple[str, ...] = (
    "jsonlite",
    "MASS",
    "Matrix",
    "ggplot2",
    "data.table",
    "dplyr",
    "tidyr",
    "readr",
    "lme4",
    "forecast",
    "igraph",
    "deSolve",
    "lpSolve",
)

_R_PACKAGE_VECTOR = ", ".join(f'"{name}"' for name in _R_PACKAGE_CANDIDATES)
_R_PROBE_SCRIPT = (
    f"pk <- c({_R_PACKAGE_VECTOR}); "
    "ok <- vapply(pk, function(p) suppressWarnings(requireNamespace(p, quietly = TRUE)), "
    "logical(1)); "
    'cat("OMM_PROBE_JSON: {\\"version\\": \\"", R.version$major, ".", R.version$minor, '
    '"\\", \\"packages\\": [", paste(sprintf("\\"%s\\"", pk[ok]), collapse = ", "), '
    '"]}\\n", sep = "")'
)

R_SPEC = LanguageSpec(
    language="r",
    label="Rscript",
    script_name="main.R",
    # --vanilla = --no-save --no-restore --no-site-file --no-init-file --no-environ：
    # 不读用户 profile / .Renviron，同输入冷启动可复现（§7.3）。
    argv=("{executable}", "--vanilla", "{script}"),
    executables=("Rscript",),
    version_argv=("--version",),
    probe_argv=("{executable}", "--vanilla", "-e", _R_PROBE_SCRIPT),
    package_candidates=_R_PACKAGE_CANDIDATES,
    # 路径类变量，不是秘密：R 前端在 POSIX 是 shell 脚本（要 PATH 上的 sh / sed）；
    # Windows 上用户库 R_LIBS_USER 由 LOCALAPPDATA 推算，缺了就找不到用户装的 jsonlite。
    env_passthrough=(
        "PATH",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "APPDATA",
        "R_HOME",
        "R_USER",
        "R_LIBS",
        "R_LIBS_USER",
        "R_LIBS_SITE",
        "TMPDIR",
    ),
    # Windows 安装器默认不改 PATH（winget 装完 Rscript 也不在 PATH 上）。
    search_globs=(
        "{ProgramFiles}/R/R-*/bin/Rscript.exe",
        "{ProgramFiles(x86)}/R/R-*/bin/Rscript.exe",
        "{LOCALAPPDATA}/Programs/R/R-*/bin/Rscript.exe",
    ),
)

#: 本刀有执行器的语言，按 §7.4 顺位。
LANGUAGE_SPECS: Mapping[str, LanguageSpec] = {
    PYTHON_SPEC.language: PYTHON_SPEC,
    R_SPEC.language: R_SPEC,
}


# -- 环境与可执行文件 -----------------------------------------------------------


def base_sandbox_env() -> dict[str, str]:
    """所有语言共用的清洗环境（白名单子集，无 PATH、无密钥）。"""
    return {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}


def runner_env(spec: LanguageSpec) -> dict[str, str]:
    """某语言子进程的完整环境 = 公共白名单 + 该语言声明的透传 + 固定注入。"""
    env = base_sandbox_env()
    for key in spec.env_passthrough:
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(spec.env_overrides)
    return env


def _version_key(path: str) -> tuple[int, ...]:
    """安装目录名里的版本号（R-4.6.1 → (4, 6, 1)），新版排前。"""
    match = re.search(r"(\d+(?:\.\d+)+)", Path(path).parent.parent.name)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _expand_glob(pattern: str) -> str | None:
    def substitute(match: re.Match[str]) -> str:
        value = os.environ.get(match.group(1))
        if value is None:
            raise KeyError(match.group(1))
        return value

    try:
        return re.sub(r"\{([^}]+)\}", substitute, pattern)
    except KeyError:
        return None


def resolve_executable(spec: LanguageSpec, override: str | None = None) -> str | None:
    """找到该语言的可执行文件绝对路径；找不到返回 None（调用方决定文案）。

    ``override`` 是装配方指定的路径 / 命令名（例如测试注入的 Rscript 替身、或
    ExecutorProfile 指定的解释器）——给了就只认它，不再回落候选表。
    """
    candidates = (override,) if override else spec.executables
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_absolute() or os.sep in candidate or (os.altsep and os.altsep in candidate):
            if path.is_file():
                return str(path)
            continue
        found = shutil.which(candidate)
        if found:
            return found
    if override:
        return None
    hits: list[str] = []
    for pattern in spec.search_globs:
        expanded = _expand_glob(pattern)
        if expanded:
            hits.extend(
                os.path.normpath(hit) for hit in glob.glob(expanded) if Path(hit).is_file()
            )
    if not hits:
        return None
    hits.sort(key=_version_key, reverse=True)
    return hits[0]


# -- 探测（ExecutorProfile 数据源） ------------------------------------------------


@dataclass(frozen=True)
class LanguageProbe:
    """一种语言在本执行器上的事实：可用与否、版本、可执行文件、候选包里可用的子集。

    进 env_probe 输出的 ``languages`` 块，也是 §13.3 ExecutorProfile.languages 的
    数据源；``detail`` 在不可用时说明原因（可行动：装什么、放哪）。
    """

    language: str
    available: bool
    version: str | None
    executable: str | None
    packages: tuple[str, ...]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "available": self.available,
            "version": self.version,
            "executable": self.executable,
            "packages": list(self.packages),
            "detail": self.detail,
        }


_probe_cache: dict[tuple[str, str | None], LanguageProbe] = {}
_probe_lock = threading.Lock()


def clear_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


def _parse_probe_output(text: str) -> tuple[str | None, tuple[str, ...]]:
    version: str | None = None
    packages: tuple[str, ...] = ()
    for match in _PROBE_LINE.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        raw_version = str(payload.get("version") or "").strip()
        version = raw_version or version
        raw_packages = payload.get("packages")
        if isinstance(raw_packages, list):
            packages = tuple(str(item) for item in raw_packages if str(item).strip())
    return version, packages


def _run_probe(
    spec: LanguageSpec, executable: str, timeout_s: float
) -> tuple[str | None, tuple[str, ...], str]:
    """跑一次探测子进程：优先带包清单的 probe_argv，退回只探版本的 version_argv。

    与正式运行走同一个 :func:`run_process_tree`（同环境、同超时杀树语义）：探测挂住
    不能拖死 env_probe 工具线程。
    """
    env = runner_env(spec)
    cwd = os.getcwd()
    if spec.probe_argv:
        try:
            done = run_process_tree(
                [part.replace("{executable}", executable) for part in spec.probe_argv],
                cwd=cwd,
                env=env,
                timeout_s=timeout_s,
            )
        except OSError as exc:
            return None, (), f"探测子进程失败：{type(exc).__name__}: {exc}"
        if done.timed_out:
            return None, (), f"探测子进程超过 {timeout_s}s 被杀"
        version, packages = _parse_probe_output(done.stdout + "\n" + done.stderr)
        if version:
            return version, packages, "probe"
    try:
        done = run_process_tree(
            [executable, *spec.version_argv], cwd=cwd, env=env, timeout_s=timeout_s
        )
    except OSError as exc:
        return None, (), f"版本探测失败：{type(exc).__name__}: {exc}"
    if done.timed_out:
        return None, (), f"版本探测超过 {timeout_s}s 被杀"
    match = _VERSION_IN_TEXT.search(done.stdout + "\n" + done.stderr)
    if match is None:
        return None, (), f"版本探测无法解析（退出码 {done.returncode}）"
    return match.group(1), (), "version"


def probe_language(
    spec: LanguageSpec,
    executable: str | None = None,
    *,
    timeout_s: float = PROBE_TIMEOUT_S,
    use_cache: bool = True,
) -> LanguageProbe:
    """探测某语言：解析可执行文件 → 子进程报版本与可用包。结果按 (语言, 指定路径) 缓存。

    探测跑在与 code_run 相同的环境里（同解释器、同白名单），否则「探到了、跑不了」
    会把不存在的能力写进 ExecutorProfile。任何失败都收成 ``available=False`` +
    可行动的 detail，不抛异常——探测器不该让 env_probe 工具崩溃。
    """
    key = (spec.language, executable)
    if use_cache:
        with _probe_lock:
            cached = _probe_cache.get(key)
        if cached is not None:
            return cached

    resolved = resolve_executable(spec, executable)
    if resolved is None:
        where = "、".join(spec.executables) or spec.label
        result = LanguageProbe(
            language=spec.language,
            available=False,
            version=None,
            executable=None,
            packages=(),
            detail=(
                f"未找到 {spec.label} 可执行文件（候选：{where}；PATH 与默认安装目录均无）。"
                f"安装 {spec.label} 后重启执行器，或在装配时显式指定路径。"
            ),
        )
    else:
        version, packages, how = _run_probe(spec, resolved, timeout_s)
        if version is None:
            result = LanguageProbe(
                language=spec.language,
                available=False,
                version=None,
                executable=resolved,
                packages=(),
                detail=f"{spec.label} 在 {resolved} 找到但无法运行：{how}",
            )
        else:
            result = LanguageProbe(
                language=spec.language,
                available=True,
                version=version,
                executable=resolved,
                packages=packages,
                detail=f"{spec.label} {version}（{how}）",
            )
    if use_cache:
        with _probe_lock:
            _probe_cache[key] = result
    return result


# -- 执行核 ------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletedRun:
    """一次子进程运行的事实：超时时 returncode 为 None、stdout / stderr 为被杀前的部分输出。"""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


#: 杀掉进程树后再收管道的宽限：正常情况瞬时；只有被杀漏的孤儿还握着管道才会等满。
_DRAIN_AFTER_KILL_S = 5.0


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """杀整棵进程树，不只杀直接子进程。

    Rscript.exe 在 Windows 上经 cmd.exe 起 Rterm.exe 才真正跑脚本；Python 脚本也可能
    自己 spawn 子进程。只 ``proc.kill()`` 直接子进程，孙进程继续跑并握着 stdout 管道，
    ``communicate()`` 会一直等到它自然结束——「超时」就名存实亡（实测 Rscript 超时
    2 s 的调用等了 12 s、脚本跑完了）。Windows 用 ``taskkill /T /F``（系统自带），
    POSIX 用新会话 + ``killpg``。
    """
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        if taskkill.is_file():
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                subprocess.run(
                    [str(taskkill), "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                    check=False,
                )
    else:
        with contextlib.suppress(OSError):  # ProcessLookupError / PermissionError 都是 OSError
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def run_process_tree(
    command: Sequence[str], *, cwd: str, env: Mapping[str, str], timeout_s: float
) -> CompletedRun:
    """``subprocess.run(capture_output, timeout)`` 的进程树安全版。

    超时 → 杀整棵树 → 再收一次管道拿部分输出（``communicate`` 超时后输出不丢，
    杀完再调一次就能取回）。收管道也设宽限，孤儿握管道不至于挂死工具线程。
    """
    popen_kwargs: dict[str, Any] = {}
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True  # 让 pid 成为进程组组长，killpg 才杀得全
    proc = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def drain(stream: Any, chunks: list[str]) -> None:
        with contextlib.suppress(OSError, ValueError):
            while line := stream.readline():
                chunks.append(line)

    readers = [
        threading.Thread(target=drain, args=(proc.stdout, stdout_chunks), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, stderr_chunks), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_DRAIN_AFTER_KILL_S)

    deadline = time.monotonic() + _DRAIN_AFTER_KILL_S
    for reader in readers:
        reader.join(max(0.0, deadline - time.monotonic()))
    # An escaped descendant may still own a copied pipe handle. Closing that stream
    # from this thread can block until the descendant exits, defeating the timeout.
    # The daemon reader owns the handle and will close naturally at EOF.

    return CompletedRun(
        returncode=None if timed_out else proc.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        timed_out=timed_out,
    )


def artifact_kind(name: str) -> str:
    return KIND_BY_SUFFIX.get(os.path.splitext(name)[1].lower(), "other")


def clip_output(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n...(+{len(text) - limit} chars truncated)"
    return text


class SubprocessRunner:
    """一种语言的子进程执行核：脚本落盘 → 快照 → 运行 → 截断 → 采集产物。

    与 python_run 的历史行为逐字节一致（PythonSandbox 现在只是它的薄壳）：脚本写到
    ``steps/<step_id>/<script_name>``、cwd = 工作区根（ws_* / 断言 / 下发数据都说
    工作区相对路径，生成代码里的相对路径必须落到同一个根）、快照在写脚本之后
    （脚本本身不算产物）、扫全工作区（代码可在根下任意位置建文件）。
    """

    def __init__(
        self,
        spec: LanguageSpec,
        workspace: TaskWorkspace,
        *,
        executable: str | None = None,
        timeout_s: float = 60.0,
        store: ArtifactStore | None = None,
    ) -> None:
        self.spec = spec
        self._workspace = workspace
        self._store = store or WorkspaceArtifactStore(workspace)
        self._executable_override = executable
        self.timeout_s = timeout_s

    @property
    def language(self) -> str:
        return self.spec.language

    def resolve_executable(self) -> str | None:
        return resolve_executable(self.spec, self._executable_override)

    def probe(self, *, use_cache: bool = True) -> LanguageProbe:
        return probe_language(self.spec, self._executable_override, use_cache=use_cache)

    def available(self) -> bool:
        return self.resolve_executable() is not None

    def run(
        self, code: str, ctx: ToolCallContext, timeout_s: float | None = None
    ) -> ToolResult:
        """运行一段代码；输出 dict = exit_code / stdout / stderr / files[/ skipped_files]。"""
        limit = self.timeout_s if timeout_s is None else min(float(timeout_s), self.timeout_s)
        executable = self.resolve_executable()
        if executable is None:
            probe = self.probe()
            return ToolResult(
                status="failed",
                error=f"{self.spec.label} 运行时不可用：{probe.detail}",
            )

        step_dir_rel = f"steps/{ctx.step_id}"
        script_path = self._workspace.write_text(
            f"{step_dir_rel}/{self.spec.script_name}", code
        )

        scan_root = self._workspace.root
        before = {path for path in scan_root.rglob("*") if path.is_file()}

        completed = run_process_tree(
            self.spec.command(executable, str(script_path)),
            cwd=str(scan_root),
            env=runner_env(self.spec),
            timeout_s=limit,
        )
        if completed.timed_out:
            return ToolResult(
                status="timeout",
                error=f"{self.spec.label} run exceeded {limit}s and was killed",
                output={
                    "stdout": clip_output(completed.stdout),
                    "stderr": clip_output(completed.stderr),
                },
            )

        artifacts, skipped = self._collect_artifacts(scan_root, before, ctx)
        output: dict[str, Any] = {
            "exit_code": completed.returncode,
            "stdout": clip_output(completed.stdout),
            "stderr": clip_output(completed.stderr),
            "files": [ref.uri for ref in artifacts],
        }
        if skipped:
            output["skipped_files"] = skipped

        if completed.returncode != 0:
            return ToolResult(
                status="failed",
                error=f"{self.spec.label} exited with code {completed.returncode}",
                output=output,
                artifacts=tuple(artifacts),
            )
        return ToolResult(status="succeeded", output=output, artifacts=tuple(artifacts))

    def _collect_artifacts(
        self, scan_root: Path, before: set[Path], ctx: ToolCallContext
    ) -> tuple[list[ArtifactRef], list[str]]:
        artifacts: list[ArtifactRef] = []
        skipped: list[str] = []
        created = sorted(
            path
            for path in scan_root.rglob("*")
            if path.is_file() and path not in before
        )
        for path in created:
            if len(artifacts) >= MAX_ARTIFACTS:
                skipped.append(f"{path.name} (artifact limit {MAX_ARTIFACTS})")
                continue
            size = path.stat().st_size
            if size > MAX_ARTIFACT_BYTES:
                skipped.append(f"{path.name} ({size} bytes over limit)")
                continue
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            artifacts.append(
                self._store.put(
                    run_id=self._workspace.run_id,
                    kind=artifact_kind(path.name),
                    name=path.name,
                    content=path.read_bytes(),
                    media_type=media_type,
                    producer_step=ctx.step_id,
                )
            )
        return artifacts, skipped


def describe_available(runners: Sequence[SubprocessRunner]) -> str:
    """给错误文案用的「当前可用语言」清单（按顺位、只列真的能跑的）。"""
    names = [runner.language for runner in runners if runner.available()]
    return "、".join(names) if names else "（无）"
