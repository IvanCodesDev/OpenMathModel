#!/usr/bin/env python3
"""Normalize complete COMAP, APMCM, CUMCM, MathorCup and Huashu Cup statements.

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

import html_layout
import pdf_layout


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "datasets/raw/sources/full-problem-archives"
EXTRACTED_ROOT = RAW_ROOT / "extracted-v2"
COMAP_MANIFEST = RAW_ROOT / "comap/manifest.json"
TIPDM_CUP_MANIFEST = RAW_ROOT / "tipdm-cup/manifest.json"
TIPDM_BDRACE_MANIFEST = RAW_ROOT / "tipdm-cup-bdrace/manifest.json"
TJJMDS_MANIFEST = RAW_ROOT / "tjjmds/manifest.json"

# What counts as "the problem" in a notice announcing a choose-your-own-topic
# contest: the numbered section naming the theme or the topic categories, plus
# the inline appendix spelling the topic requirements out. Everything else in
# the notice is contest administration.
TJJMDS_KEEP_SECTION_RE = re.compile(r"^[一二三四五六七八九十]+、\s*.{0,14}(主题|选题)")
TJJMDS_SECTION_RE = re.compile(r"^[一二三四五六七八九十]+、")
TJJMDS_KEEP_APPENDIX_RE = re.compile(r"^(附件\s*1\s*[：:]\s*)?选题具体要求$")
TJJMDS_APPENDIX_RE = re.compile(r"^附件\s*\d")
# Lines on the interpretation page that exist only to route readers to the
# video edition; the text edition follows them.
TJJMDS_VIDEO_CHROME_RE = re.compile(r"二维码|扫一扫|^《.*》(（[^）]*）)?$")
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
    2020: "https://www.mcm.edu.cn/html_cn/node/10405905647c52abfd6377c0311632b5.html",
    2019: "https://www.mcm.edu.cn/html_cn/node/b0ae8510b9ec0cc0deb2266d2de19ecb.html",
    2018: "https://www.mcm.edu.cn/html_cn/node/7cec7725b9a0ea07b4dfd175e8042c33.html",
    2017: "https://www.mcm.edu.cn/html_cn/node/460baf68ab0ed0e1e557a0c79b1c4648.html",
    2016: "https://www.mcm.edu.cn/html_cn/node/6d026d84bd785435f92e3079b4a87a2b.html",
    2015: "https://www.mcm.edu.cn/html_cn/node/ac8b96613522ef62c019d1cd45a125e3.html",
}

# The header line that separates a statement from its appendices. Present on all
# 55 statements from 2015 on and on none of the appendix or format-guide PDFs, so
# it is the discriminator -- filenames are not (2022-2025 ship bare "A题.pdf",
# and 2025's are mojibake: "A╠Γ.pdf").
CUMCM_MARKER = "全国大学生数学建模竞赛题目"
# Two title layouts: "A 题 太阳影子定位" throughout, and "问题 B 智能 RGV..."
# which 2018 B and 2019 C use instead.
CUMCM_TITLE_PATTERNS = (
    re.compile(r"([A-E])\s*题[:：]?\s+([^\r\n]+)"),
    re.compile(r"问题\s*([A-E])[:：]?\s*([^\r\n]+)"),
)

APMCM_PAGE_URLS = {
    "apmcm-2026": "https://apmcm.org/detail/2510",
    "apmcm-2024-en": "https://apmcm.org/detail/2487",
    "apmcm-2024-cn-fixed": "https://apmcm.org/detail/2478",
    "apmcm-2023": "https://apmcm.org/detail/2472",
    "apmcm-2023-wuyue": "https://apmcm.org/detail/2473",
    "apmcm-2022": "https://apmcm.org/detail/2453",
    "apmcm-2022-jan": "https://apmcm.org/detail/2463",
    "apmcm-2021": "https://apmcm.org/detail/2425",
    "apmcm-2018": "https://apmcm.org/detail/2316",
    "apmcm-2017": "https://apmcm.org/detail/2315",
    "apmcm-2016": "https://apmcm.org/detail/2314",
    "apmcm-2015": "https://apmcm.org/detail/2313",
}

# 2019, 2020 and 2025 are deliberately absent. Their detail pages publish the
# statements only through publicqn.saikr.com, pan.baidu.com, saikr.com and
# aic.modelers.cn -- none of them an official APMCM host -- so under the
# official-domain-only rule there is nothing here to collect.
APMCM_OFFSITE_YEARS = (2019, 2020, 2025)

# Letters each staged group actually ran, pinned rather than inferred from what the
# walk happens to find, so a statement that fails to match is an error instead of a
# silent omission. 2017 and 2018 really did ship only A and B; the 2022 January
# session is a separate group carrying D and E.
APMCM_GROUP_LETTERS = {
    "apmcm-2015": "ABC", "apmcm-2016": "ABC", "apmcm-2017": "AB", "apmcm-2018": "AB",
    "apmcm-2021": "ABC", "apmcm-2022": "ABC", "apmcm-2022-jan": "DE",
    "apmcm-2023": "ABC", "apmcm-2024-en": "ABCD",
    "apmcm-2024-cn-fixed": "ABC", "apmcm-2026": "ABC",
}

APMCM_TITLES = {
    (2015, "A"): "The impact of the development strategy of the Maritime Silk Road",
    (2015, "B"): "Dynamic evaluation model of urban public transport service level",
    (2015, "C"): "Identifying the error connections in the network",
    (2016, "A"): "Temperature and key element content prediction based on optical information data",
    (2016, "B"): "The Influence of Chemical Element on Properties of Deformed Steel Bar",
    (2016, "C"): "Evaluation and Customization of Film and Television",
    (2017, "A"): "Effects of Sleep on Human Body",
    (2017, "B"): "Spray Trajectory Planning Issues",
    (2018, "A"): "Real-time training model for elderly people balance ability",
    (2018, "B"): "Talents and Urban Development",
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
    (2024, "A"): "飞行器外形的优化问题",
    (2024, "B"): "洪水灾害的数据分析与预测",
    (2024, "C"): "基于量子计算的物流配送问题",
    (2026, "A"): "自来水厂水质预测与评估",
    (2026, "B"): "高性能芯片热管理系统的优化问题",
    (2026, "C"): "创业社区规划与资源配置优化问题",
}

# The Chinese-track statement is named "<letter>题 <title>.pdf". The same prefix
# names the directory holding its appendices, which is how a statement is told
# apart from an appendix that happens to be a PDF.
APMCM_CN_STATEMENT_RE = re.compile(r"^([A-E])题[\s　]")

MATHORCUP_PAGE_URLS = {
    2023: "https://mathorcup.org/detail/2417",
    2026: "https://mathorcup.org/detail/2487",
}

# 2018 remains staged because its official archive is still downloadable, but
# its PDFs expose a broken custom-font text map.  2024 exposes headings while
# mapping most body glyphs to punctuation.  Publishing either would violate the
# structured-text requirement, so only the two fully decodable official groups
# are emitted.  The gap is deliberate and documented in the source registry.
MATHORCUP_GROUP_LETTERS = {2023: "ABCD", 2026: "ABCDE"}
MATHORCUP_TITLES = {
    (2023, "A"): "量子计算机在信用评分卡组合优化中的应用",
    (2023, "B"): "城市轨道交通列车时刻表优化问题",
    (2023, "C"): "电商物流网络包裹应急调运与结构优化问题",
    (2023, "D"): "航空安全风险分析和飞行技术评估问题",
    (2026, "A"): "基于量子计算的智慧物流优化建模与算法设计",
    (2026, "B"): "机器人竞技策略的优化问题",
    (2026, "C"): "中老年人群高血脂症的风险预警及干预方案优化",
    (2026, "D"): "多场景、多目标货物运输装箱策略优化",
    (2026, "E"): "罕见病药品医保谈判定价模型及用药成本优化研究",
}

HUASHU_CUP_NOTICE_URL = "https://m.saikr.com/contest/notice_detail/44136"
# The 2020-2025 statements arrive in one organiser archive; every live edition is
# published as its own bundle once the contest closes, so 2026 carries a separate
# notice page and staging directory.
HUASHU_CUP_2026_NOTICE_URL = "https://m.saikr.com/contest/notice_detail/46038"
HUASHU_CUP_TITLES = {
    (2020, "A"): "带相变材料的低温防护服御寒仿真模拟",
    (2020, "B"): "工业零件切割优化方案设计",
    (2020, "C"): "脱贫帮扶绩效评价",
    (2021, "A"): "电动汽车无线充电优化匹配研究",
    (2021, "B"): "进出口公司的货物装运策略",
    (2021, "C"): "电动汽车目标客户销售策略研究",
    (2022, "A"): "环形振荡器的优化设计",
    (2022, "B"): "水下机器人的组装计划",
    (2022, "C"): "插层熔喷非织造材料的性能控制研究",
    (2023, "A"): "隔热材料的结构优化控制研究",
    (2023, "B"): "不透明制品最优配色方案设计",
    (2023, "C"): "母亲身心健康对婴儿成长的影响",
    (2024, "A"): "机器臂关节角路径的优化设计",
    (2024, "B"): "VLSI 电路单元的自动布局",
    (2024, "C"): "老外游中国",
    (2025, "A"): "多孔膜光反射性能的优化与控制",
    (2025, "B"): "网络切片无线资源管理方案设计",
    (2025, "C"): "可调控生物节律的 LED 光源研究",
}

HUASHU_CUP_2026_TITLES = {
    (2026, "A"): "微构体中填充导电介质的仿真优化",
    (2026, "B"): "VLSI 布图规划设计",
    (2026, "C"): "面向算电协同的多目标调度优化研究",
}

# 2026 names each statement "<letter>题 <title>.pdf" inside a directory of the same
# name. B also ships a reference paper as a sibling PDF, so the statement is picked
# by its letter prefix rather than by being the only PDF in the directory.
HUASHU_CUP_2026_STATEMENT_RE = re.compile(r"^([A-C])题[\s　]")


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


# Container noise, and files that only look like extra data. ``.DS_Store`` rides
# along in the APMCM 2015/2016 archives. A ``.doc`` with a ``.pdf`` sibling is
# either the statement this record already publishes as ``problem.pdf`` (every
# legacy year the staging converter touched leaves the pair behind) or a form that
# shipped in both formats, so bundling it advertises a duplicate as an appendix.
# The sheets and templates are blank competition paperwork, not problem data.
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
CONVERTED_SUFFIXES = {".doc", ".docx", ".wps"}
BOILERPLATE_TOKENS = (
    "control sheet", "summary sheet", "essay format and submission",
    "参赛纪律", "承诺书", "论文模板",
)


def prune_related(files: list[Path]) -> list[Path]:
    kept: list[Path] = []
    for item in files:
        lowered = item.name.lower()
        if lowered in JUNK_NAMES or "__macosx" in item.as_posix().lower():
            continue
        if item.suffix.lower() in CONVERTED_SUFFIXES and item.with_suffix(".pdf").is_file():
            continue
        if any(token in lowered for token in BOILERPLATE_TOKENS):
            continue
        kept.append(item)
    return kept


def discover_related_files(problem_id: str, pdf: Path) -> list[Path]:
    if problem_id.startswith("comap-"):
        return []
    if problem_id == "apmcm-2023-wuyue":
        roots = [item for item in pdf.parent.iterdir() if item.is_dir() and item.name.lower() == "attachment"]
        return prune_related(sorted(item for root in roots for item in root.rglob("*") if item.is_file()))
    group_root = next((parent for parent in pdf.parents if parent.parent == EXTRACTED_ROOT), None)
    if group_root is not None and pdf.parent == group_root:
        return []
    return prune_related(sorted(item for item in pdf.parent.rglob("*") if item.is_file() and item != pdf))


# Mirror statements and ordinary appendices; leave bulk data sets upstream. The
# ceiling is on the raw total rather than the zipped one because the heavy members
# are already-compressed .xlsx, so zipping barely moves the number, and measuring
# the input keeps the decision independent of zlib's output. Five bundles exceed
# it (2019 E at 119 MiB, 2020 E at 99 MiB, plus 2020 C, 2020 D, 2021 E); mirroring
# those would add ~380 MiB of payload to serve files the official site hosts.
ATTACHMENT_BUNDLE_LIMIT = 25 * 1024 * 1024


def publish_downloads(problem_id: str, pdf: Path, related_files: list[Path],
                      source_url: str) -> list[dict[str, Any]]:
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
    if not related_files:
        return attachments
    raw_bytes = sum(item.stat().st_size for item in related_files)
    if raw_bytes > ATTACHMENT_BUNDLE_LIMIT:
        # No local copy, so no bytes/sha256: those fields describe a mirrored
        # file, and the frontend keys off the leading "/" to render this as an
        # outbound link rather than a download.
        attachments.append({
            "title": f"随题附件（{len(related_files)} 个文件，约 {raw_bytes / 1048576:.0f} MB，官网下载）",
            "url": source_url,
            "kind": "data",
            "bytes": 0,
            "sha256": "",
            "external": True,
        })
        print(f"LINK-ONLY {problem_id} attachments={len(related_files)} bytes={raw_bytes}")
        return attachments
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
        (("optim", "优化", "调度", "规划", "design"), "规划优化", ["优化模型", "决策分析"]),
        (("image", "图像", "recognition"), "图像与数据建模", ["图像处理", "机器学习"]),
        (("network", "网络", "traffic", "交通"), "系统建模", ["网络模型", "系统分析"]),
        (("environment", "ecological", "生态", "warming"), "环境与生态", ["评价模型", "可持续发展"]),
    ]
    for keys, problem_type, directions in rules:
        if any(key in lower for key in keys):
            return problem_type, directions, [key for key in keys if key in lower][:3]
    return "综合建模", ["综合分析"], ["数学建模"]


def problem_record(*, problem_id: str, code: str, title: str, competition: str, category: str,
                   year: int, source_id: str, source_url: str, pdf: Path, source_status: str = "official",
                   related_files: list[Path] | None = None) -> dict[str, Any]:
    """Build one published problem record.

    ``related_files`` overrides attachment discovery. The Chinese-track archives
    need it: 2026 C ships loose in the wrapper directory beside the A and B
    folders, so walking its parent would hand C every other problem's appendices.
    """
    blocks, page_count, full_text = pdf_layout.build_blocks(pdf, problem_id, FIGURE_ROOT)
    if related_files is None:
        related_files = discover_related_files(problem_id, pdf)
    attachments = publish_downloads(problem_id, pdf, related_files, source_url)
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


def cumcm_letters(year: int) -> str:
    """Letters the competition actually ran that year.

    An E problem first appears in 2019, so asserting A-E across 2015-2018 would
    fail on a complete archive. The set is pinned per year rather than inferred
    from what was found, so a silently missing statement is still an error.
    """
    return "ABCD" if year <= 2018 else "ABCDE"


def cumcm_problems() -> list[dict[str, Any]]:
    output = []
    for year in sorted(CUMCM_PAGE_URLS, reverse=True):
        directory = EXTRACTED_ROOT / f"cumcm-{year}"
        if not directory.is_dir():
            raise RuntimeError(
                f"{directory} missing. Pre-2021 years need staging first:\n"
                f"  python datasets/recipes/stage_legacy_archives.py expand --years {year}\n"
                f"  <venv-with-pywin32> datasets/recipes/stage_legacy_archives.py convert --years {year}"
            )
        found: dict[str, tuple[Path, str]] = {}
        for pdf in sorted(directory.rglob("*.pdf")):
            first_page = (PdfReader(str(pdf)).pages[0].extract_text() or "").replace("\u3000", " ")
            if CUMCM_MARKER not in first_page:
                continue
            match = next((hit for pattern in CUMCM_TITLE_PATTERNS
                          if (hit := pattern.search(first_page))), None)
            if not match:
                continue
            letter, title = match.group(1), " ".join(match.group(2).split())
            found.setdefault(letter, (pdf, title))
        letters = cumcm_letters(year)
        if set(found) != set(letters):
            raise RuntimeError(
                f"CUMCM {year} expected {letters}: missing "
                f"{sorted(set(letters) - set(found))}, unexpected {sorted(set(found) - set(letters))}"
            )
        for letter in letters:
            pdf, title = found[letter]
            item = problem_record(
                problem_id=f"cumcm-{year}-{letter.lower()}", code=f"{year} CUMCM {letter}", title=title,
                competition="全国大学生数学建模竞赛", category="国赛", year=year,
                source_id="cumcm_official", source_url=CUMCM_PAGE_URLS[year], pdf=pdf,
            )
            output.append(item)
            print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    return output


# The English statement is "2018 APMCM Problem A.pdf" from 2016 on, but 2015 and
# 2017 drop the year prefix and ship a bare "Problem A.pdf", so the prefix is
# optional. The group's own year supplies the number either way.
APMCM_EN_STATEMENT_RE = re.compile(r"(?:20\d{2}\s+APMCM\s+)?Problem\s+([A-E])\.pdf", re.I)


def apmcm_problems() -> list[dict[str, Any]]:
    output = []
    regular_groups = ["apmcm-2015", "apmcm-2016", "apmcm-2017", "apmcm-2018",
                      "apmcm-2021", "apmcm-2022", "apmcm-2022-jan", "apmcm-2023", "apmcm-2024-en"]
    for group in regular_groups:
        year = int(re.search(r"20\d{2}", group).group())
        found: dict[str, Path] = {}
        for pdf in sorted((EXTRACTED_ROOT / group).rglob("*.pdf")):
            match = APMCM_EN_STATEMENT_RE.fullmatch(pdf.name)
            if not match:
                continue
            found.setdefault(match.group(1).upper(), pdf)
        letters = APMCM_GROUP_LETTERS[group]
        if set(found) != set(letters):
            raise RuntimeError(
                f"{group} expected {letters}: missing {sorted(set(letters) - set(found))}, "
                f"unexpected {sorted(set(found) - set(letters))}"
            )
        suffix = "-jan" if group.endswith("-jan") else ""
        for letter in letters:
            item = problem_record(
                problem_id=f"apmcm-{year}{suffix}-{letter.lower()}",
                code=f"{year} APMCM {letter}" + ("（1月场）" if suffix else ""),
                title=APMCM_TITLES[(year, letter)], competition="APMCM 亚太地区大学生数学建模竞赛",
                category="亚太赛", year=year, source_id="apmcm_problems",
                source_url=APMCM_PAGE_URLS[group], pdf=found[letter],
            )
            output.append(item)
            print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    for group, year in (("apmcm-2024-cn-fixed", 2024), ("apmcm-2026", 2026)):
        chinese_root = EXTRACTED_ROOT / group
        for letter in APMCM_GROUP_LETTERS[group]:
            candidates = [pdf for pdf in sorted(chinese_root.rglob("*.pdf"))
                          if APMCM_CN_STATEMENT_RE.match(pdf.name) and "附件" not in pdf.as_posix()]
            candidates = [pdf for pdf in candidates if pdf.name.startswith(f"{letter}题")]
            if len(candidates) != 1:
                raise RuntimeError(f"Expected one {year} APMCM Chinese problem {letter}, found {len(candidates)}")
            pdf = candidates[0]
            # 2026 C sits loose in the wrapper directory next to the A and B folders,
            # so its parent is not its own; only sweep a parent that is named for this
            # letter, otherwise C would collect every other problem's appendices.
            own_directory = APMCM_CN_STATEMENT_RE.match(pdf.parent.name)
            related = (prune_related(sorted(item for item in pdf.parent.rglob("*")
                                            if item.is_file() and item != pdf))
                       if own_directory and own_directory.group(1).upper() == letter else [])
            item = problem_record(
                problem_id=f"apmcm-{year}-cn-{letter.lower()}", code=f"{year} APMCM 中文 {letter}",
                title=APMCM_CN_TITLES[(year, letter)],
                competition="APMCM 亚太地区大学生数学建模竞赛（中文赛项）", category="亚太赛", year=year,
                source_id="apmcm_problems", source_url=APMCM_PAGE_URLS[group], pdf=pdf,
                related_files=related,
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


def mathorcup_statement(year: int, letter: str) -> tuple[Path, list[Path]]:
    """Locate one official statement and only the appendices belonging to it."""
    if year == 2026 and letter == "D":
        directory = EXTRACTED_ROOT / "mathorcup-2026-d-fixed"
        candidates = sorted(directory.glob("*.pdf"))
    else:
        directory = EXTRACTED_ROOT / f"mathorcup-{year}"
        candidates = sorted(
            pdf for pdf in directory.rglob("*.pdf")
            if pdf.parent.name == f"{letter}题" and "MathorCup" in pdf.name
        )
    if len(candidates) != 1:
        raise RuntimeError(
            f"MathorCup {year} {letter} expected one statement, found "
            f"{len(candidates)} in {directory}"
        )
    pdf = candidates[0]
    related = prune_related(sorted(item for item in pdf.parent.rglob("*") if item.is_file() and item != pdf))
    return pdf, related


def mathorcup_problems() -> list[dict[str, Any]]:
    output = []
    for year in sorted(MATHORCUP_GROUP_LETTERS, reverse=True):
        for letter in MATHORCUP_GROUP_LETTERS[year]:
            pdf, related = mathorcup_statement(year, letter)
            item = problem_record(
                problem_id=f"mathorcup-{year}-{letter.lower()}",
                code=f"{year} MathorCup {letter}",
                title=MATHORCUP_TITLES[(year, letter)],
                competition=("MathorCup 数学应用挑战赛" if year >= 2024
                             else "MathorCup 高校数学建模挑战赛"),
                category="MathorCup",
                year=year,
                source_id="mathorcup_official",
                source_url=MATHORCUP_PAGE_URLS[year],
                pdf=pdf,
                related_files=related,
            )
            output.append(item)
            print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    return output


def huashu_cup_problems() -> list[dict[str, Any]]:
    root = EXTRACTED_ROOT / "huashu-cup-historical"
    output = []
    for (year, letter), title in sorted(HUASHU_CUP_TITLES.items(), reverse=True):
        candidates = [
            path for path in root.rglob("*.pdf")
            if re.search(rf"{year}.*{letter}.*题", path.name, re.I)
            and len(path.relative_to(root).parts) <= 3
        ]
        if len(candidates) != 1:
            raise RuntimeError(f"Huashu Cup {year} {letter}: expected one statement, found {candidates}")
        pdf = candidates[0]
        item = problem_record(
            problem_id=f"huashu-cup-{year}-{letter.lower()}",
            code=f"{year} 华数杯 {letter}",
            title=title,
            competition="华数杯全国大学生数学建模竞赛",
            category="华数杯",
            year=year,
            source_id="huashu_cup_official",
            source_url=HUASHU_CUP_NOTICE_URL,
            pdf=pdf,
        )
        output.append(item)
        print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    return output


def huashu_cup_2026_problems() -> list[dict[str, Any]]:
    root = EXTRACTED_ROOT / "huashu-cup-2026"
    output = []
    for (year, letter), title in sorted(HUASHU_CUP_2026_TITLES.items()):
        candidates = []
        for path in sorted(root.rglob("*.pdf")):
            match = HUASHU_CUP_2026_STATEMENT_RE.match(path.name)
            if match and match.group(1) == letter:
                candidates.append(path)
        if len(candidates) != 1:
            raise RuntimeError(f"Huashu Cup {year} {letter}: expected one statement, found {candidates}")
        pdf = candidates[0]
        item = problem_record(
            problem_id=f"huashu-cup-{year}-{letter.lower()}",
            code=f"{year} 华数杯 {letter}",
            title=title,
            competition="华数杯全国大学生数学建模竞赛",
            category="华数杯",
            year=year,
            source_id="huashu_cup_official",
            source_url=HUASHU_CUP_2026_NOTICE_URL,
            pdf=pdf,
        )
        output.append(item)
        print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    return output


def tipdm_cup_problems() -> list[dict[str, Any]]:
    """Publish the Teddy Cup statements staged from the organiser's own domain.

    ``related_files`` is pinned empty rather than discovered: every statement of
    an edition is staged into one directory, so walking a statement's parent
    would hand it its siblings as attachments. The real data sets live on
    pan.baidu.com and are reachable only through the edition page, which is
    already published as the record's source URL.
    """
    manifest_path = TIPDM_CUP_MANIFEST
    if not manifest_path.is_file():
        raise RuntimeError(
            f"{manifest_path} missing. Stage it first:\n"
            "  python datasets/recipes/stage_tipdm_cup_statements.py all"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output = []
    for record in manifest["records"]:
        letter = record.get("letter")
        code = (f"{record['year']} 泰迪杯 {letter}" if letter
                else f"{record['year']} 泰迪杯")
        item = problem_record(
            problem_id=record["id"],
            code=code,
            title=record["title"],
            competition="“泰迪杯”数据挖掘挑战赛",
            category="泰迪杯",
            year=record["year"],
            source_id="tipdm_cup_official",
            source_url=record["notice_url"],
            pdf=ROOT / record["path"],
            related_files=[],
        )
        output.append(item)
        print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    return output


def tipdm_bdrace_problems() -> list[dict[str, Any]]:
    """Publish the Teddy Cup statements the organiser's platform serves as rich text.

    Only the bodies the platform published whole are emitted. The rest stop at a
    row of dots or an invitation to fetch the remainder from a WeChat account,
    and a partial statement does not belong in the problem library; the staging
    manifest records every one of them either way.

    These records carry no ``source_pdf`` and no attachments: the API body is the
    published form, and the real PDFs and data sets sit behind registration.
    """
    if not TIPDM_BDRACE_MANIFEST.is_file():
        raise RuntimeError(
            f"{TIPDM_BDRACE_MANIFEST} missing. Snapshot it first:\n"
            "  python datasets/recipes/stage_tipdm_bdrace_statements.py all"
        )
    manifest = json.loads(TIPDM_BDRACE_MANIFEST.read_text(encoding="utf-8"))
    output = []
    for record in manifest["problems"]:
        if not record["complete"]:
            continue
        body = (ROOT / record["path"]).read_text(encoding="utf-8")
        blocks, full_text = html_layout.build_blocks(body, record["id"], FIGURE_ROOT)
        if not blocks:
            raise RuntimeError(f"{record['id']} produced no content blocks")
        problem_type, directions, keywords = classify(record["title"])
        item = {
            "id": record["id"],
            "code": f"{record['year']} 泰迪杯 {record['letter']}",
            "title": record["title"],
            "competition": record["competition"],
            "category": "泰迪杯",
            "year": record["year"],
            "problem_type": problem_type,
            "modeling_directions": directions,
            "keywords": keywords or ["泰迪杯", str(record["year"])],
            "data_requirement": "题面由主办方平台发布；随题数据仅在竞赛期间开放",
            "status": "完整题面",
            "summary": summarize(blocks, record["title"]),
            "source_id": "tipdm_cup_official",
            "source_url": record["source_url"],
            "source_status": "official",
            "access_scope": "stored_content",
            "attachments": [],
            "content_format": "structured_text",
            "content_status": "complete",
            "content_character_count": len(full_text),
            "content_block_count": len(blocks),
            "content_blocks": blocks,
        }
        output.append(item)
        print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    return output


def tjjmds_theme_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the background, theme and topic-requirement parts of one notice.

    The preamble names the organisers and the contest's purpose -- the closest
    thing to a problem background this contest publishes -- so it is kept along
    with the theme section. The length gate drops the salutation line and the
    signature block, which are also set as short paragraphs.
    """
    kept: list[dict[str, Any]] = []
    mode: str | None = None
    seen_section = False
    for block in blocks:
        text = (block.get("text") or "").strip()
        opens = bool(TJJMDS_KEEP_SECTION_RE.match(text))
        opens_appendix = bool(TJJMDS_KEEP_APPENDIX_RE.match(text))
        if opens:
            mode = "section"
        elif opens_appendix:
            mode = "appendix"
        elif mode == "section" and TJJMDS_SECTION_RE.match(text):
            mode = None
        elif mode == "appendix" and TJJMDS_APPENDIX_RE.match(text):
            mode = None
        if TJJMDS_SECTION_RE.match(text):
            seen_section = True
        if not mode:
            if not seen_section and block["type"] == "paragraph" and len(text) >= 60:
                kept.append(block)
            continue
        if kept and text and kept[-1].get("text", "").strip() == text:
            continue
        if opens or opens_appendix:
            kept.append({"type": "heading", "level": 2, "text": text})
        else:
            kept.append(block)
    return kept


