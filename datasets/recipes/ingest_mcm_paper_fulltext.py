#!/usr/bin/env python3
"""Ingest COMAP MCM/ICM Outstanding papers from the pinned community snapshot.

The Jackksonns/MCM-ICM-Outstanding-Papers repository stores one PDF per team,
named by control number: `2016/A/56742.pdf` (2013-2015 omit the problem folder).
This recipe downloads every PDF the pinned tree names, verifies each against its
git blob hash, and reads the standard summary sheet: the declared title, the
problem letter ("Problem Chosen", needed where the path has none), the summary
paragraph and the keywords line.

Team member names are not part of unabridged papers; nothing personal is read.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from ingest_cpmcm_paper_fulltext import METHOD_VOCABULARY, git_blob_sha


ROOT = Path(__file__).resolve().parents[2]
TREE_PATH = ROOT / "datasets/raw/sources/github/Jackksonns-MCM-ICM-Outstanding-Papers-tree.json"
PAPER_ROOT = ROOT / "datasets/raw/sources/github/Jackksonns-MCM-ICM-Outstanding-Papers/papers"
OUTPUT = ROOT / "datasets/interim/mcm_paper_fulltext/papers.json"
REPOSITORY = "Jackksonns/MCM-ICM-Outstanding-Papers"
COMMIT = "d29267cb9e993419749e6111981b30a44183fdf8"
PAPER_PATH_RE = re.compile(r"^(\d{4})/(?:([A-F])/)?(\d{4,8})\.pdf$")
USER_AGENT = "OpenMathModel-dataset/1.0 (+https://github.com/IvanCodesDev/OpenMathModel)"

ICM_LETTERS = {"D", "E", "F"}
PROBLEM_CHOSEN_RE = re.compile(r"Problem\s*Chosen\s*[:：]?\s*([A-F])\b", re.I)
KEYWORD_RE = re.compile(r"^Key\s*words?\s*[:：]?\s*(.*)$", re.I)
SUMMARY_HEAD_RE = re.compile(r"^(Summary|Abstract)\s*[:：]?\s*(.*)$", re.I)
# Summary-sheet boilerplate that must never be mistaken for the title.
BOILERPLATE_RE = re.compile(
    r"("
    r"control\s*number|problem\s*chosen|summary\s*sheet|contest\s*(date|year)|"
    r"for\s+office\s+use\s+only|^[TF]\s*[1-4]\s*[_.]*$|^_+$|"
    r"mathematical\s+contest\s+in\s+modeling|interdisciplinary\s+contest|"
    r"^\d{4}\s*(mcm|icm|mcm/icm)|^(mcm|icm|mcm/icm)\b|^page\s+\d+|^team\s*#?\s*\d+|^\d{4,8}$"
    r")",
    re.I,
)


def read_tree() -> list[dict[str, Any]]:
    return json.loads(TREE_PATH.read_text(encoding="utf-8-sig"))["tree"]


def paper_targets(years: set[int]) -> list[dict[str, Any]]:
    targets = []
    for item in read_tree():
        if item.get("type") != "blob":
            continue
        match = PAPER_PATH_RE.match(item.get("path", ""))
        if not match or int(match.group(1)) not in years:
            continue
        targets.append({
            "path": item["path"],
            "year": int(match.group(1)),
            "letter": match.group(2) or "",
            "team_id": match.group(3),
            "size": int(item.get("size", 0)),
            "git_blob_sha": item["sha"],
        })
    return sorted(targets, key=lambda item: (-item["year"], item["letter"], item["team_id"]))


def local_path(target: dict[str, Any]) -> Path:
    letter = target["letter"] or "_"
    return PAPER_ROOT / f"{target['year']}/{letter}/{target['team_id']}.pdf"


def fetch_one(target: dict[str, Any], attempts: int = 3) -> tuple[dict[str, Any], str]:
    destination = local_path(target)
    if destination.exists() and git_blob_sha(destination.read_bytes()) == target["git_blob_sha"]:
        return target, "cached"
    quoted = urllib.parse.quote(target["path"])
    # raw.githubusercontent.com 在部分开发网络下无法解析；jsDelivr 提供同一提交的
    # 字节级镜像。本机对各域名的 DNS 解析会间歇失效，故把 jsDelivr 官方备用域都
    # 列进候选，逐个尝试；所有来源都要过 git blob 哈希校验。
    urls = tuple(
        f"https://{host}/gh/{REPOSITORY}@{COMMIT}/{quoted}"
        for host in ("cdn.jsdelivr.net", "gcore.jsdelivr.net", "testingcf.jsdelivr.net", "fastly.jsdelivr.net")
    ) + (f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{quoted}",)
    last_error = "unknown"
    for attempt in range(attempts):
        for url in urls:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                payload = urllib.request.urlopen(request, timeout=180).read()
            except Exception as error:  # network flake; try the next source
                last_error = f"{type(error).__name__}: {error}"
                continue
            if git_blob_sha(payload) != target["git_blob_sha"]:
                last_error = "blob hash mismatch"
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            return target, "downloaded"
        time.sleep(2 * (attempt + 1))
    return target, f"failed ({last_error})"


def fetch(years: set[int], workers: int) -> dict[str, int]:
    targets = paper_targets(years)
    counts = {"downloaded": 0, "cached": 0, "failed": 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (target, status) in enumerate(pool.map(fetch_one, targets), start=1):
            key = status if status in counts else "failed"
            counts[key] += 1
            if status.startswith("failed"):
                print(f"PAPER_FETCH_FAIL {target['path']} {status}")
            elif index % 20 == 0 or index == len(targets):
                print(f"PAPER_FETCH {index}/{len(targets)} downloaded={counts['downloaded']} cached={counts['cached']}")
    return counts


def page_lines(pdf_path: Path, page_count: int = 2) -> tuple[list[str], int]:
    import pdfplumber

    lines: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        for page in pdf.pages[:page_count]:
            text = page.extract_text() or ""
            lines.extend(" ".join(part.split()) for part in text.splitlines() if part.strip())
    return lines, total


def title_candidate(line: str) -> bool:
    if not 8 <= len(line) <= 160:
        return False
    if BOILERPLATE_RE.search(line):
        return False
    letters = sum(1 for char in line if char.isalpha())
    return letters >= max(6, len(line) // 3)


def cover_fields(pdf_path: Path, path_letter: str) -> dict[str, Any]:
    lines, page_count = page_lines(pdf_path)

    letter = path_letter
    title_lines: list[str] = []
    summary: list[str] = []
    keywords: list[str] = []
    summary_started = False

    for index, line in enumerate(lines):
        if not letter:
            match = PROBLEM_CHOSEN_RE.search(line)
            if match:
                letter = match.group(1).upper()
        keyword_match = KEYWORD_RE.match(line)
        if keyword_match and not keywords:
            keywords = [
                part.strip() for part in re.split(r"[;；,，、]\s*|\s{2,}", keyword_match.group(1))
                if 1 < len(part.strip()) <= 40
            ]
            if summary_started:
                break
            continue
        if not summary_started:
            head_match = SUMMARY_HEAD_RE.match(line)
            if head_match and len(head_match.group(1)) >= len(line.strip()) - len(head_match.group(2)) - 2:
                summary_started = True
                if head_match.group(2):
                    summary.append(head_match.group(2))
                continue
            if title_candidate(line):
                # Wrapped titles span consecutive candidate lines before the summary.
                if title_lines and index and not title_candidate(lines[index - 1]):
                    continue
                if len(title_lines) < 3:
                    title_lines.append(line)
            continue
        if len(" ".join(summary)) < 900:
            summary.append(line)
        elif keywords:
            break

    title = " ".join(" ".join(title_lines).split())
    return {
        "title": title,
        "letter": letter,
        "keywords": keywords[:8],
        "summary_text": " ".join(summary),
        "page_count": page_count,
    }


def derive_models(fields: dict[str, Any]) -> list[str]:
    haystack = " ".join([fields["title"], fields["summary_text"], *fields["keywords"]]).lower()
    found = [label for label, forms in METHOD_VOCABULARY.items()
             if any(form.lower() in haystack for form in forms)]
    return found[:6]


def summarize(text: str, limit: int = 220) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    ends = [match.end() for match in re.finditer(r"[.!?。]\s", window)]
    return window[:ends[-1]].strip() if ends else window.rstrip() + "…"


def blob_url(path: str) -> str:
    return f"https://github.com/{REPOSITORY}/blob/{COMMIT}/{urllib.parse.quote(path)}"


def build(years: set[int]) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    targets = paper_targets(years)
    for index, target in enumerate(targets, start=1):
        path = local_path(target)
        if not path.exists():
            skipped.append(f"{target['path']} (not downloaded)")
            continue
        try:
            fields = cover_fields(path, target["letter"])
        except Exception as error:
            skipped.append(f"{target['path']} ({type(error).__name__}: {error})")
            continue
        letter = fields["letter"]
        family = "ICM" if letter in ICM_LETTERS else "MCM"
        family_id = family.lower() if letter else "mcmicm"
        letter_id = letter.lower() if letter else "x"
        paper_id = f"comap-{target['year']}-{family_id}-{letter_id}-{target['team_id']}"
        records[paper_id] = {
            "title": fields["title"],
            "letter": letter,
            "team_id": target["team_id"],
            "year": target["year"],
            "problem_code": f"{target['year']} {family} {letter}".rstrip() if letter else f"{target['year']} MCM/ICM",
            "keywords": fields["keywords"],
            "models": derive_models(fields),
            "summary": summarize(fields["summary_text"]),
            "page_count": fields["page_count"],
            "source_path": target["path"],
            "source_url": blob_url(target["path"]),
            "git_blob_sha": target["git_blob_sha"],
            "source_file_bytes": target["size"],
        }
        if index % 20 == 0 or index == len(targets):
            print(f"PAPER_PARSE {index}/{len(targets)} parsed={len(records)} skipped={len(skipped)}")

    with_title = sum(1 for item in records.values() if item["title"])
    result = {
        "schema_version": "1.0.0",
        "source_id": "github_jackksonns_mcm_icm",
        "repository": REPOSITORY,
        "commit": COMMIT,
        "years": sorted(years),
        "stats": {
            "target_count": len(targets),
            "parsed_count": len(records),
            "skipped_count": len(skipped),
            "with_title": with_title,
            "with_letter": sum(1 for item in records.values() if item["letter"]),
            "with_keywords": sum(1 for item in records.values() if item["keywords"]),
            "with_models": sum(1 for item in records.values() if item["models"]),
        },
        "skipped": skipped,
        "papers": records,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def verify() -> dict[str, Any]:
    data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    for paper_id, record in data["papers"].items():
        if record["title"] and (record["title"].isdigit() or len(record["title"]) < 8):
            raise RuntimeError(f"Unusable title for {paper_id}")
    stats = data["stats"]
    ratio = stats["parsed_count"] / max(1, stats["target_count"])
    if ratio < 0.95:
        raise RuntimeError(f"Too many unreadable PDFs: {ratio:.0%}")
    # 该仓库相当一部分 PDF 是无文本层的整页图片（2025 年抽样为纯图片版）；
    # 封面识别率只作报告，不作门禁——图片版全文在阅读器中照常可读，
    # 题名/院校可由官方结果页记录补充，后续可用 OCR 批处理再提升识别率。
    title_ratio = stats["with_title"] / max(1, stats["parsed_count"])
    print(f"MCM_PAPER_TITLE_RATIO {title_ratio:.0%}")
    print("MCM_PAPER_FULLTEXT_VERIFY_OK " + json.dumps(stats, ensure_ascii=False))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "build", "verify", "all"))
    parser.add_argument("--years", type=int, nargs="+", default=list(range(2013, 2026)))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    years = set(args.years)
    if args.command in {"fetch", "all"}:
        print(json.dumps(fetch(years, args.workers), ensure_ascii=False))
    if args.command in {"build", "all"}:
        result = build(years)
        print(json.dumps(result["stats"], ensure_ascii=False))
    if args.command in {"verify", "all"}:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
