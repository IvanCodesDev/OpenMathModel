#!/usr/bin/env python3
"""Normalize complete COMAP, APMCM and CUMCM PDF statements for the frontend.

Requires ``pypdf`` and ``pdfplumber``. Source archives and PDFs remain in
datasets/raw. Paragraph structure, headings, lists, tables and figure placement
are recovered from page geometry by :mod:`pdf_layout`; original PDFs and data
attachments are published as local downloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader

import pdf_layout


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "datasets/raw/sources/full-problem-archives"
EXTRACTED_ROOT = RAW_ROOT / "extracted-v2"
COMAP_MANIFEST = RAW_ROOT / "comap/manifest.json"
OUTPUT = ROOT / "datasets/interim/full_problem_sources/problems.json"
LEGACY_PAGE_ROOT = ROOT / "apps/web/public/problem-pages"
FIGURE_ROOT = ROOT / "apps/web/public/problem-figures"
DOWNLOAD_ROOT = ROOT / "apps/web/public/problem-files"

CUMCM_PAGE_URLS = {
    2025: "https://www.mcm.edu.cn/html_cn/node/03c91a444e62eee81a3740fa97a461a6.html",
    2024: "https://www.mcm.edu.cn/html_cn/node/a0c1fb5c31d43551f08cd8ad16870444.html",
    2023: "https://www.mcm.edu.cn/html_cn/node/c74d72127066f510a5723a94b5323a26.html",
    2022: "https://www.mcm.edu.cn/html_cn/node/388239ded4b057d37b7b8e51e33fe903.html",
    2021: "https://www.mcm.edu.cn/html_cn/node/90d223833c1eb50f899aa096a66c6896.html",
}

APMCM_PAGE_URLS = {
    "apmcm-2024-en": "https://apmcm.org/detail/2487",
    "apmcm-2024-cn-fixed": "https://apmcm.org/detail/2478",
    "apmcm-2023": "https://apmcm.org/detail/2472",
    "apmcm-2023-wuyue": "https://apmcm.org/detail/2473",
    "apmcm-2022": "https://apmcm.org/detail/2453",
    "apmcm-2022-jan": "https://apmcm.org/detail/2463",
    "apmcm-2021": "https://apmcm.org/detail/2425",
}

APMCM_TITLES = {
    (2021, "A"): "Image Edge Analysis and Application",
    (2021, "B"): "Optimal Design of Thermal Emitter in Thermophotovoltaic Technology",
    (2021, "C"): "Construction of Ecological Conservation and Assessment of Its Impact on Environment",
    (2022, "A"): "Feature Extraction of Sequence Images and Modeling Analysis of Mold Flux Melting and Crystallization",
    (2022, "B"): "Optimal Design of High-speed Train",
    (2022, "C"): "Global Warming OR Not?",
    (2022, "D"): "Structural Optimization of Heat Transfer Fins in the Energy Storage System",
    (2022, "E"): "How Many Nuclear Bombs can Destroy the Earth?",
    (2023, "A"): "Image Recognition for Fruit-Picking Robots",
    (2023, "B"): "Microclimate Regulation in Glass Greenhouses",
    (2023, "C"): "The Development Trend of New Energy Electric Vehicles in China",
    (2024, "A"): "Research on Underwater Image Enhancement in Complex Scenarios",
    (2024, "B"): "Optimization of the Shape of Air Conditioner",
    (2024, "C"): "Development Analyses and Strategies for Pet Industry and Related Industries",
    (2024, "D"): "Exploring Frontiers in Quantum-Accelerated AI",
}

APMCM_CN_TITLES = {
    "A": "飞行器外形的优化问题",
    "B": "洪水灾害的数据分析与预测",
    "C": "基于量子计算的物流配送问题",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


SENTENCE_END_RE = re.compile(r"[。．.!?！？；;]")


def summarize(blocks: list[dict[str, Any]], fallback: str, limit: int = 120) -> str:
    """First substantive paragraph, trimmed at a sentence boundary."""
    for block in blocks:
        if block.get("type") != "paragraph":
            continue
        text = " ".join(str(block.get("text", "")).split())
        if len(text) < 40:
            continue
        if len(text) <= limit:
            return text
        window = text[:limit]
        ends = [match.end() for match in SENTENCE_END_RE.finditer(window)]
        return window[:ends[-1]] if ends else window.rstrip() + "…"
    return fallback


def discover_related_files(problem_id: str, pdf: Path) -> list[Path]:
    if problem_id.startswith("comap-"):
        return []
    if problem_id == "apmcm-2023-wuyue":
        roots = [item for item in pdf.parent.iterdir() if item.is_dir() and item.name.lower() == "attachment"]
        return sorted([item for root in roots for item in root.rglob("*") if item.is_file()])
    group_root = next((parent for parent in pdf.parents if parent.parent == EXTRACTED_ROOT), None)
    if group_root is not None and pdf.parent == group_root:
        return []
    return sorted(item for item in pdf.parent.rglob("*") if item.is_file() and item != pdf)


def publish_downloads(problem_id: str, pdf: Path, related_files: list[Path]) -> list[dict[str, Any]]:
    destination = DOWNLOAD_ROOT / problem_id
    destination.mkdir(parents=True, exist_ok=True)
    statement = destination / "problem.pdf"
    shutil.copy2(pdf, statement)
    attachments: list[dict[str, Any]] = [{
        "title": "原题 PDF",
        "url": f"/problem-files/{problem_id}/problem.pdf",
        "kind": "problem",
        "bytes": statement.stat().st_size,
        "sha256": sha256(statement),
    }]
    if related_files:
        bundle = destination / "attachments.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for item in related_files:
                archive.write(item, item.relative_to(pdf.parent).as_posix())
        attachments.append({
            "title": f"随题附件包（{len(related_files)} 个文件）",
            "url": f"/problem-files/{problem_id}/attachments.zip",
            "kind": "data",
            "bytes": bundle.stat().st_size,
            "sha256": sha256(bundle),
        })
    return attachments


def classify(title: str) -> tuple[str, list[str], list[str]]:
    lower = title.lower()
    rules = [
        (("predict", "预测", "forecast"), "预测分析", ["预测模型", "数据分析"]),
        (("optim", "优化", "调度", "design"), "规划优化", ["优化模型", "决策分析"]),
        (("image", "图像", "recognition"), "图像与数据建模", ["图像处理", "机器学习"]),
        (("network", "网络", "traffic", "交通"), "系统建模", ["网络模型", "系统分析"]),
        (("environment", "ecological", "生态", "warming"), "环境与生态", ["评价模型", "可持续发展"]),
    ]
    for keys, problem_type, directions in rules:
        if any(key in lower for key in keys):
            return problem_type, directions, [key for key in keys if key in lower][:3]
    return "综合建模", ["综合分析"], ["数学建模"]


def problem_record(*, problem_id: str, code: str, title: str, competition: str, category: str,
                   year: int, source_id: str, source_url: str, pdf: Path, source_status: str = "official") -> dict[str, Any]:
    blocks, page_count, full_text = pdf_layout.build_blocks(pdf, problem_id, FIGURE_ROOT)
    related_files = discover_related_files(problem_id, pdf)
    attachments = publish_downloads(problem_id, pdf, related_files)
    problem_type, directions, keywords = classify(title)
    return {
        "id": problem_id,
        "code": code,
        "title": title,
        "competition": competition,
        "category": category,
        "year": year,
        "problem_type": problem_type,
        "modeling_directions": directions,
        "keywords": keywords or [category, str(year)],
        "data_requirement": "含本地原题 PDF" + (f"及 {len(related_files)} 个随题附件" if related_files else ""),
        "status": "完整题面",
        "summary": summarize(blocks, title),
        "source_id": source_id,
        "source_url": source_url,
        "source_status": source_status,
        "access_scope": "stored_content",
        "attachments": attachments,
        "content_format": "structured_text",
        "content_status": "complete",
        "content_character_count": len(full_text),
        "content_block_count": len(blocks),
        "content_blocks": blocks,
        "source_pdf": {
            "path": pdf.relative_to(ROOT).as_posix(),
            "bytes": pdf.stat().st_size,
            "sha256": sha256(pdf),
            "page_count": page_count,
        },
    }


def comap_problems() -> list[dict[str, Any]]:
    records = json.loads(COMAP_MANIFEST.read_text(encoding="utf-8"))
    output = []
    for item in records:
        parts = item["code"].split()
        year, family, letter = int(parts[0]), parts[1], parts[2]
        output.append(problem_record(
            problem_id=item["id"], code=item["code"], title=item["title"],
            competition="COMAP MCM/ICM", category="美赛", year=year,
            source_id="comap_mcm_icm", source_url=item["source_url"], pdf=ROOT / item["path"],
        ))
        print(f"FULL {item['id']} blocks={output[-1]['content_block_count']} chars={output[-1]['content_character_count']}")
    return output


def cumcm_problems() -> list[dict[str, Any]]:
    output = []
    for year in range(2021, 2026):
        directory = EXTRACTED_ROOT / f"cumcm-{year}"
        found: dict[str, tuple[Path, str]] = {}
        for pdf in directory.rglob("*.pdf"):
            first_page = (PdfReader(str(pdf)).pages[0].extract_text() or "").replace("\u3000", " ")
            if "全国大学生数学建模竞赛题目" not in first_page:
                continue
            match = re.search(r"([A-E])\s*题\s+([^\r\n]+)", first_page)
            if not match:
                continue
            letter, title = match.group(1), " ".join(match.group(2).split())
            found.setdefault(letter, (pdf, title))
        if set(found) != set("ABCDE"):
            raise RuntimeError(f"CUMCM {year} missing letters: {sorted(set('ABCDE') - set(found))}")
        for letter in "ABCDE":
            pdf, title = found[letter]
            item = problem_record(
                problem_id=f"cumcm-{year}-{letter.lower()}", code=f"{year} CUMCM {letter}", title=title,
                competition="全国大学生数学建模竞赛", category="国赛", year=year,
                source_id="cumcm_official", source_url=CUMCM_PAGE_URLS[year], pdf=pdf,
            )
            output.append(item)
            print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    return output


def apmcm_problems() -> list[dict[str, Any]]:
    output = []
    regular_groups = ["apmcm-2021", "apmcm-2022", "apmcm-2022-jan", "apmcm-2023", "apmcm-2024-en"]
    for group in regular_groups:
        year = int(re.search(r"20\d{2}", group).group())
        for pdf in sorted((EXTRACTED_ROOT / group).rglob("*.pdf")):
            match = re.fullmatch(rf"{year} APMCM Problem ([A-E])\.pdf", pdf.name, re.I)
            if not match:
                continue
            letter = match.group(1).upper()
            suffix = "-jan" if group.endswith("-jan") else ""
            item = problem_record(
                problem_id=f"apmcm-{year}{suffix}-{letter.lower()}",
                code=f"{year} APMCM {letter}" + ("（1月场）" if suffix else ""),
                title=APMCM_TITLES[(year, letter)], competition="APMCM 亚太地区大学生数学建模竞赛",
                category="亚太赛", year=year, source_id="apmcm_problems", source_url=APMCM_PAGE_URLS[group], pdf=pdf,
            )
            output.append(item)
            print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    chinese_root = EXTRACTED_ROOT / "apmcm-2024-cn-fixed"
    for letter, title in APMCM_CN_TITLES.items():
        candidates = [pdf for pdf in chinese_root.rglob("*.pdf") if pdf.name.startswith(f"{letter}题 ") and "附件" not in pdf.as_posix()]
        if len(candidates) != 1:
            raise RuntimeError(f"Expected one 2024 APMCM Chinese problem {letter}, found {len(candidates)}")
        item = problem_record(
            problem_id=f"apmcm-2024-cn-{letter.lower()}", code=f"2024 APMCM 中文 {letter}", title=title,
            competition="APMCM 亚太地区大学生数学建模竞赛（中文赛项）", category="亚太赛", year=2024,
            source_id="apmcm_problems", source_url=APMCM_PAGE_URLS["apmcm-2024-cn-fixed"], pdf=candidates[0],
        )
        output.append(item)
        print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    wuyue = EXTRACTED_ROOT / "apmcm-2023-wuyue/2023 APMCM  Wuyue Cup Problems/2023 APMCM Wuyue Cup Problem.pdf"
    item = problem_record(
        problem_id="apmcm-2023-wuyue", code="2023 APMCM 五岳杯", title="APMCM Wuyue Cup Problem",
        competition="APMCM 亚太地区大学生数学建模竞赛（五岳杯）", category="亚太赛", year=2023,
        source_id="apmcm_problems", source_url=APMCM_PAGE_URLS["apmcm-2023-wuyue"], pdf=wuyue,
    )
    output.append(item)
    print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    return output


def build() -> dict[str, Any]:
    for generated_root in (LEGACY_PAGE_ROOT, FIGURE_ROOT, DOWNLOAD_ROOT):
        if generated_root.exists():
            shutil.rmtree(generated_root)
    problems = comap_problems() + cumcm_problems() + apmcm_problems()
    problems.sort(key=lambda item: (-item["year"], item["category"], item["code"]))
    result = {
        "schema_version": "1.0.0",
        "stats": {
            "problem_count": len(problems),
            "comap_count": sum(item["source_id"] == "comap_mcm_icm" for item in problems),
            "apmcm_count": sum(item["source_id"] == "apmcm_problems" for item in problems),
            "cumcm_count": sum(item["source_id"] == "cumcm_official" for item in problems),
            "page_count": sum(item["source_pdf"]["page_count"] for item in problems),
            "text_block_count": sum(
                block["type"] in {"heading", "paragraph", "list_item"}
                for item in problems for block in item["content_blocks"]
            ),
            "figure_count": sum(
                block["type"] == "image" for item in problems for block in item["content_blocks"]
            ),
            "table_count": sum(
                block["type"] == "table" for item in problems for block in item["content_blocks"]
            ),
            "paragraph_count": sum(
                block["type"] == "paragraph" for item in problems for block in item["content_blocks"]
            ),
            "attachment_count": sum(len(item["attachments"]) for item in problems),
            "download_bytes": sum(
                attachment["bytes"] for item in problems for attachment in item["attachments"]
            ),
            "content_character_count": sum(item["content_character_count"] for item in problems),
        },
        "problems": problems,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def verify() -> dict[str, Any]:
    data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    expected = {"comap_count": 30, "apmcm_count": 19, "cumcm_count": 25, "problem_count": 74}
    for key, value in expected.items():
        if data["stats"].get(key) != value:
            raise RuntimeError(f"Expected {key}={value}, found {data['stats'].get(key)}")
    for problem in data["problems"]:
        if problem["content_status"] != "complete" or problem["content_block_count"] < 1:
            raise RuntimeError(f"Incomplete problem {problem['id']}")
        if any(block["type"] == "page" for block in problem["content_blocks"]):
            raise RuntimeError(f"Page screenshot block remains in {problem['id']}")
        if not any(block["type"] in {"heading", "paragraph", "list_item"} for block in problem["content_blocks"]):
            raise RuntimeError(f"No structured text in {problem['id']}")
        source_pdf = ROOT / problem["source_pdf"]["path"]
        if sha256(source_pdf) != problem["source_pdf"]["sha256"]:
            raise RuntimeError(f"PDF hash mismatch {source_pdf}")
        for block in problem["content_blocks"]:
            if block["type"] == "image":
                asset = ROOT / "apps/web/public" / block["src"].lstrip("/")
                if not asset.exists() or asset.stat().st_size < 512:
                    raise RuntimeError(f"Missing figure asset {asset}")
        for attachment in problem["attachments"]:
            asset = ROOT / "apps/web/public" / attachment["url"].lstrip("/")
            if not asset.exists() or asset.stat().st_size != attachment["bytes"]:
                raise RuntimeError(f"Missing download asset {asset}")
            if sha256(asset) != attachment["sha256"]:
                raise RuntimeError(f"Download hash mismatch {asset}")
    print("FULL_ARCHIVE_PROBLEMS_VERIFY_OK " + json.dumps(data["stats"], ensure_ascii=False))
    return data["stats"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify", "all"))
    args = parser.parse_args()
    if args.command in {"build", "all"}:
        result = build()
        print(json.dumps(result["stats"], ensure_ascii=False))
    if args.command in {"verify", "all"}:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
