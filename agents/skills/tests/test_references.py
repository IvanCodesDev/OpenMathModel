"""本次运行的已验证引用库（references.py）：卡片 id 解析、条目格式、方案阶段结算、论文阶段取子集与核对。"""

from __future__ import annotations

from omm_agent_skills import (
    REFERENCE_SOURCE_PLAN,
    REFERENCE_SOURCE_USER,
    audit_citations,
    build_reference_library,
    card_ids_in_text,
    format_reference_entry,
    mark_cited,
    reference_inventory,
    reference_titles,
    render_reference_material,
    verified_reference_ids,
)

PAPER = {
    "id": "paper:comap-2025-d-2504188",
    "kind": "paper",
    "title": "A Roadmap to a Better City · Team 2504188",
    "year": 2025,
    "competition": "COMAP MCM/ICM",
    "award": "Outstanding Winner",
    "institution": "Nanjing University of Posts & Telecommunications",
    "source_url": "http://www.contest.comap.org/undergraduate/contests/mcm/contests/2025/results/#d",
    "full_text_url": "https://github.com/example/papers/2025/D/2504188.pdf",
}
PAPER_NO_INSTITUTION = {
    "id": "paper:cumcm-2021-c-01",
    "kind": "paper",
    "title": "基于混合整数规划的订购方案",
    "year": 2021,
    "competition": "全国大学生数学建模竞赛",
    "award": "国家一等奖",
    "institution": "",
    "source_url": "https://example.test/paper-2021-c-1",
    "full_text_url": "",
}
PROBLEM = {
    "id": "problem:cumcm-2021-c",
    "kind": "problem",
    "title": "生产企业原材料的订购与运输",
    "year": 2021,
    "competition": "全国大学生数学建模竞赛",
    "code": "2021 CUMCM C",
    "source_url": "https://example.test/cumcm-2021-c",
}


class Port:
    """KnowledgePort 假端口：read 按 id，search 按 kind 回固定候选（标题匹配靠调用方）。"""

    def __init__(self, cards=(PAPER, PAPER_NO_INSTITUTION, PROBLEM), fail=False):
        self.cards = {card["id"]: card for card in cards}
        self.fail = fail
        self.reads: list[str] = []
        self.searches: list[tuple[str, str | None]] = []

    def read(self, card_id):
        self.reads.append(card_id)
        if self.fail:
            raise RuntimeError("index corrupt")
        return dict(self.cards[card_id]) if card_id in self.cards else None

    def search(self, query, *, kind=None, task_type=None, limit=8):
        self.searches.append((query, kind))
        if self.fail:
            raise RuntimeError("index corrupt")
        return [dict(card) for card in self.cards.values() if kind is None or card["kind"] == kind][:limit]


# -- 卡片 id 与条目格式 -------------------------------------------------------------


def test_card_ids_in_text_scans_strings_lists_and_mappings_in_order():
    plan = {
        "approach": "借鉴 [problem:cumcm-2021-c] 的分层思路，再看 [paper:comap-2025-d-2504188]",
        "steps": ["先 [paper:comap-2025-d-2504188]", "后 [problem:cumcm-2021-c]"],
        "risks": ["区间 [0,1] 与编号 [3] 不是卡片"],
    }
    assert card_ids_in_text(plan["approach"], plan["steps"], plan["risks"]) == [
        "problem:cumcm-2021-c", "paper:comap-2025-d-2504188",
    ]
    assert card_ids_in_text(None, "", 42) == []


