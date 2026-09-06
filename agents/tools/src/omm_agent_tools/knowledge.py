"""knowledge_search / knowledge_read：赛题与获奖论文卡片库的只读检索（设计 §10.3，H3 薄版一）。

「知识库不是文件夹，是可检索的经验」（§10.3.1）：这里把导出快照
``knowledge-library.json``（§10.3.2 单一事实源的三消费者之一）读成两类卡片
——``problem:<id>`` 赛题卡、``paper:<id>`` 获奖论文卡——并建一份进程内倒排，
供方案阶段的 Proposer 预检索「相似赛题与获奖论文用了什么模型」，以及后续
智能体经 ToolBus 自行检索 / 顺链跳读。

实现口径（MVP，stdlib-only，纯文件 + 内存，§10.3.4）：
- 分词 = ASCII 词（小写，≥2 字符）+ CJK 字二元组（不引入分词依赖：「调度」
  「排队论」都能召回）+ 受控词表的别名标签（线性规划 ↔ LP ↔ linear programming
  等，文档侧与查询侧打同一个 ``§g<n>`` 记号，做概念层的召回）；
- BM25 排序（k1=1.2，b=0.75）；标题 ×3、关键词 / 建模方向 / 论文 models ×2 加权；
  赛题正文只索引前 ``PROBLEM_CONTENT_INDEX_CHARS`` 字——全文 1.15 MB 进倒排换不来
  召回质量；
- 首次 ``search`` 才建索引（惰性）：API 每 tick 都重新装配节点，装配期付不起
  索引成本；建好后按卡片数不变复用；
- 每条结果都带出处（``source_id`` / ``source_url``）：没有出处的卡不是知识（§10.3.5）；
- 赛题卡聚合「挂接获奖论文用过的模型」（``linked_paper_models``），一次检索就能
  回答「同类题别人用了什么」。

检索策略写死在工具描述里：**先精确后概念，绝不从模糊匹配开始**——题面里的
专名 / 题号 / 竞赛名先查（高 idf 命中），再退到建模方向 / 别名标签这类概念词。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from omm_agent_core import ToolResult

from .registry import ToolCallContext, ToolSpec

__all__ = [
    "ALIAS_GROUPS",
    "CARD_KINDS",
    "KNOWLEDGE_LIBRARY_ENV",
    "KNOWLEDGE_READ_TOOL",
    "KNOWLEDGE_SEARCH_TOOL",
    "PROBLEM_CONTENT_INDEX_CHARS",
    "READ_CONTENT_MAX_CHARS",
    "SEARCH_DEFAULT_LIMIT",
    "SEARCH_MAX_LIMIT",
    "KnowledgeCard",
    "KnowledgeLibrary",
    "knowledge_tool_specs",
    "load_knowledge_library",
    "resolve_library_path",
    "tokenize",
]

CARD_KINDS: tuple[str, ...] = ("problem", "paper")
KNOWLEDGE_SEARCH_TOOL = "knowledge_search"
KNOWLEDGE_READ_TOOL = "knowledge_read"
#: 环境变量：显式指定快照路径（部署时快照不一定随源码走）。
KNOWLEDGE_LIBRARY_ENV = "OMM_KNOWLEDGE_LIBRARY"
#: 找不到显式路径时按序探测的仓内候选（相对仓库根）：未来正典位在前，现快照在后。
DEFAULT_LIBRARY_CANDIDATES: tuple[str, ...] = (
    "datasets/knowledge/knowledge-library.json",
    "apps/web/src/data/knowledge-library.json",
)
#: 赛题正文进倒排的字数上限（标题 / 关键词 / 摘要 / 建模方向不受此限）。
PROBLEM_CONTENT_INDEX_CHARS = 1500
#: knowledge_read 返回的赛题正文上限（超长截断并标注）。
READ_CONTENT_MAX_CHARS = 6000
SEARCH_DEFAULT_LIMIT = 8
SEARCH_MAX_LIMIT = 20
#: 命中卡片里摘要类字段的截断长度（命中列表是给人 / 模型扫的，不是全文）。
_HIT_TEXT_CHARS = 200

_BM25_K1 = 1.2
_BM25_B = 0.75
_TITLE_WEIGHT = 3
_TAG_WEIGHT = 2
_LINKED_MODELS_LIMIT = 12
_LINKED_PAPERS_LIMIT = 20

#: 受控词表（§10.3.3 methods 卡的雏形）：同组任一说法出现即打同一标签。
#: ASCII 说法按词边界匹配（"ga" 不能命中 "game"），中文说法按子串匹配。
#: 有歧义的短缩写（nlp / logistic 等）刻意不收。
ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("线性规划", "lp", "linear programming"),
    ("整数规划", "混合整数规划", "mip", "milp", "ilp", "integer programming"),
    ("非线性规划", "nonlinear programming"),
    ("动态规划", "dp", "dynamic programming"),
    ("多目标", "多目标优化", "多目标规划", "nsga", "nsga-ii", "pareto", "multi-objective"),
    ("遗传算法", "ga", "genetic algorithm"),
    ("模拟退火", "simulated annealing"),
    ("粒子群", "pso", "particle swarm"),
    ("蚁群", "aco", "ant colony"),
    ("启发式", "元启发式", "heuristic", "metaheuristic"),
    ("图论", "网络流", "最短路", "dijkstra", "network flow", "shortest path"),
    ("路径规划", "车辆路径", "旅行商", "vrp", "tsp", "vehicle routing"),
    ("调度", "排程", "scheduling"),
    ("选址", "facility location", "p-median"),
    ("库存", "inventory"),
    ("排队论", "排队", "queueing", "queuing"),
    ("博弈", "博弈论", "game theory", "nash"),
    ("元胞自动机", "cellular automata", "cellular automaton"),
    ("微分方程", "常微分方程", "偏微分方程", "ode", "pde", "differential equation"),
    ("传染病模型", "sir", "seir", "compartmental model"),
    ("马尔可夫", "markov", "hmm"),
    ("蒙特卡洛", "monte carlo", "随机模拟"),
    ("仿真", "离散事件", "simulation", "agent-based"),
    ("回归", "线性回归", "逻辑回归", "regression"),
    ("时间序列", "arima", "sarima", "prophet", "time series", "lstm"),
    ("预测", "forecast", "forecasting", "prediction"),
    ("聚类", "k-means", "kmeans", "dbscan", "层次聚类", "clustering"),
    ("分类", "支持向量机", "svm", "classification", "classifier"),
    ("随机森林", "决策树", "梯度提升", "random forest", "xgboost", "lightgbm", "gbdt", "decision tree"),
    ("神经网络", "深度学习", "neural network", "deep learning", "cnn", "rnn", "transformer", "mlp"),
    ("主成分", "因子分析", "降维", "pca", "principal component"),
    ("层次分析", "综合评价", "熵权", "ahp", "topsis"),
    ("灰色预测", "灰色模型", "gm(1,1)", "grey model", "gray model"),
    ("模糊", "模糊综合评价", "fuzzy"),
    ("贝叶斯", "bayes", "bayesian"),
    ("假设检验", "方差分析", "显著性", "anova", "hypothesis test"),
    ("敏感性分析", "sensitivity analysis"),
    ("插值", "拟合", "interpolation", "curve fitting", "spline"),
    ("图像", "计算机视觉", "image", "computer vision"),
    ("文本", "自然语言", "text mining", "natural language"),
    ("优化", "optimization", "optimisation"),
    ("能源", "电力", "energy", "power grid"),
    ("交通", "traffic", "transportation"),
    ("物流", "供应链", "logistics", "supply chain"),
    ("环境", "生态", "environment", "ecology", "ecological"),
    ("金融", "投资", "资产", "finance", "portfolio"),
    ("排放", "碳", "carbon", "emission"),
    ("水", "水资源", "water"),
)

_ASCII_WORD = re.compile(r"[a-z0-9]+(?:[.\-/][a-z0-9]+)*")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_HAS_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _alias_matchers() -> tuple[tuple[str, tuple[str, ...], re.Pattern[str] | None], ...]:
    matchers: list[tuple[str, tuple[str, ...], re.Pattern[str] | None]] = []
    for index, group in enumerate(ALIAS_GROUPS):
        cjk = tuple(phrase for phrase in group if _HAS_CJK.search(phrase))
        ascii_phrases = [phrase for phrase in group if not _HAS_CJK.search(phrase)]
        pattern = None
        if ascii_phrases:
            alternatives = "|".join(re.escape(phrase) for phrase in sorted(ascii_phrases, key=len, reverse=True))
            pattern = re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])")
        matchers.append((f"§g{index}", cjk, pattern))
    return tuple(matchers)


_ALIAS_MATCHERS = _alias_matchers()


def tokenize(text: str) -> list[str]:
    """文本 → 记号列表（ASCII 词 + CJK 字二元组；单字 CJK 串保留原字）。"""
    lowered = text.lower()
    tokens: list[str] = _ASCII_WORD.findall(lowered)
    tokens = [token for token in tokens if len(token) >= 2]
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def alias_tags(text: str) -> list[str]:
    """文本里出现的受控词表标签（每组至多一次）。"""
    lowered = text.lower()
    tags: list[str] = []
    for tag, cjk_phrases, ascii_pattern in _ALIAS_MATCHERS:
        if any(phrase in lowered for phrase in cjk_phrases) or (
            ascii_pattern is not None and ascii_pattern.search(lowered)
        ):
            tags.append(tag)
    return tags


def _clean_strs(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    cleaned: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_READABLE = re.compile(r"[A-Za-z\u3400-\u4dbf\u4e00-\u9fff]")


def _readable_title(title: str, fallback: str) -> str:
    """封面解析失败的标题（如 ``!!!!"#$%``）没有任何字母 / 汉字：换成可读的兜底。"""
    return title if _READABLE.search(title) else fallback


