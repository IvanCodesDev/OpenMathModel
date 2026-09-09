"""表格产物的前 N 行预览（数据页「原始数据」/「清洗后预览」的数据源）。

只做分隔符文本：解码按 utf-8-sig → gbk → latin-1 逐个尝试（latin-1 永不失败，作兜底），
分隔符用 ``csv.Sniffer`` 在前 64 KB 上嗅探（候选 ``, ; \\t |``），嗅探不出按后缀
（``.tsv`` → 制表符，其余逗号）。首行作表头，其后取 ``rows`` 行；单元格截到 200 字符；
数据行总数只在内容 ≤ 10 MB 时全量计数（更大的文件如实给 null）。纯函数，不碰 IO。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

__all__ = [
    "PREVIEW_MAX_ROWS",
    "PREVIEW_DEFAULT_ROWS",
    "TablePreview",
    "is_previewable_table",
    "parse_table_preview",
]

PREVIEW_DEFAULT_ROWS = 20
PREVIEW_MAX_ROWS = 200
_CELL_MAX_CHARS = 200
_SNIFF_BYTES = 64 * 1024
_COUNT_LIMIT_BYTES = 10 * 1024 * 1024
_ENCODINGS = ("utf-8-sig", "gbk", "latin-1")
_DELIMITERS = ",;\t|"
_TABLE_SUFFIXES = (".csv", ".tsv", ".txt")
_TEXT_MEDIA_PREFIXES = ("text/",)
_TEXT_MEDIA_TYPES = frozenset({
    "application/csv",
    "application/vnd.ms-excel",  # Windows mimetypes 给 .csv 的登记值
    "application/octet-stream",
})


@dataclass(frozen=True)
class TablePreview:
    encoding: str
    delimiter: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    row_count: int | None = None
    truncated: bool = False


def is_previewable_table(kind: str | None, name: str | None, media_type: str | None) -> bool:
    """只认表格产物：登记 kind=table，或文件名是分隔符文本后缀且媒体类型是文本类。"""
    lowered = str(name or "").lower()
    media = str(media_type or "").lower()
    if str(kind or "") == "table":
        return True
    if not lowered.endswith(_TABLE_SUFFIXES):
        return False
    return media.startswith(_TEXT_MEDIA_PREFIXES) or media in _TEXT_MEDIA_TYPES or not media


def _decode(content: bytes) -> tuple[str, str]:
    for encoding in _ENCODINGS[:-1]:
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return content.decode(_ENCODINGS[-1]), _ENCODINGS[-1]


def _sniff_delimiter(sample: str, name: str | None) -> str:
    if sample.strip():
        try:
            return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
        except csv.Error:
            pass
    return "\t" if str(name or "").lower().endswith(".tsv") else ","


def _clip(cell: str) -> str:
    return cell if len(cell) <= _CELL_MAX_CHARS else cell[: _CELL_MAX_CHARS - 1] + "…"


def parse_table_preview(content: bytes, name: str | None = None, rows: int = PREVIEW_DEFAULT_ROWS) -> TablePreview:
    """字节 → 预览。``rows`` 夹到 [1, PREVIEW_MAX_ROWS]。空文件 → 空表头空行、row_count 0。"""
    limit = max(1, min(int(rows), PREVIEW_MAX_ROWS))
    text, encoding = _decode(content)
    delimiter = _sniff_delimiter(text[:_SNIFF_BYTES], name)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    columns: list[str] = []
    preview: list[list[str]] = []
    data_rows = 0
    count_all = len(content) <= _COUNT_LIMIT_BYTES
    truncated = False
    for index, record in enumerate(reader):
        if index == 0:
            columns = [_clip(cell.strip()) for cell in record]
            continue
        if not any(cell.strip() for cell in record):
            continue
        data_rows += 1
        if len(preview) < limit:
            # 行宽对齐表头：短行补空、长行截到表头宽度（多余列并入最后一格方便看见）
            cells = [_clip(cell) for cell in record]
            if columns and len(cells) > len(columns):
                cells = cells[: len(columns) - 1] + [_clip(delimiter.join(cells[len(columns) - 1 :]))]
            if columns and len(cells) < len(columns):
                cells = cells + [""] * (len(columns) - len(cells))
            preview.append(cells)
        elif not count_all:
            truncated = True
            break
    if count_all:
        truncated = data_rows > len(preview)
        row_count: int | None = data_rows
    else:
        row_count = None
    return TablePreview(
        encoding=encoding,
        delimiter=delimiter,
        columns=columns,
        rows=preview,
        row_count=row_count,
        truncated=truncated,
    )
