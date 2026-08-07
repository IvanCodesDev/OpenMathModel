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
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber
from pypdf import PdfReader

from image_guard import looks_like_qr_code


# Mathematical Alphanumeric Symbols: how LaTeX-set PDFs encode formula variables.
MATH_ALNUM_RE = re.compile(r"[\U0001D400-\U0001D7FF]")
# Two rules this close together are one border drawn twice (stroke plus fill),
# not two columns, so they collapse when a table's grid is reconstructed.
GRID_TOLERANCE = 1.5
# Symbol-font runs reach the PDF as the font's own byte codes offset into the
# Private Use Area (0xF000 + code). This is the standard Adobe Symbol encoding;
# only the codes that carry meaning outside the font are listed, so bracket
# fragments and the Apple logo stay untranslated.
SYMBOL_PUA_RE = re.compile(r"[-]")
SYMBOL_PUA_MAP = {
    0xF022: "∀", 0xF024: "∃", 0xF027: "∋", 0xF02A: "∗", 0xF02D: "−",
    0xF040: "≅",
    0xF041: "Α", 0xF042: "Β", 0xF043: "Χ", 0xF044: "Δ", 0xF045: "Ε",
    0xF046: "Φ", 0xF047: "Γ", 0xF048: "Η", 0xF049: "Ι", 0xF04A: "ϑ",
    0xF04B: "Κ", 0xF04C: "Λ", 0xF04D: "Μ", 0xF04E: "Ν", 0xF04F: "Ο",
    0xF050: "Π", 0xF051: "Θ", 0xF052: "Ρ", 0xF053: "Σ", 0xF054: "Τ",
    0xF055: "Υ", 0xF056: "ς", 0xF057: "Ω", 0xF058: "Ξ", 0xF059: "Ψ",
    0xF05A: "Ζ", 0xF05C: "∴", 0xF05E: "⊥",
    0xF061: "α", 0xF062: "β", 0xF063: "χ", 0xF064: "δ", 0xF065: "ε",
    0xF066: "φ", 0xF067: "γ", 0xF068: "η", 0xF069: "ι", 0xF06A: "ϕ",
    0xF06B: "κ", 0xF06C: "λ", 0xF06D: "μ", 0xF06E: "ν", 0xF06F: "ο",
    0xF070: "π", 0xF071: "θ", 0xF072: "ρ", 0xF073: "σ", 0xF074: "τ",
    0xF075: "υ", 0xF076: "ϖ", 0xF077: "ω", 0xF078: "ξ", 0xF079: "ψ",
    0xF07A: "ζ", 0xF07E: "∼",
    0xF0A2: "′", 0xF0A3: "≤", 0xF0A4: "⁄", 0xF0A5: "∞",
    0xF0AB: "↔", 0xF0AC: "←", 0xF0AD: "↑", 0xF0AE: "→", 0xF0AF: "↓",
    0xF0B0: "°", 0xF0B1: "±", 0xF0B2: "″", 0xF0B3: "≥", 0xF0B4: "×",
    0xF0B5: "∝", 0xF0B6: "∂", 0xF0B7: "•", 0xF0B8: "÷", 0xF0B9: "≠",
    0xF0BA: "≡", 0xF0BB: "≈", 0xF0BC: "…",
    0xF0C4: "⊗", 0xF0C5: "⊕", 0xF0C6: "∅", 0xF0C7: "∩", 0xF0C8: "∪",
    0xF0C9: "⊃", 0xF0CA: "⊇", 0xF0CB: "⊄", 0xF0CC: "⊂", 0xF0CD: "⊆",
    0xF0CE: "∈", 0xF0CF: "∉",
    0xF0D0: "∠", 0xF0D1: "∇", 0xF0D5: "∏", 0xF0D6: "√", 0xF0D7: "⋅",
    0xF0D8: "¬", 0xF0D9: "∧", 0xF0DA: "∨", 0xF0DB: "⇔", 0xF0DC: "⇐",
    0xF0DD: "⇑", 0xF0DE: "⇒", 0xF0DF: "⇓",
    0xF0E0: "◊", 0xF0E1: "⟨", 0xF0E5: "∑", 0xF0F1: "⟩", 0xF0F2: "∫",
}
# A stacked fraction is drawn as a thin filled rectangle with the numerator
# hugging it from above and the denominator from below. Underlined prose has the
# same shape, so the vertical gap must stay well under one line of leading and
# both sides must sit within the bar's horizontal span.
FRACTION_BAR_MAX_HEIGHT = 2.5
FRACTION_BAR_MIN_WIDTH = 4.0
FRACTION_BAR_MAX_WIDTH = 260.0
FRACTION_MAX_GAP = 5.0
FRACTION_SPILL = 2.0
# How far past a side's own baseline band a sub/superscript may sit.
FRACTION_SIDE_SLACK = 4.0
# find_fraction_bars identifies the words a fraction owns and page_lines drops
# them by (x0, top, text) key, so the two must tokenize a page the same way.
# They did not: the default settings merge "供货量−接收量" into one word while
# these split it into three, so no key matched, the numerator survived, and the
# statement read "即损耗率=供货量(供货量−接收量)/供货量−接收量" with both sides
# duplicated. One setting, named once, keeps the two views in agreement.
WORD_EXTRACTION = {"extra_attrs": ["size", "fontname"], "use_text_flow": False}
# Four or more lowercase latin letters in a row is a natural-language word;
# function names are spelled out in formulas too, so they are excluded first.
PROSE_WORD_RE = re.compile(r"[a-z]{4,}")
MATH_FUNCTION_RE = re.compile(r"(?:sin|cos|tan|cot|sec|csc|exp|log|ln|max|min|lim)")
FRACTION_COMPOUND_RE = re.compile(r"[\s+\-−=×⋅∗/^_(),]")
# Glyphs from symbol subset fonts that decode to punctuation but carry no text
# (observed as BXSYMA+FangSong "!" runs inside the APMCM statements).
JUNK_TOKEN_RE = re.compile(r"^[!\u0001-\u0008\u000b\u000c\u000e-\u001f]+$")
PAGE_LABEL_RE = re.compile(r"^(?:[-–—\s]*\d{1,3}[-–—\s]*|page\s+\d{1,3}(?:\s+of\s+\d{1,3})?)$", re.I)
COMAP_FOOTER_RE = re.compile(r"comap\.org|mathmodels\.org|©\s*\d{4}\s*by\s*COMAP", re.I)
# A dash bullet must introduce words, not a number. "6 – 3" wrapping across a
# line break otherwise reads as a marker: the score's second half opens a line,
# so the sentence splits and the dash is lifted away, leaving "3. The fifth and
# final set..." as a bogus list item and "...win the set 6" missing its score.
# Ranges and scores are the only dash-plus-digit forms that occur, so requiring a
# non-digit keeps genuine dash bullets working.
BULLET_RE = re.compile(r"^([•●▪◦·‣∙*]|[-–—]\s(?!\d)|\(?\d{1,2}[.)、]\s|\(?[a-zA-Z][.)]\s|[①-⑳])")
MARKER_TOKEN_RE = re.compile(r"^(?:[•●▪◦·‣∙*\-–—]|\(?\d{1,2}[.)、]|\(?[a-zA-Z][.)]|[①-⑳])$")
# Markers safe to lift out of the text and hand to the renderer as a field.
# Deliberately narrower than BULLET_RE: a lone capital plus a period is far more
# often a bibliography author initial ("K. R. Rao, ...", "M. Musallam and ...")
# than an enumerator, and stripping those would delete real content. Lowercase
# "a)" carries no such ambiguity, so it stays in.
LIST_MARKER_RE = re.compile(r"^(?:[•●▪◦·‣∙*]|[-–—]\s(?!\d)|\(?\d{1,2}[.)、]\s|\(?[a-z][.)]\s|[①-⑳])\s*")
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


