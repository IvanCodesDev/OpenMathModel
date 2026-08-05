#!/usr/bin/env python3
"""Layout-aware conversion of competition PDF statements into semantic blocks.

``pypdf.extract_text`` returns a flat line stream and these archives carry no
blank lines between paragraphs, so a page-at-a-time text join collapses an
entire page into one block. This module reads word geometry with ``pdfplumber``
and rebuilds structure from the signals the original typesetter actually used:

* first-line indent (CUMCM statements separate paragraphs only this way),
* inter-paragraph leading (COMAP and APMCM statements),
* short final lines in justified text,
* font size and weight for headings and run-in labels,
* bullet markers plus hanging indents for list continuation lines,
* ruled table borders,
* figure bounding boxes, so images keep their reading-order position.

The emitted block shapes match ``datasets/catalog/knowledge-library.schema.json``
so the frontend renderer stays unchanged.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader


# Glyphs from symbol subset fonts that decode to punctuation but carry no text
# (observed as BXSYMA+FangSong "!" runs inside the APMCM statements).
JUNK_TOKEN_RE = re.compile(r"^[!\u0001-\u0008\u000b\u000c\u000e-\u001f]+$")
PAGE_LABEL_RE = re.compile(r"^(?:[-–—\s]*\d{1,3}[-–—\s]*|page\s+\d{1,3}(?:\s+of\s+\d{1,3})?)$", re.I)
COMAP_FOOTER_RE = re.compile(r"comap\.org|mathmodels\.org|©\s*\d{4}\s*by\s*COMAP", re.I)
BULLET_RE = re.compile(r"^([•●▪◦·‣∙*]|[-–—]\s|\(?\d{1,2}[.)、]\s|\(?[a-zA-Z][.)]\s|[①-⑳])")
MARKER_TOKEN_RE = re.compile(r"^(?:[•●▪◦·‣∙*\-–—]|\(?\d{1,2}[.)、]|\(?[a-zA-Z][.)]|[①-⑳])$")
# Bibliography entries carry their own numbering and always open a new block.
REFERENCE_ENTRY_RE = re.compile(r"^\[\d{1,3}\]\s*\S")
RUN_IN_LABEL_RE = re.compile(
    r"^(问题\s*[0-9０-９]{1,2}|问题[一二三四五六七八九十]{1,3}|第[一二三四五六七八九十]{1,3}问"
    r"|Question\s*\d{1,2}|Task\s*\d{1,2}|Part\s*[A-Z0-9]{1,2})\s*[：:.、）)]?\s*"
)
# Headings that name a section on their own line and must not absorb the
# wrapped title that follows them ("Problem C" above the actual题目 title).
STANDALONE_HEADING_RE = re.compile(
    r"^(?:Problem\s+[A-Z0-9]+|\d{4}\s+(?:MCM|ICM|APMCM|CUMCM)\b.*|[A-E]\s*题.*|问题\s*\d+.*|"
    r"[一二三四五六七八九十]+、.*|附件\s*\d*.*)$"
)
TERMINAL_PUNCTUATION = "。．.!?！？:：;；\"”』」)）]】"
CJK_RANGES = (
    (0x2E80, 0x303F),
    (0x3040, 0x33FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFFEF),
)


def is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in CJK_RANGES)


def needs_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left[-1] == "-" and right[0].isalpha():
        return False
    if is_cjk(left[-1]) or is_cjk(right[0]):
        return False
    return left[-1] not in "([/“‘" and right[0] not in ",.!?;:)]”’%"


def join_fragments(parts: list[str]) -> str:
    output = ""
    for part in parts:
        if not part:
            continue
        if output.endswith("-") and part[:1].isalpha():
            output = output[:-1] + part
        else:
            output += (" " if needs_space(output, part) else "") + part
    return output.strip()


@dataclass
class Line:
    page: int
    top: float
    bottom: float
    x0: float
    x1: float
    size: float
    text: str
    bold_prefix: str
    all_bold: bool
    word_positions: list[tuple[float, float, str]] = field(default_factory=list)


@dataclass
class Metrics:
    body_size: float = 10.0
    body_left: float = 0.0
    right_edge: float = 0.0
    line_gap: float = 15.0
    justified: bool = False
    heading_levels: dict[float, int] = field(default_factory=dict)

    @property
    def indent_min(self) -> float:
        return max(6.0, self.body_size * 0.7)

    @property
    def paragraph_gap(self) -> float:
        return self.line_gap * 1.28

    @property
    def short_line_slack(self) -> float:
        return max(12.0, self.body_size * 1.6)


def _clean(value: str) -> str:
    return " ".join(value.replace("\u00ad", "").replace("\u3000", " ").split())


def _is_bold(fontname: str) -> bool:
    name = fontname.split("+")[-1].lower()
    return "bold" in name or "heavy" in name or "black" in name or name.endswith("-bd")


def page_lines(page: pdfplumber.page.Page, page_number: int, exclude: list[tuple[float, ...]]) -> list[Line]:
    """Group a page's words into visual lines, skipping regions owned by tables."""
    try:
        words = page.extract_words(extra_attrs=["size", "fontname"], use_text_flow=False)
    except Exception:
        return []

    kept = []
    for word in words:
        text = word["text"].strip()
        if not text or JUNK_TOKEN_RE.match(text):
            continue
        centre_x = (word["x0"] + word["x1"]) / 2
        centre_y = (word["top"] + word["bottom"]) / 2
        if any(x0 <= centre_x <= x1 and top <= centre_y <= bottom for x0, top, x1, bottom in exclude):
            continue
        kept.append(word)

    # Cluster by vertical overlap rather than by a fixed tolerance, so that
    # sub/superscripts (CO2, footnote markers) stay on their own baseline's line.
    buckets: list[list[dict[str, Any]]] = []
    bounds: list[list[float]] = []
    for word in sorted(kept, key=lambda item: (item["top"], item["x0"])):
        height = max(1.0, word["bottom"] - word["top"])
        if buckets:
            top, bottom = bounds[-1]
            overlap = min(word["bottom"], bottom) - max(word["top"], top)
            if overlap >= 0.35 * min(height, bottom - top):
                buckets[-1].append(word)
                bounds[-1] = [min(top, word["top"]), max(bottom, word["bottom"])]
                continue
        buckets.append([word])
        bounds.append([word["top"], word["bottom"]])

    lines: list[Line] = []
    for bucket in buckets:
        bucket.sort(key=lambda item: item["x0"])
        sizes = Counter(round(item.get("size", 10.0), 1) for item in bucket)
        dominant = sizes.most_common(1)[0][0]
        parts: list[str] = []
        for item in bucket:
            token = _clean(item["text"])
            if not token:
                continue
            # Superscript footnote markers belong to the word they annotate.
            if parts and round(item.get("size", 10.0), 1) <= dominant * 0.85:
                parts[-1] += token
            else:
                parts.append(token)
        text = join_fragments(parts)
        if not text:
            continue
        bold_prefix_parts: list[str] = []
        for item in bucket:
            if not _is_bold(item.get("fontname", "")):
                break
            bold_prefix_parts.append(_clean(item["text"]))
        lines.append(Line(
            page=page_number,
            top=min(item["top"] for item in bucket),
            bottom=max(item["bottom"] for item in bucket),
            x0=min(item["x0"] for item in bucket),
            x1=max(item["x1"] for item in bucket),
            size=sizes.most_common(1)[0][0],
            text=text,
            bold_prefix=join_fragments(bold_prefix_parts),
            all_bold=all(_is_bold(item.get("fontname", "")) for item in bucket),
            word_positions=[(item["x0"], item["x1"], _clean(item["text"])) for item in bucket],
        ))
    return lines


