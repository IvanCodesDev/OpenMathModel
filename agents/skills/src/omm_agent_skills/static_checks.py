"""Reviewer 静态检查：沙盒脚本先过一遍不烧模型的确定性检查，再给审稿人（§8.4，H4 待做项）。

生成者-评审者环今天是「复跑核对指标 + 独立审稿人判读」。审稿人是模型，判「这段代码有没有
联网 / 有没有固定种子 / 读了哪些文件」这种事实既慢又不稳；这里用 ``ast`` 把机器能判的事实
判掉：**阻断项**（危险调用、联网、无种子随机、绝对路径 / 越界路径、语法错误）不等审稿人，
直接作为一条驳回意见回沙盒修复波；**提示项**（读写不在白名单前缀、未用的 import、伪指标行）
只进审稿材料供参考。规则与严重度写死在表里，三个沙盒消费方（清洗 / 实验 / 检验）各带自己的
读写白名单 profile。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CLEANING_STATIC_PROFILE",
    "EXPERIMENT_STATIC_PROFILE",
    "STATIC_RULES",
    "VALIDATION_STATIC_PROFILE",
    "StaticCheckProfile",
    "major_static_feedback",
    "run_static_checks",
    "static_material",
]

#: 规则表：id → (严重度, 一句话说明)。major = 阻断（回沙盒修复），minor = 提示（进审稿材料）。
STATIC_RULES: dict[str, tuple[str, str]] = {
    "syntax_error": ("major", "脚本无法解析"),
    "banned_call": ("major", "调用了沙盒禁止的能力（子进程 / 动态执行 / 交互输入 / 删文件）"),
    "network": ("major", "导入或调用了联网库（沙盒不联网，结果不可复现也不可审）"),
    "no_seed": ("major", "使用了随机性却没有固定种子（任务卡要求 random_seed）"),
    "abs_path": ("major", "文件路径用了绝对路径或 .. 越界（只准在工作区内相对读写）"),
    "read_outside_whitelist": ("minor", "读取的文件不在任务卡数据清单 / 允许前缀内"),
    "write_outside_whitelist": ("minor", "写出的文件不在允许前缀内"),
    "unused_import": ("minor", "导入了但从未使用的模块"),
    "fake_metrics_line": ("minor", "打印了 OMM_ 开头但不是 OMM_METRICS_JSON 标记行的内容（易被误当指标）"),
}

#: 危险调用：模块.函数 或 内建名。
_BANNED_ATTRIBUTES = {
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"), ("subprocess", "check_output"),
    ("subprocess", "check_call"), ("os", "system"), ("os", "popen"), ("os", "remove"), ("os", "unlink"),
    ("os", "rmdir"), ("os", "removedirs"), ("shutil", "rmtree"), ("os", "execv"), ("os", "execvp"),
    ("os", "spawn"), ("os", "kill"), ("ctypes", "CDLL"),
}
_BANNED_BUILTINS = {"eval", "exec", "compile", "__import__", "input", "breakpoint"}
_BANNED_MODULES = {"subprocess", "ctypes", "pty", "multiprocessing"}
_NETWORK_MODULES = {
    "socket", "urllib", "urllib3", "requests", "httpx", "http", "ftplib", "smtplib", "aiohttp",
    "websocket", "websockets", "paramiko", "telnetlib", "xmlrpc",
}
#: 随机性来源（模块级导入或属性调用）与种子调用。
_RANDOM_MODULES = {"random", "secrets"}
_RANDOM_ATTRIBUTE_ROOTS = {"random", "np", "numpy", "torch", "jax", "scipy"}
_RANDOM_ATTRIBUTES = {
    "random", "rand", "randn", "randint", "choice", "shuffle", "permutation", "normal", "uniform",
    "sample", "randrange", "gauss", "binomial", "poisson", "exponential", "default_rng", "RandomState",
    "manual_seed", "seed",
}
_SEED_CALLS = {"seed", "manual_seed", "default_rng", "RandomState", "Generator", "PCG64", "SeedSequence"}
_SEED_KWARGS = {"random_state", "seed", "random_seed", "rng"}
#: 读 / 写文件的调用：函数名 → 路径参数位置（第一个位置参数）。
_READ_CALLS = {
    "open", "read_csv", "read_excel", "read_json", "read_parquet", "read_table", "loadtxt", "genfromtxt",
    "load", "read_text", "read_bytes", "imread", "load_workbook",
}
_WRITE_CALLS = {"to_csv", "to_excel", "to_json", "to_parquet", "savefig", "save", "savetxt", "write_text", "write_bytes", "imsave"}
_WRITE_MODES = re.compile(r"^[wax]")
#: 标记行前缀：只有这一种是节点解析的指标行。
_METRICS_LINE_PREFIX = "OMM_METRICS_JSON:"
_MATERIAL_LIMIT = 12


@dataclass(frozen=True)
class StaticCheckProfile:
    """某个沙盒消费方的静态检查口径：允许读 / 写的相对路径前缀（空 = 不查该项）、是否要求种子。"""

    name: str
    allowed_read_prefixes: tuple[str, ...] = ()
    allowed_write_prefixes: tuple[str, ...] = ()
    require_seed: bool = True
    #: 消费方额外允许读的具体文件（如检验阶段读实验脚本）。
    allowed_read_files: tuple[str, ...] = field(default=())


#: 清洗：读任务卡数据文件（data/），写 cleaned/。
CLEANING_STATIC_PROFILE = StaticCheckProfile(
    name="data_cleaning",
    allowed_read_prefixes=("data/", "cleaned/"),
    allowed_write_prefixes=("cleaned/",),
)
#: 实验：读 data/ 与 cleaned/，写结果表 / 指标 / 图件（工作区根下的 results*、metrics* 也算）。
EXPERIMENT_STATIC_PROFILE = StaticCheckProfile(
    name="experiment",
    allowed_read_prefixes=("data/", "cleaned/", "results", "metrics", "figures/", "outputs/"),
    allowed_write_prefixes=("results", "metrics", "figures/", "outputs/"),
)
#: 检验：在实验的基础上可读实验脚本与结果，写 validation/ 与图件。
VALIDATION_STATIC_PROFILE = StaticCheckProfile(
    name="validating",
    allowed_read_prefixes=("data/", "cleaned/", "results", "metrics", "figures/", "outputs/", "validation/"),
    allowed_write_prefixes=("validation/", "figures/", "results", "outputs/"),
    allowed_read_files=("experiment.py",),
)


def _finding(rule: str, location: int | None, detail: str) -> dict[str, Any]:
    severity, _ = STATIC_RULES[rule]
    return {
        "id": rule,
        "severity": severity,
        "rule": STATIC_RULES[rule][1],
        "location": f"第 {location} 行" if location else "",
        "detail": detail,
    }


def _dotted(node: ast.AST) -> str:
    """``np.random.seed`` → ``"np.random.seed"``；非属性链给空串。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _is_path_violation(literal: str) -> bool:
    text = literal.replace("\\", "/")
    return bool(re.match(r"^(?:[A-Za-z]:/|/|~)", text)) or "/../" in f"/{text}/" or text.startswith("../")


