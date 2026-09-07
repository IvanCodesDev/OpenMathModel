"""论文终稿审计链（H5：§8.4「审计者链」的确定性实现）。

三条审计顺序过终稿，全是**代码判定**、不经模型转述，结果统一成 ``audit_finding``
（scope / kind / numbers / detail）进 DocumentDraft 与 G4 卡片；不硬阻断，交人裁：

- **数值审计**（``frozen_numbers.audit_document``）：正文数值 ∈ 冻结清单 ∪ 材料数值；
- **图表审计**：正文引用的「图 N」必须对应**真实图件**（图题编号匹配、插图 url 在本次
  运行产出的图件集合里），「表 N」必须在全文找得到带该编号表题的 Markdown 表格；
- **引用审计**：正文引用标记（``[n]`` / ``\\cite{key}``）与「参考文献」章的条目必须来自
  已验证的引用库（refs/）。

今天真实图件集合与引用库都还不存在（``figure_render`` / refs/ 未建），调用方传空集：
写手引用的任何图与文献在今天都无从核实，如实记为发现——这正是 §9 硬规则「引用必须
带出处 id」「模拟内容必须标明」在论文阶段的兜底。两处参数已为后续真实化留口。

``numbers`` 字段沿用契约既有名字（删属性 = BREAKING），装的是取样后的违规 token：
数值 / 图表编号 / 引用标记。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .frozen_numbers import AUDIT_SAMPLE_LIMIT, audit_document

FINDING_PHANTOM_FIGURE = "phantom_figure"
FINDING_PHANTOM_TABLE = "phantom_table"
FINDING_UNVERIFIED_CITATION = "unverified_citation"

#: 图 / 表编号：「图 1」「图1」「图 3-2」「图 2.1」「Figure 1」「Fig. 1」「Table 2」。
_LABEL_NUMBER = r"(\d{1,2}(?:[-–.]\d{1,2})?)"
_FIGURE_REF = re.compile(
    rf"(?<![地附插图])(?:图|Figure|Fig\.)\s*{_LABEL_NUMBER}(?![\d.])"
)
_TABLE_REF = re.compile(rf"(?<![附图])(?:表|Table)\s*{_LABEL_NUMBER}(?![\d.])")
#: 表题 / 图题所在行：行首可带加粗、标题井号、HTML 标签等标记。
_CAPTION_PREFIX = r"^\s*(?:[*_#>\-]+\s*|<[^>]+>\s*)*"
_TABLE_CAPTION = re.compile(rf"{_CAPTION_PREFIX}(?:表|Table)\s*{_LABEL_NUMBER}(?![\d.])")
_FIGURE_CAPTION = re.compile(rf"{_CAPTION_PREFIX}(?:图|Figure|Fig\.)\s*{_LABEL_NUMBER}(?![\d.])")
#: Markdown 表格行 / 分隔行。
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
#: 插图：Markdown 图片与 HTML <img>。
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
#: 表题与表格之间允许隔的行数（空行、说明一句）。
_CAPTION_WINDOW = 2

#: 引用标记：[3] / [1,4] / [2-5] / ［6］；排除 Markdown 链接 [x](y)、图片 ![x](y)、脚注 [^n]。
_CITATION_MARK = re.compile(
    r"(?<!!)[\[［](\d{1,3}(?:\s*[,，\-–]\s*\d{1,3})*)[\]］](?!\()"
)
_LATEX_CITE = re.compile(r"\\cite[tp]?\*?(?:\[[^\]]*\])?\{([^}]*)\}")
_MATH_SEGMENT = re.compile(
    r"\$\$.*?\$\$|\$[^$\n]*?\$|\\\(.*?\\\)|\\\[.*?\\\]", re.DOTALL
)
_REFERENCE_HEADING = re.compile(r"参考文献|references|bibliography", re.IGNORECASE)
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s*(.+?)\s*#*\s*$")
#: 参考文献条目：[1] … / 1. … / 1、… / （1）…
_REFERENCE_ENTRY = re.compile(r"^\s*(?:[\[［(（]\s*(\d{1,3})\s*[\]］)）]|(\d{1,3})\s*[.．、)）])\s*\S")


def _scopes(sections: Sequence[Mapping[str, Any]], abstract: str) -> list[tuple[str, str]]:
    """(scope 名, 正文) 序列：各章按序号计、标题原样，最后是摘要。"""
    scopes = [
        (f"第{index}章《{str(section.get('heading') or f'第 {index} 章')}》", str(section.get("content") or ""))
        for index, section in enumerate(sections, start=1)
    ]
    scopes.append(("摘要", abstract or ""))
    return scopes


def _finding(scope: str, kind: str, tokens: Sequence[str], detail: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "kind": kind,
        "numbers": list(tokens[:AUDIT_SAMPLE_LIMIT]),
        "detail": detail,
    }


def _unique(tokens: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _join(tokens: Sequence[str], limit: int = 3) -> str:
    return "、".join(tokens[:limit]) + ("…" if len(tokens) > limit else "")


# ── 图表审计 ──────────────────────────────────────────────────────────────


def _basename(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]


def _is_table_line(line: str) -> bool:
    return bool(_TABLE_ROW.match(line) or _TABLE_SEPARATOR.match(line))


def _table_blocks(lines: Sequence[str]) -> list[tuple[int, int]]:
    """Markdown 表格块的 (起, 止) 行号（含分隔行才算表格，单根竖线的句子不算）。"""
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines + [""]):
        if _is_table_line(line):
            if start is None:
                start = index
            continue
        if start is not None:
            if any(_TABLE_SEPARATOR.match(row) for row in lines[start:index]):
                blocks.append((start, index - 1))
            start = None
    return blocks


def _defined_tables(text: str) -> set[str]:
    """带编号表题、且表题 ±N 行内有 Markdown 表格的「表 N」编号。"""
    lines = text.splitlines()
    blocks = _table_blocks(lines)
    defined: set[str] = set()
    for index, line in enumerate(lines):
        match = _TABLE_CAPTION.match(line)
        if not match or _is_table_line(line):
            continue
        near = any(
            start - _CAPTION_WINDOW <= index <= end + _CAPTION_WINDOW for start, end in blocks
        )
        if near:
            defined.add(match.group(1))
    return defined


def _images(text: str) -> list[tuple[str, str, int]]:
    """插图 (alt, url, 行号)。"""
    found: list[tuple[str, str, int]] = []
    for index, line in enumerate(text.splitlines()):
        for match in _MARKDOWN_IMAGE.finditer(line):
            found.append((match.group(1), match.group(2), index))
        for match in _HTML_IMAGE.finditer(line):
            found.append(("", match.group(1), index))
    return found


def _real_figures(text: str, available_figures: set[str]) -> set[str]:
    """真实图件的图题编号：url（或其文件名）在产出集合里，编号取自 alt 或 ±N 行的图题。"""
    lines = text.splitlines()
    defined: set[str] = set()
    for alt, url, index in _images(text):
        if url not in available_figures and _basename(url) not in available_figures:
            continue
        labels = [match.group(1) for match in _FIGURE_REF.finditer(alt)]
        lo, hi = max(0, index - _CAPTION_WINDOW), min(len(lines), index + _CAPTION_WINDOW + 1)
        for line in lines[lo:hi]:
            caption = _FIGURE_CAPTION.match(line)
            if caption:
                labels.append(caption.group(1))
        defined.update(labels)
    return defined


def _references(text: str, pattern: re.Pattern[str], caption: re.Pattern[str]) -> list[str]:
    """正文里的图 / 表引用编号（图题 / 表题所在行不算引用），去重保序。"""
    labels: list[str] = []
    for line in text.splitlines():
        if caption.match(line):
            continue
        labels.extend(match.group(1) for match in pattern.finditer(line))
    return _unique(labels)


def audit_figures_and_tables(
    sections: Sequence[Mapping[str, Any]],
    abstract: str,
    available_figures: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """图表审计：引用的图必须是真实图件，引用的表必须有带编号表题的表格。

    定义在全文范围找（图题 / 表题可能在别的章），发现按章 / 摘要报：
    ``phantom_figure`` = 幽灵图引用 + 不在产出集合里的插图；``phantom_table`` = 幽灵表引用。
    """
    available = {str(item) for item in available_figures if str(item)}
    scopes = _scopes(sections, abstract)
    whole = "\n".join(text for _, text in scopes)
    tables = _defined_tables(whole)
    figures = _real_figures(whole, available)

    findings: list[dict[str, Any]] = []
    for scope, text in scopes:
        phantom_figures = [f"图 {label}" for label in _references(text, _FIGURE_REF, _FIGURE_CAPTION) if label not in figures]
        fake_images = _unique(
            _basename(url) or url
            for _alt, url, _index in _images(text)
            if url not in available and _basename(url) not in available
        )
        if phantom_figures or fake_images:
            parts: list[str] = []
            if phantom_figures:
                parts.append(f"引用了 {len(phantom_figures)} 处不存在的图（{_join(phantom_figures)}）")
            if fake_images:
                parts.append(f"插入了 {len(fake_images)} 张不是本次运行产出的图件（{_join(fake_images)}）")
            tail = (
                "本次运行没有可引用的真实图件"
                if not available
                else "图件须是本次运行的产出并带编号一致的图题"
            )
            findings.append(
                _finding(
                    scope,
                    FINDING_PHANTOM_FIGURE,
                    phantom_figures + fake_images,
                    f"{scope}{'，'.join(parts)}；{tail}",
                )
            )
        phantom_tables = [f"表 {label}" for label in _references(text, _TABLE_REF, _TABLE_CAPTION) if label not in tables]
        if phantom_tables:
            findings.append(
                _finding(
                    scope,
                    FINDING_PHANTOM_TABLE,
                    phantom_tables,
                    f"{scope}引用了 {len(phantom_tables)} 处没有对应表格的表（{_join(phantom_tables)}）"
                    "：全文找不到带该编号表题的 Markdown 表格",
                )
            )
    return findings


# ── 引用审计 ──────────────────────────────────────────────────────────────


def _expand_citation(mark: str) -> list[str]:
    """``1,3-5`` → ['1', '3', '4', '5']（区间过大按原样保留端点，防误写吞掉整段）。"""
    ids: list[str] = []
    for part in re.split(r"\s*[,，]\s*", mark.strip()):
        bounds = re.split(r"\s*[-–]\s*", part)
        if len(bounds) == 2 and all(b.isdigit() for b in bounds):
            lo, hi = int(bounds[0]), int(bounds[1])
            if lo <= hi <= lo + 20:
                ids.extend(str(n) for n in range(lo, hi + 1))
                continue
        ids.append(part.strip())
    return ids


def _reference_block(heading: str, text: str) -> tuple[str, str]:
    """把一章拆成 (正文, 参考文献列表)：整章是参考文献章，或正文里有「参考文献」标题行。"""
    if _REFERENCE_HEADING.search(heading or ""):
        return "", text
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _MARKDOWN_HEADING.match(line)
        if match and _REFERENCE_HEADING.search(match.group(1)):
            body = "\n".join(lines[:index])
            rest = lines[index + 1:]
            for offset, line in enumerate(rest):
                if _MARKDOWN_HEADING.match(line):
                    body += "\n" + "\n".join(rest[offset:])
                    rest = rest[:offset]
                    break
            return body, "\n".join(rest)
    return text, ""


def _reference_entries(block: str) -> list[str]:
    entries: list[str] = []
    for line in block.splitlines():
        match = _REFERENCE_ENTRY.match(line)
        if match:
            entries.append(match.group(1) or match.group(2))
    return _unique(entries)


def _strip_math(text: str) -> str:
    """去掉 LaTeX 数学段（``$$…$$`` / ``$…$`` / ``\\(…\\)`` / ``\\[…\\]``）：区间 ``[0,1]``、
    矩阵下标不是引用。"""
    return _MATH_SEGMENT.sub(" ", text)


def _looks_like_interval(ids: Sequence[str]) -> bool:
    """``[0,1]`` / ``[0, 100]``：两个元素且含 0——引用编号从 1 起，这是取值区间不是文献。"""
    return len(ids) == 2 and "0" in ids


def _citations(text: str) -> list[tuple[str, list[str]]]:
    """正文引用标记 (原样, 展开后的 id 列表)，去重保序。"""
    marks: list[tuple[str, list[str]]] = []
    prose = _strip_math(text)
    for match in _CITATION_MARK.finditer(prose):
        ids = _expand_citation(match.group(1))
        if _looks_like_interval(ids):
            continue
        marks.append((match.group(0), ids))
    for match in _LATEX_CITE.finditer(text):
        keys = [key.strip() for key in match.group(1).split(",") if key.strip()]
        marks.append((match.group(0), keys))
    seen: set[str] = set()
    unique: list[tuple[str, list[str]]] = []
    for mark, ids in marks:
        if mark not in seen:
            seen.add(mark)
            unique.append((mark, ids))
    return unique


def audit_citations(
    sections: Sequence[Mapping[str, Any]],
    abstract: str,
    verified_refs: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """引用审计：引用标记与参考文献条目必须来自已验证的引用库。

    ``verified_refs`` 是已验证条目的编号 / key 集合（refs/ 就绪后由节点传入）；空集时
    每一处引用都记「未经验证」，参考文献章的条目另记一条（写手编造文献最常见的落点）。
    引用了列表里没有的编号在 detail 里点明。
    """
    verified = {str(item) for item in verified_refs if str(item)}
    scopes = _scopes(sections, abstract)
    split = [
        (scope, *_reference_block(str(section.get("heading") or ""), text))
        for (scope, text), section in zip(scopes, list(sections) + [{}])
    ]
    listed: set[str] = set()
    for _scope, _body, block in split:
        listed.update(_reference_entries(block))

    findings: list[dict[str, Any]] = []
    for scope, body, block in split:
        marks = [(mark, ids) for mark, ids in _citations(body) if not all(i in verified for i in ids)]
        if marks:
            dangling = _unique(
                i for _mark, ids in marks for i in ids if listed and i not in listed and i not in verified
            )
            tail = ""
            if dangling:
                tail = f"；其中 {_join([f'[{i}]' for i in dangling])} 在参考文献列表中没有条目"
            findings.append(
                _finding(
                    scope,
                    FINDING_UNVERIFIED_CITATION,
                    [mark for mark, _ids in marks],
                    f"{scope}有 {len(marks)} 处引用未经验证（{_join([m for m, _ in marks])}）"
                    f"：参考文献库尚未建立，无法核实{tail}",
                )
            )
        entries = [entry for entry in _reference_entries(block) if entry not in verified]
        if entries:
            findings.append(
                _finding(
                    scope,
                    FINDING_UNVERIFIED_CITATION,
                    [f"[{entry}]" for entry in entries],
                    f"{scope}列出的 {len(entries)} 条参考文献均未经验证"
                    f"（{_join([f'[{e}]' for e in entries])}）：参考文献库尚未建立，条目真实性无法核实",
                )
            )
    return findings


# ── 审计链 ────────────────────────────────────────────────────────────────


def audit_chain(
    sections: Sequence[Mapping[str, Any]],
    abstract: str,
    *,
    allowed: set[str],
    abstract_allowed: set[str] | None = None,
    available_figures: Iterable[str] = (),
    verified_refs: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """顺序过三审计：数值 → 图表 → 引用；发现按审计顺序拼接（数值发现在前）。"""
    return [
        *audit_document(sections, abstract, allowed, abstract_allowed),
        *audit_figures_and_tables(sections, abstract, available_figures),
        *audit_citations(sections, abstract, verified_refs),
    ]


_KIND_LABELS = {
    "unsourced_number": "无出处数值",
    FINDING_PHANTOM_FIGURE: "图表引用不实",
    FINDING_PHANTOM_TABLE: "图表引用不实",
    FINDING_UNVERIFIED_CITATION: "引用未经验证",
}


def count_by_kind(findings: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        kind = str(finding.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def summarize_kinds(findings: Sequence[Mapping[str, Any]]) -> str:
    """「无出处数值 2 处、图表引用不实 1 处」——G4 卡片 title 的分类计数。"""
    grouped: dict[str, int] = {}
    for kind, count in count_by_kind(findings).items():
        label = _KIND_LABELS.get(kind, kind)
        grouped[label] = grouped.get(label, 0) + count
    return "、".join(f"{label} {count} 处" for label, count in grouped.items())