def document_metrics(pages: list[list[Line]]) -> Metrics:
    metrics = Metrics()
    weighted_sizes: Counter[float] = Counter()
    for lines in pages:
        for line in lines:
            weighted_sizes[line.size] += len(line.text)
    if not weighted_sizes:
        return metrics
    metrics.body_size = weighted_sizes.most_common(1)[0][0]

    body = [line for lines in pages for line in lines if abs(line.size - metrics.body_size) <= 0.6]
    if not body:
        return metrics

    metrics.body_left = Counter(round(line.x0) for line in body).most_common(1)[0][0]
    rights = sorted(line.x1 for line in body)
    metrics.right_edge = rights[min(len(rights) - 1, int(len(rights) * 0.9))]

    gaps: Counter[float] = Counter()
    for lines in pages:
        body_lines = [line for line in lines if abs(line.size - metrics.body_size) <= 0.6]
        for previous, current in zip(body_lines, body_lines[1:]):
            delta = round(current.top - previous.top, 1)
            if 0 < delta <= metrics.body_size * 4:
                gaps[delta] += 1
    if gaps:
        threshold = max(2, gaps.most_common(1)[0][1] * 0.4)
        common = [value for value, count in gaps.most_common(3) if count >= threshold]
        metrics.line_gap = min(common) if common else gaps.most_common(1)[0][0]

    full_width = sum(1 for line in body if line.x1 >= metrics.right_edge - 4)
    metrics.justified = full_width / len(body) >= 0.5

    heading_sizes = sorted({line.size for lines in pages for line in lines if line.size > metrics.body_size + 0.6}, reverse=True)
    metrics.heading_levels = {size: min(3, index + 1) for index, size in enumerate(heading_sizes)}
    return metrics