def _blocks_text(blocks: Any) -> str:
    """content_blocks → 纯文本（heading / paragraph / list_item / code 取 text，
    table 取单元格，image / document_break 跳过）。"""
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "table":
            for row in block.get("rows") or []:
                if isinstance(row, list):
                    parts.append(" | ".join(str(cell) for cell in row))
            continue
        if kind in ("image", "document_break"):
            continue
        text = _text(block.get("text"))
        if text:
            parts.append(text)
    return "\n".join(parts)


@dataclass(frozen=True)
class KnowledgeCard:
    """一张卡：赛题（problem）或获奖论文（paper）；``id`` 带类别前缀。"""

    id: str
    kind: str
    raw_id: str
    title: str
    year: int | None
    competition: str
    code: str
    source_id: str
    source_url: str
    problem_type: str = ""
    modeling_directions: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    summary: str = ""
    content: str = ""
    data_requirement: str = ""
    attachments: tuple[str, ...] = ()
    award: str = ""
    models: tuple[str, ...] = ()
    innovation: str = ""
    institution: str = ""
    problem_id: str | None = None
    full_text_url: str = ""
    access_scope: str = ""

    def filter_text(self) -> str:
        """task_type 过滤用的口径：题型 + 建模方向（论文借挂接赛题的，见 library）。"""
        return " ".join((self.problem_type, *self.modeling_directions)).lower()


