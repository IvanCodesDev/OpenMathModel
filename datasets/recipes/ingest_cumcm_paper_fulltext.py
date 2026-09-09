#!/usr/bin/env python3
"""Index CUMCM (国赛) award papers from a pinned community snapshot.

全国大学生数学建模竞赛的优秀论文只在知网的竞赛专栏和纸质《优秀论文选》里发布，
官网 mcm.edu.cn 的历年页面只挂赛题压缩包，没有一篇可下载的论文。因此这批论文与
美赛论文一样，取自固定 commit 的社区仓库快照：每个文件都按 Git blob 哈希校验，
产品侧标注 community_repository_snapshot，绝不冒充官方发布。

两类文件被排除：一是评述、评阅要点、赛题与附件（不是论文），二是文件名里带参赛
队员姓名的那些——姓名是个人信息，既不解析也不落进任何发布字段或本地路径。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from paper_covers import derive_models, keyword_note, page_lines, parse_cover, summarize


ROOT = Path(__file__).resolve().parents[2]
TREE_PATH = ROOT / "datasets/raw/sources/github/personqianduixue-Math_Model-tree.json"
PAPER_ROOT = ROOT / "datasets/raw/sources/paper-archives/cumcm"
OUTPUT = ROOT / "datasets/interim/cumcm_paper_fulltext/papers.json"
REPOSITORY = "personqianduixue/Math_Model"
COMMIT = "8783d0d822f89f98aa6182dd933cc2e9f3e2ddce"
PAPER_PREFIX = "2-1国赛题目+论文/"
USER_AGENT = "OpenMathModel-dataset/1.0 (+https://github.com/IvanCodesDev/OpenMathModel)"
COMPETITION = "全国大学生数学建模竞赛"

#: 评述与评阅要点是命题组的点评、Problems 目录是赛题与数据，都不是参赛论文。
NON_PAPER_RE = re.compile(
    r"(评述|评注|评阅|评卷|要点|赛题|题目|附件|附录|说明|模板|规范|格式|通知|数据|"
    r"/Problems?/|/problems?/|result|-master/)",
    re.I,
)
#: 「编号_姓名_姓名_姓名」「编号-姓名，姓名」这类文件名带参赛队员姓名，整份跳过。
MEMBER_NAME_RE = re.compile(r"[_\-][\u4e00-\u9fff]{2,4}(?:[_\-，,、][\u4e00-\u9fff]{2,4})+")
CONTROL_NUMBER_RE = re.compile(r"^([A-F])[-_]?(\d{3,})$", re.I)
LETTER_DIRECTORY_RE = re.compile(r"/([A-F])\s*题?(?:[^/]*)?/")
LETTER_PREFIX_RE = re.compile(r"^([A-F])[-_]?\d")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
COLLECTION_RE = re.compile(r"(论文全集|优秀论文选)")


def read_tree() -> list[dict[str, Any]]:
    if not TREE_PATH.exists():
        TREE_PATH.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            f"https://api.github.com/repos/{REPOSITORY}/git/trees/{COMMIT}?recursive=1",
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            TREE_PATH.write_bytes(response.read())
    return json.loads(TREE_PATH.read_text(encoding="utf-8-sig"))["tree"]


def paper_letter(tail: str) -> str:
    match = LETTER_DIRECTORY_RE.search("/" + tail)
    if match:
        return match.group(1).upper()
    name = PurePosixPath(tail).stem
    match = LETTER_PREFIX_RE.match(name)
    return match.group(1).upper() if match else ""


def paper_slug(tail: str, letter: str, used: set[tuple[Any, ...]], scope: tuple[Any, ...]) -> str:
    name = PurePosixPath(tail).stem.strip()
    control = CONTROL_NUMBER_RE.match(name)
    if control:
        candidate = f"{control.group(1).upper()}{control.group(2)}"
    elif re.fullmatch(r"\d{3,}", name):
        candidate = f"{letter or 'X'}{name}"
    else:
        # 老年份的论文以题名命名（《最优捕鱼策略》），路径里不放中文题名，
        # 用稳定短哈希代替，题名照常出现在索引里。
        candidate = f"{letter or 'X'}-{hashlib.sha1(tail.encode('utf-8')).hexdigest()[:8]}"
    slug = candidate
    counter = 1
    while (*scope, slug) in used:
        counter += 1
        slug = f"{candidate}-{counter}"
    used.add((*scope, slug))
    return slug


def paper_targets(years: set[int] | None = None) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    used: set[tuple[Any, ...]] = set()
    for item in sorted(read_tree(), key=lambda entry: entry.get("path", "")):
        path = item.get("path", "")
        if item.get("type") != "blob" or not path.startswith(PAPER_PREFIX) or not path.lower().endswith(".pdf"):
            continue
        tail = path[len(PAPER_PREFIX):]
        year_part = tail.split("/", 1)[0]
        if not YEAR_RE.match(year_part):
            continue
        year = int(year_part)
        if years and year not in years:
            continue
        if NON_PAPER_RE.search(tail) or MEMBER_NAME_RE.search(tail):
            continue
        letter = paper_letter(tail)
        targets.append({
            "path": path,
            "tail": tail,
            "year": year,
            "letter": letter,
            "slug": paper_slug(tail, letter, used, (year, letter)),
            "size": int(item.get("size", 0)),
            "git_blob_sha": item["sha"],
            "collection": bool(COLLECTION_RE.search(tail)),
        })
    return sorted(targets, key=lambda item: (-item["year"], item["letter"], item["slug"]))


def git_blob_sha(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(payload) + payload).hexdigest()


def local_path(target: dict[str, Any]) -> Path:
    return PAPER_ROOT / str(target["year"]) / (target["letter"] or "X") / f"{target['slug']}.pdf"


def blob_url(path: str) -> str:
    return f"https://github.com/{REPOSITORY}/blob/{COMMIT}/{urllib.parse.quote(path)}"


def fetch_one(target: dict[str, Any], attempts: int = 3) -> tuple[dict[str, Any], str]:
    destination = local_path(target)
    if destination.exists() and git_blob_sha(destination.read_bytes()) == target["git_blob_sha"]:
        return target, "cached"
    quoted = urllib.parse.quote(target["path"])
    # jsDelivr 拒绝超过 20MB 的文件（2019—2020 年的扫描版论文常常超标），
    # 所以镜像先试、raw 兜底；两条路都要过 blob 哈希校验。
    urls = tuple(
        f"https://{host}/gh/{REPOSITORY}@{COMMIT}/{quoted}"
        for host in ("cdn.jsdelivr.net", "gcore.jsdelivr.net", "fastly.jsdelivr.net")
    ) + (f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{quoted}",)
    last_error = "unknown"
    for attempt in range(attempts):
        for url in urls:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                payload = urllib.request.urlopen(request, timeout=300).read()
            except Exception as error:
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


def fetch(years: set[int] | None, workers: int) -> dict[str, int]:
    targets = paper_targets(years)
    counts = {"downloaded": 0, "cached": 0, "failed": 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (target, status) in enumerate(pool.map(fetch_one, targets), start=1):
            counts[status if status in counts else "failed"] += 1
            if status.startswith("failed"):
                print(f"CUMCM_FETCH_FAIL {target['path']} {status}")
            elif index % 20 == 0 or index == len(targets):
                print(f"CUMCM_FETCH {index}/{len(targets)} downloaded={counts['downloaded']} cached={counts['cached']}")
            sys.stdout.flush()
    return counts


def filename_title(tail: str) -> str:
    """老年份的论文以题名命名，可作为封面解析失败时的题名来源。"""
    name = PurePosixPath(tail).stem.strip()
    name = re.sub(r"^\d{4}年?", "", name)
    name = re.sub(r"[（(]\d+[)）]$", "", name)
    name = name.replace("_", " ").strip(" -—《》")
    if len(name) < 6 or re.fullmatch(r"[A-Za-z0-9 .\-]+", name):
        return ""
    return " ".join(name.split())


def build(years: set[int] | None) -> dict[str, Any]:
    papers: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    targets = paper_targets(years)
    for index, target in enumerate(targets, start=1):
        path = local_path(target)
        if not path.exists():
            skipped.append(f"{target['tail']} (not downloaded)")
            continue
        try:
            lines, page_count = page_lines(path)
            fields = parse_cover(lines)
        except Exception as error:
            skipped.append(f"{target['tail']} ({type(error).__name__}: {error})")
            continue
        year, letter = target["year"], target["letter"]
        title = fields["title"] or filename_title(target["tail"])
        if target["collection"]:
            title = title or f"{year} 年全国大学生数学建模竞赛优秀论文全集"
        paper_id = f"cumcm-paper-{year}-{(letter or 'x').lower()}-{target['slug'].lower()}"
        papers[paper_id] = {
            "title": title,
            "record_type": "paper",
            "problem_id": f"cumcm-{year}-{letter.lower()}" if letter else None,
            "problem_code": f"{year} CUMCM{(' ' + letter) if letter else ''}",
            "competition": COMPETITION,
            "category": "优秀论文",
            "year": year,
            "award": "优秀论文",
            "distinctions": [],
            "institution": fields["institution"] or None,
            "team_id": fields["team_id"] or target["slug"],
            "keywords": fields["keywords"],
            "models": derive_models(title, fields["abstract"], fields["keywords"]),
            "summary": summarize(fields["abstract"])
            or f"{year} 年全国大学生数学建模竞赛{(' ' + letter + ' 题') if letter else ''}优秀论文全文。",
            "innovation": keyword_note(fields["keywords"]) if fields["keywords"]
            else "题目与正文取自固定提交的社区快照，封面未提供关键词。",
            "page_count": page_count,
            "source_id": "github_personqianduixue_math_model",
            "source_url": blob_url(target["path"]),
            "full_text_url": blob_url(target["path"]),
            "local_pdf_path": f"/paper-files/archive/cumcm/{year}/{letter or 'X'}/{target['slug']}.pdf",
            "source_file_bytes": target["size"],
            "source_git_blob_sha": target["git_blob_sha"],
        }
        if index % 25 == 0 or index == len(targets):
            print(f"CUMCM_PARSE {index}/{len(targets)} parsed={len(papers)} skipped={len(skipped)}")
            sys.stdout.flush()

    result = {
        "schema_version": "1.0.0",
        "source_id": "github_personqianduixue_math_model",
        "repository": REPOSITORY,
        "commit": COMMIT,
        "competition": COMPETITION,
        "stats": {
            "target_count": len(targets),
            "paper_count": len(papers),
            "skipped_count": len(skipped),
            "with_title": sum(1 for item in papers.values() if item["title"]),
            "with_keywords": sum(1 for item in papers.values() if item["keywords"]),
            "with_models": sum(1 for item in papers.values() if item["models"]),
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
    for paper_id, record in data["papers"].items():
        path = ROOT / record["local_pdf_path"].replace(
            "/paper-files/archive/cumcm/", "datasets/raw/sources/paper-archives/cumcm/"
        )
        if not path.exists():
            raise RuntimeError(f"Staged PDF missing: {paper_id}")
        if git_blob_sha(path.read_bytes()) != record["source_git_blob_sha"]:
            raise RuntimeError(f"Staged PDF hash mismatch: {paper_id}")
        if MEMBER_NAME_RE.search(record["source_url"]) or MEMBER_NAME_RE.search(record["local_pdf_path"]):
            raise RuntimeError(f"Published path carries participant names: {paper_id}")
    stats = data["stats"]
    ratio = stats["with_title"] / max(1, stats["paper_count"])
    # 2019—2020 那两届的仓库文件是无文本层的扫描件，封面读不出题名；那批论文在
    # 阅读器里照常可读，题名交由赛题标题兜底，因此识别率只报不卡。
    print(f"CUMCM_PAPER_TITLE_RATIO {ratio:.0%}")
    if ratio < 0.7:
        raise RuntimeError(f"Title recognition too low: {ratio:.0%}")
    print("CUMCM_PAPER_FULLTEXT_VERIFY_OK " + json.dumps(stats, ensure_ascii=False))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "build", "verify", "all"))
    parser.add_argument("--years", type=int, nargs="*", default=None)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    years = set(args.years) if args.years else None
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
