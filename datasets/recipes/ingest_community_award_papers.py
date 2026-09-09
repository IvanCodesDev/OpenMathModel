#!/usr/bin/env python3
"""Fill the recent-year gaps in the CUMCM and CPMCM award-paper shelves.

近几届的论文没有任何官方公开下载：国赛（CUMCM）优秀论文只进知网竞赛专栏与纸质
《优秀论文选》，研究生赛（华为杯）除 2004–2023 的社区合集外别无公开来源，两家官网
都只发获奖名单。因此这批论文逐篇取自 GitHub 上作者自己公开的参赛仓库或社区汇编，
每条都固定到具体 commit 与文件路径，并如实登记该论文自称的奖项等级与出处。

与其它 recipe 一致：不解析参赛队员姓名，不再分发原件，产品侧只发布索引与来源链接。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from paper_covers import derive_models, keyword_note, page_lines, parse_cover, summarize


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = ROOT / "datasets/raw/sources/paper-archives"
OUTPUT = ROOT / "datasets/interim/community_award_papers/papers.json"
USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"

CUMCM = {
    "archive": "cumcm",
    "competition": "全国大学生数学建模竞赛",
    "code_label": "CUMCM",
    "problem_prefix": "cumcm",
}
CPMCM = {
    "archive": "cpmcm",
    "competition": "中国研究生数学建模竞赛",
    "code_label": "CPMCM",
    "problem_prefix": "cpmcm",
}

#: 逐篇登记。award 用来源自己写明的奖项等级；写不明的一律不收，宁缺毋滥。
ENTRIES: tuple[dict[str, Any], ...] = (
    # 国赛 2021–2024：社区汇编仓库，一题一篇。
    *(
        {
            "contest": CUMCM,
            "repository": "yan-fanyu/CUMCM-Paper-And-SourceCode",
            "commit": "d58f56783795842ea6a4487c551bc7789848406b",
            "path": path,
            "year": year,
            "letter": letter,
            "award": "优秀论文",
        }
        for year, letter, path in (
            (2021, "A", "2021A射电望远镜/2021A射电望远镜论文.pdf"),
            (2021, "B", "2021B乙醇偶合制备烯烃/2021B.pdf"),
            (2021, "C", "2021C原材料的订购与运输/2021C.pdf"),
            (2022, "A", "2022A波浪能最大输出功率OK/2022A波浪能.pdf"),
            (2022, "B", "2022B无人机定位OK/2022B无人机定位论文.pdf"),
            (2022, "C", "2022C古代玻璃制品成分分析/2022C古代玻璃论文.pdf"),
            (2023, "A", "2023A定日镜场的优化设计/2023A论文.pdf"),
            (2023, "B", "2023B多波束测线问题/2023B多波束论文.pdf"),
            (2023, "C", "2023C蔬菜类商品的自动定价与补货决策/2023C蔬菜类商品的自动定价与补货决策论文.pdf"),
            (2024, "A", "2024A板凳龙/2024A-PDF论文.pdf"),
            (2024, "B", "2024B生产决策/2024B-企业生产过程.pdf"),
            (2024, "C", "2024C种植策略/2024C-种植策略.pdf"),
        )
    ),
    # 国赛 2025：参赛队自己公开的获奖论文，奖项以仓库说明为准。
    {
        "contest": CUMCM,
        "repository": "CUMCM-2025B-Team/CUMCM-2025-Problem-B",
        "commit": "78c70dad9809828e1de6dd5405bf2e4879aa7316",
        "path": "25国赛.pdf",
        "year": 2025,
        "letter": "B",
        "award": "全国一等奖",
    },
    {
        "contest": CUMCM,
        "repository": "Jackyleo-Zhao/cumcm-2025",
        "commit": "fbc90c2d0adcce48415438f38b5824e3f695003d",
        "path": "paper/paper.pdf",
        "year": 2025,
        "letter": "C",
        "award": "全国二等奖",
    },
    {
        "contest": CUMCM,
        "repository": "Aiden-DUT/CUMCM2025-C-NIPT-Problem",
        "commit": "ab4c7ca3695d752d72b9b5802e558dd2531ad5d4",
        "path": "C定稿论文.pdf",
        "year": 2025,
        "letter": "C",
        "award": "省级一等奖",
        "distinctions": ["辽宁赛区一等奖"],
    },
    {
        "contest": CUMCM,
        "repository": "firstlove-one/CUMCM-2025-Problem-C",
        "commit": "a670c23671592e2982f133b024e3fe6fd54e6150",
        "path": "论文.pdf",
        "year": 2025,
        "letter": "C",
        "award": "省级一等奖",
        "distinctions": ["湖北赛区一等奖"],
    },
    {
        "contest": CUMCM,
        "repository": "Yipintianxia-MiddleRingRoad/2025CUMCM_Provincial_first_prize_YYL_ZCY_LJT",
        "commit": "a60de4e061c76914d910f44e4618d5b329d2d7ba",
        "path": "2025国赛_四川省一_论文PDF_YYL_ZCY_LJT.pdf",
        "year": 2025,
        "letter": "A",
        "award": "省级一等奖",
        "distinctions": ["四川赛区一等奖"],
    },
    # 华为杯 2024–2025：社区合集停在 2023，这两届只有参赛队自己公开的获奖论文。
    {
        "contest": CPMCM,
        "repository": "LY-zhang-yi-hao/Huawei_Mathcup_OpenAccess",
        "commit": "2c562dbadf6f73398670145ff28f020dda75d564",
        "path": "E.pdf",
        "year": 2024,
        "letter": "E",
        "award": "全国二等奖",
    },
    {
        "contest": CPMCM,
        "repository": "HaoyuZhao31415/MathModel",
        "commit": "cbedd16f603fc28e802951beacbdff57649eece0",
        "path": "华为杯2025 (7).pdf",
        "year": 2025,
        "letter": "A",
        "award": "华为专项二等奖",
        "distinctions": ["全国第 7 名"],
    },
)


def slug(entry: dict[str, Any]) -> str:
    digest = hashlib.sha1(f"{entry['repository']}/{entry['path']}".encode("utf-8")).hexdigest()[:8]
    return f"{entry['letter']}{entry['year']}-{digest}"


def local_path(entry: dict[str, Any]) -> Path:
    return ARCHIVE_ROOT / entry["contest"]["archive"] / str(entry["year"]) / entry["letter"] / f"{slug(entry)}.pdf"


def blob_url(entry: dict[str, Any]) -> str:
    return (
        f"https://github.com/{entry['repository']}/blob/{entry['commit']}/"
        + urllib.parse.quote(entry["path"])
    )


def download_urls(entry: dict[str, Any]) -> tuple[str, ...]:
    quoted = urllib.parse.quote(entry["path"])
    # jsDelivr 拒绝 20MB 以上的文件，raw 兜底。
    return tuple(
        f"https://{host}/gh/{entry['repository']}@{entry['commit']}/{quoted}"
        for host in ("cdn.jsdelivr.net", "gcore.jsdelivr.net")
    ) + (f"https://raw.githubusercontent.com/{entry['repository']}/{entry['commit']}/{quoted}",)


def fetch_one(entry: dict[str, Any], attempts: int = 3) -> str:
    destination = local_path(entry)
    if destination.exists() and destination.read_bytes()[:5] == b"%PDF-":
        return "cached"
    last_error = "unknown"
    for attempt in range(attempts):
        for url in download_urls(entry):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                payload = urllib.request.urlopen(request, timeout=300).read()
            except Exception as error:
                last_error = f"{type(error).__name__}: {error}"
                continue
            if payload[:5] != b"%PDF-":
                last_error = "not a PDF"
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            return "downloaded"
        time.sleep(2 * (attempt + 1))
    return f"failed ({last_error})"


def fetch() -> dict[str, int]:
    counts = {"downloaded": 0, "cached": 0, "failed": 0}
    for entry in ENTRIES:
        status = fetch_one(entry)
        counts[status if status in counts else "failed"] += 1
        marker = "OK" if not status.startswith("failed") else "FAIL"
        print(f"COMMUNITY_FETCH {marker} {entry['year']} {entry['letter']} {entry['repository']} {status}")
        sys.stdout.flush()
    return counts


def build() -> dict[str, Any]:
    papers: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for entry in ENTRIES:
        path = local_path(entry)
        if not path.exists():
            skipped.append(f"{entry['repository']}/{entry['path']} (not downloaded)")
            continue
        try:
            lines, page_count = page_lines(path)
            fields = parse_cover(lines)
        except Exception as error:
            skipped.append(f"{entry['repository']}/{entry['path']} ({type(error).__name__}: {error})")
            continue
        contest = entry["contest"]
        year, letter = entry["year"], entry["letter"]
        payload = path.read_bytes()
        paper_id = f"{contest['problem_prefix']}-paper-{year}-{letter.lower()}-{slug(entry).lower()}"
        papers[paper_id] = {
            "title": fields["title"],
            "record_type": "paper",
            "problem_id": f"{contest['problem_prefix']}-{year}-{letter.lower()}",
            "problem_code": f"{year} {contest['code_label']} {letter}",
            "competition": contest["competition"],
            "category": "优秀论文",
            "year": year,
            "award": entry["award"],
            "distinctions": list(entry.get("distinctions", [])),
            "institution": fields["institution"] or None,
            "team_id": fields["team_id"] or slug(entry),
            "keywords": fields["keywords"],
            "models": derive_models(fields["title"], fields["abstract"], fields["keywords"]),
            "summary": summarize(fields["abstract"])
            or f"{year} 年{contest['competition']} {letter} 题{entry['award']}论文全文。",
            "innovation": keyword_note(fields["keywords"]) if fields["keywords"]
            else "论文由参赛队在公开仓库发布，封面未提供关键词。",
            "page_count": page_count,
            "source_id": "github_community_award_papers",
            "source_url": blob_url(entry),
            "full_text_url": blob_url(entry),
            "local_pdf_path": f"/paper-files/archive/{contest['archive']}/{year}/{letter}/{slug(entry)}.pdf",
            "source_file_bytes": len(payload),
            "source_sha256": hashlib.sha256(payload).hexdigest(),
            "source_repository": entry["repository"],
            "source_commit": entry["commit"],
        }
    result = {
        "schema_version": "1.0.0",
        "source_id": "github_community_award_papers",
        "stats": {
            "entry_count": len(ENTRIES),
            "paper_count": len(papers),
            "skipped_count": len(skipped),
            "with_title": sum(1 for item in papers.values() if item["title"]),
            "by_competition": {
                competition: sum(1 for item in papers.values() if item["competition"] == competition)
                for competition in {item["competition"] for item in papers.values()}
            },
            "years": sorted({item["year"] for item in papers.values()}),
        },
        "skipped": skipped,
        "papers": papers,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def verify() -> dict[str, Any]:
    data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    if data["stats"]["paper_count"] != len(ENTRIES):
        raise RuntimeError(f"Expected {len(ENTRIES)} papers, found {data['stats']['paper_count']}")
    for paper_id, record in data["papers"].items():
        path = ROOT / record["local_pdf_path"].replace(
            "/paper-files/archive/", "datasets/raw/sources/paper-archives/"
        )
        if not path.exists() or path.stat().st_size != record["source_file_bytes"]:
            raise RuntimeError(f"Staged PDF missing or resized: {paper_id}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["source_sha256"]:
            raise RuntimeError(f"Staged PDF hash mismatch: {paper_id}")
        if record["source_commit"] not in record["source_url"]:
            raise RuntimeError(f"Source URL is not pinned to the recorded commit: {paper_id}")
    print("COMMUNITY_AWARD_PAPERS_VERIFY_OK " + json.dumps(data["stats"], ensure_ascii=False))
    return data["stats"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "build", "verify", "all"))
    args = parser.parse_args()
    if args.command in {"fetch", "all"}:
        print(json.dumps(fetch(), ensure_ascii=False))
    if args.command in {"build", "all"}:
        result = build()
        print(json.dumps(result["stats"], ensure_ascii=False))
    if args.command in {"verify", "all"}:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