def _problem_card(record: Mapping[str, Any]) -> KnowledgeCard | None:
    raw_id = _text(record.get("id"))
    title = _text(record.get("title"))
    if not raw_id or not title:
        return None
    attachments = tuple(
        _text(item.get("title"))
        for item in record.get("attachments") or []
        if isinstance(item, Mapping) and _text(item.get("title"))
    )
    return KnowledgeCard(
        id=f"problem:{raw_id}",
        kind="problem",
        raw_id=raw_id,
        title=title,
        year=_int_or_none(record.get("year")),
        competition=_text(record.get("competition")),
        code=_text(record.get("code")),
        source_id=_text(record.get("source_id")),
        source_url=_text(record.get("source_url")),
        problem_type=_text(record.get("problem_type")),
        modeling_directions=tuple(_clean_strs(record.get("modeling_directions"))),
        keywords=tuple(_clean_strs(record.get("keywords"))),
        summary=_text(record.get("summary")),
        content=_blocks_text(record.get("content_blocks")),
        data_requirement=_text(record.get("data_requirement")),
        attachments=attachments,
        access_scope=_text(record.get("access_scope")),
    )


def _paper_card(record: Mapping[str, Any]) -> KnowledgeCard | None:
    raw_id = _text(record.get("id"))
    title = _text(record.get("title"))
    if not raw_id or not title:
        return None
    linked = _text(record.get("problem_id"))
    code = _text(record.get("problem_code"))
    return KnowledgeCard(
        id=f"paper:{raw_id}",
        kind="paper",
        raw_id=raw_id,
        title=_readable_title(title, f"{code or _text(record.get('competition'))} 论文（标题解析失败）"),
        year=_int_or_none(record.get("year")),
        competition=_text(record.get("competition")),
        code=code,
        source_id=_text(record.get("source_id")),
        source_url=_text(record.get("source_url")),
        summary=_text(record.get("summary")),
        award=_text(record.get("award")),
        models=tuple(_clean_strs(record.get("models"))),
        innovation=_text(record.get("innovation")),
        institution=_text(record.get("institution")),
        problem_id=f"problem:{linked}" if linked else None,
        full_text_url=_text(record.get("full_text_url")),
        access_scope=_text(record.get("access_scope")),
    )


