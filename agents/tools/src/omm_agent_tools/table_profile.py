"""table_profile：表格数据的确定性画像工具（设计 §9.1 数据阶段，H2）。

数字纪律的源头之一：画像里的每个数字（行数/缺失/极值/均值）都由本工具的
确定性代码统计产出，LLM 只负责**判读**画像（质量与就绪度、清洗计划），
永不自行编造统计值（§1.3 原则 5）。

实现口径（MVP，stdlib-only）：
- 只处理 UTF-8 CSV（分隔符在逗号/分号/制表符里嗅探）；Excel 等富格式归
  附件解析链（ADR-0010），不在此重复造轮子；
- 行数上限保护：超限截断统计并如实标注 truncated——画像是判读依据，
  五万行的统计已足够代表性，读满十亿行只会拖垮 tick；
- 类型推断按整列成功解析计（int ⊂ float ⊂ str），空串计缺失。
"""

from __future__ import annotations

import csv
import io
from typing import Any

from omm_agent_core import ToolResult

from .registry import ToolCallContext, ToolSpec
from .workspace import TaskWorkspace, WorkspaceViolation

__all__ = ["PROFILE_MAX_ROWS", "profile_csv_text", "table_profile_spec"]

#: 统计的行数上限（超出部分不读，truncated=true）。
PROFILE_MAX_ROWS = 50_000

#: 单元格样例的截断长度（画像附带首行样例帮助判读列含义）。
_SAMPLE_CHARS = 80


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return ","


def _parse_number(value: str) -> float | None:
    text = value.strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def profile_csv_text(text: str, *, max_rows: int = PROFILE_MAX_ROWS) -> dict[str, Any]:
    """CSV 文本 → 确定性画像 dict（纯函数，同输入同输出）。"""
    delimiter = _sniff_delimiter(text[:4096])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return {"rows": 0, "columns": [], "delimiter": delimiter, "truncated": False}

    names = [name.strip() or f"col_{index + 1}" for index, name in enumerate(header)]
    width = len(names)
    missing = [0] * width
    numeric = [True] * width
    integer = [True] * width
    total = [0.0] * width
    count = [0] * width
    minimum: list[float | None] = [None] * width
    maximum: list[float | None] = [None] * width
    sample: list[str] = []

    rows = 0
    truncated = False
    for record in reader:
        if rows >= max_rows:
            truncated = True
            break
        rows += 1
        if rows == 1:
            sample = [str(cell)[:_SAMPLE_CHARS] for cell in record[:width]]
        for index in range(width):
            cell = record[index].strip() if index < len(record) else ""
            if not cell:
                missing[index] += 1
                continue
            number = _parse_number(cell)
            if number is None:
                numeric[index] = False
                continue
            if not number.is_integer():
                integer[index] = False
            count[index] += 1
            total[index] += number
            minimum[index] = number if minimum[index] is None else min(minimum[index], number)
            maximum[index] = number if maximum[index] is None else max(maximum[index], number)

    columns: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        is_numeric = numeric[index] and count[index] > 0
        column: dict[str, Any] = {
            "name": name,
            "type": ("int" if integer[index] else "float") if is_numeric else "str",
            "missing": missing[index],
        }
        if index < len(sample):
            column["sample"] = sample[index]
        if is_numeric:
            column["min"] = minimum[index]
            column["max"] = maximum[index]
            column["mean"] = round(total[index] / count[index], 4)
        columns.append(column)

    return {
        "rows": rows,
        "columns": columns,
        "delimiter": delimiter,
        "truncated": truncated,
    }


def table_profile_spec(workspace: TaskWorkspace) -> ToolSpec:
    """注册用 spec：读工作区内的 CSV 产出画像（tier=readonly）。"""

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
                status="failed", error=f"文件不是 UTF-8 文本，无法按 CSV 画像：{path}"
            )
        profile = profile_csv_text(text)
        profile["path"] = path
        return ToolResult(status="succeeded", output=profile)

    return ToolSpec(
        name="table_profile",
        description=(
            "对工作区内的 CSV 文件做确定性画像：行数、各列类型/缺失数/数值统计"
            f"（min/max/mean）与首行样例；至多统计 {PROFILE_MAX_ROWS} 行（超限如实标注）。"
            "画像数字由代码统计产出，判读时不得改写。"
        ),
        handler=handler,
        risk="low",
        timeout_s=60.0,
        required_args=("path",),
        tier="readonly",
    )