def test_format_reference_entry_uses_real_metadata_and_markdown_links():
    assert format_reference_entry(PAPER) == (
        "Nanjing University of Posts & Telecommunications. A Roadmap to a Better City · Team 2504188[Z]. "
        "COMAP MCM/ICM 2025，Outstanding Winner. [全文](https://github.com/example/papers/2025/D/2504188.pdf)"
    )
    # 没有机构 → 用赛事名顶作者位；没有全文链接 → 退到来源页；缺什么少什么、不编造
    assert format_reference_entry(PAPER_NO_INSTITUTION) == (
        "全国大学生数学建模竞赛. 基于混合整数规划的订购方案[Z]. 全国大学生数学建模竞赛 2021，国家一等奖. "
        "[全文](https://example.test/paper-2021-c-1)"
    )
    assert format_reference_entry(PROBLEM) == (
        "全国大学生数学建模竞赛 2021 2021 CUMCM C. 生产企业原材料的订购与运输[Z]. [来源](https://example.test/cumcm-2021-c)"
    )
    # 非 http(s) 的链接不进条目
    assert format_reference_entry({**PROBLEM, "source_url": "javascript:alert(1)"}).endswith("[Z].")


# -- 方案阶段结算 -------------------------------------------------------------------


def test_build_reference_library_resolves_plan_citations_rationale_and_user_references():
    port = Port()
    plans = [
        {"id": "A", "approach": "分层建模，借鉴 [problem:cumcm-2021-c]", "steps": ["复用 [paper:comap-2025-d-2504188] 的评价体系"]},
        {"id": "B", "approach": "启发式，借鉴 [paper:comap-2025-d-2504188] 与不存在的 [paper:ghost]"},
    ]
    references, warnings = build_reference_library(
        port,
        plans,
        rationale="综合看 [problem:cumcm-2021-c] 的做法更稳",
        reference_metadata=[
            {"kind": "paper", "title": "基于混合整数规划的订购方案", "excerpt": "…"},
            {"kind": "problem", "title": "不存在的赛题", "excerpt": "…"},
            {"kind": "method", "title": "TOPSIS", "excerpt": "…"},
            "garbage",
        ],
    )
    assert [r["card_id"] for r in references] == [
        "problem:cumcm-2021-c", "paper:comap-2025-d-2504188", "paper:cumcm-2021-c-01",
    ]
    assert references[0]["cited_by"] == ["A", "rationale"] and references[0]["source"] == REFERENCE_SOURCE_PLAN
    assert references[1]["cited_by"] == ["A", "B"]
    assert references[2] == {
        "card_id": "paper:cumcm-2021-c-01",
        "kind": "paper",
        "title": "基于混合整数规划的订购方案",
        "text": format_reference_entry(PAPER_NO_INSTITUTION),
        "url": "https://example.test/paper-2021-c-1",
        "source": REFERENCE_SOURCE_USER,
        "cited_by": ["user"],
    }
    assert references[0]["url"] == "https://example.test/cumcm-2021-c"
    assert warnings == [
        "方案文本引用了 1 张知识库里不存在的卡片（paper:ghost），不进引用库",
        "用户提供的 1 份资料在知识库里找不到同题条目（不存在的赛题），不进引用库",
    ]
    # 用户资料按标题精确匹配、只在对应 kind 里找；method / 垃圾条目不查
    assert port.searches == [("基于混合整数规划的订购方案", "paper"), ("不存在的赛题", "problem")]


def test_build_reference_library_without_port_or_with_failing_port_is_empty_and_quiet():
    assert build_reference_library(None, [{"id": "A", "approach": "[problem:cumcm-2021-c]"}]) == ([], [])
    failing = Port(fail=True)
    references, warnings = build_reference_library(
        failing, [{"id": "A", "approach": "[problem:cumcm-2021-c]"}],
        reference_metadata=[{"kind": "paper", "title": "基于混合整数规划的订购方案"}],
    )
    assert references == []
    assert warnings[0].startswith("方案文本引用了 1 张知识库里不存在的卡片")
    assert warnings[1].startswith("用户提供的 1 份资料在知识库里找不到同题条目")


# -- 论文阶段：按选中方案取子集、编号、材料、审计集合、已引用 ----------------------------