def drop_running_heads(pages: list[list[Line]], page_height: float) -> list[list[Line]]:
    """Remove page numbers and headers/footers that repeat across the document."""
    edge_counts: Counter[str] = Counter()
    for lines in pages:
        seen = set()
        for line in lines:
            near_edge = line.top < page_height * 0.09 or line.bottom > page_height * 0.91
            if near_edge and line.text not in seen:
                seen.add(line.text)
                edge_counts[line.text] += 1

    repeated = {
        text for text, count in edge_counts.items()
        if count >= 2 and count >= len(pages) * 0.5
    }

    cleaned: list[list[Line]] = []
    for lines in pages:
        keep = []
        for line in lines:
            near_edge = line.top < page_height * 0.09 or line.bottom > page_height * 0.91
            if near_edge and (line.text in repeated or PAGE_LABEL_RE.match(line.text)):
                continue
            if COMAP_FOOTER_RE.search(line.text) and len(line.text) < 160:
                continue
            keep.append(line)
        cleaned.append(keep)
    return cleaned


def _is_heading(line: Line, metrics: Metrics) -> bool:
    if line.size >= metrics.body_size + 0.7:
        return True
    if line.all_bold and len(line.text) <= 48 and not line.text.endswith((",", "，", ";", "；")):
        return True
    return False


def extract_tables(page: pdfplumber.page.Page) -> list[dict[str, Any]]:
    """Return ruled tables only, and only when the grid really holds tabular data.

    Rules and underlines around body copy make ``find_tables`` report prose as a
    sparse two-column grid. Accepting those would delete the statement text from
    the flow, so a candidate must be mostly populated and hold short values; a
    rejected candidate simply stays in the paragraph stream.
    """
    try:
        found = page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
    except Exception:
        return []
    tables = []
    for table in found:
        if len(table.rows) < 2 or len(table.columns) < 2:
            continue
        try:
            rows = table.extract()
        except Exception:
            continue
        normalized = [[_clean(cell or "") for cell in row] for row in rows]
        cells = [cell for row in normalized for cell in row]
        filled = [cell for cell in cells if cell]
        if not cells or len(filled) / len(cells) < 0.5:
            continue
        if statistics.median(len(cell) for cell in filled) > 20:
            continue
        tables.append({"bbox": tuple(table.bbox), "rows": normalized})
    return tables


