"""真实图件清单（H5 figure_render 第一步：§9.1「图表 id 真实」的图源）。

论文里的图只准来自**本次运行沙盒真正落盘并被采集**的图件：实验 / 检验节点把本步产物
里 ``kind == "figure"`` 的 ArtifactRef 确定性筛成 ``figures[]`` 写进 outputs；论文节点
从上游 outputs 收成一张编号固定的清单（图 1..N）进每章材料，写手按清单插图，终稿审计
（``paper_audit.audit_figures_and_tables``）用同一张清单核对引用。

图题（caption）由画图的沙盒工程师在终答 ``figure_notes`` 里逐张说明，这里只做
**与事实求交**：说明只挂到真实存在的图件上，文件没产出的说明直接丢弃——模型声称画了
什么不算，采集到了才算。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .paper_audit import image_urls

__all__ = [
    "FIGURE_KIND",
    "PAPER_FIGURE_STAGE",
    "available_figure_names",
    "figure_inventory",
    "figure_manifest",
    "mark_inserted",
    "parse_figure_notes",
    "render_figure_material",
    "renderable_data_files",
]

#: 与 omm_agent_tools.python_runner._KIND_BY_SUFFIX 的图件 kind 一致（契约 artifact.kind enum）。
FIGURE_KIND = "figure"
#: 论文阶段按章需求补画的图（figure_render 第二步）在清单里的来源阶段。
PAPER_FIGURE_STAGE = "PAPER_WRITING"

#: 图件清单的阶段来源与顺序：实验图先编号，检验图其后（§9.1 图源只列这两处）；
#: 论文阶段补的图（extra）排在最后。
_FIGURE_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("EXPERIMENTING", ("figures",)),
    ("VALIDATING", ("robustness", "figures")),
)

#: 论文阶段补图只准读的工作区数据文件：表格 / 指标 JSON；脚本、产物副本、步骤目录与既有图件不算。
_DATA_SUFFIXES = (".csv", ".tsv", ".json")
_DATA_EXCLUDED_PREFIXES = ("steps/", "artifacts/", "figures/")

#: figure_notes 一行：「文件名 — 说明」；分隔符容忍 — / – / - / : / ：，文件名可带路径或反引号。
_NOTE_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?`?(?P<name>[^`\s—–:：]+?\.[A-Za-z0-9]{1,5})`?\s*(?:[—–\-:：]+|\s)\s*(?P<note>.+?)\s*$"
)
#: 说明的长度上限：图题是一句话，不是一段实验报告。
_NOTE_MAX_CHARS = 200


def _basename(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]


def parse_figure_notes(text: Any) -> dict[str, str]:
    """终答 ``figure_notes`` → {文件名(basename): 说明}。「无」/ 空 / 不成行的内容一律忽略。"""
    notes: dict[str, str] = {}
    for line in str(text or "").splitlines():
        match = _NOTE_LINE.match(line)
        if not match:
            continue
        name = _basename(match.group("name"))
        note = match.group("note").strip().strip("。").strip()
        if name and note and name not in notes:
            notes[name] = note[:_NOTE_MAX_CHARS]
    return notes


def figure_manifest(artifacts: Iterable[Any], figure_notes: Any = None) -> list[dict[str, Any]]:
    """本步产物里的图件 → ``[{name, artifact_id, media_type, caption}]``（按出现顺序、文件名去重）。

    只认 ``kind == "figure"`` 的产物引用（沙盒按后缀归类）；caption 来自 figure_notes 与
    真实文件名求交，没说明的图件 caption 为空串，由论文写手按文件名与实验摘要自拟。
    """
    notes = parse_figure_notes(figure_notes)
    seen: set[str] = set()
    figures: list[dict[str, Any]] = []
    for ref in artifacts:
        if str(getattr(ref, "kind", "") or "") != FIGURE_KIND:
            continue
        name = _basename(str(getattr(ref, "uri", "") or ""))
        if not name or name in seen:
            continue
        seen.add(name)
        figures.append({
            "name": name,
            "artifact_id": str(getattr(ref, "artifact_id", "") or ""),
            "media_type": str(getattr(ref, "media_type", "") or ""),
            "caption": notes.get(name, ""),
        })
    return figures


def _stage_figures(outputs: Mapping[str, Any], path: Sequence[str]) -> list[Mapping[str, Any]]:
    node: Any = outputs
    for key in path:
        if not isinstance(node, Mapping):
            return []
        node = node.get(key)
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, Mapping) and str(item.get("name") or "").strip()]


def figure_inventory(
    prior_outputs: Mapping[str, Mapping[str, Any]],
    extra: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """上游各阶段的图件（+ 论文阶段补的图）→ 编号固定的清单 ``[{number, name, artifact_id, caption, source_stage}]``。

    实验 → 检验 → 论文补图顺序、文件名去重（先到先得）；编号一经给出就是全文的「图 N」，写手按它
    插图与引用，审计按它核对——不做事后重编号（改编号要同步改正文所有引用，风险大于收益）。
    ``extra`` 是本节点刚渲染出来的图件（``figure_manifest`` 形状），来源阶段记 PAPER_WRITING。
    """
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: Mapping[str, Any], stage: str) -> None:
        name = _basename(str(item.get("name")))
        if not name or name in seen:
            return
        seen.add(name)
        inventory.append({
            "number": len(inventory) + 1,
            "name": name,
            "artifact_id": str(item.get("artifact_id") or ""),
            "caption": str(item.get("caption") or "").strip(),
            "source_stage": stage,
        })

    for stage, path in _FIGURE_SOURCES:
        outputs = prior_outputs.get(stage) or {}
        for item in _stage_figures(outputs, path):
            add(item, stage)
    for item in extra:
        if isinstance(item, Mapping) and str(item.get("name") or "").strip():
            add(item, PAPER_FIGURE_STAGE)
    return inventory


def renderable_data_files(files: Iterable[str]) -> list[str]:
    """工作区里论文补图**只准**读的数据文件：`.csv / .tsv / .json`，排除步骤目录、产物副本与既有图件。"""
    kept: list[str] = []
    for raw in files:
        path = str(raw or "").replace("\\", "/").lstrip("./")
        if not path or path in kept:
            continue
        if any(path.startswith(prefix) for prefix in _DATA_EXCLUDED_PREFIXES):
            continue
        if not path.lower().endswith(_DATA_SUFFIXES):
            continue
        kept.append(path)
    return kept


_STAGE_LABELS = {"EXPERIMENTING": "实验阶段", "VALIDATING": "检验阶段", PAPER_FIGURE_STAGE: "论文阶段补图"}


def render_figure_material(inventory: Sequence[Mapping[str, Any]]) -> str:
    """图件清单 → 论文材料段：编号｜文件名｜来源｜说明；没有图件时如实写「无」并重申纪律。"""
    if not inventory:
        return "无（本次运行没有产出图件；正文不得插入图片，也不得引用任何「图 N」）"
    lines = [
        "本次运行真实产出的图件（插图只准从此表选，编号固定；用 `![图 N 标题](文件名)` 独立成段插入，"
        "正文引用写「图 N」；未插入的图不得引用）：",
        "",
        "| 编号 | 文件名 | 来源 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for item in inventory:
        caption = str(item.get("caption") or "").strip() or "（画图工程师未给说明，按文件名与实验摘要拟题）"
        stage = _STAGE_LABELS.get(str(item.get("source_stage") or ""), str(item.get("source_stage") or ""))
        lines.append(f"| 图 {item['number']} | {item['name']} | {stage} | {caption} |")
    return "\n".join(lines)


def available_figure_names(inventory: Sequence[Mapping[str, Any]]) -> set[str]:
    """给终稿审计的真实图件集合：文件名（写手照抄的插图 url / basename 就是它）。"""
    return {str(item.get("name")) for item in inventory if str(item.get("name") or "")}


def mark_inserted(
    inventory: Sequence[Mapping[str, Any]], sections: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """清单逐条标 ``inserted``：正文任一插图的 url（或其 basename）命中文件名即已插入。"""
    urls: set[str] = set()
    for section in sections:
        for url in image_urls(str(section.get("content") or "")):
            urls.add(url)
            urls.add(_basename(url))
    return [{**dict(item), "inserted": str(item.get("name")) in urls} for item in inventory]
