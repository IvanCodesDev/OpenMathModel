"""本次运行的已验证引用库（H5 refs/ 第一步：§10.3.5「论文引用只准来自 refs/ 已验证条目」的图源）。

``datasets/knowledge/refs/`` 今天还没有文件化；本次运行能核实的引用条目只有两类，都来自
知识库卡片（记录级出处：``source_url`` / ``full_text_url``）：

- **方案引用的先例**：方案卡 ``approach / fit / rationale / steps`` 文本里按模板纪律标出的
  卡片 id（``[problem:…]`` / ``[paper:…]``）——方案阶段真正借鉴了谁；
- **用户提供的资料**：首页「添加上下文」挑的赛题 / 论文（``params.reference_metadata``，只有
  标题），按标题在知识库里精确匹配回卡。

方案节点（有 ``KnowledgePort``）把两类条目解析成 ``references[]`` 写进 outputs；论文节点按
G1 选中的方案取子集、编成 [1..N] 进每章材料，写手只准引 ``[n]``、参考文献章逐条照抄，终稿
审计按同一张表核编号与条目正文。解析不到的卡片 id / 匹配不到的标题**不入库**（没有可核出处
就不装作有），只记警告。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from omm_agent_core import KnowledgePort

from .paper_audit import cited_reference_ids

__all__ = [
    "REFERENCE_SOURCE_PLAN",
    "REFERENCE_SOURCE_USER",
    "build_reference_library",
    "card_ids_in_text",
    "format_reference_entry",
    "mark_cited",
    "reference_inventory",
    "reference_titles",
    "render_reference_material",
    "verified_reference_ids",
]

#: 与 document-draft.v1 ``paper_reference.source`` enum 对齐。
REFERENCE_SOURCE_PLAN = "plan_citation"
REFERENCE_SOURCE_USER = "user_reference"

#: 方案文本里的卡片 id 标记（与 nodes.knowledge_hit_ids 同一口径）。
_CARD_REF = re.compile(r"\[((?:problem|paper):[^\]\s]+)\]")
#: 方案卡里可能带卡片 id 的文本字段。
_PLAN_TEXT_KEYS = ("name", "approach", "fit", "rationale", "steps", "risks")
#: 用户资料按标题匹配时的检索候选数。
_TITLE_MATCH_LIMIT = 5
_HTTP_URL = re.compile(r"^https?://\S+$")
_TITLE_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)

#: ``cited_by`` 里方案 id 之外的两个固定来源。
CITED_BY_RATIONALE = "rationale"
CITED_BY_USER = "user"


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_text_of(item) for item in value)
    if isinstance(value, Mapping):
        return "\n".join(_text_of(item) for item in value.values())
    return "" if value is None else str(value)


def card_ids_in_text(*texts: Any) -> list[str]:
    """文本里出现的卡片 id（去重、保序）。"""
    seen: dict[str, None] = {}
    for text in texts:
        for match in _CARD_REF.finditer(_text_of(text)):
            seen.setdefault(match.group(1), None)
    return list(seen)


def _normalize_title(text: Any) -> str:
    return _TITLE_NOISE.sub("", str(text or "")).casefold()


def _http_url(*candidates: Any) -> str | None:
    for candidate in candidates:
        text = str(candidate or "").strip()
        if _HTTP_URL.match(text):
            return text
    return None


def format_reference_entry(card: Mapping[str, Any]) -> str:
    """卡片元数据 → 参考文献条目正文（不含编号）；全文 / 来源链接写成 Markdown 链接。

    论文卡：「{机构 或 “{赛事} 参赛队 {队号}”}. {题目}[Z]. {赛事} {年份}，{奖项}. [全文](url)」；
    赛题卡：「{赛事} {年份} {题号}. {题目}[Z]. [来源](url)」。字段缺哪个就少哪段，不编造。
    """
    kind = str(card.get("kind") or "")
    title = str(card.get("title") or "").strip()
    competition = str(card.get("competition") or "").strip()
    year = card.get("year")
    year_text = str(year).strip() if year not in (None, "") else ""
    parts: list[str] = []
    if kind == "paper":
        institution = str(card.get("institution") or "").strip()
        team = str(card.get("team_id") or "").strip()
        author = institution or (f"{competition} 参赛队 {team}".strip() if team else competition)
        if author:
            parts.append(f"{author}.")
        parts.append(f"{title}[Z].")
        venue = " ".join(bit for bit in (competition, year_text) if bit)
        award = str(card.get("award") or "").strip()
        if venue or award:
            parts.append(f"{venue}{'，' if venue and award else ''}{award}.")
        url = _http_url(card.get("full_text_url"), card.get("source_url"))
        if url:
            parts.append(f"[全文]({url})")
    else:
        head = " ".join(bit for bit in (competition, year_text, str(card.get("code") or "").strip()) if bit)
        if head:
            parts.append(f"{head}.")
        parts.append(f"{title}[Z].")
        url = _http_url(card.get("source_url"))
        if url:
            parts.append(f"[来源]({url})")
    return " ".join(parts)


def _entry(card: Mapping[str, Any], source: str, cited_by: Sequence[str]) -> dict[str, Any]:
    return {
        "card_id": str(card.get("id") or ""),
        "kind": str(card.get("kind") or ""),
        "title": str(card.get("title") or "").strip(),
        "text": format_reference_entry(card),
        "url": _http_url(card.get("full_text_url"), card.get("source_url")),
        "source": source,
        "cited_by": list(cited_by),
    }


def _read_card(port: KnowledgePort, card_id: str) -> Mapping[str, Any] | None:
    try:
        card = port.read(card_id)
    except Exception:  # noqa: BLE001 — 知识库是材料不是闸门：读不到就当没有
        return None
    if not isinstance(card, Mapping) or not str(card.get("title") or "").strip():
        return None
    return card


def _match_title(port: KnowledgePort, kind: str, title: str) -> Mapping[str, Any] | None:
    """按标题在知识库里精确匹配（归一化相等）；BM25 候选里没有同题的不算命中。"""
    wanted = _normalize_title(title)
    if not wanted:
        return None
    try:
        hits = port.search(title, kind=kind, limit=_TITLE_MATCH_LIMIT)
    except Exception:  # noqa: BLE001
        return None
    for hit in hits:
        if isinstance(hit, Mapping) and _normalize_title(hit.get("title")) == wanted:
            return _read_card(port, str(hit.get("id") or "")) or hit
    return None


def build_reference_library(
    port: KnowledgePort | None,
    plans: Sequence[Mapping[str, Any]],
    rationale: Any = None,
    reference_metadata: Iterable[Any] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """方案阶段结算引用库：(条目列表, 警告列表)。

    条目 = 方案卡文本里的卡片 id（``cited_by`` 记方案 id）+ 方案集 rationale 里的 id
    （``cited_by: ["rationale"]``）+ 用户资料按标题匹配到的卡（``cited_by: ["user"]``）；同一张卡
    合并 ``cited_by``。端口缺席 → 空库；解析不到的 id / 匹配不到的标题记警告、不入库。
    """
    warnings: list[str] = []
    if port is None:
        return [], warnings
    entries: dict[str, dict[str, Any]] = {}

    def add(card: Mapping[str, Any], source: str, cited_by: str) -> None:
        card_id = str(card.get("id") or "")
        existing = entries.get(card_id)
        if existing is None:
            entries[card_id] = _entry(card, source, [cited_by])
        elif cited_by not in existing["cited_by"]:
            existing["cited_by"].append(cited_by)

    unresolved: list[str] = []
    for plan in plans:
        if not isinstance(plan, Mapping):
            continue
        plan_id = str(plan.get("id") or "").strip()
        for card_id in card_ids_in_text(*(plan.get(key) for key in _PLAN_TEXT_KEYS)):
            card = _read_card(port, card_id)
            if card is None:
                unresolved.append(card_id)
                continue
            add(card, REFERENCE_SOURCE_PLAN, plan_id or CITED_BY_RATIONALE)
    for card_id in card_ids_in_text(rationale):
        card = _read_card(port, card_id)
        if card is None:
            unresolved.append(card_id)
            continue
        add(card, REFERENCE_SOURCE_PLAN, CITED_BY_RATIONALE)
    unresolved = list(dict.fromkeys(unresolved))
    if unresolved:
        warnings.append(
            f"方案文本引用了 {len(unresolved)} 张知识库里不存在的卡片（{'、'.join(unresolved[:5])}），不进引用库"
        )

    unmatched: list[str] = []
    for item in reference_metadata:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "").strip()
        title = str(item.get("title") or "").strip()
        if kind not in ("problem", "paper") or not title:
            continue
        card = _match_title(port, kind, title)
        if card is None:
            unmatched.append(title)
            continue
        add(card, REFERENCE_SOURCE_USER, CITED_BY_USER)
    if unmatched:
        warnings.append(
            f"用户提供的 {len(unmatched)} 份资料在知识库里找不到同题条目（{'、'.join(unmatched[:3])}），不进引用库"
        )
    return list(entries.values()), warnings


def reference_inventory(
    prior_outputs: Mapping[str, Mapping[str, Any]], plan_id: Any
) -> list[dict[str, Any]]:
    """方案阶段的引用库 → 本篇论文的可引用文献表 ``[{number, title, text, url, source, card_id}]``。

    只取 G1 选中方案引用的、方案集 rationale 引用的、用户提供的条目（未选方案独有的先例
    不进论文）；顺序 = 方案引用（按结算顺序）→ 用户提供；编号一经给出就是全文的「[n]」。
    """
    planning = prior_outputs.get("MODEL_PLANNING") or {}
    raw = planning.get("references")
    if not isinstance(raw, list):
        return []
    wanted = {str(plan_id or ""), CITED_BY_RATIONALE, CITED_BY_USER}
    selected = [
        item for item in raw
        if isinstance(item, Mapping) and str(item.get("title") or "").strip()
        and wanted & {str(tag) for tag in (item.get("cited_by") or [])}
    ]
    ordered = [item for item in selected if str(item.get("source")) != REFERENCE_SOURCE_USER]
    ordered += [item for item in selected if str(item.get("source")) == REFERENCE_SOURCE_USER]
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ordered:
        card_id = str(item.get("card_id") or "")
        key = card_id or _normalize_title(item.get("title"))
        if key in seen:
            continue
        seen.add(key)
        source = str(item.get("source") or "")
        inventory.append({
            "number": len(inventory) + 1,
            "title": str(item.get("title") or "").strip(),
            "text": str(item.get("text") or "").strip() or str(item.get("title") or "").strip(),
            "url": _http_url(item.get("url")),
            "source": source if source in (REFERENCE_SOURCE_PLAN, REFERENCE_SOURCE_USER) else REFERENCE_SOURCE_PLAN,
            "card_id": card_id or None,
        })
    return inventory


_SOURCE_LABELS = {REFERENCE_SOURCE_PLAN: "方案引用的先例", REFERENCE_SOURCE_USER: "用户提供"}


def render_reference_material(inventory: Sequence[Mapping[str, Any]]) -> str:
    """可引用文献表 → 论文材料段；库为空时如实写「无」并重申纪律。"""
    if not inventory:
        return (
            "无（本次运行没有可核实的引用条目；正文不得使用 [n] / \\cite{} 等引用标记，"
            "也不得写参考文献列表）"
        )
    lines = [
        "本次运行可核实的引用条目（正文引用只准写 `[n]`，n 取自此表；论文末章「参考文献」按编号逐条写 "
        "`[n] 条目`，条目正文逐字照抄本表、不得增删改；表外文献一律不得引用）：",
        "",
        "| 编号 | 条目 | 来源 |",
        "| --- | --- | --- |",
    ]
    for item in inventory:
        source = _SOURCE_LABELS.get(str(item.get("source") or ""), str(item.get("source") or ""))
        lines.append(f"| [{item['number']}] | {item['text']} | {source} |")
    return "\n".join(lines)


def verified_reference_ids(inventory: Sequence[Mapping[str, Any]]) -> set[str]:
    """给引用审计的已验证编号集合。"""
    return {str(item["number"]) for item in inventory if item.get("number")}


def reference_titles(inventory: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """给引用审计核条目正文的 编号 → 标题。"""
    return {str(item["number"]): str(item.get("title") or "") for item in inventory if item.get("number")}


def mark_cited(
    inventory: Sequence[Mapping[str, Any]],
    sections: Sequence[Mapping[str, Any]],
    abstract: str = "",
) -> list[dict[str, Any]]:
    """文献表逐条标 ``cited``：正文（含摘要）任一引用标记展开后命中编号即已引用。"""
    cited = cited_reference_ids(sections, abstract)
    return [{**dict(item), "cited": str(item.get("number")) in cited} for item in inventory]
