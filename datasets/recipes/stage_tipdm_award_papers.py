#!/usr/bin/env python3
"""Collect the “泰迪杯” award papers the organiser publishes on its own site.

竞赛官网 tdb.tipdm.net 的「优秀作品」栏目逐篇挂出获奖作品全文，PDF 直接托管在
主办方自己的 CMS（www.tipdm.org/u/cms/www/…），并统一加了一页封面，写明作品名称、
荣获奖项与作品单位。这份 recipe 抓取该栏目的条目清单，逐篇下载 PDF、固定字节数与
SHA-256，再从封面读出题名、奖项、单位、摘要与关键词。

封面同页还印着作品成员与指导老师姓名：那是个人信息，解析时一律不取。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from paper_covers import derive_models, keyword_note, page_lines, parse_cover, summarize


ROOT = Path(__file__).resolve().parents[2]
PAPER_ROOT = ROOT / "datasets/raw/sources/paper-archives/tipdm"
OUTPUT = ROOT / "datasets/interim/tipdm_award_papers/papers.json"
LIST_URL = "https://tdb.tipdm.net/tzsdt/index.jhtml"
LIST_PAGE_URL = "https://tdb.tipdm.net/tzsdt/index_{page}.jhtml"
USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"
SOURCE_ID = "tipdm_cup_official"
COMPETITION = "“泰迪杯”数据挖掘挑战赛"
#: 第一届挑战赛在 2013 年举办，此后每年一届，届次即可换算成年份。
FIRST_EDITION_YEAR = 2013

ENTRY_RE = re.compile(
    r'<h1>\s*<a[^>]+href="(/tzsdt/(\d+)\.jhtml)"[^>]*>(.*?)</a>\s*</h1>'
    r'(?:\s*<div class="des">(.*?)</div>)?',
    re.I | re.S,
)
PDF_ANCHOR_RE = re.compile(r'<a\s[^>]*href="([^"]+\.pdf)"[^>]*>(.*?)</a>', re.I | re.S)
FULLTEXT_LABEL_RE = re.compile(r"(全文|原文)")
EDITION_RE = re.compile(r"第([〇零一二三四五六七八九十]+)届")
CODE_RE = re.compile(r"挑战赛\s*([A-F])(\d{0,2})")
EDITION_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def chinese_number(text: str) -> int:
    text = text.replace("〇", "零")
    if "十" not in text:
        return sum(EDITION_DIGITS.get(char, 0) for char in text)
    tens, _, ones = text.partition("十")
    return (EDITION_DIGITS.get(tens, 1) if tens else 1) * 10 + (EDITION_DIGITS.get(ones, 0) if ones else 0)


def get(url: str, timeout: int = 60) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    head = raw[:3000].decode("latin-1", "ignore")
    match = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    return raw.decode(match.group(1) if match else "utf-8", "replace")


def discover(delay: float, max_pages: int = 20) -> list[dict[str, Any]]:
    """Walk the 优秀作品 listing and keep the entries that name an edition and a problem."""
    entries: dict[str, dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        url = LIST_URL if page == 1 else LIST_PAGE_URL.format(page=page)
        body = get(url)
        found = 0
        for path, entry_id, raw_title, raw_teaser in ENTRY_RE.findall(body):
            title = " ".join(html.unescape(re.sub(r"<[^>]+>", "", raw_title)).split())
            teaser = " ".join(html.unescape(re.sub(r"<[^>]+>", "", raw_teaser)).split())
            detail_url = urllib.parse.urljoin(url, path)
            if detail_url in entries:
                continue
            edition = EDITION_RE.search(title)
            code = CODE_RE.search(title)
            if not edition or not code or "挑战赛" not in title:
                continue
            year = FIRST_EDITION_YEAR + chinese_number(edition.group(1)) - 1
            entries[detail_url] = {
                "entry_id": entry_id,
                "detail_url": detail_url,
                "listing_title": title,
                "listing_teaser": teaser,
                "year": year,
                "letter": code.group(1).upper(),
                "code": f"{code.group(1).upper()}{code.group(2) or ''}",
            }
            found += 1
        print(f"TIPDM_LIST page={page} new={found} total={len(entries)}")
        sys.stdout.flush()
        if not found and page > 1:
            break
        time.sleep(delay)
    return sorted(entries.values(), key=lambda item: (-item["year"], item["code"], item["entry_id"]))


def local_path(entry: dict[str, Any]) -> Path:
    return PAPER_ROOT / str(entry["year"]) / entry["letter"] / f"{entry['code']}.pdf"


def full_text_href(body: str) -> str:
    """Pick the 全文/原文 link.

    编辑器把链接文字包在 <strong>/<span> 里，标签顺序也不固定，所以按锚文本判断而不是
    按整段标记匹配；个别页面把同一份 PDF 挂了两次（缩略图 + 文末链接），取最后一个
    带「全文/原文」字样的锚点即为正文入口。
    """
    anchors = PDF_ANCHOR_RE.findall(body)
    labelled = [href for href, label in anchors if FULLTEXT_LABEL_RE.search(re.sub(r"<[^>]+>", "", label))]
    if labelled:
        return labelled[-1]
    return anchors[-1][0] if anchors else ""


def fetch(delay: float) -> dict[str, Any]:
    entries = discover(delay)
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    # 同一届偶尔有两条目录条目用同一个作品编号（2015 A3 就是这样），编号相同但
    # 全文不同，若不区分会互相覆盖。
    taken: dict[tuple[int, str], str] = {}
    for index, entry in enumerate(entries, start=1):
        body = get(entry["detail_url"])
        href = full_text_href(body)
        if not href:
            missing.append(f"{entry['detail_url']} ({entry['listing_title']}) 没有全文链接")
            continue
        pdf_url = urllib.parse.urljoin(entry["detail_url"], html.unescape(href))
        # 主办方站点混用 http/https，统一走 https，避免明文回源。
        pdf_url = re.sub(r"^http://", "https://", pdf_url)
        if taken.get((entry["year"], entry["code"])) == pdf_url:
            continue
        suffix = 1
        while (entry["year"], entry["code"]) in taken:
            suffix += 1
            entry["code"] = f"{entry['code'].split('-')[0]}-{suffix}"
        taken[(entry["year"], entry["code"])] = pdf_url
        destination = local_path(entry)
        if not destination.exists():
            payload = urllib.request.urlopen(
                urllib.request.Request(pdf_url, headers={"User-Agent": USER_AGENT}), timeout=180
            ).read()
            if payload[:5] != b"%PDF-":
                missing.append(f"{pdf_url} 返回的不是 PDF")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        records.append({
            **entry,
            "pdf_url": pdf_url,
            "path": destination.relative_to(ROOT).as_posix(),
            "bytes": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        })
        if index % 10 == 0 or index == len(entries):
            print(f"TIPDM_FETCH {index}/{len(entries)} kept={len(records)} missing={len(missing)}")
            sys.stdout.flush()
        time.sleep(delay)
    manifest = {
        "schema_version": "1.0.0",
        "source_id": SOURCE_ID,
        "listing_url": LIST_URL,
        "stats": {"entry_count": len(entries), "paper_count": len(records), "missing_count": len(missing)},
        "missing": missing,
        "records": records,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    (OUTPUT.parent / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


#: 封面上的奖项写法很散（「特等奖并获泰迪杯」「一等并获网宿创新奖」），按奖等归一，
#: 供论文页的奖项筛选使用；企业冠名等完整表述保留在 distinctions 里不丢。
AWARD_RANKS = (("特等", "特等奖"), ("一等", "一等奖"), ("二等", "二等奖"), ("三等", "三等奖"))


def normalize_award(raw: str) -> tuple[str, list[str]]:
    text = " ".join(raw.split())
    if not text:
        return "优秀作品", []
    for marker, canonical in AWARD_RANKS:
        if text.startswith(marker):
            return canonical, [text] if text != canonical else []
    return "优秀作品", [text]


def clean_listing_title(title: str) -> str:
    """挑战赛条目形如「第十届挑战赛A1-基于深度学习的农田害虫定位与识别研究」。"""
    _, _, tail = title.partition("-")
    return " ".join((tail or title).split())


def build() -> dict[str, Any]:
    manifest_path = OUTPUT.parent / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError("Run `stage_tipdm_award_papers.py fetch` first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    papers: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for record in manifest["records"]:
        path = ROOT / record["path"]
        if not path.exists():
            skipped.append(f"{record['path']} (not downloaded)")
            continue
        try:
            lines, page_count = page_lines(path, page_limit=4)
            fields = parse_cover(lines)
        except Exception as error:
            skipped.append(f"{record['path']} ({type(error).__name__}: {error})")
            continue
        year, letter, code = record["year"], record["letter"], record["code"]
        title = fields["title"] or clean_listing_title(record["listing_title"])
        award, distinctions = normalize_award(fields["award"])
        paper_id = f"tipdm-paper-{year}-{letter.lower()}-{code.lower()}"
        problem_id = f"tipdm-cup-{year}-{letter.lower()}"
        papers[paper_id] = {
            "title": title,
            "record_type": "paper",
            "problem_id": problem_id,
            "problem_code": f"{year} 泰迪杯 {letter}",
            "competition": COMPETITION,
            "category": "优秀作品",
            "year": year,
            "award": award,
            "distinctions": distinctions,
            "institution": fields["institution"] or None,
            "team_id": code,
            "keywords": fields["keywords"],
            "models": derive_models(title, fields["abstract"], fields["keywords"]),
            "summary": summarize(fields["abstract"]) or f"{year} 年“泰迪杯”数据挖掘挑战赛 {letter} 题获奖作品全文。",
            "innovation": keyword_note(fields["keywords"]),
            "page_count": page_count,
            "source_id": SOURCE_ID,
            "source_url": record["detail_url"],
            "full_text_url": record["pdf_url"],
            "local_pdf_path": f"/paper-files/archive/tipdm/{year}/{letter}/{code}.pdf",
            "source_file_bytes": record["bytes"],
            "source_sha256": record["sha256"],
        }
    result = {
        "schema_version": "1.0.0",
        "source_id": SOURCE_ID,
        "competition": COMPETITION,
        "listing_url": LIST_URL,
        "stats": {
            "paper_count": len(papers),
            "with_title": sum(1 for item in papers.values() if item["title"]),
            "with_award_rank": sum(1 for item in papers.values() if item["award"] != "优秀作品"),
            "with_institution": sum(1 for item in papers.values() if item["institution"]),
            "with_keywords": sum(1 for item in papers.values() if item["keywords"]),
            "skipped_count": len(skipped),
        },
        "skipped": skipped,
        "papers": papers,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def verify() -> dict[str, Any]:
    data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    for paper_id, record in data["papers"].items():
        path = PAPER_ROOT / str(record["year"]) / record["problem_code"].split()[-1] / f"{record['team_id']}.pdf"
        if not path.exists() or path.stat().st_size != record["source_file_bytes"]:
            raise RuntimeError(f"Staged PDF missing or resized: {paper_id}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["source_sha256"]:
            raise RuntimeError(f"Staged PDF hash mismatch: {paper_id}")
        host = urllib.parse.urlparse(record["full_text_url"]).hostname or ""
        if not host.endswith("tipdm.org") and not host.endswith("tipdm.net"):
            raise RuntimeError(f"Full text is not served by the organiser: {paper_id} -> {record['full_text_url']}")
        if not record["title"]:
            raise RuntimeError(f"Unusable title for {paper_id}")
    print("TIPDM_AWARD_PAPERS_VERIFY_OK " + json.dumps(data["stats"], ensure_ascii=False))
    return data["stats"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "build", "verify", "all"))
    parser.add_argument("--delay", type=float, default=1.5, help="每次请求之间的间隔秒数")
    args = parser.parse_args()
    if args.command in {"fetch", "all"}:
        manifest = fetch(args.delay)
        print(json.dumps(manifest["stats"], ensure_ascii=False))
    if args.command in {"build", "all"}:
        result = build()
        print(json.dumps(result["stats"], ensure_ascii=False))
    if args.command in {"verify", "all"}:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