@dataclass
class _Index:
    postings: dict[str, tuple[array, array]]
    doc_len: array
    avg_len: float
    doc_count: int


class KnowledgeLibrary:
    """卡片库：加载 → （惰性）倒排 → ``search`` / ``read``（实现 core 的 KnowledgePort）。"""

    def __init__(
        self,
        problems: Iterable[Mapping[str, Any]] = (),
        papers: Iterable[Mapping[str, Any]] = (),
        *,
        source: str = "",
        dataset_version: str = "",
        unavailable_reason: str = "",
    ) -> None:
        self.source = source
        self.dataset_version = dataset_version
        self.unavailable_reason = unavailable_reason
        self._cards: list[KnowledgeCard] = []
        self._by_id: dict[str, int] = {}
        self._linked_papers: dict[str, list[int]] = defaultdict(list)
        for record in problems:
            card = _problem_card(record) if isinstance(record, Mapping) else None
            if card is not None and card.id not in self._by_id:
                self._by_id[card.id] = len(self._cards)
                self._cards.append(card)
        for record in papers:
            card = _paper_card(record) if isinstance(record, Mapping) else None
            if card is not None and card.id not in self._by_id:
                self._by_id[card.id] = len(self._cards)
                self._cards.append(card)
                if card.problem_id:
                    self._linked_papers[card.problem_id].append(self._by_id[card.id])
        self._index: _Index | None = None
        self._lock = threading.Lock()

    # -- construction helpers --------------------------------------------------

    @classmethod
    def empty(cls, reason: str = "") -> "KnowledgeLibrary":
        return cls(unavailable_reason=reason or "知识库为空")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, source: str = "") -> "KnowledgeLibrary":
        return cls(
            payload.get("problems") or [],
            payload.get("papers") or [],
            source=source,
            dataset_version=_text(payload.get("dataset_version")),
        )

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> "KnowledgeLibrary":
        """读快照文件；解析失败按原样抛（``load_knowledge_library`` 负责兜成空库）。"""
        file = Path(path)
        with file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, Mapping):
            raise ValueError("knowledge library payload must be a JSON object")
        return cls.from_payload(payload, source=str(file))

    # -- introspection -----------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._cards)

    @property
    def stats(self) -> dict[str, Any]:
        problems = sum(1 for card in self._cards if card.kind == "problem")
        return {
            "available": self.available,
            "problems": problems,
            "papers": len(self._cards) - problems,
            "source": self.source,
            "dataset_version": self.dataset_version,
            "indexed": self._index is not None,
        }

    def __len__(self) -> int:
        return len(self._cards)

    # -- indexing --------------------------------------------------------------------

    def _card_terms(self, card: KnowledgeCard) -> Counter[str]:
        terms: Counter[str] = Counter()
        for _ in range(_TITLE_WEIGHT):
            terms.update(tokenize(card.title))
        boosted = " ".join((card.problem_type, *card.modeling_directions, *card.keywords, *card.models))
        for _ in range(2):
            terms.update(tokenize(boosted))
        plain = " ".join((card.competition, card.code, card.summary, card.innovation, card.award))
        terms.update(tokenize(plain))
        if card.content:
            terms.update(tokenize(card.content[:PROBLEM_CONTENT_INDEX_CHARS]))
        semantic = " ".join(
            (card.title, boosted, card.summary, card.innovation, card.content[:PROBLEM_CONTENT_INDEX_CHARS])
        )
        for tag in alias_tags(semantic):
            terms[tag] += _TAG_WEIGHT
        return terms

    def _build_index(self) -> _Index:
        postings: dict[str, tuple[list[int], list[int]]] = defaultdict(lambda: ([], []))
        doc_len = array("L")
        for doc_id, card in enumerate(self._cards):
            terms = self._card_terms(card)
            doc_len.append(sum(terms.values()))
            for term, tf in terms.items():
                ids, tfs = postings[term]
                ids.append(doc_id)
                tfs.append(min(tf, 65535))
        packed = {
            term: (array("L", ids), array("H", tfs)) for term, (ids, tfs) in postings.items()
        }
        count = len(self._cards)
        avg_len = (sum(doc_len) / count) if count else 0.0
        return _Index(postings=packed, doc_len=doc_len, avg_len=avg_len or 1.0, doc_count=count)

    def ensure_index(self) -> None:
        if self._index is not None:
            return
        with self._lock:
            if self._index is None:
                self._index = self._build_index()

    # -- query -------------------------------------------------------------------------

    @staticmethod
    def query_terms(query: str) -> list[str]:
        """查询 → 去重记号（词 + 二元组 + 别名标签）；空查询 → []。"""
        seen: dict[str, None] = {}
        for token in tokenize(query):
            seen.setdefault(token, None)
        for tag in alias_tags(query):
            seen.setdefault(tag, None)
        return list(seen)

    def _scores(self, terms: Sequence[str]) -> dict[int, float]:
        index = self._index
        assert index is not None
        scores: dict[int, float] = defaultdict(float)
        n_docs = index.doc_count
        for term in terms:
            posting = index.postings.get(term)
            if posting is None:
                continue
            ids, tfs = posting
            df = len(ids)
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            for doc_id, tf in zip(ids, tfs):
                norm = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * index.doc_len[doc_id] / index.avg_len)
                scores[doc_id] += idf * tf * (_BM25_K1 + 1.0) / (tf + norm)
        return scores

    def _matches_task_type(self, card: KnowledgeCard, needle: str) -> bool:
        if needle in card.filter_text():
            return True
        if card.kind == "paper" and card.problem_id:
            linked = self._by_id.get(card.problem_id)
            if linked is not None and needle in self._cards[linked].filter_text():
                return True
        return False

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        task_type: str | None = None,
        limit: int = SEARCH_DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """BM25 检索；``kind`` / ``task_type`` 是过滤不是加权；结果按分数降序、同分按卡片序。"""
        if kind is not None and kind not in CARD_KINDS:
            raise ValueError(f"unknown card kind {kind!r}; expected one of {CARD_KINDS}")
        limit = max(1, min(int(limit), SEARCH_MAX_LIMIT))
        terms = self.query_terms(query)
        if not terms or not self._cards:
            return []
        self.ensure_index()
        needle = str(task_type or "").strip().lower()
        ranked = sorted(self._scores(terms).items(), key=lambda item: (-item[1], item[0]))
        hits: list[dict[str, Any]] = []
        for doc_id, score in ranked:
            card = self._cards[doc_id]
            if kind is not None and card.kind != kind:
                continue
            if needle and not self._matches_task_type(card, needle):
                continue
            hits.append(self._hit(card, score))
            if len(hits) >= limit:
                break
        return hits

    def read(self, card_id: str) -> dict[str, Any] | None:
        """全卡：赛题带正文（截断标注）与挂接论文清单；论文带全部元数据与全文链接。"""
        card = self._lookup(card_id)
        if card is None:
            return None
        payload = self._hit(card, None)
        payload.pop("score", None)
        if card.kind == "problem":
            payload["summary"] = card.summary
            payload["data_requirement"] = card.data_requirement
            payload["attachments"] = list(card.attachments)
            payload["content"] = card.content[:READ_CONTENT_MAX_CHARS]
            payload["content_truncated"] = len(card.content) > READ_CONTENT_MAX_CHARS
            payload["content_chars"] = len(card.content)
            payload["linked_papers"] = [
                {
                    "id": paper.id,
                    "title": paper.title,
                    "year": paper.year,
                    "award": paper.award,
                    "models": list(paper.models),
                }
                for paper in self._linked(card.id)[:_LINKED_PAPERS_LIMIT]
            ]
        else:
            payload["summary"] = card.summary
            payload["innovation"] = card.innovation
            linked = self._by_id.get(card.problem_id or "")
            payload["problem_title"] = self._cards[linked].title if linked is not None else None
        return payload

    # -- helpers ------------------------------------------------------------------------

    def _lookup(self, card_id: str) -> KnowledgeCard | None:
        wanted = str(card_id or "").strip()
        if not wanted:
            return None
        index = self._by_id.get(wanted)
        if index is None:
            # 容忍无前缀 id（模型抄卡片 id 时常把前缀丢了）：唯一匹配才认
            candidates = [self._by_id[f"{kind}:{wanted}"] for kind in CARD_KINDS if f"{kind}:{wanted}" in self._by_id]
            if len(candidates) != 1:
                return None
            index = candidates[0]
        return self._cards[index]

    def _linked(self, problem_card_id: str) -> list[KnowledgeCard]:
        return [self._cards[index] for index in self._linked_papers.get(problem_card_id, [])]

    def linked_paper_models(self, problem_card_id: str) -> list[dict[str, Any]]:
        """挂接获奖论文用过的模型，按出现次数降序、同次数按首次出现序。"""
        counts: Counter[str] = Counter()
        order: dict[str, int] = {}
        for paper in self._linked(problem_card_id):
            for model in paper.models:
                counts[model] += 1
                order.setdefault(model, len(order))
        ranked = sorted(counts.items(), key=lambda item: (-item[1], order[item[0]]))
        return [{"model": model, "count": count} for model, count in ranked[:_LINKED_MODELS_LIMIT]]

    def _hit(self, card: KnowledgeCard, score: float | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": card.id,
            "kind": card.kind,
            "title": card.title,
            "year": card.year,
            "competition": card.competition,
            "code": card.code,
            "source_id": card.source_id,
            "source_url": card.source_url,
            "access_scope": card.access_scope,
        }
        if score is not None:
            payload["score"] = round(score, 3)
        if card.kind == "problem":
            payload.update(
                {
                    "problem_type": card.problem_type,
                    "modeling_directions": list(card.modeling_directions),
                    "keywords": list(card.keywords),
                    "summary": _clip(card.summary, _HIT_TEXT_CHARS),
                    "linked_paper_count": len(self._linked_papers.get(card.id, [])),
                    "linked_paper_models": self.linked_paper_models(card.id),
                }
            )
        else:
            payload.update(
                {
                    "award": card.award,
                    "models": list(card.models),
                    "institution": card.institution,
                    "problem_id": card.problem_id,
                    "summary": _clip(card.summary, _HIT_TEXT_CHARS),
                    "innovation": _clip(card.innovation, _HIT_TEXT_CHARS),
                    "full_text_url": card.full_text_url,
                }
            )
        return payload