def extract_figures(page: pdfplumber.page.Page, pypdf_page: Any, problem_id: str,
                    page_number: int, destination: Path) -> list[dict[str, Any]]:
    """Pair pdfplumber bounding boxes with pypdf image bytes by XObject name."""
    try:
        placed = page.images
    except Exception:
        placed = []
    if not placed:
        return []
    try:
        payloads = list(pypdf_page.images)
    except Exception:
        payloads = []
    by_name = {Path(item.name).stem: item for item in payloads}

    figures = []
    for index, image in enumerate(sorted(placed, key=lambda item: (item["top"], item["x0"])), start=1):
        payload = by_name.get(str(image.get("name") or ""))
        if payload is None and len(payloads) == len(placed):
            payload = payloads[index - 1]
        if payload is None:
            continue
        if image["x1"] - image["x0"] < 24 or image["bottom"] - image["top"] < 24:
            continue
        extension = Path(payload.name).suffix.lower()
        if extension not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            extension = ".png"
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"p{page_number:03d}-{index:02d}{extension}"
        target.write_bytes(payload.data)
        if target.stat().st_size < 512:
            target.unlink()
            continue
        figures.append({
            "top": image["top"],
            "block": {
                "type": "image",
                "src": f"/problem-figures/{problem_id}/{target.name}",
                "alt": f"{problem_id} 第 {page_number} 页插图 {index}",
            },
        })
    return figures


class _ParagraphBuilder:
    """Streams lines into paragraph/list blocks using the document's own metrics."""

    def __init__(self, metrics: Metrics) -> None:
        self.metrics = metrics
        self.blocks: list[dict[str, Any]] = []
        self._parts: list[str] = []
        self._kind = "paragraph"
        self._lead: str | None = None
        self._hanging_x0: float | None = None
        self._previous: Line | None = None

    def flush(self) -> None:
        if not self._parts:
            return
        text = join_fragments(self._parts)
        self._parts = []
        lead, self._lead = self._lead, None
        kind, self._kind = self._kind, "paragraph"
        self._hanging_x0 = None
        if not text or PAGE_LABEL_RE.match(text):
            return
        block: dict[str, Any] = {"type": kind, "text": text}
        if lead:
            block["lead"] = lead
        self.blocks.append(block)

    def add_heading(self, line: Line) -> None:
        self.flush()
        level = self.metrics.heading_levels.get(line.size, 2 if line.all_bold else 3)
        previous = self.blocks[-1] if self.blocks else None
        mergeable = (
            previous is not None
            and previous.get("type") == "heading"
            and previous.get("level") == level
            and not STANDALONE_HEADING_RE.match(previous["text"])
            and self._previous is not None
            and self._previous.page == line.page
            and line.top - self._previous.top <= max(line.size * 1.9, self.metrics.line_gap * 1.5)
        )
        if mergeable:
            previous["text"] = join_fragments([previous["text"], line.text])
        else:
            self.blocks.append({"type": "heading", "level": level, "text": line.text})
        self._previous = line

    def add_block(self, block: dict[str, Any]) -> None:
        self.flush()
        self.blocks.append(block)
        self._previous = None

    def _starts_paragraph(self, line: Line) -> bool:
        metrics = self.metrics
        previous = self._previous
        if previous is None or not self._parts:
            return True
        if BULLET_RE.match(line.text) or REFERENCE_ENTRY_RE.match(line.text):
            return True
        # A run-in label ("Question 5:", "问题 3") always opens a paragraph,
        # including at the top of a page where no leading signal survives.
        label = RUN_IN_LABEL_RE.match(line.text)
        if label and self._is_run_in_label(line, label.group(0)):
            return True
        if self._kind == "list_item" and self._hanging_x0 is not None:
            if line.x0 >= self._hanging_x0 - metrics.body_size * 0.5:
                return False
        if line.page != previous.page:
            if line.x0 > metrics.body_left + metrics.indent_min:
                return True
            # Only carry a paragraph over a page break when the previous line
            # filled its measure and stopped mid-sentence.
            ran_out_of_room = previous.x1 >= metrics.right_edge - metrics.short_line_slack
            unfinished = not self._parts[-1].rstrip().endswith(tuple(TERMINAL_PUNCTUATION))
            return not (unfinished and (ran_out_of_room or line.text[:1].islower()))
        if line.top - previous.top > metrics.paragraph_gap:
            return True
        if line.x0 > metrics.body_left + metrics.indent_min:
            return True
        if metrics.justified and previous.x1 < metrics.right_edge - metrics.short_line_slack:
            return True
        return False

    def _content_x0(self, line: Line, marker: str) -> float:
        for x0, _x1, word in line.word_positions:
            if MARKER_TOKEN_RE.match(word):
                continue
            if word and marker.strip().startswith(word):
                continue
            return x0
        return line.x0 + max(self.metrics.body_size * 1.2, 12.0)

    def _is_run_in_label(self, line: Line, label: str) -> bool:
        """A label counts as a run-in heading when the original set it apart.

        CUMCM statements render "问题1" in a synthetically emboldened SimSun that
        reports no bold flag, so fall back to the ideographic space the
        typesetter left between the label and the sentence.
        """
        if line.bold_prefix and line.text.startswith(line.bold_prefix):
            return True
        target = "".join(label.split())
        consumed = ""
        for index, (_x0, x1, word) in enumerate(line.word_positions):
            consumed += "".join(word.split())
            if len(consumed) < len(target):
                continue
            following = line.word_positions[index + 1:]
            return bool(following) and following[0][0] - x1 >= self.metrics.body_size * 0.5
        return False

    def add_line(self, line: Line) -> None:
        if self._starts_paragraph(line):
            self.flush()
            text = line.text
            bullet = BULLET_RE.match(text)
            if bullet:
                self._kind = "list_item"
                self._hanging_x0 = self._content_x0(line, bullet.group(0))
            else:
                label = RUN_IN_LABEL_RE.match(text)
                if label and self._is_run_in_label(line, label.group(0)):
                    self._lead = label.group(0).strip()
                    text = text[label.end():]
            self._parts = [text]
        else:
            self._parts.append(line.text)
        self._previous = line


