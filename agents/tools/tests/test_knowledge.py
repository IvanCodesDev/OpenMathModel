"""knowledge_search / knowledge_read：卡片库的分词、别名、BM25 排序、过滤、出处与工具规格。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omm_agent_core import KnowledgePort
from omm_agent_tools import (
    KNOWLEDGE_LIBRARY_ENV,
    KNOWLEDGE_READ_TOOL,
    KNOWLEDGE_SEARCH_TOOL,
    KnowledgeLibrary,
    RecordingInvoker,
    ToolRegistry,
    knowledge_tool_specs,
    load_knowledge_library,
    resolve_library_path,
)
from omm_agent_tools.knowledge import (
    READ_CONTENT_MAX_CHARS,
    SEARCH_MAX_LIMIT,
    alias_tags,
    tokenize,
)

PROBLEMS = [
    {
        "id": "cumcm-2021-c",
        "code": "2021 CUMCM C",
        "title": "生产企业原材料的订购与运输",
        "competition": "全国大学生数学建模竞赛",
        "year": 2021,
        "problem_type": "规划优化",
        "modeling_directions": ["优化模型", "决策分析"],
        "keywords": ["订购", "运输", "供应商"],
        "summary": "为企业制定未来 24 周的原材料订购方案与转运方案。",
        "source_id": "cumcm_official",
        "source_url": "https://example.test/cumcm-2021-c",
        "access_scope": "stored_content",
        "content_blocks": [
            {"type": "heading", "level": 1, "text": "2021 高教社杯 C 题"},
            {"type": "paragraph", "text": "某建筑和装饰板材的生产企业所用原材料主要是木质纤维和其他植物素纤维材料。"},
            {"type": "table", "rows": [["供应商", "材料类型"], ["S001", "A"]]},
            {"type": "image", "src": "/problem-figures/x.png", "alt": "图"},
        ],
    },
    {
        "id": "cumcm-2020-b",
        "code": "2020 CUMCM B",
        "title": "穿越沙漠",
        "competition": "全国大学生数学建模竞赛",
        "year": 2020,
        "problem_type": "规划优化",
        "modeling_directions": ["优化模型", "博弈"],
        "keywords": ["动态规划", "博弈"],
        "summary": "玩家凭借初始资金在沙漠中行走，求最优行进与采购策略。",
        "source_id": "cumcm_official",
        "source_url": "https://example.test/cumcm-2020-b",
    },
    {
        "id": "comap-2022-icm-e",
        "code": "2022 ICM E",
        "title": "Forestry for Carbon Sequestration",
        "competition": "COMAP MCM/ICM",
        "year": 2022,
        "problem_type": "环境与生态",
        "modeling_directions": ["评价模型", "可持续发展"],
        "keywords": ["carbon", "forest"],
        "summary": "Develop a carbon sequestration model for forests and their products.",
        "source_id": "comap_mcm_icm",
        "source_url": "https://example.test/comap-2022-icm-e",
    },
    {
        "id": "cpmcm-2019-a",
        "code": "2019 CPMCM A",
        "title": "无线智能传播模型",
        "competition": "中国研究生数学建模竞赛",
        "year": 2019,
        "problem_type": "预测分析",
        "modeling_directions": ["预测模型"],
        "keywords": ["传播损耗", "神经网络"],
        "summary": "建立无线电波传播损耗的预测模型。",
        "source_id": "cpmcm_official",
        "source_url": "https://example.test/cpmcm-2019-a",
    },
]

PAPERS = [
    {
        "id": "paper-2021-c-1",
        "title": "基于混合整数规划的原材料订购与转运方案",
        "problem_id": "cumcm-2021-c",
        "problem_code": "2021 CUMCM C",
        "competition": "全国大学生数学建模竞赛",
        "year": 2021,
        "award": "国家一等奖",
        "models": ["整数规划", "TOPSIS"],
        "innovation": "把供应商评价与订购决策统一到一个 MIP 里。",
        "summary": "获奖论文。",
        "source_id": "cumcm_papers",
        "source_url": "https://example.test/paper-2021-c-1",
        "full_text_url": "https://example.test/paper-2021-c-1.pdf",
    },
    {
        "id": "paper-2021-c-2",
        "title": "供应商评价与多目标订购优化",
        "problem_id": "cumcm-2021-c",
        "problem_code": "2021 CUMCM C",
        "competition": "全国大学生数学建模竞赛",
        "year": 2021,
        "award": "国家二等奖",
        "models": ["TOPSIS", "多目标优化"],
        "innovation": "熵权 TOPSIS 选供应商，NSGA-II 求订购方案。",
        "summary": "获奖论文。",
        "source_id": "cumcm_papers",
        "source_url": "https://example.test/paper-2021-c-2",
    },
    {
        "id": "paper-2022-e-1",
        "title": "Forestry for Carbon Sequestration · Team 1",
        "problem_id": "comap-2022-icm-e",
        "problem_code": "2022 ICM E",
        "competition": "COMAP MCM/ICM",
        "year": 2022,
        "award": "Outstanding Winner",
        "models": [],
        "innovation": "A linear programming harvest schedule with a carbon budget.",
        "summary": "Outstanding Winner.",
        "source_id": "comap_mcm_icm",
        "source_url": "https://example.test/paper-2022-e-1",
    },
    {
        "id": "paper-orphan",
        "title": "!!!!\"#$%&'()",
        "problem_id": None,
        "problem_code": "2018 CPMCM F",
        "competition": "中国研究生数学建模竞赛",
        "year": 2018,
        "award": "优秀论文",
        "models": ["排队论", "离散事件仿真"],
        "innovation": "机场出租车排队与乘客到达的离散事件仿真。",
        "summary": "优秀论文。",
        "source_id": "cpmcm_papers",
        "source_url": "https://example.test/paper-orphan",
    },
]


@pytest.fixture()
def library() -> KnowledgeLibrary:
    return KnowledgeLibrary(PROBLEMS, PAPERS, source="fixture", dataset_version="test")


# ── 分词与别名 ────────────────────────────────────────────────────────────────────


def test_tokenize_mixes_ascii_words_and_cjk_bigrams() -> None:
    tokens = tokenize("基于 MIP 的订购方案 gm(1,1) 与 NSGA-II")
    assert "mip" in tokens and "nsga-ii" in tokens
    assert "订购" in tokens and "购方" in tokens and "方案" in tokens
    assert "gm" in tokens, "括号里的数字位不是词的一部分，但 gm 本身保留"
    assert "与" in tokens, "单字 CJK 串保留原字"
    assert all(len(token) >= 2 or "\u4e00" <= token <= "\u9fff" for token in tokens)


def test_alias_tags_unify_chinese_and_english_method_names() -> None:
    tag_lp = alias_tags("线性规划")
    assert tag_lp and alias_tags("we solve an LP") == tag_lp
    assert alias_tags("linear programming relaxation") == tag_lp
    assert alias_tags("ga tuning") == alias_tags("遗传算法")
    assert set(alias_tags("the game of gaussian noise")).isdisjoint(alias_tags("遗传算法")), "ga 必须按词边界匹配"
    assert alias_tags("没有任何方法名的句子") == []


# ── 建库 / 出处 / 挂接 ─────────────────────────────────────────────────────────────


def test_library_builds_prefixed_cards_with_provenance(library: KnowledgeLibrary) -> None:
    assert isinstance(library, KnowledgePort)
    assert library.available and len(library) == 8
    assert library.stats["problems"] == 4 and library.stats["papers"] == 4
    assert library.stats["indexed"] is False, "索引惰性：建库不付索引成本"
    card = library.read("problem:cumcm-2021-c")
    assert card is not None
    assert card["source_id"] == "cumcm_official" and card["source_url"].startswith("https://")
    assert card["kind"] == "problem" and "score" not in card


def test_problem_card_aggregates_linked_paper_models(library: KnowledgeLibrary) -> None:
    card = library.read("problem:cumcm-2021-c")
    assert card is not None
    assert card["linked_paper_count"] == 2
    assert card["linked_paper_models"] == [
        {"model": "TOPSIS", "count": 2},
        {"model": "整数规划", "count": 1},
        {"model": "多目标优化", "count": 1},
    ], "按出现次数降序、同次数按首次出现序"
    assert [paper["id"] for paper in card["linked_papers"]] == ["paper:paper-2021-c-1", "paper:paper-2021-c-2"]


def test_read_problem_flattens_content_blocks_and_marks_truncation(library: KnowledgeLibrary) -> None:
    card = library.read("problem:cumcm-2021-c")
    assert card is not None
    assert "2021 高教社杯 C 题" in card["content"]
    assert "供应商 | 材料类型" in card["content"], "表格取单元格"
    assert "/problem-figures" not in card["content"], "图片块不进正文"
    assert card["content_truncated"] is False and card["content_chars"] == len(card["content"])
    assert card["attachments"] == [] and card["data_requirement"] == ""

    long_blocks = [{"type": "paragraph", "text": "长" * (READ_CONTENT_MAX_CHARS + 100)}]
    big = KnowledgeLibrary([{**PROBLEMS[0], "id": "big", "content_blocks": long_blocks}], [])
    card = big.read("problem:big")
    assert card is not None
    assert len(card["content"]) == READ_CONTENT_MAX_CHARS and card["content_truncated"] is True


def test_read_paper_carries_award_models_and_linked_problem_title(library: KnowledgeLibrary) -> None:
    card = library.read("paper:paper-2021-c-1")
    assert card is not None
    assert card["award"] == "国家一等奖" and card["models"] == ["整数规划", "TOPSIS"]
    assert card["problem_id"] == "problem:cumcm-2021-c" and card["problem_title"] == "生产企业原材料的订购与运输"
    assert card["full_text_url"].endswith(".pdf")
    orphan = library.read("paper:paper-orphan")
    assert orphan is not None and orphan["problem_title"] is None
    assert orphan["title"] == "2018 CPMCM F 论文（标题解析失败）", "封面解析失败的标题换可读兜底"


def test_read_tolerates_missing_prefix_only_when_unique(library: KnowledgeLibrary) -> None:
    assert library.read("cumcm-2021-c")["id"] == "problem:cumcm-2021-c"
    assert library.read("nope") is None and library.read("") is None
    clash = KnowledgeLibrary(
        [{**PROBLEMS[0], "id": "same"}],
        [{**PAPERS[0], "id": "same"}],
    )
    assert clash.read("same") is None, "无前缀 id 撞两类卡时不猜"
    assert clash.read("paper:same")["kind"] == "paper"


# ── 检索 ─────────────────────────────────────────────────────────────────────────


def test_search_ranks_exact_title_match_first_and_carries_provenance(library: KnowledgeLibrary) -> None:
    hits = library.search("生产企业原材料的订购与运输")
    assert hits and hits[0]["id"] == "problem:cumcm-2021-c"
    assert hits[0]["score"] > hits[-1]["score"] or len(hits) == 1
    for hit in hits:
        assert hit["source_id"] and hit["source_url"] and hit["kind"] in ("problem", "paper")
    assert library.stats["indexed"] is True
    assert library.search("生产企业原材料的订购与运输") == hits, "同查询同结果"


def test_search_expands_aliases_across_languages(library: KnowledgeLibrary) -> None:
    hits = library.search("线性规划", kind="paper")
    assert [hit["id"] for hit in hits][:1] == ["paper:paper-2022-e-1"], "英文论文靠 linear programming 别名标签召回"
    hits = library.search("排队", kind="paper")
    assert hits and hits[0]["id"] == "paper:paper-orphan"


def test_search_filters_by_kind_and_task_type(library: KnowledgeLibrary) -> None:
    problems = library.search("订购 运输 供应商", kind="problem")
    assert problems and all(hit["kind"] == "problem" for hit in problems)
    papers = library.search("订购 运输 供应商", kind="paper")
    assert papers and all(hit["kind"] == "paper" for hit in papers)

    only_forecast = library.search("模型 预测 优化", task_type="预测")
    assert [hit["id"] for hit in only_forecast] == ["problem:cpmcm-2019-a"]
    via_linked_problem = library.search("订购", kind="paper", task_type="规划优化")
    assert via_linked_problem and all(hit["problem_id"] == "problem:cumcm-2021-c" for hit in via_linked_problem)
    assert library.search("订购", task_type="不存在的题型") == []
    with pytest.raises(ValueError):
        library.search("x", kind="method")


def test_search_limit_is_clamped_and_empty_query_returns_nothing(library: KnowledgeLibrary) -> None:
    assert library.search("") == [] and library.search("   ") == []
    assert len(library.search("模型 优化 预测 订购 沙漠 forest", limit=1)) == 1
    assert len(library.search("模型 优化 预测 订购 沙漠 forest", limit=SEARCH_MAX_LIMIT + 50)) <= SEARCH_MAX_LIMIT
    assert KnowledgeLibrary.empty("测试").search("订购") == []
    assert KnowledgeLibrary.empty("测试").available is False


def test_problem_hits_carry_linked_paper_models(library: KnowledgeLibrary) -> None:
    hit = library.search("原材料 订购", kind="problem", limit=1)[0]
    assert hit["id"] == "problem:cumcm-2021-c"
    assert hit["linked_paper_models"][0] == {"model": "TOPSIS", "count": 2}
    assert hit["linked_paper_count"] == 2
    assert hit["problem_type"] == "规划优化" and hit["modeling_directions"] == ["优化模型", "决策分析"]


# ── 加载与缓存 ────────────────────────────────────────────────────────────────────


def test_load_from_explicit_path_env_and_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = tmp_path / "lib.json"
    snapshot.write_text(
        json.dumps({"dataset_version": "t1", "problems": PROBLEMS, "papers": PAPERS}, ensure_ascii=False),
        encoding="utf-8",
    )
    library = load_knowledge_library(snapshot)
    assert library.available and library.dataset_version == "t1" and library.source == str(snapshot)
    assert load_knowledge_library(snapshot) is library, "同路径同 mtime → 进程内缓存"

    monkeypatch.setenv(KNOWLEDGE_LIBRARY_ENV, str(snapshot))
    assert resolve_library_path() == snapshot
    assert load_knowledge_library() is library

    missing = load_knowledge_library(tmp_path / "absent.json")
    assert missing.available is False and "不存在" in missing.unavailable_reason
    assert missing.search("订购") == [] and missing.read("x") is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert "解析失败" in load_knowledge_library(broken).unavailable_reason
    not_object = tmp_path / "list.json"
    not_object.write_text("[]", encoding="utf-8")
    assert "解析失败" in load_knowledge_library(not_object).unavailable_reason


def test_repo_snapshot_smoke() -> None:
    """仓内快照存在时做一次真语料冒烟（部署包里没有快照就跳过）。"""
    path = resolve_library_path()
    if path is None:
        pytest.skip("repo knowledge-library snapshot not present")
    library = load_knowledge_library()
    assert library.available and library.stats["problems"] > 0 and library.stats["papers"] > 0
    hits = library.search("原材料 订购 运输", kind="problem", limit=3)
    assert hits and all(hit["source_id"] for hit in hits)


# ── ToolBus 规格 ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def invoker(library: KnowledgeLibrary) -> RecordingInvoker:
    registry = ToolRegistry()
    for spec in knowledge_tool_specs(library):
        registry.register(spec)
    return RecordingInvoker(registry, lambda *_: None, caller_max_tier="readonly")


def test_specs_are_readonly_low_risk_with_strategy_in_description(library: KnowledgeLibrary) -> None:
    specs = {spec.name: spec for spec in knowledge_tool_specs(library)}
    assert set(specs) == {KNOWLEDGE_SEARCH_TOOL, KNOWLEDGE_READ_TOOL}
    for spec in specs.values():
        assert spec.tier == "readonly" and spec.risk == "low"
    assert specs[KNOWLEDGE_SEARCH_TOOL].required_args == ("query",)
    assert specs[KNOWLEDGE_READ_TOOL].required_args == ("card_id",)
    assert "先精确后概念" in specs[KNOWLEDGE_SEARCH_TOOL].description


def test_search_tool_returns_hits_and_honest_notes(invoker: RecordingInvoker) -> None:
    result = invoker.invoke("r", "s", KNOWLEDGE_SEARCH_TOOL, {"query": "原材料 订购", "kind": "problem", "limit": 2})
    assert result.ok
    assert result.output["total"] == len(result.output["hits"]) <= 2
    assert result.output["hits"][0]["id"] == "problem:cumcm-2021-c" and result.output["kind"] == "problem"

    miss = invoker.invoke("r", "s", KNOWLEDGE_SEARCH_TOOL, {"query": "量子纠缠通信卫星"})
    assert miss.ok and miss.output["hits"] == [] and "未命中" in miss.output["note"]

    assert not invoker.invoke("r", "s", KNOWLEDGE_SEARCH_TOOL, {}).ok
    assert not invoker.invoke("r", "s", KNOWLEDGE_SEARCH_TOOL, {"query": "  "}).ok
    bad_kind = invoker.invoke("r", "s", KNOWLEDGE_SEARCH_TOOL, {"query": "x", "kind": "method"})
    assert not bad_kind.ok and "kind" in (bad_kind.error or "")
    bad_limit = invoker.invoke("r", "s", KNOWLEDGE_SEARCH_TOOL, {"query": "x", "limit": "many"})
    assert not bad_limit.ok and "limit" in (bad_limit.error or "")


def test_read_tool_returns_full_card_or_fails_honestly(invoker: RecordingInvoker) -> None:
    result = invoker.invoke("r", "s", KNOWLEDGE_READ_TOOL, {"card_id": "paper:paper-2021-c-1"})
    assert result.ok and result.output["award"] == "国家一等奖"
    missing = invoker.invoke("r", "s", KNOWLEDGE_READ_TOOL, {"card_id": "paper:nope"})
    assert not missing.ok and "未找到卡片" in (missing.error or "")
    assert not invoker.invoke("r", "s", KNOWLEDGE_READ_TOOL, {}).ok


def test_search_tool_on_empty_library_reports_reason() -> None:
    registry = ToolRegistry()
    for spec in knowledge_tool_specs(KnowledgeLibrary.empty("知识库快照不存在：x")):
        registry.register(spec)
    invoker = RecordingInvoker(registry, lambda *_: None, caller_max_tier="readonly")
    result = invoker.invoke("r", "s", KNOWLEDGE_SEARCH_TOOL, {"query": "订购"})
    assert result.ok and result.output["hits"] == [] and "不存在" in result.output["note"]


def test_specs_accept_any_knowledge_port_duck_typed() -> None:
    """装配方注入的可以是任何 KnowledgePort（测试替身 / 其它实现）：没有
    ``available`` 属性就按可用处理，search / read 原样透传。"""

    class DuckPort:
        def __init__(self) -> None:
            self.queries: list[dict] = []

        def search(self, query, *, kind=None, task_type=None, limit=8):
            self.queries.append({"query": query, "kind": kind, "task_type": task_type, "limit": limit})
            return [{"id": "problem:duck", "kind": "problem", "title": "鸭子题"}]

        def read(self, card_id):
            return {"id": card_id, "kind": "problem", "title": "鸭子题", "content": "全卡"} if card_id == "problem:duck" else None

    port = DuckPort()
    registry = ToolRegistry()
    for spec in knowledge_tool_specs(port):
        registry.register(spec)
    invoker = RecordingInvoker(registry, lambda *_: None, caller_max_tier="readonly")

    result = invoker.invoke("r", "s", KNOWLEDGE_SEARCH_TOOL, {"query": "鸭子", "task_type": "优化", "limit": 3})
    assert result.ok and result.output["hits"][0]["id"] == "problem:duck" and result.output["task_type"] == "优化"
    assert port.queries == [{"query": "鸭子", "kind": None, "task_type": "优化", "limit": 3}]
    card = invoker.invoke("r", "s", KNOWLEDGE_READ_TOOL, {"card_id": "problem:duck"})
    assert card.ok and card.output["content"] == "全卡"
    assert not invoker.invoke("r", "s", KNOWLEDGE_READ_TOOL, {"card_id": "problem:none"}).ok