def _fold_symbol_pua(value: str) -> str:
    """Map Adobe Symbol glyphs out of the Private Use Area onto real Unicode.

    Word writes Symbol-font runs into a PDF as U+F020-U+F0FF: the font's own byte
    codes with 0xF000 added. Nothing in the Private Use Area has an assigned
    meaning, so no system font covers those codepoints and the browser draws a
    tofu box -- the "symbol did not render" symptom. The statements hit this on
    the multiplication sign of a loss-rate formula (U+F0B4) and on the bullet of
    every Symbol-set list (U+F0B7).

    The mapping is the standard Adobe Symbol encoding, so it is a lookup rather
    than a guess. Codepoints outside the table are left alone: they would be
    Wingdings or another dingbat font sharing the same PUA range, and inventing a
    character for them would be worse than leaving one visible box.
    """
    if not SYMBOL_PUA_RE.search(value):
        return value
    return SYMBOL_PUA_RE.sub(
        lambda match: SYMBOL_PUA_MAP.get(ord(match.group()), match.group()), value)


def _fold_math_alphanumerics(value: str) -> str:
    """Map Mathematical Alphanumeric Symbols onto their plain base letters.

    LaTeX-set statements encode variables in the U+1D400-U+1D7FF block, so a
    formula arrives as ``sin\U0001D6FC\U0001D460 =cos\U0001D6FF``. Almost no UI or
    CJK font ships those codepoints, so the browser falls back per character and
    the line renders in a jumble of mismatched typefaces at mismatched sizes --
    the "formula rendering is broken" symptom. Their NFKC base characters
    (``alpha``, ``s``, ``delta``) carry the same meaning and are covered by every
    system font, so the text renders cleanly with no markup at all.

    The rewrite is deliberately scoped to that one block instead of running NFKC
    over the whole string: blanket NFKC also expands the circled numerals used as
    list markers (U+2460 becomes ``1``) and the full-width forms these documents
    rely on, which would corrupt real content.
    """
    if not MATH_ALNUM_RE.search(value):
        return value
    return MATH_ALNUM_RE.sub(lambda match: unicodedata.normalize("NFKC", match.group()), value)


