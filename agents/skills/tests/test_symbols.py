"""符号一致性的代码侧核验（symbols.py）：记号规范化、脚本标识符收集、三级匹配、材料与警告。"""

from __future__ import annotations

import pytest

from omm_agent_skills import (
    check_symbols,
    script_identifiers,
    symbol_candidates,
    symbol_check_material,
    symbol_check_warning,
)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("$x_{ij}$", [("x_ij", "exact"), ("xij", "variant"), ("x", "base")]),
        ("x_i", [("x_i", "exact"), ("xi", "variant"), ("x", "base")]),
        ("\\(c_i\\)", [("c_i", "exact"), ("ci", "variant"), ("c", "base")]),
        ("\\lambda", [("lambda_", "exact"), ("lam", "variant"), ("lmbda", "variant"), ("lamb", "variant")]),
        ("λ_t", [("lambda__t", "exact"), ("lambda_t", "variant"), ("lam_t", "variant"), ("lamt", "variant"),
                 ("lmbda_t", "variant"), ("lmbdat", "variant"), ("lamb_t", "variant"), ("lambt", "variant"),
                 ("lambda_", "base"), ("lam", "base"), ("lmbda", "base"), ("lamb", "base")]),
        ("\\alpha_{t}", [("alpha_t", "exact"), ("alphat", "variant"), ("alpha", "base")]),
        ("N", [("N", "exact")]),
        ("c_{max}", [("c_max", "exact"), ("cmax", "variant"), ("c", "base")]),
        ("\\mathbf{x}^{k}", [("x_k", "exact"), ("xk", "variant"), ("x", "base")]),
        ("i \\in \\mathcal{I}", [("i", "exact")]),
        ("x_i = 1", [("x_i", "exact"), ("xi", "variant"), ("x", "base")]),
        ("", []),
        ("$$", []),
        ("3x", []),
    ],
)
def test_symbol_candidates_normalize_latex_greek_and_scripts(symbol, expected):
    assert symbol_candidates(symbol) == expected


def test_script_identifiers_collect_bindings_but_not_imports():
    code = (
        "import numpy as np\n"
        "from math import sqrt\n"
        "N = 10\n"
        "x_ij, alloc = [], {}\n"
        "for i in range(N):\n"
        "    lam = 0.5\n"
        "squares = [k * k for k in range(N)]\n"
        "def solve(demand, *args, **kwargs):\n"
        "    total: float = 0\n"
        "    total += demand\n"
        "    return total\n"
        "class Model:\n"
        "    def fit(self, c_i):\n"
        "        self.cost = c_i\n"
        "print(sqrt(N))\n"
    )
    names = script_identifiers(code)
    assert {"N", "x_ij", "alloc", "i", "lam", "squares", "k", "solve", "demand", "total", "Model", "fit", "c_i", "cost"} <= names
    # import 名、惯用名不算模型量
    assert not {"np", "sqrt", "self", "args", "kwargs"} & names


def test_script_identifiers_raise_on_syntax_error():
    with pytest.raises(SyntaxError):
        script_identifiers("def broken(:\n    pass\n")


SYMBOL_ROWS = [
    {"symbol": "$x_i$", "definition": "是否开店"},
    {"symbol": "\\(c_i\\)", "definition": "开店成本"},
    {"symbol": "\\lambda", "definition": "到达率"},
    {"symbol": "N", "definition": "候选点数"},
    {"symbol": "c_{max}", "definition": "成本上限"},
    {"symbol": "z", "definition": "总利润"},
]


def test_check_symbols_matches_by_three_levels_and_lists_unmapped():
    code = (
        "N = 5\n"
        "x_i = [0] * N\n"          # 精确
        "CI = [1.0] * N\n"         # 变体：大小写 + 去下划线
        "lam = 0.7\n"              # 变体：希腊字母拼法（首拼法 lambda_ 未出现）
        "c = 12.0\n"               # 仅主体：c_{max} 只对上 c
        "profit = sum(x_i)\n"      # 未对应到任何记号
        "print(profit, CI, lam, c)\n"
    )
    result = check_symbols(SYMBOL_ROWS, code)
    assert result["covered"] == [
        {"symbol": "$x_i$", "variable": "x_i", "match": "exact"},
        {"symbol": "\\(c_i\\)", "variable": "CI", "match": "variant"},
        {"symbol": "\\lambda", "variable": "lam", "match": "variant"},
        {"symbol": "N", "variable": "N", "match": "exact"},
        {"symbol": "c_{max}", "variable": "c", "match": "base"},
    ]
    assert result["missing"] == ["z"]
    assert result["unmapped_variables"] == ["profit"]
    assert result["coverage"] == round(5 / 6, 3)
    assert "error" not in result


def test_check_symbols_prefers_exact_over_variant_and_ignores_empty_rows():
    code = "xi = 1\nx_i = 2\nlambda_ = 3\n"
    rows = [{"symbol": "x_i"}, {"symbol": "\\lambda"}, {"symbol": ""}, "not a mapping", {"definition": "无记号"}]
    result = check_symbols(rows, code)
    assert [(item["symbol"], item["variable"], item["match"]) for item in result["covered"]] == [
        ("x_i", "x_i", "exact"),
        ("\\lambda", "lambda_", "exact"),
    ]
    assert result["missing"] == [] and result["coverage"] == 1.0
    assert result["unmapped_variables"] == ["xi"]


def test_check_symbols_without_symbol_table_is_empty_and_uncounted():
    result = check_symbols([], "a = 1\n")
    assert result == {"covered": [], "missing": [], "unmapped_variables": ["a"], "coverage": None}
    assert symbol_check_warning(result) is None
    assert symbol_check_material(result) == "符号对照（代码侧核验）：方案阶段未生成符号表，无可对照。"


def test_check_symbols_tolerates_syntax_errors_without_raising():
    result = check_symbols(SYMBOL_ROWS[:2], "def broken(:\n")
    assert result["covered"] == [] and result["missing"] == ["$x_i$", "\\(c_i\\)"]
    assert result["coverage"] == 0.0 and result["error"].startswith("脚本无法解析：")
    assert symbol_check_warning(result).startswith("符号一致性核验未能进行（脚本无法解析：")
    assert "2 条记号均未能对照" in symbol_check_material(result)


def test_material_and_warning_are_readable_and_bounded():
    code = "x_i = 1\n" + "".join(f"extra_{n} = {n}\n" for n in range(45))
    result = check_symbols(SYMBOL_ROWS, code)
    material = symbol_check_material(result)
    assert material.startswith("符号对照（代码侧核验，确定性）：符号表 6 条记号，脚本变量命中 1 条（精确 1 / 变体 0 / 仅主体 0）。")
    assert "- 命中：$x_i$ → x_i（精确）" in material
    assert "- 未命中（脚本里找不到对应变量）：\\(c_i\\)、\\lambda、N、c_{max}、z" in material
    assert "…（共 40 个）" in material, "未对应变量最多列 20 个并给总数（收集本身截到 40）"
    assert symbol_check_warning(result) == (
        "符号表 6 条记号中 5 条在实验脚本里找不到对应变量（\\(c_i\\)、\\lambda、N、c_{max}、z）"
    )
    six = check_symbols(SYMBOL_ROWS + [{"symbol": "\\mu"}], "a = 1\n")
    assert symbol_check_warning(six).endswith("（$x_i$、\\(c_i\\)、\\lambda、N、c_{max}…）")