def _within(literal: str, prefixes: Iterable[str], files: Iterable[str] = ()) -> bool:
    text = literal.replace("\\", "/").lstrip("./")
    return text in set(files) or any(text.startswith(prefix) for prefix in prefixes)


def _literal_arg(call: ast.Call, index: int = 0) -> tuple[str, ast.AST] | None:
    if len(call.args) > index and isinstance(call.args[index], ast.Constant) and isinstance(call.args[index].value, str):
        return call.args[index].value, call.args[index]
    return None


def _open_mode(call: ast.Call) -> str:
    if len(call.args) > 1 and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str):
        return call.args[1].value
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return "r"


def run_static_checks(code: str, profile: StaticCheckProfile) -> list[dict[str, Any]]:
    """脚本 × profile → 发现列表（按行号排序；每条 ``{id, severity, rule, location, detail}``）。不执行代码。"""
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return [_finding("syntax_error", exc.lineno, f"{exc.msg}")]

    findings: list[dict[str, Any]] = []
    imported: dict[str, int] = {}
    import_roots: dict[str, str] = {}
    used_names: set[str] = set()
    uses_random = False
    seeded = False
    random_lines: list[int] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = (alias.asname or alias.name).split(".", 1)[0]
                imported[local] = node.lineno
                import_roots[local] = alias.name.split(".", 1)[0]
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            for alias in node.names:
                local = alias.asname or alias.name
                imported[local] = node.lineno
                import_roots[local] = root
                if root in _RANDOM_MODULES or (root in _RANDOM_ATTRIBUTE_ROOTS and alias.name in _RANDOM_ATTRIBUTES):
                    uses_random = True
                    random_lines.append(node.lineno)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used_names.add(base.id)

    for local, root in import_roots.items():
        if root in _BANNED_MODULES:
            findings.append(_finding("banned_call", imported[local], f"import {root}"))
        if root in _NETWORK_MODULES:
            findings.append(_finding("network", imported[local], f"import {root}"))
        if root in _RANDOM_MODULES:
            uses_random = True
            random_lines.append(imported[local])

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        name = node.func.id if isinstance(node.func, ast.Name) else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
        parts = dotted.split(".") if dotted else []
        root = import_roots.get(parts[0], parts[0]) if parts else ""
        # 危险调用与联网
        if isinstance(node.func, ast.Name) and node.func.id in _BANNED_BUILTINS:
            findings.append(_finding("banned_call", node.lineno, f"{node.func.id}(…)"))
        elif len(parts) >= 2 and (root, parts[-1]) in _BANNED_ATTRIBUTES:
            findings.append(_finding("banned_call", node.lineno, f"{dotted}(…)"))
        if len(parts) >= 2 and root in _NETWORK_MODULES:
            findings.append(_finding("network", node.lineno, f"{dotted}(…)"))
        # 随机性与种子
        if name in _SEED_CALLS and (node.args or node.keywords):
            seeded = True
        if any(keyword.arg in _SEED_KWARGS and keyword.value is not None for keyword in node.keywords):
            seeded = True
        if len(parts) >= 2 and root in _RANDOM_ATTRIBUTE_ROOTS and name in _RANDOM_ATTRIBUTES and name not in _SEED_CALLS:
            if "random" in parts or root in ("random", "torch"):
                uses_random = True
                random_lines.append(node.lineno)
        # 不带种子的生成器构造（default_rng() / RandomState()）= 用了随机性且没种子
        if name in ("default_rng", "RandomState", "Generator") and not (node.args or node.keywords):
            uses_random = True
            random_lines.append(node.lineno)
        # 文件读写
        if name == "open" and isinstance(node.func, ast.Name):
            literal = _literal_arg(node)
            if literal:
                path, _ = literal
                writing = bool(_WRITE_MODES.match(_open_mode(node)))
                findings.extend(_path_findings(path, node.lineno, profile, writing))
        elif name in _WRITE_CALLS:
            literal = _literal_arg(node)
            if literal:
                findings.extend(_path_findings(literal[0], node.lineno, profile, True))
        elif name in _READ_CALLS:
            literal = _literal_arg(node)
            if literal:
                findings.extend(_path_findings(literal[0], node.lineno, profile, False))
        # 伪指标行
        if isinstance(node.func, ast.Name) and node.func.id == "print" and node.args:
            head = node.args[0]
            text = None
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                text = head.value
            elif isinstance(head, ast.BinOp) and isinstance(head.left, ast.Constant) and isinstance(head.left.value, str):
                text = head.left.value
            elif isinstance(head, ast.JoinedStr) and head.values and isinstance(head.values[0], ast.Constant):
                text = str(head.values[0].value)
            if text is not None and text.lstrip().startswith("OMM_") and not text.lstrip().startswith(_METRICS_LINE_PREFIX):
                findings.append(_finding("fake_metrics_line", node.lineno, text.strip()[:60]))

    if profile.require_seed and uses_random and not seeded:
        line = min(random_lines) if random_lines else None
        findings.append(_finding("no_seed", line, "用了随机性（random / numpy.random / torch）但没有 seed(…) / random_state"))

    for local, lineno in imported.items():
        if local not in used_names and local != "*":
            findings.append(_finding("unused_import", lineno, f"import {local}"))

    # 去重（同一行同一规则同一细节只留一条），按行号排序
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in findings:
        unique.setdefault((item["id"], item["location"], item["detail"]), item)
    return sorted(unique.values(), key=lambda item: (int(re.sub(r"\D", "", item["location"]) or 0), item["id"]))


