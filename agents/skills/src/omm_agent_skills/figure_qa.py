"""渲染后图件 QA（确定性、轻量）：文件在场之外再看一眼「像不像一张能进论文的图」。

figure_render 的渲染验证今天只认「规划文件在工作区 + 采集为 ≥ 256 B 的 figure 产物」——一张
1 × 1 像素、整幅空白、或只有坐标框没有数据的 PNG 照样能过。这里借 nature-figure「渲染后必须
二次核验」的立场，做几项**不烧模型、可复现**的判定：文件头是否可读、像素尺寸够不够论文级、
宽高比是否离谱、位图是不是（近乎）空白、SVG 里有没有绘图元素与文字。结论进渲染验证与审稿材料，
不判美观、不判「画得对不对」——那是审稿人与人的事（视觉判读另议）。

依赖纪律：文件头解析全是标准库；像素级「空白」判定用 Pillow（matplotlib 环境必带），缺席时
如实标「未评估」而不是装作看过。
"""

from __future__ import annotations

import io
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

__all__ = [
    "MIN_RASTER_HEIGHT",
    "MIN_RASTER_WIDTH",
    "FigureCheck",
    "FigureInspection",
    "figure_qa_material",
    "inspect_figure",
]

#: 论文级位图的像素下限：单栏图 3.5 英寸 @ 300 dpi ≈ 1050 px 宽；这里放宽到 600 × 400
#: （≈ 2 × 1.3 英寸 @ 300 dpi，或 6 × 4 英寸 @ 100 dpi 的旧习惯）以下才算不够。
MIN_RASTER_WIDTH = 600
MIN_RASTER_HEIGHT = 400
#: 宽高比合理区间（横向长条 4:1 到竖向 1:4）。
_ASPECT_MIN, _ASPECT_MAX = 0.25, 4.0
#: 位图「非空白」下限：与主色不同的像素占比。一张 1950 × 1200 的折线图连坐标轴与文字通常 ≥ 1%。
_MIN_INK_RATIO = 0.002
#: 采样上限：大图先等比缩到不超过这么多像素再数（判空白不需要全分辨率）。
_SAMPLE_PIXELS = 400_000
#: SVG 绘图元素（去命名空间后的标签名）。
_SVG_MARKS = {"path", "line", "rect", "circle", "ellipse", "polyline", "polygon", "use", "image"}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class FigureCheck:
    id: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class FigureInspection:
    """一张图件的确定性事实 + 判定；``status`` 三态：ok / suspect（有判定未过）/ unreadable。"""

    name: str
    format: str  # png / jpeg / gif / svg / unknown
    width: int | None
    height: int | None
    dpi: float | None
    ink_ratio: float | None  # 位图非主色像素占比；未评估为 None
    text_count: int | None  # SVG 文本元素数；非 SVG 为 None
    mark_count: int | None  # SVG 绘图元素数；非 SVG 为 None
    checks: tuple[FigureCheck, ...]
    note: str = ""  # 未评估项的原因（如缺 Pillow / 解码失败）

    @property
    def readable(self) -> bool:
        return self.format != "unknown"

    @property
    def status(self) -> str:
        if not self.readable:
            return "unreadable"
        return "ok" if all(check.passed for check in self.checks) else "suspect"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "dpi": self.dpi,
            "ink_ratio": self.ink_ratio,
            "text_count": self.text_count,
            "mark_count": self.mark_count,
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
            "note": self.note,
        }


# -- 文件头 -------------------------------------------------------------------------


def _png_header(content: bytes) -> tuple[int, int, float | None] | None:
    if not content.startswith(_PNG_SIGNATURE) or len(content) < 33:
        return None
    length, kind = struct.unpack(">I4s", content[8:16])
    if kind != b"IHDR" or length != 13:
        return None
    width, height = struct.unpack(">II", content[16:24])
    dpi: float | None = None
    offset = 8
    while offset + 8 <= len(content):
        chunk_len, chunk_type = struct.unpack(">I4s", content[offset : offset + 8])
        data_start = offset + 8
        if chunk_type == b"pHYs" and chunk_len == 9 and data_start + 9 <= len(content):
            ppu_x, _ppu_y, unit = struct.unpack(">IIB", content[data_start : data_start + 9])
            if unit == 1 and ppu_x > 0:
                dpi = round(ppu_x * 0.0254, 1)
            break
        if chunk_type in (b"IDAT", b"IEND"):
            break
        offset = data_start + chunk_len + 4
    return width, height, dpi