# ── 加载与缓存 ────────────────────────────────────────────────────────────────


def _repo_root() -> Path:
    # agents/tools/src/omm_agent_tools/knowledge.py → 仓库根（editable 安装下成立；
    # 打包部署时靶不中，走环境变量显式指定）。
    return Path(__file__).resolve().parents[4]


def resolve_library_path(path: str | os.PathLike[str] | None = None) -> Path | None:
    """显式路径 → ``OMM_KNOWLEDGE_LIBRARY`` → 仓内候选；都不存在 → None。"""
    if path is not None:
        candidate = Path(path)
        return candidate if candidate.is_file() else None
    env_value = os.environ.get(KNOWLEDGE_LIBRARY_ENV, "").strip()
    if env_value:
        candidate = Path(env_value)
        return candidate if candidate.is_file() else None
    root = _repo_root()
    for relative in DEFAULT_LIBRARY_CANDIDATES:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


_CACHE: dict[str, tuple[int, KnowledgeLibrary]] = {}
_CACHE_LOCK = threading.Lock()


def load_knowledge_library(path: str | os.PathLike[str] | None = None) -> KnowledgeLibrary:
    """进程内缓存的加载入口：找不到 / 解析失败 → ``available=False`` 的空库（记原因，不抛）。

    缓存键 = 解析后的绝对路径，mtime 变了重新加载（快照被重新导出时不用重启进程）。
    """
    resolved = resolve_library_path(path)
    if resolved is None:
        wanted = str(path) if path is not None else (
            os.environ.get(KNOWLEDGE_LIBRARY_ENV, "").strip() or " / ".join(DEFAULT_LIBRARY_CANDIDATES)
        )
        return KnowledgeLibrary.empty(f"知识库快照不存在：{wanted}")
    key = str(resolved.resolve())
    try:
        mtime = resolved.stat().st_mtime_ns
    except OSError as exc:
        return KnowledgeLibrary.empty(f"知识库快照不可读：{exc}")
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            library = KnowledgeLibrary.from_path(resolved)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            library = KnowledgeLibrary.empty(f"知识库快照解析失败（{resolved.name}）：{exc}")
        _CACHE[key] = (mtime, library)
        return library


