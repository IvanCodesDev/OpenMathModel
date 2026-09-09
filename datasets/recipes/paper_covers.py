#!/usr/bin/env python3
"""Read the cover sheet of a competition award paper.

Every contest in this wave prints one of two covers. Chinese tracks lead with the
edition line and the entry number, then the declared title, 摘要 and 关键词;
English tracks (APMCM 主赛) print the COMAP-style summary sheet. Both are read
here so that each competition recipe only has to describe its own directory
layout and provenance.

Team member and advisor names are deliberately never extracted. They are personal
data, the product has no use for them, and several sources print them right next
to the fields that are wanted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ingest_cpmcm_paper_fulltext import METHOD_VOCABULARY


TITLE_MARKER_RE = re.compile(r"^(?:题\s*目|论文题目|作品名称|论文名称|标\s*题|Title)\s*[:：]\s*(.*)$", re.I)
INSTITUTION_RE = re.compile(
    r"^(?:学\s*校|院\s*校|学校名称|参赛学校|作品单位|参赛单位|所在学校|单\s*位)\s*[:：]\s*(.+)$"
)
AWARD_RE = re.compile(r"^(?:荣获奖项|获奖情况|获奖等级|奖\s*项)\s*[:：]\s*(.+)$")
TEAM_RE = re.compile(
    r"(?:参赛编号|参赛队号|队伍编号|报名编号|作品编号|控制号|Team\s*Control\s*Number|Team\s*Number|Team\s*#|Control\s*Number)"
    r"\s*[:：#]?\s*([A-Za-z]{0,6}\d{5,16})"
)
BARE_TEAM_RE = re.compile(r"^[A-Za-z]{0,6}\d{6,16}$")
ABSTRACT_RE = re.compile(r"^(?:摘\s*要|Summary|Abstract)\s*[:：]?\s*(.*)$", re.I)
# "Summary Sheet" is the sheet's own banner, not the summary itself: on the COMAP
# layout APMCM copies it sits above the title, so treating it as the abstract
# marker swallows the title and the control number into the summary text.
SHEET_BANNER_RE = re.compile(r"^(?:.*\b)?summary\s*sheet\s*[:：]?$", re.I)
KEYWORD_RE = re.compile(r"^(?:关\s*键\s*词|关\s*键\s*字|Key\s*words?)\s*[:：]?\s*(.*)$", re.I)
STOP_RE = re.compile(
    r"^(?:目\s*录|Contents|一\s*[、.]|1\s*[.、]\s*(?:问题重述|引言)|第一章|参考文献|References|Team\s*#)",
    re.I,
)

# Cover furniture that must never be mistaken for a title: edition banners, entry
# number labels, group labels, page numbers and the summary-sheet boilerplate.
BOILERPLATE_RE = re.compile(
    r"(?:"
    r"^\d{1,3}$|^第\s*[〇零一二三四五六七八九十百]+\s*[届章]|"
    r"参赛编号|参赛队号|所属类别|选\s*题|题\s*号|本科组|专科组|研究生组|队伍编号|报名编号|作品编号|"
    r"数学建模竞赛|建模挑战赛|挑战赛|优秀作品|优秀论文|承诺书|编\s*号|评阅|密\s*封|"
    r"作品成员|指导老师|指导教师|队员姓名|参赛队员|封面为后期添加|"
    r"Problem\s*Chosen|Summary\s*Sheet|Team\s*Control\s*Number|Control\s*Number|^Team\b|"
    r"summary\s*sheet|MCM|ICM|APMCM|Page\s+\d+"
    r")",
    re.I,
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
#: 社区流传的扫描本常在题名上方压一行公众号或资源站水印，跟着题名一起被读出来。
WATERMARK_RE = re.compile(
    r"^(?:微信公众号|公众号|关注公众号|来源)\s*[:：]?\s*\S{2,20}\s+|"
    r"^(?:www\.|http)\S+\s+|\s*[（(]?更多资料[^）)]*[）)]?$"
)


def page_lines(pdf_path: Path, page_limit: int = 3) -> tuple[list[str], int]:
    """Return the text lines of the leading pages plus the document page count.

    PyMuPDF is used rather than the pdfplumber pipeline the problem recipes rely
    on: award papers run to forty pages of figures, and only the cover is wanted,
    so a fast text layer read beats a full layout reconstruction.
    """
    import pymupdf

    lines: list[str] = []
    with pymupdf.open(str(pdf_path)) as document:
        total = document.page_count
        for index in range(min(page_limit, total)):
            for raw in document[index].get_text().splitlines():
                text = " ".join(raw.split())
                if text:
                    lines.append(text)
    return lines, total


def looks_like_title(text: str) -> bool:
    if not 6 <= len(text) <= 120:
        return False
    if BOILERPLATE_RE.search(text) or BARE_TEAM_RE.match(text):
        return False
    if re.fullmatch(r"[\W\d_]+", text):
        return False
    return True


def infer_title(lines: list[str], abstract_index: int) -> str:
    """Chinese covers print the title on the line(s) directly above 摘要."""
    collected: list[str] = []
    for text in reversed(lines[:abstract_index]):
        if not looks_like_title(text):
            break
        collected.insert(0, text)
        # A wrapped title spans at most two lines; anything longer is body text.
        if len(collected) == 2 or len("".join(collected)) > 40:
            break
    return " ".join(collected).strip()


def abstract_start(lines: list[str]) -> int:
    """Locate the line the summary body starts on.

    An exact 摘要 / Summary / Abstract marker wins. Only when a sheet prints no
    marker at all (APMCM from 2023 on) does the "<year> APMCM summary sheet"
    banner stand in for it, because there the body really does follow the banner.
    """
    banner = -1
    for index, text in enumerate(lines):
        if SHEET_BANNER_RE.match(text):
            if banner < 0:
                banner = index
            continue
        if ABSTRACT_RE.match(text):
            return index
    return banner


def parse_cover(lines: list[str]) -> dict[str, Any]:
    title = ""
    institution = ""
    team_id = ""
    award = ""
    abstract: list[str] = []
    keywords: list[str] = []

    for text in lines:
        if not title:
            match = TITLE_MARKER_RE.match(text)
            if match and match.group(1).strip():
                title = match.group(1).strip()
        if not institution:
            match = INSTITUTION_RE.match(text)
            if match:
                institution = match.group(1).strip()
        if not award:
            match = AWARD_RE.match(text)
            if match:
                award = match.group(1).strip()
        if not team_id:
            match = TEAM_RE.search(text)
            if match:
                team_id = match.group(1).strip(" -")

    abstract_index = abstract_start(lines)
    if abstract_index >= 0:
        match = ABSTRACT_RE.match(lines[abstract_index])
        head = (match.group(1).strip() if match else "")
        if head:
            abstract.append(head)
        for follower in lines[abstract_index + 1:]:
            keyword_match = KEYWORD_RE.match(follower)
            if keyword_match:
                keywords = split_keywords(keyword_match.group(1))
                break
            if STOP_RE.match(follower):
                break
            abstract.append(follower)

    if not title and abstract_index > 0:
        title = infer_title(lines, abstract_index)
    if not team_id:
        for text in lines[:12]:
            if BARE_TEAM_RE.match(text):
                team_id = text
                break

    return {
        "title": " ".join(WATERMARK_RE.sub("", title).split()),
        "institution": institution,
        "team_id": team_id,
        "award": award,
        "abstract": [part for part in abstract if len(part) > 8],
        "keywords": keywords[:8],
    }


def split_keywords(value: str) -> list[str]:
    parts = [part.strip(" .。;；,，、") for part in re.split(r"[;；,，、]|\s{2,}", value)]
    return [part for part in parts if 1 < len(part) <= 40]


def derive_models(*fragments: Any) -> list[str]:
    haystack = " ".join(
        part for fragment in fragments
        for part in ([fragment] if isinstance(fragment, str) else list(fragment))
    ).lower()
    found = [label for label, forms in METHOD_VOCABULARY.items()
             if any(form.lower() in haystack for form in forms)]
    return found[:6]


def summarize(paragraphs: list[str], limit: int = 140) -> str:
    text = " ".join(paragraphs).strip() if len(paragraphs) > 1 else (paragraphs[0] if paragraphs else "")
    if len(text) <= limit:
        return text
    window = text[:limit]
    ends = [match.end() for match in re.finditer(r"[。．.；;]", window)]
    return window[:ends[-1]] if ends else window.rstrip() + "…"


def keyword_note(keywords: list[str]) -> str:
    cleaned = [part for part in keywords if part]
    return (
        "题目、摘要与关键词已从论文封面解析。"
        + (f"关键词：{'、'.join(cleaned[:8])}。" if cleaned else "")
    )


def is_chinese(text: str) -> bool:
    return bool(CJK_RE.search(text))