def _path_findings(path: str, lineno: int, profile: StaticCheckProfile, writing: bool) -> list[dict[str, Any]]:
    if _is_path_violation(path):
        return [_finding("abs_path", lineno, f"{'写' if writing else '读'} {path}")]
    if writing:
        if profile.allowed_write_prefixes and not _within(path, profile.allowed_write_prefixes):
            return [_finding("write_outside_whitelist", lineno, f"写 {path}（允许前缀：{'、'.join(profile.allowed_write_prefixes)}）")]
        return []
    allowed = tuple(dict.fromkeys(tuple(profile.allowed_read_prefixes) + tuple(profile.allowed_write_prefixes)))
    if allowed and not _within(path, allowed, profile.allowed_read_files):
        return [_finding("read_outside_whitelist", lineno, f"读 {path}（允许前缀：{'、'.join(allowed)}）")]
    return []


def major_findings(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in findings if item.get("severity") == "major"]


def static_material(findings: Sequence[Mapping[str, Any]] | None, profile: StaticCheckProfile | None = None) -> str:
    """审稿材料段：逐条发现（严重度 / 行号 / 规则 / 细节），最多列 _MATERIAL_LIMIT 条；未执行 / 0 项如实写。"""
    if findings is None:
        return "静态检查：未执行（本消费方未配置检查口径）。"
    if not findings:
        scope = f"（{profile.name}）" if profile else ""
        return f"静态检查{scope}：0 项发现（危险调用 / 联网 / 无种子随机 / 越界路径 / 读写白名单 / 未用导入 / 伪指标行均未触发）。"
    majors = sum(1 for item in findings if item.get("severity") == "major")
    lines = [f"静态检查：{len(findings)} 项发现（阻断 {majors} 项，提示 {len(findings) - majors} 项）。阻断项已作为驳回意见回沙盒修复；提示项供你参考。"]
    for item in list(findings)[:_MATERIAL_LIMIT]:
        where = f"{item.get('location')}：" if item.get("location") else ""
        lines.append(f"- [{item.get('severity')}] {where}{item.get('rule')}——{item.get('detail')}")
    if len(findings) > _MATERIAL_LIMIT:
        lines.append(f"- …（共 {len(findings)} 项）")
    return "\n".join(lines)


def major_static_feedback(findings: Sequence[Mapping[str, Any]]) -> str | None:
    """阻断项 → 喂给沙盒修复波的一段话（没有阻断项给 None）。"""
    majors = major_findings(findings)
    if not majors:
        return None
    lines = [f"静态检查有 {len(majors)} 项阻断性问题，必须修正后重新运行："]
    for item in majors:
        where = f"{item['location']}：" if item.get("location") else ""
        lines.append(f"- {where}{item['rule']}（{item['detail']}）")
    lines.append("修法：删除或替换被禁止的调用；随机性显式 seed（任务卡给的 random_seed）；文件只用工作区内的相对路径。其余逻辑不变。")
    return "\n".join(lines)
