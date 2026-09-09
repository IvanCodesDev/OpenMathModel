"""符号一致性的代码侧核验：实验脚本变量名 ↔ 方案符号表（§9.1「同一符号贯穿」，H3 待做项）。

方案阶段的符号表是全文记号的底稿，论文以它为准；实验沙盒写出的脚本用什么变量名
今天没人核——论文里的 $x_{ij}$ 与代码里的 ``alloc`` 是不是同一个量，只能靠人猜。
这里做**确定性**核验：``ast`` 解析通过验收的脚本收集绑定过的标识符，把每条记号规范化成
候选标识符，按三级匹配（精确 / 变体 / 仅主体）对账。结果进节点产出、审稿材料与警告，
不改脚本、不改符号表、不硬阻断——脚本与记号对不上是审稿人与人裁的事实依据，不是失败。

标识符收集只有 Python ``ast`` 版：实现语言不是 python 时（§7.4 多语言 Runner）
``check_symbols`` 返回 ``skipped`` 结果——记号全部既不算命中也不算未命中，材料与警告如实写
「未执行」（R-C 纪律），不把「没查」写成「全对」或「全错」。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "MATCH_LEVELS",
    "SYMBOL_CHECK_LANGUAGES",
    "check_symbols",
    "script_identifiers",
    "symbol_candidates",
    "symbol_check_material",
    "symbol_check_warning",
]

#: 有代码侧核验规则的实现语言（契约小写标识）；其余语言的收集器随 H7 各 Runner 落地再补。
SYMBOL_CHECK_LANGUAGES: frozenset[str] = frozenset({"python"})

#: 匹配等级（从强到弱）：exact = 记号规范化后与变量名逐字相同；variant = 大小写 / 下标连接
#: 方式 / 希腊字母拼写的变体；base = 只有主体字母对上（``x_{ij}`` ↔ ``x``），最弱。
MATCH_LEVELS = ("exact", "variant", "base")

#: 希腊字母 → 代码里常见拼法（首项是 LaTeX 名本身）。``lambda`` 是关键字，代码只能写变体。
_GREEK_VARIANTS: dict[str, tuple[str, ...]] = {
    "alpha": ("alpha",),
    "beta": ("beta",),
    "gamma": ("gamma",),
    "delta": ("delta",),
    "epsilon": ("epsilon", "eps"),
    "varepsilon": ("epsilon", "eps"),
    "zeta": ("zeta",),
    "eta": ("eta",),
    "theta": ("theta",),
    "iota": ("iota",),
    "kappa": ("kappa",),
    "lambda": ("lambda_", "lam", "lmbda", "lamb"),
    "mu": ("mu",),
    "nu": ("nu",),
    "xi": ("xi",),
    "pi": ("pi",),
    "rho": ("rho",),
    "sigma": ("sigma",),
    "tau": ("tau",),
    "upsilon": ("upsilon",),
    "phi": ("phi", "varphi"),
    "varphi": ("phi", "varphi"),
    "chi": ("chi",),
    "psi": ("psi",),
    "omega": ("omega",),
}
_GREEK_UNICODE = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "ζ": "zeta",
    "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu",
    "ν": "nu", "ξ": "xi", "π": "pi", "ρ": "rho", "σ": "sigma", "τ": "tau",
    "υ": "upsilon", "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
    "Γ": "gamma", "Δ": "delta", "Θ": "theta", "Λ": "lambda", "Ξ": "xi", "Π": "pi",
    "Σ": "sigma", "Φ": "phi", "Ψ": "psi", "Ω": "omega",
}
#: 只改样式、不改含义的 LaTeX 命令与定界符：抹掉后剩下的才是记号本体。
_STYLE_COMMANDS = re.compile(
    r"\\(?:mathrm|mathbf|mathit|mathcal|mathbb|boldsymbol|bm|hat|bar|tilde|vec|dot|text|operatorname|widehat|overline)"
)
_DELIMITERS = re.compile(r"\$|\\\(|\\\)|\\\[|\\\]")
#: 记号本体：主体（字母 / 希腊字母命令）+ 任意多段下标 ``_x`` / ``_{...}`` 与上标 ``^x`` / ``^{...}``。
_SYMBOL_BODY = re.compile(
    r"^(?P<base>\\?[A-Za-z][A-Za-z0-9]*)(?P<rest>(?:[_^](?:\{[^{}]*\}|[A-Za-z0-9]))*)$"
)
_SCRIPT_PART = re.compile(r"[_^](\{[^{}]*\}|[A-Za-z0-9])")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PY_KEYWORDS = frozenset({
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class",
    "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
    "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return",
    "try", "while", "with", "yield",
})
#: 脚本里这些名字不算「模型量」的绑定：入口惯用名、临时名、模块名。
_IGNORED_IDENTIFIERS = frozenset({"_", "__name__", "self", "cls", "main", "args", "kwargs"})
#: 材料 / 警告里最多点名多少个未对应的脚本变量与未命中的记号。
_MATERIAL_UNMAPPED_LIMIT = 20
_WARNING_MISSING_LIMIT = 5


def _normalize_symbol(symbol: str) -> str:
    """去定界符 / 样式命令 / 空白，把 Unicode 希腊字母换成 ``\\name``，方便统一解析。"""
    text = _DELIMITERS.sub("", str(symbol or ""))
    text = _STYLE_COMMANDS.sub("", text)
    text = "".join(f"\\{_GREEK_UNICODE[ch]}" if ch in _GREEK_UNICODE else ch for ch in text)
    return re.sub(r"\s+", "", text)


def _base_variants(base: str) -> list[str]:
    """主体的拼法变体：希腊字母命令给常见代码拼法，普通字母原样。"""
    if base.startswith("\\"):
        name = base[1:]
        variants = _GREEK_VARIANTS.get(name.lower())
        if variants:
            return list(variants)
        return [name]
    return [base]


def symbol_candidates(symbol: str) -> list[tuple[str, str]]:
    """记号 → ``[(候选标识符, 匹配等级)]``，按等级从强到弱、去重、只留合法 Python 标识符。

    ``x_{ij}`` → ``x_ij``（exact）、``xij`` / ``x_i_j`` / ``X_IJ``（variant，大小写在匹配时忽略）、``x``（base）；
    ``\\lambda`` → ``lambda_`` / ``lam`` / ``lmbda`` / ``lamb``（exact 与 variant 共用同一批拼法）；
    ``i \\in \\mathcal{I}`` 这类带关系符的行不是单个记号，只取第一个记号本体。
    """
    normalized = _normalize_symbol(symbol)
    if not normalized:
        return []
    # 「i \in I」「x_i = 1」：取关系符 / 等号 / 逗号前的第一段；样式命令抹掉后残留的
    # 花括号（``\mathbf{x}^k`` → ``{x}^k``）只包着主体，剥掉
    head = re.split(r"\\in|\\le|\\ge|\\to|[=<>,;:∈≤≥]", normalized, maxsplit=1)[0]
    head = re.sub(r"^\{([^{}]+)\}", r"\1", head)
    match = _SYMBOL_BODY.match(head)
    if match is None:
        return []
    bases = _base_variants(match.group("base"))
    parts = [part.strip("{}") for part in _SCRIPT_PART.findall(match.group("rest"))]
    parts = [re.sub(r"[^A-Za-z0-9]", "", part) for part in parts]
    parts = [part for part in parts if part]

    candidates: list[tuple[str, str]] = []

    def add(name: str, level: str) -> None:
        if not name or not _IDENTIFIER.match(name) or name in _PY_KEYWORDS:
            return
        if all(existing != name for existing, _ in candidates):
            candidates.append((name, level))

    for index, base in enumerate(bases):
        level = "exact" if index == 0 else "variant"
        if parts:
            add(f"{base}_{''.join(parts)}", level)
            add(f"{base}{''.join(parts)}", "variant")
            add(f"{base}_{'_'.join(parts)}", "variant")
        else:
            add(base, level)
    if parts:
        for base in bases:
            add(base, "base")
    return candidates


def script_identifiers(code: str) -> set[str]:
    """脚本里被**绑定**过的标识符：赋值目标 / 增量赋值 / 带注解赋值 / 形参 / 循环与推导式目标 /
    函数与类名 / ``self.x`` 一类属性目标。import 名不算（那是库，不是模型量）。语法错误直接抛
    ``SyntaxError``，由调用方决定怎么记。"""
    tree = ast.parse(code)
    imported: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".", 1)[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return {name for name in names - imported if name not in _IGNORED_IDENTIFIERS}


def _match(symbol: str, identifiers: set[str], lowered: Mapping[str, str]) -> tuple[str, str] | None:
    """按等级顺序找第一个命中的脚本变量：exact 逐字，variant / base 忽略大小写。"""
    for candidate, level in symbol_candidates(symbol):
        if level == "exact" and candidate in identifiers:
            return candidate, "exact"
        hit = lowered.get(candidate.lower())
        if hit is not None:
            return hit, ("variant" if level in ("exact", "variant") else "base")
    return None


def check_symbols(
    rows: Sequence[Mapping[str, Any]], code: str, language: str = "python"
) -> dict[str, Any]:
    """符号表 × 脚本 → ``{covered, missing, unmapped_variables, coverage, error?, skipped?}``。

    ``covered`` = ``[{symbol, variable, match}]``；``missing`` = 找不到对应变量的记号；
    ``unmapped_variables`` = 脚本里没有对应到任何记号的绑定名（排序、截断）；``coverage`` =
    命中 / 记号数，符号表为空时 None。脚本解析失败不抛：全空 + ``error``。

    ``language`` 是任务卡的实现语言：不在 :data:`SYMBOL_CHECK_LANGUAGES` 里 → 不解析，
    ``skipped`` 带原因、``symbols_total`` 记有多少条记号没对照，``coverage`` 为 None。
    """
    symbols = [str(row.get("symbol") or "").strip() for row in rows if isinstance(row, Mapping)]
    symbols = [symbol for symbol in symbols if symbol]
    result: dict[str, Any] = {
        "covered": [],
        "missing": [],
        "unmapped_variables": [],
        "coverage": None,
    }
    normalized = str(language or "python").strip().lower() or "python"
    if normalized not in SYMBOL_CHECK_LANGUAGES:
        result["skipped"] = (
            f"实现语言 {normalized} 尚无代码侧核验规则（当前只有 Python ast 版）；本项未执行"
        )
        result["symbols_total"] = len(symbols)
        return result
    try:
        identifiers = script_identifiers(code or "")
    except SyntaxError as exc:
        result["error"] = f"脚本无法解析：{exc.msg}（第 {exc.lineno} 行）"
        result["missing"] = list(symbols)
        result["coverage"] = 0.0 if symbols else None
        return result
    lowered: dict[str, str] = {}
    for name in sorted(identifiers):
        lowered.setdefault(name.lower(), name)
    used: set[str] = set()
    for symbol in symbols:
        hit = _match(symbol, identifiers, lowered)
        if hit is None:
            result["missing"].append(symbol)
            continue
        variable, level = hit
        used.add(variable)
        result["covered"].append({"symbol": symbol, "variable": variable, "match": level})
    result["unmapped_variables"] = sorted(identifiers - used)[:_MATERIAL_UNMAPPED_LIMIT * 2]
    if symbols:
        result["coverage"] = round(len(result["covered"]) / len(symbols), 3)
    return result


def symbol_check_material(result: Mapping[str, Any]) -> str:
    """审稿材料段：命中 / 未命中 / 脚本里多出来的变量，供审稿人判断脚本是否忠实于方案记号。"""
    if result.get("skipped"):
        total = int(result.get("symbols_total") or 0)
        if total == 0:
            return "符号对照（代码侧核验）：方案阶段未生成符号表，无可对照。"
        return (
            f"符号对照（代码侧核验）：未执行——{result['skipped']}；符号表 {total} 条记号"
            "请你按脚本变量名人工核对。"
        )
    covered = list(result.get("covered") or [])
    missing = list(result.get("missing") or [])
    total = len(covered) + len(missing)
    if total == 0:
        return "符号对照（代码侧核验）：方案阶段未生成符号表，无可对照。"
    if result.get("error"):
        return f"符号对照（代码侧核验）：{result['error']}，{total} 条记号均未能对照。"
    by_level = {level: sum(1 for item in covered if item.get("match") == level) for level in MATCH_LEVELS}
    lines = [
        f"符号对照（代码侧核验，确定性）：符号表 {total} 条记号，脚本变量命中 {len(covered)} 条"
        f"（精确 {by_level['exact']} / 变体 {by_level['variant']} / 仅主体 {by_level['base']}）。"
    ]
    if covered:
        lines.append("- 命中：" + "；".join(
            f"{item['symbol']} → {item['variable']}（{_LEVEL_LABELS[item['match']]}）" for item in covered
        ))
    if missing:
        lines.append("- 未命中（脚本里找不到对应变量）：" + "、".join(missing))
    unmapped = list(result.get("unmapped_variables") or [])
    if unmapped:
        shown = unmapped[:_MATERIAL_UNMAPPED_LIMIT]
        more = f"…（共 {len(unmapped)} 个）" if len(unmapped) > len(shown) else ""
        lines.append(f"- 脚本里未对应到符号表的变量：{', '.join(shown)}{more}")
    return "\n".join(lines)


def symbol_check_warning(result: Mapping[str, Any]) -> str | None:
    """质量警告（只在有未命中记号、或有记号却没能核验时给）：可对账的一句中文。"""
    if result.get("skipped"):
        total = int(result.get("symbols_total") or 0)
        if total == 0:
            return None
        return f"符号一致性核验未执行（{result['skipped']}），符号表 {total} 条记号未对照"
    missing = list(result.get("missing") or [])
    if not missing:
        return None
    total = len(missing) + len(result.get("covered") or [])
    shown = "、".join(missing[:_WARNING_MISSING_LIMIT])
    more = "…" if len(missing) > _WARNING_MISSING_LIMIT else ""
    if result.get("error"):
        return f"符号一致性核验未能进行（{result['error']}），符号表 {total} 条记号无法对照"
    return f"符号表 {total} 条记号中 {len(missing)} 条在实验脚本里找不到对应变量（{shown}{more}）"


_LEVEL_LABELS = {"exact": "精确", "variant": "变体", "base": "仅主体"}