def build_blocks(pdf_path: Path, problem_id: str, figure_root: Path | None,
                 page_limit: int | None = None, with_assets: bool = True) -> tuple[list[dict[str, Any]], int, str]:
    """Return semantic blocks, the page count and a plain-text rendition.

    ``page_limit`` and ``with_assets`` let callers read only a cover sheet, which
    is how paper metadata is recovered without exporting the paper's figures.
    """
    reader = PdfReader(str(pdf_path))
    with pdfplumber.open(str(pdf_path)) as pdf:
        wanted = pdf.pages if page_limit is None else pdf.pages[:page_limit]
        page_height = max((page.height for page in wanted), default=842.0)
        raw_pages: list[list[Line]] = []
        page_tables: list[list[dict[str, Any]]] = []
        page_figures: list[list[dict[str, Any]]] = []
        for index, page in enumerate(wanted):
            tables = extract_tables(page) if with_assets else []
            page_tables.append(tables)
            raw_pages.append(page_lines(page, index + 1, [table["bbox"] for table in tables]))
            pypdf_page = reader.pages[index] if index < len(reader.pages) else None
            page_figures.append(
                extract_figures(page, pypdf_page, problem_id, index + 1, figure_root / problem_id)
                if with_assets and figure_root is not None and pypdf_page is not None else []
            )

    pages = drop_running_heads(raw_pages, page_height)
    metrics = document_metrics(pages)

    builder = _ParagraphBuilder(metrics)
    for index, lines in enumerate(pages):
        floats: list[tuple[float, dict[str, Any]]] = [
            (figure["top"], figure["block"]) for figure in page_figures[index]
        ]
        floats += [
            (table["bbox"][1], {"type": "table", "rows": table["rows"]}) for table in page_tables[index]
        ]
        floats.sort(key=lambda item: item[0])
        cursor = 0
        for line in lines:
            while cursor < len(floats) and floats[cursor][0] <= line.top:
                builder.add_block(floats[cursor][1])
                cursor += 1
            if _is_heading(line, metrics):
                builder.add_heading(line)
            else:
                builder.add_line(line)
        while cursor < len(floats):
            builder.add_block(floats[cursor][1])
            cursor += 1
    builder.flush()

    blocks = builder.blocks
    plain = "\n".join(
        block.get("text", "") if block["type"] != "table"
        else "\n".join("\t".join(row) for row in block["rows"])
        for block in blocks
        if block["type"] != "image"
    )
    return blocks, len(reader.pages), plain
