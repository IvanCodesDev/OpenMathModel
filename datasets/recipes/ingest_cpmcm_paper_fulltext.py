#!/usr/bin/env python3
"""Recover real bibliographic data from CPMCM outstanding-paper PDFs.

The GitHub tree snapshot only yields file names, so every paper record carried a
team number where its title belongs and a placeholder everywhere else. This
recipe downloads the PDFs the snapshot names, checks each against its git blob
hash, and reads the standard CPMCM cover sheet: the school, the team number, the
declared 题目 and the 摘要/关键词 block.

Team member names are deliberately not extracted. They are personal data and the
product has no use for them.
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

import pdf_layout


ROOT = Path(__file__).resolve().parents[2]
TREE_PATH = ROOT / "datasets/raw/sources/github/zhanwen-MathModel-tree.json"
PAPER_ROOT = ROOT / "datasets/raw/sources/github/zhanwen-MathModel/papers"
OUTPUT = ROOT / "datasets/interim/cpmcm_paper_fulltext/papers.json"
REPOSITORY = "zhanwen/MathModel"
COMMIT = "cd5be91735ebf11d5ee52eb170e86a6d07131977"
PAPER_PATH_RE = re.compile(r"^国赛论文/(\d{4})年优秀论文/([A-F])/(.+)\.pdf$")
USER_AGENT = "OpenMathModel-dataset/1.0 (+https://github.com/IvanCodesDev/OpenMathModel)"

TITLE_RE = re.compile(r"^题\s*目\s*[:：]?\s*(.*)$")
ABSTRACT_RE = re.compile(r"^摘\s*要\s*[:：]?\s*(.*)$")
KEYWORD_RE = re.compile(r"^关\s*键\s*词\s*[:：]?\s*(.*)$")
SCHOOL_RE = re.compile(r"学\s*校\s*[:：]?\s*([^\s].*?)\s*$")
TEAM_RE = re.compile(r"参赛队号\s*[:：]?\s*([A-Za-z]?\d{6,})")
STOP_RE = re.compile(r"^(目\s*录|Abstract|Key\s*words|一\s*、|1\s*[.、]\s*问题重述)")
# Cover sheets often place 题目/摘要/关键词 close enough that they land in one
# visual paragraph, so split them apart before reading the fields.
MARKER_SPLIT_RE = re.compile(r"(?=题\s*目\s*[:：]|摘\s*要\s*[:：]|关\s*键\s*词\s*[:：])")

# Method vocabulary used to tag papers from their own abstract and keywords.
# Each entry maps a canonical label to the surface forms that may appear.
METHOD_VOCABULARY: dict[str, tuple[str, ...]] = {
    "线性规划": ("线性规划",),
    "整数规划": ("整数规划", "混合整数", "0-1规划", "0−1规划"),
    "非线性规划": ("非线性规划",),
    "动态规划": ("动态规划",),
    "多目标优化": ("多目标优化", "多目标规划", "帕累托", "Pareto"),
    "遗传算法": ("遗传算法", "NSGA", "Genetic Algorithm"),
    "粒子群算法": ("粒子群", "PSO"),
    "模拟退火": ("模拟退火",),
    "蚁群算法": ("蚁群",),
    "蒙特卡洛模拟": ("蒙特卡洛", "Monte Carlo", "蒙特卡罗"),
    "神经网络": ("神经网络", "BP网络", "Neural Network"),
    "卷积神经网络": ("卷积神经网络", "CNN"),
    "循环神经网络": ("LSTM", "GRU", "循环神经网络"),
    "Transformer": ("Transformer", "注意力机制", "Attention"),
    "随机森林": ("随机森林", "Random Forest"),
    "梯度提升树": ("XGBoost", "LightGBM", "GBDT", "CatBoost", "梯度提升"),
    "支持向量机": ("支持向量机", "SVM"),
    "决策树": ("决策树",),
    "贝叶斯方法": ("贝叶斯", "Bayes"),
    "聚类分析": ("聚类", "K-means", "Kmeans", "DBSCAN"),
    "主成分分析": ("主成分分析", "PCA"),
    "回归分析": ("回归分析", "线性回归", "逻辑回归", "多元回归", "岭回归"),
    "时间序列分析": ("时间序列", "ARIMA", "ARMA", "指数平滑"),
    "灰色预测": ("灰色预测", "GM(1,1)", "灰色模型"),
    "层次分析法": ("层次分析法", "AHP"),
    "熵权法": ("熵权法",),
    "TOPSIS": ("TOPSIS", "优劣解距离"),
    "模糊综合评价": ("模糊综合", "模糊评价", "模糊数学"),
    "马尔可夫模型": ("马尔可夫", "Markov", "马尔科夫"),
    "排队论": ("排队论", "排队模型"),
    "图论": ("图论", "最短路", "最小生成树", "网络流"),
    "元胞自动机": ("元胞自动机",),
    "微分方程模型": ("微分方程", "偏微分方程", "常微分"),
    "有限元方法": ("有限元",),
    "卡尔曼滤波": ("卡尔曼", "Kalman"),
    "小波分析": ("小波",),
    "强化学习": ("强化学习", "Q-learning", "DQN"),
    "离散事件仿真": ("离散事件仿真", "事件仿真"),
    "数据包络分析": ("数据包络", "DEA"),
    "元启发式搜索": ("禁忌搜索", "变邻域", "启发式算法"),
}


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
            "letter": match.group(2),
            "stem": match.group(3),
            "size": int(item.get("size", 0)),
            "git_blob_sha": item["sha"],
        })
    return sorted(targets, key=lambda item: (-item["year"], item["letter"], item["stem"]))


def git_blob_sha(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(payload) + payload).hexdigest()


def local_path(target: dict[str, Any]) -> Path:
    return PAPER_ROOT / f"{target['year']}/{target['letter']}/{target['stem']}.pdf"


def fetch_one(target: dict[str, Any], attempts: int = 3) -> tuple[dict[str, Any], str]:
    destination = local_path(target)
    if destination.exists() and git_blob_sha(destination.read_bytes()) == target["git_blob_sha"]:
        return target, "cached"
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{urllib.parse.quote(target['path'])}"
    last_error = "unknown"
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            payload = urllib.request.urlopen(request, timeout=180).read()
        except Exception as error:  # network flake; retry with backoff
            last_error = f"{type(error).__name__}: {error}"
            time.sleep(2 * (attempt + 1))
            continue
        if git_blob_sha(payload) != target["git_blob_sha"]:
            last_error = "blob hash mismatch"
            time.sleep(2 * (attempt + 1))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return target, "downloaded"
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


def collapse_doubled(text: str) -> str:
    """Undo the duplicated glyph runs some covers use to fake a bold face."""
    stripped = text.replace(" ", "")
    if len(stripped) < 6 or len(stripped) % 2:
        return text
    pairs = [stripped[index:index + 2] for index in range(0, len(stripped), 2)]
    if all(pair[0] == pair[1] for pair in pairs):
        return "".join(pair[0] for pair in pairs)
    return text


def cover_lines(blocks: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for block in blocks:
        text = " ".join(str(block.get("text", "")).split())
        for part in MARKER_SPLIT_RE.split(text):
            part = collapse_doubled(part.strip())
            if part:
                lines.append(part)
    return lines


def cover_page_limit(pdf_path: Path) -> int:
    """Stop before the table of contents.

    Its dot leaders dominate the left-margin and line-gap statistics, which would
    otherwise flatten the abstract into a single paragraph.
    """
    import pdfplumber

    with pdfplumber.open(str(pdf_path)) as pdf:
        for index, page in enumerate(pdf.pages[:6], start=1):
            text = page.extract_text() or ""
            if re.search(r"目\s*录", text[:200]) or len(re.findall(r"\.{8,}", text)) >= 3:
                return max(1, index - 1)
        return min(4, len(pdf.pages))


def cover_fields(pdf_path: Path) -> dict[str, Any]:
    limit = cover_page_limit(pdf_path)
    blocks, page_count, _ = pdf_layout.build_blocks(pdf_path, pdf_path.stem, None, page_limit=limit, with_assets=False)
    texts = cover_lines(blocks)

    title = ""
    institution = ""
    team_id = ""
    abstract: list[str] = []
    keywords: list[str] = []

    for index, text in enumerate(texts):
        if not title:
            match = TITLE_RE.match(text)
            if match:
                title = match.group(1).strip()
                # A long title wraps onto the following block.
                if not title and index + 1 < len(texts):
                    title = texts[index + 1].strip()
        if not institution:
            match = SCHOOL_RE.search(text)
            if match and "参赛队号" not in match.group(1):
                institution = match.group(1).strip()
        if not team_id:
            match = TEAM_RE.search(text)
            if match:
                team_id = match.group(1)

    for index, text in enumerate(texts):
        match = ABSTRACT_RE.match(text)
        if not match:
            continue
        head = match.group(1).strip()
        if head:
            abstract.append(head)
        for follower in texts[index + 1:]:
            keyword_match = KEYWORD_RE.match(follower)
            if keyword_match:
                keywords = [
                    part.strip() for part in re.split(r"[;；,，、\s]{1,}", keyword_match.group(1))
                    if len(part.strip()) > 1
                ]
                break
            if STOP_RE.match(follower):
                break
            abstract.append(follower)
        break

    return {
        "title": " ".join(title.split()),
        "institution": institution,
        "team_id": team_id,
        "abstract": [part for part in abstract if len(part) > 8],
        "keywords": keywords[:8],
        "page_count": page_count,
    }


def derive_models(fields: dict[str, Any]) -> list[str]:
    haystack = " ".join([fields["title"], *fields["abstract"], *fields["keywords"]])
    found = [label for label, forms in METHOD_VOCABULARY.items()
             if any(form.lower() in haystack.lower() for form in forms)]
    return found[:6]


def summarize(paragraphs: list[str], limit: int = 140) -> str:
    text = paragraphs[0] if paragraphs else ""
    if len(text) <= limit:
        return text
    window = text[:limit]
    ends = [match.end() for match in re.finditer(r"[。．.；;]", window)]
    return window[:ends[-1]] if ends else window.rstrip() + "…"


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
            fields = cover_fields(path)
        except Exception as error:
            skipped.append(f"{target['path']} ({type(error).__name__}: {error})")
            continue
        if not fields["title"] or not fields["abstract"]:
            skipped.append(f"{target['path']} (cover sheet not recognised)")
            continue
        digest = hashlib.sha1(target["path"].encode("utf-8")).hexdigest()[:12]
        records[f"cpmcm-paper-{target['year']}-{target['letter'].lower()}-{digest}"] = {
            "title": fields["title"],
            "institution": fields["institution"] or None,
            "team_id": fields["team_id"] or target["stem"],
            "keywords": fields["keywords"],
            "models": derive_models(fields),
            "summary": summarize(fields["abstract"]),
            "content_blocks": [{"type": "paragraph", "text": part} for part in fields["abstract"]],
            "page_count": fields["page_count"],
            "git_blob_sha": target["git_blob_sha"],
            "source_file_bytes": target["size"],
        }
        if index % 20 == 0 or index == len(targets):
            print(f"PAPER_PARSE {index}/{len(targets)} parsed={len(records)} skipped={len(skipped)}")

    result = {
        "schema_version": "1.0.0",
        "source_id": "github_zhanwen_mathmodel",
        "repository": REPOSITORY,
        "commit": COMMIT,
        "years": sorted(years),
        "stats": {
            "target_count": len(targets),
            "parsed_count": len(records),
            "skipped_count": len(skipped),
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
        if not record["title"] or record["title"].isdigit():
            raise RuntimeError(f"Unusable title for {paper_id}")
        if not record["content_blocks"]:
            raise RuntimeError(f"Empty abstract for {paper_id}")
    ratio = data["stats"]["parsed_count"] / max(1, data["stats"]["target_count"])
    if ratio < 0.8:
        raise RuntimeError(f"Cover-sheet recognition too low: {ratio:.0%}")
    print("CPMCM_PAPER_FULLTEXT_VERIFY_OK " + json.dumps(data["stats"], ensure_ascii=False))
    return data["stats"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "build", "verify", "all"))
    parser.add_argument("--years", type=int, nargs="+", default=[2021, 2022, 2023])
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
