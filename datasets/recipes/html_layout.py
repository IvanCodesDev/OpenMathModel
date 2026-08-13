#!/usr/bin/env python3
"""Convert an HTML statement body into the same semantic blocks as the PDFs.

Some organisers publish a statement as rich text through an API rather than as
a PDF, so there is no page geometry to recover structure from -- the markup
already says what is a heading, a paragraph and a list item. This module maps
that markup onto the block shapes in
``datasets/catalog/knowledge-library.schema.json`` so a record collected this
way renders exactly like one parsed from a PDF.

The vocabulary is deliberately narrow because the source is a Quill editor:
paragraphs, headings, ordered and unordered lists, line breaks, inline emphasis
and data-URI images. Anything else is treated as inline text rather than
guessed at.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pdf_layout import RUN_IN_LABEL_RE, write_web_image


BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 3, "h5": 3, "h6": 3}
EMPHASIS_TAGS = {"strong", "b"}
SKIP_TAGS = {"script", "style"}
DATA_URI_RE = re.compile(r"^data:(image/[a-z0-9.+-]+)?;?(base64)?,", re.I)
# A run of nothing but dots stands in for statement text the publisher withheld;
# see stage_tipdm_bdrace_statements. Nothing legitimate is only punctuation.
ELISION_RE = re.compile(r"^[.．。…\s·]{3,}$")
SENTENCE_FINAL = "。．.!?！？；;，,、"


def _clean(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\ufeff", "").replace("\u200b", "")
    return re.sub(r"[ \t]+", " ", value).strip()


class _Reader(HTMLParser):
    """Streams markup into blocks, one per block-level element."""

    def __init__(self, problem_id: str, figure_root: Path | None) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self._problem_id = problem_id
        self._figure_root = figure_root
        self._image_count = 0
        self._text: list[str] = []
        self._kind = "paragraph"
        self._level = 2
        self._marker: str | None = None
        self._bold_runs: list[str] = []
        self._emphasis = 0
        self._skip = 0
        self._list_stack: list[dict[str, Any]] = []

    def _flush(self) -> None:
        text = _clean("".join(self._text))
        bold = _clean("".join(self._bold_runs))
        kind, level, marker = self._kind, self._level, self._marker
        self._text, self._bold_runs = [], []
        self._kind, self._level, self._marker = "paragraph", 2, None
        if not text or ELISION_RE.match(text):
            return
        if kind == "heading":
            self.blocks.append({"type": "heading", "level": level, "text": text})
            return
        block: dict[str, Any] = {"type": kind, "text": text}
        if kind == "list_item":
            if marker:
                block["marker"] = marker
            self.blocks.append(block)
            return
        # A paragraph set entirely in bold is a section heading the editor never
        # tagged as one ("一、问题背景"). The length gate matches the one the PDF
        # reader uses, so both paths agree. A sentence ending in a full stop is
        # emphasis rather than a heading, which is what "根据出题方要求，竞赛结束后
        # 不对外提供数据。" is.
        if (bold and bold == text and len(text) <= 48
                and text[-1] not in SENTENCE_FINAL):
            self.blocks.append({"type": "heading", "level": 2, "text": text})
            return
        label = RUN_IN_LABEL_RE.match(text)
        if label:
            block["lead"] = label.group(0).strip()
            block["text"] = text[label.end():].lstrip()
            if not block["text"]:
                block.pop("lead")
                block["text"] = text
        self.blocks.append(block)

    def _add_image(self, source: str) -> None:
        if self._figure_root is None or not DATA_URI_RE.match(source):
            return
        header, _, payload = source.partition(",")
        try:
            data = (base64.b64decode(payload, validate=False) if "base64" in header.lower()
                    else urllib.parse.unquote_to_bytes(payload))
        except (binascii.Error, ValueError):
            return
        if len(data) < 512:
            return
        self._image_count += 1
        target = write_web_image(data, self._figure_root,
                                 f"inline-{self._image_count:02d}")
        if target is None:
            return
        self._flush()
        self.blocks.append({
            "type": "image",
            "src": f"/problem-figures/{self._problem_id}/{target.name}",
            "alt": f"{self._problem_id} 题面插图 {self._image_count}",
        })

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag == "br":
            self._text.append("\n")
            return
        if tag == "img":
            self._add_image(dict(attrs).get("src") or "")
            return
        if tag in {"ol", "ul"}:
            self._flush()
            self._list_stack.append({"ordered": tag == "ol", "index": 0})
            return
        if tag in EMPHASIS_TAGS:
            self._emphasis += 1
            return
        if tag in BLOCK_TAGS:
            self._flush()
            if tag in HEADING_TAGS:
                self._kind, self._level = "heading", HEADING_TAGS[tag]
            elif tag == "li":
                self._kind = "list_item"
                if self._list_stack and self._list_stack[-1]["ordered"]:
                    self._list_stack[-1]["index"] += 1
                    self._marker = f"{self._list_stack[-1]['index']}."

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in EMPHASIS_TAGS:
            self._emphasis = max(0, self._emphasis - 1)
            return
        if tag in {"ol", "ul"}:
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._text.append(data)
        if self._emphasis:
            self._bold_runs.append(data)

    def close(self) -> None:  # noqa: D102 - flushes the trailing block
        super().close()
        self._flush()


def build_blocks(source: str, problem_id: str,
                 figure_root: Path | None) -> tuple[list[dict[str, Any]], str]:
    """Return semantic blocks and a plain-text rendition of one HTML statement.

    ``figure_root`` receives any inline data-URI image, named so it cannot
    collide with the ``pNNN-NN`` files the PDF reader writes into the same
    directory.
    """
    destination = (figure_root / problem_id) if figure_root is not None else None
    reader = _Reader(problem_id, destination)
    reader.feed(unicodedata.normalize("NFC", source))
    reader.close()
    blocks = reader.blocks
    plain = "\n".join(block["text"] for block in blocks if block["type"] != "image")
    return blocks, plain