def _clean(value: str) -> str:
    value = _fold_symbol_pua(value)
    value = _fold_math_alphanumerics(value)
    return " ".join(value.replace("\u00ad", "").replace("\u200c", "").replace("\u3000", " ").split())


def _is_bold(fontname: str) -> bool:
    name = fontname.split("+")[-1].lower()
    return "bold" in name or "heavy" in name or "black" in name or name.endswith("-bd")


def _looks_like_prose(text: str) -> bool:
    """True when a fraction side reads as a sentence rather than as a term.

    Length is measured on the joined text, not on the extracted word count: the
    shared tokenization splits a CJK run into one word per term, so "供货量−接收量"
    arrives as three words and a raw token count would reject it as prose. After
    join_fragments, CJK stays unspaced and only latin words carry separators, so
    counting space-separated groups measures the same thing the old merged-token
    count did.
    """
    if len(text.split()) > 3:
        return True
    return bool(PROSE_WORD_RE.search(MATH_FUNCTION_RE.sub("", text)))


def _wrap_fraction_side(text: str) -> str:
    """Parenthesize a side only when flattening to ``a/b`` would change grouping."""
    if len(text) <= 1 or not FRACTION_COMPOUND_RE.search(text):
        return text
    return f"({text})"


def find_fraction_bars(page: pdfplumber.page.Page,
                       exclude: list[tuple[float, ...]]) -> list[dict[str, Any]]:
    """Locate stacked fractions and describe them as inline ``numerator/denominator``.

    A display fraction occupies three separate baselines -- numerator, rule,
    denominator -- so the line grouper in :func:`page_lines` emits three unrelated
    lines and the paragraph builder turns one formula into three blocks. That is
    why a statement reads ``π`` / ``ω = (ST−12),`` / ``12``: the numerator, the
    rest of the equation and the denominator are shown as separate paragraphs in
    the wrong order.

    Rules around body copy have the same geometry, so three guards apply: the gap
    to each side stays far below one line of leading (a real fraction is set
    tight, prose leading is roughly twice as wide), both sides sit inside the
    bar's horizontal span, and neither side reads as prose.
    """
    try:
        # A rule is either a filled rectangle or a stroked line, and which one a
        # writer emits is arbitrary -- Word uses both. pdfplumber files them in
        # separate lists, and a stroked bar has zero height, so a rects-only scan
        # silently misses those fractions and leaves the denominator stranded as
        # its own paragraph. Vertical strokes come along too but fail the width
        # and height gates below.
        rects = list(page.rects) + [
            line for line in page.lines if abs(line["y1"] - line["y0"]) <= FRACTION_BAR_MAX_HEIGHT
        ]
        words = page.extract_words(**WORD_EXTRACTION)
    except Exception:
        return []
    # Cell borders have exactly the geometry of a fraction bar, and a header cell
    # wrapped over two lines then reads as numerator over denominator. ``exclude``
    # only carries the grids that passed extract_tables()' gate, so rule out every
    # ruled grid on the page -- a fraction is never drawn inside one.
    try:
        exclude = list(exclude) + [
            table.bbox for table in page.find_tables(
                {"vertical_strategy": "lines", "horizontal_strategy": "lines"})
        ]
    except Exception:
        exclude = list(exclude)
    bars = []
    for rect in rects:
        height = rect["y1"] - rect["y0"]
        width = rect["x1"] - rect["x0"]
        if height >= FRACTION_BAR_MAX_HEIGHT:
            continue
        if not FRACTION_BAR_MIN_WIDTH <= width <= FRACTION_BAR_MAX_WIDTH:
            continue
        centre_x = (rect["x0"] + rect["x1"]) / 2
        centre_y = (rect["top"] + rect["bottom"]) / 2
        if any(x0 <= centre_x <= x1 and top <= centre_y <= bottom
               for x0, top, x1, bottom in exclude):
            continue

        def within(word: dict[str, Any]) -> bool:
            return (word["x0"] >= rect["x0"] - FRACTION_SPILL
                    and word["x1"] <= rect["x1"] + FRACTION_SPILL)

        above = [w for w in words
                 if within(w) and 0 < rect["top"] - w["bottom"] <= FRACTION_MAX_GAP]
        below = [w for w in words
                 if within(w) and 0 < w["top"] - rect["bottom"] <= FRACTION_MAX_GAP]
        if not above or not below:
            continue
        # Sub- and superscripts sit a few points off their side's own baseline, so
        # the tight gap window above misses them and they survive as stray
        # one-letter paragraphs (the ``s`` of an alpha-sub-s in a numerator).
        # Grow each side to cover its full vertical band instead.
        claimed = {id(word) for word in above + below}

        def expand(side: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not side:
                return side
            top = min(word["top"] for word in side) - FRACTION_SIDE_SLACK
            bottom = max(word["bottom"] for word in side) + FRACTION_SIDE_SLACK
            extra = [word for word in words
                     if id(word) not in claimed and within(word)
                     and top <= (word["top"] + word["bottom"]) / 2 <= bottom]
            claimed.update(id(word) for word in extra)
            return side + extra

        above, below = expand(above), expand(below)
        # join_fragments rather than " ".join: the shared tokenization splits a
        # CJK run into one word per term, and a plain space join would set the
        # numerator as "供货量 − 接收量".
        numerator = _clean(join_fragments([w["text"] for w in sorted(above, key=lambda w: w["x0"])]))
        denominator = _clean(join_fragments([w["text"] for w in sorted(below, key=lambda w: w["x0"])]))
        if not numerator or not denominator:
            continue
        if _looks_like_prose(numerator) or _looks_like_prose(denominator):
            continue
        bars.append({
            "text": f"{_wrap_fraction_side(numerator)}/{_wrap_fraction_side(denominator)}",
            "x0": min(w["x0"] for w in above + below),
            "x1": max(w["x1"] for w in above + below),
            # The replacement token sits on the bar itself, which is where the
            # rest of the equation is set, so it joins that line rather than the
            # numerator's or the denominator's.
            "top": rect["top"] - 1.0,
            "bottom": rect["bottom"] + 1.0,
            "members": {(round(w["x0"], 1), round(w["top"], 1), w["text"]) for w in above + below},
        })
    return bars


def page_lines(page: pdfplumber.page.Page, page_number: int, exclude: list[tuple[float, ...]],
               fractions: list[dict[str, Any]] | None = None) -> list[Line]:
    """Group a page's words into visual lines, skipping regions owned by tables.

    ``fractions`` describes stacked fractions found by :func:`find_fraction_bars`.
    Their numerator and denominator words are removed and replaced by a single
    ``a/b`` token placed on the fraction bar's own baseline, so the fraction joins
    the line carrying the rest of the equation instead of splitting into three.
    """
    try:
        words = page.extract_words(**WORD_EXTRACTION)
    except Exception:
        return []

    fractions = fractions or []
    consumed: set[tuple[float, float, str]] = set()
    for fraction in fractions:
        consumed |= fraction["members"]

    kept = []
    for word in words:
        text = word["text"].strip()
        if not text or JUNK_TOKEN_RE.match(text):
            continue
        if (round(word["x0"], 1), round(word["top"], 1), word["text"]) in consumed:
            continue
        centre_x = (word["x0"] + word["x1"]) / 2
        centre_y = (word["top"] + word["bottom"]) / 2
        if any(x0 <= centre_x <= x1 and top <= centre_y <= bottom for x0, top, x1, bottom in exclude):
            continue
        kept.append(word)

    body_size = statistics.median(
        [word.get("size", 10.0) for word in kept]) if kept else 10.0
    for fraction in fractions:
        kept.append({
            "text": fraction["text"],
            "x0": fraction["x0"],
            "x1": fraction["x1"],
            "top": fraction["top"],
            "bottom": fraction["bottom"],
            "size": body_size,
            "fontname": "",
        })

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


def _grid_lines(table: Any) -> tuple[list[float], list[float]]:
    """Return the table's distinct column and row boundaries.

    A merged cell widens one column's bbox to span its neighbours, so the raw
    edge list repeats values; near-duplicates within ``GRID_TOLERANCE`` are the
    same rule drawn twice (stroke plus fill) and collapse to one.
    """
    def dedupe(values: set[float]) -> list[float]:
        merged: list[float] = []
        for value in sorted(values):
            if merged and value - merged[-1] <= GRID_TOLERANCE:
                continue
            merged.append(value)
        return merged

    verticals = dedupe({round(v, 1) for column in table.columns
                        for v in (column.bbox[0], column.bbox[2])})
    horizontals = dedupe({round(v, 1) for row in table.rows
                          for v in (row.bbox[1], row.bbox[3])})
    return verticals, horizontals


def _regrid(page: pdfplumber.page.Page, table: Any,
            rows: list[list[str | None]]) -> list[list[str | None]] | None:
    """Re-read a table on its own full column grid, or return None.

    Word tables draw interior verticals per row, and a row that omits them makes
    ``find_tables`` report one wide cell: the Olympic medal table arrives with
    ``China`` next to a single ``40 27 24 91``. The column boundaries survive in
    the rows that *did* rule themselves, so re-reading every row against the
    union of boundaries recovers the missing splits.

    A genuine spanning cell (a header sitting over several columns) must not be
    chopped up, so the result is accepted only when it is the same shape and
    carries exactly the same text per row -- redistributing content across
    columns is allowed, inventing or dropping any is not.
    """
    verticals, horizontals = _grid_lines(table)
    if len(verticals) < 3 or len(horizontals) < 3:
        return None
    try:
        regridded = page.crop(table.bbox).extract_table({
            "vertical_strategy": "explicit",
            "horizontal_strategy": "explicit",
            "explicit_vertical_lines": verticals,
            "explicit_horizontal_lines": horizontals,
        })
    except Exception:
        return None
    if not regridded or len(regridded) != len(rows):
        return None
    if any(len(new) != len(old) for new, old in zip(regridded, rows)):
        return None
    for new, old in zip(regridded, rows):
        if _row_signature(new) != _row_signature(old):
            return None
    return regridded


def _row_signature(row: list[str | None]) -> str:
    """A row's text with all whitespace and cell boundaries removed."""
    return "".join("".join(_clean(cell or "").split()) for cell in row)


def _label_axis_ratio(normalized: list[list[str]]) -> tuple[float, float]:
    """Return how populated the header row and the first column are."""
    header = normalized[0] if normalized else []
    header_ratio = sum(1 for cell in header if cell) / len(header) if header else 0.0
    body = [row for row in normalized[1:] if row]
    first_column = [row[0] for row in body]
    column_ratio = sum(1 for cell in first_column if cell) / len(first_column) if first_column else 0.0
    return header_ratio, column_ratio


def extract_tables(page: pdfplumber.page.Page) -> list[dict[str, Any]]:
    """Return ruled tables only, and only when the grid really holds tabular data.

    Rules and underlines around body copy make ``find_tables`` report prose as a
    sparse two-column grid. Accepting those would delete the statement text from
    the flow, so a rejected candidate stays in the paragraph stream.

    Structure is judged from the label axis, not from overall density. Competition
    statements ask contestants to "fill the result into 表1", so the answer template
    ships as a ruled grid with a complete header row and a deliberately blank body.
    Those grids sit near 0.2 overall density, so an occupancy threshold drops them
    and their header cells then reappear as one run-on paragraph. A populated header
    row *or* a populated first column is what separates a real table -- filled or
    blank -- from prose that merely happens to be boxed.

    The short-cell guard stays: boxed body copy yields few but very long cells.
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
        rows = _regrid(page, table, rows) or rows
        normalized = [[_clean(cell or "") for cell in row] for row in rows]
        filled = [cell for row in normalized for cell in row if cell]
        if not filled:
            continue
        if statistics.median(len(cell) for cell in filled) > 20:
            continue
        header_ratio, column_ratio = _label_axis_ratio(normalized)
        if header_ratio < 0.6 and column_ratio < 0.6:
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
        if looks_like_qr_code(target):
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
        self._marker: str | None = None
        self._hanging_x0: float | None = None
        self._previous: Line | None = None

    def flush(self) -> None:
        if not self._parts:
            return
        text = join_fragments(self._parts)
        self._parts = []
        lead, self._lead = self._lead, None
        marker, self._marker = self._marker, None
        kind, self._kind = self._kind, "paragraph"
        self._hanging_x0 = None
        if not text or PAGE_LABEL_RE.match(text):
            return
        block: dict[str, Any] = {"type": kind, "text": text}
        if lead:
            block["lead"] = lead
        # Ordered markers carry meaning ("问题 2" refers to item 2), so the
        # original is preserved; the renderer supplies a bullet where the source
        # had a plain dot and this field is absent.
        if marker and kind == "list_item":
            block["marker"] = marker
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
                # Hand the marker to the renderer as its own field instead of
                # leaving it in the text. The frontend draws a bullet for every
                # list item, so a marker left inline shows up as a second one.
                strippable = LIST_MARKER_RE.match(text)
                if strippable:
                    self._marker = strippable.group(0).strip()
                    text = text[strippable.end():].lstrip()
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
            boxes = [table["bbox"] for table in tables]
            raw_pages.append(page_lines(page, index + 1, boxes, find_fraction_bars(page, boxes)))
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