def tjjmds_problems() -> list[dict[str, Any]]:
    """Publish the statistical modelling contest's per-edition themes.

    The contest sets no problems: each edition announces a theme -- or, before
    2021, topic categories with judging criteria -- and every team writes a
    paper on a title of its own choosing. The theme section of the official
    notice is therefore the entire published form of the assignment, and that
    is what these records carry; a per-year A/B/C statement does not exist
    anywhere to collect.
    """
    if not TJJMDS_MANIFEST.is_file():
        raise RuntimeError(
            f"{TJJMDS_MANIFEST} missing. Snapshot it first:\n"
            "  python datasets/recipes/stage_tjjmds_notices.py all"
        )
    manifest = json.loads(TJJMDS_MANIFEST.read_text(encoding="utf-8"))
    interpretations = {item["year"]: item for item in manifest.get("interpretations", [])}
    output = []
    for record in manifest["records"]:
        body = (ROOT / record["body_path"]).read_text(encoding="utf-8")
        blocks, _full = html_layout.build_blocks(body, record["id"], FIGURE_ROOT)
        blocks = tjjmds_theme_blocks(blocks)
        if not blocks:
            raise RuntimeError(f"{record['id']}: no theme section found in the notice")
        # The 2022 edition also published its theme interpretation as text --
        # several professors spelling out suggested research directions. That is
        # the most statement-like material this contest has ever released, so it
        # rides along as an appendix; the other editions ship video only.
        extra = interpretations.get(record["year"])
        if extra is not None:
            jiedu_body = (ROOT / extra["body_path"]).read_text(encoding="utf-8")
            jiedu_blocks, _ = html_layout.build_blocks(jiedu_body, record["id"], FIGURE_ROOT)
            jiedu_blocks = [block for block in jiedu_blocks
                            if not TJJMDS_VIDEO_CHROME_RE.search(block.get("text") or "")]
            if not jiedu_blocks:
                raise RuntimeError(f"{record['id']}: interpretation text came out empty")
            blocks = blocks + [{"type": "document_break", "title": "主题解读（文字版）"}] + jiedu_blocks
        plain = "\n".join(block.get("text", "") for block in blocks)
        item = {
            "id": record["id"],
            "code": f"{record['year']} 统计建模",
            "title": record["title"],
            "competition": "全国大学生统计建模大赛",
            "category": "统计建模",
            "year": record["year"],
            "problem_type": "统计建模",
            "modeling_directions": ["统计模型", "数据分析"],
            "keywords": ["统计建模", "自拟题目", str(record["year"])],
            "data_requirement": "围绕年度主题自拟题目，数据由参赛队自行搜集",
            "status": "完整题面",
            "summary": summarize(blocks, record["title"]),
            "source_id": "tjjmds_official",
            "source_url": record["notice_url"],
            "source_status": "official",
            "access_scope": "stored_content",
            "attachments": [],
            "content_format": "structured_text",
            "content_status": "complete",
            "content_character_count": len(plain),
            "content_block_count": len(blocks),
            "content_blocks": blocks,
        }
        output.append(item)
        print(f"FULL {item['id']} blocks={item['content_block_count']} chars={item['content_character_count']}")
    return output