# ── ToolBus 规格（只读，H3 薄版一：先实现与单测，注册随智能体检索路径落地）─────


def knowledge_tool_specs(library: KnowledgeLibrary) -> list[ToolSpec]:
    """两个只读工具：``knowledge_search`` / ``knowledge_read``（tier=readonly）。"""

    def search_handler(arguments: dict[str, Any], _ctx: ToolCallContext) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(status="failed", error="query 不能为空")
        kind = _optional_text(arguments.get("kind"))
        if kind is not None and kind not in CARD_KINDS:
            return ToolResult(
                status="failed", error=f"kind 只能是 {' / '.join(CARD_KINDS)}，收到 {kind!r}"
            )
        task_type = _optional_text(arguments.get("task_type"))
        raw_limit = arguments.get("limit", SEARCH_DEFAULT_LIMIT)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return ToolResult(status="failed", error=f"limit 必须是整数，收到 {raw_limit!r}")
        if not library.available:
            return ToolResult(
                status="succeeded",
                output={
                    "query": query,
                    "hits": [],
                    "total": 0,
                    "note": library.unavailable_reason or "知识库不可用",
                },
            )
        hits = library.search(query, kind=kind, task_type=task_type, limit=limit)
        output: dict[str, Any] = {"query": query, "hits": hits, "total": len(hits)}
        if kind:
            output["kind"] = kind
        if task_type:
            output["task_type"] = task_type
        if not hits:
            output["note"] = "未命中：先换更精确的专名 / 题号 / 竞赛名，再退到建模方向等概念词"
        return ToolResult(status="succeeded", output=output)

    def read_handler(arguments: dict[str, Any], _ctx: ToolCallContext) -> ToolResult:
        card_id = str(arguments.get("card_id") or "").strip()
        if not card_id:
            return ToolResult(status="failed", error="card_id 不能为空")
        card = library.read(card_id)
        if card is None:
            return ToolResult(status="failed", error=f"未找到卡片：{card_id}")
        return ToolResult(status="succeeded", output=card)

    return [
        ToolSpec(
            name=KNOWLEDGE_SEARCH_TOOL,
            description=(
                "检索赛题与获奖论文卡片库（关键词 / BM25 + 别名扩展），返回带出处的命中列表；"
                "赛题卡附「挂接获奖论文用过的模型」。参数：query（必填）、kind（problem / paper）、"
                f"task_type（题型或建模方向子串）、limit（≤{SEARCH_MAX_LIMIT}）。"
                "策略：先精确后概念——先用题面里的专名 / 题号 / 竞赛名查，未命中再退到建模方向、"
                "方法名等概念词，绝不从模糊匹配开始；命中后用 knowledge_read 顺链读全卡。只读。"
            ),
            handler=search_handler,
            risk="low",
            timeout_s=15.0,
            required_args=("query",),
            tier="readonly",
        ),
        ToolSpec(
            name=KNOWLEDGE_READ_TOOL,
            description=(
                "按卡片 id（如 problem:cumcm-2021-c / paper:…）读全卡：赛题含正文"
                f"（至多 {READ_CONTENT_MAX_CHARS} 字，超长标注）与挂接论文清单，论文含奖项 / 模型 / 全文链接。只读。"
            ),
            handler=read_handler,
            risk="low",
            timeout_s=10.0,
            required_args=("card_id",),
            tier="readonly",
        ),
    ]