def _jpeg_size(content: bytes) -> tuple[int, int] | None:
    if not content.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 9 <= len(content):
        if content[offset] != 0xFF:
            offset += 1
            continue
        marker = content[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        segment_len = struct.unpack(">H", content[offset + 2 : offset + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", content[offset + 5 : offset + 9])
            return width, height
        offset += 2 + segment_len
    return None


def _gif_size(content: bytes) -> tuple[int, int] | None:
    if content[:6] not in (b"GIF87a", b"GIF89a") or len(content) < 10:
        return None
    width, height = struct.unpack("<HH", content[6:10])
    return width, height


def _svg_facts(content: bytes) -> tuple[int | None, int | None, int, int] | None:
    """(width, height, text_count, mark_count)；不是 SVG / 解析失败 → None。"""
    head = content[:512].lstrip()
    if b"<svg" not in head and b"<?xml" not in head:
        return None
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    if not root.tag.endswith("svg"):
        return None
    width = _svg_length(root.get("width"))
    height = _svg_length(root.get("height"))
    view_box = (root.get("viewBox") or "").replace(",", " ").split()
    if (width is None or height is None) and len(view_box) == 4:
        try:
            width = width or int(float(view_box[2]))
            height = height or int(float(view_box[3]))
        except ValueError:
            pass
    text_count = 0
    mark_count = 0
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "text":
            text_count += 1
        elif tag in _SVG_MARKS:
            mark_count += 1
    return width, height, text_count, mark_count


def _svg_length(value: str | None) -> int | None:
    if not value:
        return None
    digits = ""
    for char in value.strip():
        if char.isdigit() or char in ".-":
            digits += char
        else:
            break
    try:
        return int(float(digits)) if digits else None
    except ValueError:
        return None


# -- 像素 ---------------------------------------------------------------------------


def _ink_ratio(content: bytes) -> tuple[float | None, str]:
    """非主色像素占比；(None, 原因) 表示未评估。用 Pillow 解码，缺席 / 失败如实说。"""
    try:
        from PIL import Image
    except ImportError:
        return None, "未评估非空白：环境无 Pillow"
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            picture = image.convert("RGB")
            pixels = picture.width * picture.height
            if pixels > _SAMPLE_PIXELS:
                scale = (_SAMPLE_PIXELS / pixels) ** 0.5
                size = (max(1, int(picture.width * scale)), max(1, int(picture.height * scale)))
                picture = picture.resize(size)
            colors = picture.getcolors(maxcolors=picture.width * picture.height)
    except Exception as exc:  # 任何解码问题都只是「未评估」，不让 QA 崩掉渲染验证
        return None, f"未评估非空白：解码失败（{type(exc).__name__}）"
    if not colors:
        return None, "未评估非空白：像素统计为空"
    total = sum(count for count, _ in colors)
    dominant = max(count for count, _ in colors)
    return round(1.0 - dominant / total, 4), ""


# -- 判定 ---------------------------------------------------------------------------


def inspect_figure(name: str, content: bytes) -> FigureInspection:
    """一张图件字节 → 事实 + 判定。永不抛：读不出格式就是 ``unreadable``。"""
    width: int | None = None
    height: int | None = None
    dpi: float | None = None
    text_count: int | None = None
    mark_count: int | None = None
    ink: float | None = None
    note = ""
    checks: list[FigureCheck] = []

    png = _png_header(content)
    jpeg = _jpeg_size(content) if png is None else None
    gif = _gif_size(content) if png is None and jpeg is None else None
    svg = _svg_facts(content) if png is None and jpeg is None and gif is None else None

    if png is not None:
        fmt = "png"
        width, height, dpi = png
    elif jpeg is not None:
        fmt = "jpeg"
        width, height = jpeg
    elif gif is not None:
        fmt = "gif"
        width, height = gif
    elif svg is not None:
        fmt = "svg"
        width, height, text_count, mark_count = svg
    else:
        return FigureInspection(
            name=name, format="unknown", width=None, height=None, dpi=None, ink_ratio=None,
            text_count=None, mark_count=None, checks=(), note="不是可识别的 PNG / JPEG / GIF / SVG",
        )

    raster = fmt in ("png", "jpeg", "gif")
    if raster:
        big_enough = (width or 0) >= MIN_RASTER_WIDTH and (height or 0) >= MIN_RASTER_HEIGHT
        floor = f"{MIN_RASTER_WIDTH} × {MIN_RASTER_HEIGHT}"
        why = "" if big_enough else f"（论文级至少 {floor}，按 300 dpi 即约 2 × 1.3 英寸）"
        checks.append(FigureCheck("min_size", big_enough, f"{width} × {height} px{why}"))
    if width and height:
        aspect = width / height
        sane = _ASPECT_MIN <= aspect <= _ASPECT_MAX
        why = "" if sane else "（超出 1:4 ~ 4:1）"
        checks.append(FigureCheck("aspect_sane", sane, f"宽高比 {aspect:.2f}{why}"))
    if raster:
        ink, ink_note = _ink_ratio(content)
        if ink is None:
            note = ink_note
        else:
            not_blank = ink >= _MIN_INK_RATIO
            why = (
                ""
                if not_blank
                else f"（低于 {_MIN_INK_RATIO:.1%}，近乎空白：多半只有底色或坐标框没画上数据）"
            )
            checks.append(FigureCheck("not_blank", not_blank, f"非主色像素占比 {ink:.2%}{why}"))
    if fmt == "svg":
        has_marks = (mark_count or 0) > 0
        why = "" if has_marks else "（没有 path / line / rect… 元素，图是空的）"
        checks.append(FigureCheck("has_marks", has_marks, f"{mark_count} 个绘图元素{why}"))
        has_text = (text_count or 0) > 0
        why = (
            ""
            if has_text
            else "（无 <text>：可能没有标题 / 轴标签，或文字已转成路径——请审稿人看渲染图核对）"
        )
        checks.append(FigureCheck("has_text", has_text, f"{text_count} 个文本元素{why}"))
    return FigureInspection(
        name=name, format=fmt, width=width, height=height, dpi=dpi, ink_ratio=ink,
        text_count=text_count, mark_count=mark_count, checks=tuple(checks), note=note,
    )


def figure_qa_material(inspections: Iterable[FigureInspection]) -> str:
    """审稿材料段：逐图一行事实 + 未过判定；空清单如实写。"""
    rows = list(inspections)
    if not rows:
        return "渲染后核验：无图件。"
    suspects = sum(1 for row in rows if row.status == "suspect")
    unreadable = sum(1 for row in rows if row.status == "unreadable")
    lines = [
        f"渲染后核验（确定性）：{len(rows)} 张图件，{suspects} 张有疑点，{unreadable} 张不可读。"
    ]
    for row in rows:
        facts = [row.format.upper()]
        if row.width and row.height:
            facts.append(f"{row.width} × {row.height}")
        if row.dpi:
            facts.append(f"{row.dpi:g} dpi")
        if row.ink_ratio is not None:
            facts.append(f"非主色像素 {row.ink_ratio:.1%}")
        if row.text_count is not None:
            facts.append(f"文本元素 {row.text_count}")
        failed = [check for check in row.checks if not check.passed]
        verdict = {"ok": "通过", "unreadable": "不可读"}.get(row.status, "疑点")
        line = f"- {row.name}：{verdict}｜{'，'.join(facts)}"
        if failed:
            line += "｜" + "；".join(f"{check.id}：{check.detail}" for check in failed)
        if row.note:
            line += f"｜{row.note}"
        lines.append(line)
    return "\n".join(lines)