def build() -> dict[str, Any]:
    for generated_root in (LEGACY_PAGE_ROOT, FIGURE_ROOT, DOWNLOAD_ROOT):
        if generated_root.exists():
            shutil.rmtree(generated_root)
    problems = (comap_problems() + cumcm_problems() + apmcm_problems()
                + mathorcup_problems() + huashu_cup_problems() + huashu_cup_2026_problems()
                + tipdm_cup_problems() + tipdm_bdrace_problems() + tjjmds_problems())
    problems.sort(key=lambda item: (-item["year"], item["category"], item["code"]))
    result = {
        "schema_version": "1.0.0",
        "stats": {
            "problem_count": len(problems),
            "comap_count": sum(item["source_id"] == "comap_mcm_icm" for item in problems),
            "apmcm_count": sum(item["source_id"] == "apmcm_problems" for item in problems),
            "cumcm_count": sum(item["source_id"] == "cumcm_official" for item in problems),
            "mathorcup_count": sum(item["source_id"] == "mathorcup_official" for item in problems),
            "huashu_cup_count": sum(item["source_id"] == "huashu_cup_official" for item in problems),
            "tipdm_cup_count": sum(item["source_id"] == "tipdm_cup_official" for item in problems),
            "tjjmds_count": sum(item["source_id"] == "tjjmds_official" for item in problems),
            # A statement collected as rich text has no page count to add.
            "page_count": sum(item["source_pdf"]["page_count"] for item in problems
                              if item.get("source_pdf")),
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
    # CUMCM now spans 2015-2025: 4 problems for 2015-2018, 5 for 2019-2025.
    # APMCM spans 2015-2026 minus the three offsite years, so 25 English plus 6
    # Chinese-track plus the 2023 Wuyue Cup.
    # COMAP adds 2015-2017 (14 statements). 2018-2020 are absent because the
    # official year indexes for those three serve no PDFs at all.
    # Huashu Cup is 18 from the 2020-2025 archive plus the 3 of the 2026 edition.
    # The Teddy Cup contributes 12: one combined 2015 paper, A-C for 2016, 2019
    # and 2020, the 2020 Nandu special problem, and the single 2023 statement the
    # organiser's platform published whole rather than cutting off.
    # The statistical modelling contest sets no problems; its eight records are
    # the per-edition themes, one for every notice the official site publishes.
    expected = {
        "comap_count": 44,
        "apmcm_count": 32,
        "cumcm_count": 51,
        "mathorcup_count": 9,
        "huashu_cup_count": 21,
        "tipdm_cup_count": 12,
        "tjjmds_count": 8,
        "problem_count": 177,
    }
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
        # A record published from a rich-text API has no source PDF to hash; its
        # snapshot is pinned by its own staging recipe instead.
        if problem.get("source_pdf"):
            source_pdf = ROOT / problem["source_pdf"]["path"]
            if sha256(source_pdf) != problem["source_pdf"]["sha256"]:
                raise RuntimeError(f"PDF hash mismatch {source_pdf}")
        for block in problem["content_blocks"]:
            if block["type"] == "image":
                asset = ROOT / "apps/web/public" / block["src"].lstrip("/")
                if not asset.exists() or asset.stat().st_size < 512:
                    raise RuntimeError(f"Missing figure asset {asset}")
        for attachment in problem["attachments"]:
            if attachment.get("external"):
                # Nothing was mirrored, so there is no file to hash. What must
                # hold is that the link is absolute and points at an official
                # host -- a relative URL here would 404 against the frontend.
                if not attachment["url"].startswith("https://"):
                    raise RuntimeError(f"External attachment is not absolute in {problem['id']}")
                continue
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
