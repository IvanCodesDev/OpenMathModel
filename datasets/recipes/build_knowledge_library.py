#!/usr/bin/env python3
"""Normalize collected competition pages into the frontend knowledge library."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = ROOT / "datasets" / "raw" / "snapshots"
DEFAULT_OUTPUT = ROOT / "apps" / "web" / "src" / "data" / "knowledge-library.json"
FULL_PROBLEMS_PATH = ROOT / "datasets" / "interim" / "github_zhanwen_mathmodel" / "full-problems.json"
ARCHIVE_FULL_PROBLEMS_PATH = ROOT / "datasets" / "interim" / "full_problem_sources" / "problems.json"


def strip_tags(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).replace("\xa0", " ").split())


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self.href: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self.href is not None:
            self.items.append({"href": self.href, "text": " ".join("".join(self.text).split())})
            self.href = None
            self.text = []


def anchors(raw: str, base_url: str) -> list[dict[str, str]]:
    parser = AnchorParser()
    parser.feed(raw)
    output = []
    for item in parser.items:
        if not item["href"]:
            continue
        absolute = urllib.parse.urljoin(base_url, item["href"])
        parts = urllib.parse.urlsplit(absolute)
        normalized = urllib.parse.urlunsplit((
            parts.scheme,
            parts.netloc,
            urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/:@"),
            urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&;%+,:/?"),
            parts.fragment,
        ))
        output.append({"url": normalized, "text": item["text"]})
    return output


def latest_records(source_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in (SNAPSHOT_ROOT / source_id).rglob("manifest.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        candidates.append((path, data))
    if not candidates:
        return {}, []
    _, best = max(candidates, key=lambda item: (item[1]["summary"]["fetched"], item[1].get("run_status") == "completed", item[1]["run_id"]))
    return best, best["records"]


def read_raw(record: dict[str, Any]) -> str:
    return (ROOT / record["stored_path"]).read_text(encoding="utf-8", errors="replace")


def classify(title: str) -> tuple[str, list[str], list[str]]:
    lower = title.lower()
    rules = [
        (("predict", "forecast", "wordle", "data with"), "预测分析", ["数据分析", "预测模型"]),
        (("tourism", "sustain", "agriculture", "wildlife", "drought", "pollution"), "可持续发展", ["评价模型", "多目标决策"]),
        (("roadmap", "city", "traffic", "sports", "olympic"), "规划优化", ["运筹优化", "情景分析"]),
        (("network", "cyber", "ai", "battery", "submersible"), "系统建模", ["系统分析", "仿真建模"]),
        (("price", "insurance", "resource", "medal", "gdp"), "评价与决策", ["统计分析", "决策模型"]),
    ]
    for keywords, problem_type, directions in rules:
        contains = lambda keyword: bool(re.search(rf"\b{re.escape(keyword)}\b", lower)) if len(keyword) <= 2 else keyword in lower
        if any(contains(keyword) for keyword in keywords):
            matched = [keyword.replace("predict", "预测").replace("forecast", "预测") for keyword in keywords if contains(keyword)]
            return problem_type, directions, matched or [problem_type]
    return "综合建模", ["综合分析"], ["数学建模"]


def attachment_kind(title: str, url: str) -> str:
    text = f"{title} {url}".lower()
    if "result" in text:
        return "results"
    if any(word in text for word in ("data", ".csv", ".xlsx", ".zip")):
        return "data"
    if "problem" in text:
        return "problem"
    return "other"


def comap_problems(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    corrections = {"understanding used sailboat prices": "Y", "the future of the olympics": "Z"}
    for record in records:
        year_match = re.search(r"/contests/(202[1-5])/problems/?", record["url"])
        if not year_match:
            continue
        year = int(year_match.group(1))
        raw = read_raw(record)
        page_anchors = anchors(raw, record["url"])
        for item in page_anchors:
            match = re.match(r"(MCM|ICM)\s+Problem\s+([A-Z]):\s*(.+)", item["text"], re.I)
            if not match:
                continue
            family, letter, title = match.group(1).upper(), match.group(2).upper(), match.group(3).strip()
            letter = corrections.get(title.lower(), letter)
            problem_id = f"comap-{year}-{family.lower()}-{letter.lower()}"
            problem_type, directions, keywords = classify(title)
            output[problem_id] = {
                "id": problem_id,
                "code": f"{year} {family} {letter}",
                "title": title,
                "competition": "COMAP MCM/ICM",
                "category": "美赛",
                "year": year,
                "problem_type": problem_type,
                "modeling_directions": directions,
                "keywords": keywords,
                "data_requirement": "题面与外部资料",
                "status": "已索引",
                "summary": "已从 COMAP 往届赛题页提取题号、标题和来源；题面内容通过来源链接查看。",
                "source_id": "comap_mcm_icm",
                "source_url": item["url"],
                "source_status": "pending_human_confirmation",
                "access_scope": "linked_content",
                "attachments": [],
            }
        for item in page_anchors:
            if not re.search(r"\.(pdf|zip|csv|xlsx?)(?:$|\?)", item["url"], re.I):
                continue
            code_match = re.search(r"Problem[_ ]([A-Z])", f"{item['text']} {item['url']}", re.I)
            if not code_match:
                continue
            letter = code_match.group(1).upper()
            family = "ICM" if letter in {"D", "E", "F", "Z"} else "MCM"
            problem_id = f"comap-{year}-{family.lower()}-{letter.lower()}"
            if problem_id in output:
                output[problem_id]["attachments"].append({"title": item["text"] or Path(urllib.parse.urlparse(item["url"]).path).name, "url": item["url"], "kind": attachment_kind(item["text"], item["url"])})
                if attachment_kind(item["text"], item["url"]) == "data":
                    output[problem_id]["data_requirement"] = "含公开数据附件"
    return sorted(output.values(), key=lambda item: (-item["year"], item["code"]))


def collection_problems(source_id: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for record in records:
        raw = read_raw(record)
        page_links = anchors(raw, record["url"])
        if source_id == "cmathc_cpmcm" and record["url"].endswith("/tm/325.html"):
            source_link = next((item["url"] for item in page_links if "pan.baidu.com" in item["url"]), record["url"])
            output.append({
                "id": "cpmcm-2025-problems", "code": "2025 CPMCM A–F", "title": "第二十二届中国研究生数学建模竞赛题目合集（A–F）",
                "competition": "中国研究生数学建模竞赛", "category": "研究生赛", "year": 2025,
                "problem_type": "赛题合集", "modeling_directions": ["综合建模"], "keywords": ["华为杯", "研究生数学建模", "六道赛题"],
                "data_requirement": "加密赛题合集", "status": "元数据", "summary": "官方发布页说明本届比赛共六道题目，当前以合集元数据和下载入口呈现。",
                "source_id": source_id, "source_url": record["url"], "source_status": "pending_human_confirmation", "access_scope": "linked_content",
                "attachments": [{"title": "2025 赛题下载入口", "url": source_link, "kind": "collection"}],
            })
        if source_id == "apmcm_problems" and "/detail/" in record["url"]:
            title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
            page_title = strip_tags(title_match.group(1)).split("-APMCM", 1)[0] if title_match else "APMCM Contest Problems"
            year_match = re.search(r"(20\d{2})", page_title)
            if not year_match or not 2021 <= int(year_match.group(1)) <= 2025:
                continue
            year = int(year_match.group(1))
            bundle = next((item for item in page_links if re.search(r"\.(zip|pdf)(?:$|\?)", item["url"], re.I)), None)
            suffix = re.search(r"/detail/(\d+)", record["url"]).group(1)
            output.append({
                "id": f"apmcm-{year}-{suffix}", "code": f"{year} APMCM", "title": page_title,
                "competition": "APMCM 亚太地区大学生数学建模竞赛", "category": "亚太赛", "year": year,
                "problem_type": "赛题合集", "modeling_directions": ["综合建模"], "keywords": ["APMCM", "亚太赛", str(year)],
                "data_requirement": "含赛题压缩包" if bundle else "题面链接", "status": "已索引", "summary": "已从 APMCM 历年赛题详情页提取标题、年份和公开附件入口。",
                "source_id": source_id, "source_url": record["url"], "source_status": "pending_human_confirmation", "access_scope": "linked_content",
                "attachments": ([{"title": bundle["text"] or f"{year} APMCM Problems", "url": bundle["url"], "kind": "collection"}] if bundle else []),
            })
    unique = {item["id"]: item for item in output}
    return sorted(unique.values(), key=lambda item: (-item["year"], item["id"]))


def comap_papers(records: list[dict[str, Any]], problem_titles: dict[str, str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for record in records:
        year_match = re.search(r"/contests/(202[1-5])/results/?", record["url"])
        if not year_match:
            continue
        year = int(year_match.group(1))
        raw = read_raw(record)
        markers: list[tuple[int, str]] = []
        for marker in re.finditer(r"<a\s+name=[\"']?([a-z])[\"']?", raw, re.I):
            letter = marker.group(1).upper()
            if letter in "ABCDEFYZ":
                markers.append((marker.start(), letter))
        collection_match = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>\s*Now Available:\s*View\s+unabridged", raw, re.I)
        full_text_url = urllib.parse.urljoin(record["url"], collection_match.group(1)) if collection_match else None
        for match in re.finditer(r"<strong>\s*(\d{6,8})[\s\t]+([^<]+)</strong>(.*?)(?=<p|</p>|<br|$)", raw, re.I | re.S):
            previous = [item for item in markers if item[0] < match.start()]
            if not previous:
                continue
            letter = previous[-1][1]
            team_id, institution = match.group(1), strip_tags(match.group(2)).rstrip(",")
            family = "ICM" if letter in {"D", "E", "F", "Z"} else "MCM"
            problem_id = f"comap-{year}-{family.lower()}-{letter.lower()}"
            tail = strip_tags(match.group(3))
            distinctions = []
            if "—" in tail:
                distinctions = [part.strip() for part in re.split(r"[&,]", tail.split("—", 1)[1]) if part.strip()]
            problem_title = problem_titles.get(problem_id, f"Problem {letter}")
            output.append({
                "id": f"comap-{year}-{family.lower()}-{letter.lower()}-{team_id}",
                "title": f"{problem_title} · Team {team_id}", "record_type": "paper", "problem_id": problem_id,
                "problem_code": f"{year} {family} {letter}", "competition": "COMAP MCM/ICM", "category": "Outstanding Winner", "year": year,
                "award": "Outstanding Winner", "distinctions": distinctions, "institution": institution, "team_id": team_id,
                "models": [], "innovation": "官方结果页未提供方法标签，待全文解析补充。",
                "summary": f"{institution} 团队在 {year} {family} Problem {letter} 中获得 Outstanding Winner。",
                "source_id": "comap_mcm_icm", "source_url": f"{record['url']}#{letter.lower()}", "full_text_url": full_text_url,
                "source_status": "pending_human_confirmation", "access_scope": "metadata_only",
            })
    return sorted(output, key=lambda item: (-item["year"], item["problem_code"], item["team_id"] or ""))


def paper_collections(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for record in records:
        if not record["url"].endswith("/lw/529.html"):
            continue
        raw = read_raw(record)
        page_links = anchors(raw, record["url"])
        download = next((item["url"] for item in page_links if "pan.baidu.com" in item["url"]), None)
        output.append({
            "id": "cpmcm-outstanding-papers-2004-2023", "title": "中国研究生数学建模竞赛优秀论文（2004–2023）",
            "record_type": "collection", "problem_id": None, "problem_code": "2004–2023 CPMCM", "competition": "中国研究生数学建模竞赛",
            "category": "优秀论文合集", "year": 2023, "award": "优秀论文合集", "distinctions": [], "institution": None, "team_id": None,
            "models": [], "innovation": "合集索引，单篇论文方法标签待附件解析后补充。",
            "summary": "来源页面提供 2004–2023 年中国研究生数学建模竞赛优秀论文合集下载入口。",
            "source_id": "cmathc_cpmcm", "source_url": record["url"], "full_text_url": download,
            "source_status": "pending_human_confirmation", "access_scope": "linked_content",
        })
    return output


def repository_full_problems() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not FULL_PROBLEMS_PATH.exists():
        raise FileNotFoundError(
            "Missing extracted full problems. Run: python datasets/recipes/ingest_mathmodel_full_problems.py all"
        )
    data = json.loads(FULL_PROBLEMS_PATH.read_text(encoding="utf-8"))
    return data, data["problems"]


def archive_full_problems() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not ARCHIVE_FULL_PROBLEMS_PATH.exists():
        raise FileNotFoundError(
            "Missing full COMAP/APMCM/CUMCM statements. Run the bundled Python with "
            "datasets/recipes/ingest_full_problem_archives.py all"
        )
    data = json.loads(ARCHIVE_FULL_PROBLEMS_PATH.read_text(encoding="utf-8"))
    return data, data["problems"]


def build() -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    source_records: dict[str, list[dict[str, Any]]] = {}
    for source_id in ("comap_mcm_icm", "cmathc_cpmcm", "apmcm_problems"):
        manifest, records = latest_records(source_id)
        manifests.append(manifest)
        source_records[source_id] = records
    full_problem_data, full_problems = repository_full_problems()
    archive_problem_data, archive_problems = archive_full_problems()
    problems = archive_problems + full_problems
    problems.sort(key=lambda item: (-item["year"], item["category"], item["code"]))
    problem_titles = {item["id"]: item["title"] for item in problems}
    papers = comap_papers(source_records["comap_mcm_icm"], problem_titles)
    papers += paper_collections(source_records["cmathc_cpmcm"])
    papers += full_problem_data["papers"]
    latest_time = max(
        [item.get("finished_at", "") for item in manifests if item]
        + [full_problem_data.get("generated_at", ""), archive_problem_data.get("generated_at", "")],
        default=datetime.now(timezone.utc).isoformat(),
    )
    fingerprint_input = json.dumps({"problems": problems, "papers": papers}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    version = hashlib.sha256(fingerprint_input).hexdigest()[:12]
    return {
        "schema_version": "1.0.0", "dataset_version": f"wave-a-{version}", "generated_at": latest_time,
        "stats": {"problem_count": len(problems), "paper_count": len(papers), "source_count": 5},
        "problems": problems, "papers": papers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **result["stats"], "dataset_version": result["dataset_version"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