def library_outputs():
    references, _warnings = build_reference_library(
        Port(),
        [
            {"id": "A", "approach": "[problem:cumcm-2021-c]"},
            {"id": "B", "approach": "[paper:comap-2025-d-2504188]"},
        ],
        rationale=None,
        reference_metadata=[{"kind": "paper", "title": "基于混合整数规划的订购方案"}],
    )
    return {"MODEL_PLANNING": {"plans": [{"id": "A"}, {"id": "B"}], "references": references}}


def test_reference_inventory_keeps_chosen_plan_and_user_entries_in_fixed_order():
    inventory = reference_inventory(library_outputs(), "A")
    assert [(r["number"], r["card_id"], r["source"]) for r in inventory] == [
        (1, "problem:cumcm-2021-c", REFERENCE_SOURCE_PLAN),
        (2, "paper:cumcm-2021-c-01", REFERENCE_SOURCE_USER),
    ]
    assert set(inventory[0]) == {"number", "title", "text", "url", "source", "card_id"}
    # 选另一张方案卡：换成它引用的先例，用户资料照常在
    assert [r["card_id"] for r in reference_inventory(library_outputs(), "B")] == [
        "paper:comap-2025-d-2504188", "paper:cumcm-2021-c-01",
    ]
    # 缺库 / 畸形 → 空
    assert reference_inventory({}, "A") == []
    assert reference_inventory({"MODEL_PLANNING": {"references": "oops"}}, "A") == []
    assert reference_inventory({"MODEL_PLANNING": {"references": [{"title": "", "cited_by": ["A"]}]}}, "A") == []


def test_render_reference_material_and_audit_sets():
    inventory = reference_inventory(library_outputs(), "A")
    material = render_reference_material(inventory)
    assert "| 编号 | 条目 | 来源 |" in material
    assert "| [1] | 全国大学生数学建模竞赛 2021 2021 CUMCM C. 生产企业原材料的订购与运输[Z]. [来源](https://example.test/cumcm-2021-c) | 方案引用的先例 |" in material
    assert "| [2] | 全国大学生数学建模竞赛. 基于混合整数规划的订购方案[Z]. 全国大学生数学建模竞赛 2021，国家一等奖. [全文](https://example.test/paper-2021-c-1) | 用户提供 |" in material
    assert "逐字照抄本表" in material
    assert render_reference_material([]).startswith("无（本次运行没有可核实的引用条目")
    assert verified_reference_ids(inventory) == {"1", "2"}
    assert reference_titles(inventory) == {"1": "生产企业原材料的订购与运输", "2": "基于混合整数规划的订购方案"}


def test_mark_cited_and_audit_agree_on_the_same_library():
    inventory = reference_inventory(library_outputs(), "A")
    sections = [
        {"heading": "5 模型建立", "content": "分层思路借鉴了赛题先例[1]，取值区间 [0,1] 与公式 $x_{[2]}$ 不算引用。"},
        {"heading": "参考文献", "content": f"[1] {inventory[0]['text']}\n[2] {inventory[1]['text']}"},
    ]
    marked = mark_cited(inventory, sections, "摘要不引用。")
    assert [(r["number"], r["cited"]) for r in marked] == [(1, True), (2, False)]
    assert "cited" not in inventory[0], "原表不被改写"
    # 同一张表喂审计：编号在库、条目照抄 → 0 发现
    assert audit_citations(sections, "", verified_reference_ids(inventory), reference_titles(inventory)) == []
    # 条目被改写成另一篇 → 记发现；引用表外编号 → 记发现
    forged = [
        sections[0],
        {"heading": "参考文献", "content": f"[1] 张三. 编造的一本书. 2020.\n[2] {inventory[1]['text']}"},
    ]
    findings = audit_citations(forged, "另见 [3]。", verified_reference_ids(inventory), reference_titles(inventory))
    assert [(f["scope"], f["numbers"]) for f in findings] == [
        ("第2章《参考文献》", ["[1]"]),
        ("摘要", ["[3]"]),
    ]
    assert "库中为《生产企业原材料的订购与运输》" in findings[0]["detail"]
    assert "不在本次运行的引用库（2 条）中" in findings[1]["detail"]
