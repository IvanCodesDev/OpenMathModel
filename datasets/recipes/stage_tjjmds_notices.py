#!/usr/bin/env python3
"""Snapshot the statistical modelling contest's annual notices.

This contest publishes no problem statements at all: every edition announces a
theme (or, before 2021, a set of topic categories with judging criteria) and
teams write a paper on a title of their own choosing. The notice on the
organiser's site is therefore the entire published form of "the problem", and
this recipe stores it the way the other sources store their statements -- one
pinned snapshot per edition plus the extracted article body.

Titles are pinned here rather than parsed on every run, the same way the other
ingesters pin theirs; ``discover`` refuses to write a manifest whose fetched
notice no longer contains its pinned theme, so a silently re-worded page fails
loudly instead of publishing a stale title.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "datasets/raw/sources/full-problem-archives/tjjmds"
MANIFEST = SOURCE_ROOT / "manifest.json"
BASE = "http://tjjmds.ai-learning.net"
USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"

# ``check`` is a phrase the notice must contain, tying the pinned title to the
# page it was read from. Editions before the fifth predate the current official
# site and are not listed on it.
EDITIONS: tuple[dict[str, Any], ...] = (
    {"year": 2017, "edition": 5, "article": "36596",
     "title": "统计建模、市场调查分析、大数据工程三类自选题",
     "check": "“统计建模类”、“市场调查分析类”、“大数据工程类”"},
    {"year": 2019, "edition": 6, "article": "36597",
     "title": "统计建模、大数据应用、市场调查分析、生物医学四类自选题",
     "check": "“统计建模类”、“大数据应用类”、“市场调查分析类”和“生物医学类”"},
    {"year": 2021, "edition": 7, "article": "36600",
     "title": "数据新动能的统计测度研究",
     "check": "数据新动能的统计测度研究"},
    {"year": 2022, "edition": 8, "article": "36602",
     "title": "构建新发展格局的统计测度",
     "check": "构建新发展格局的统计测度"},
    {"year": 2023, "edition": 9, "article": "36882",
     "title": "中国式现代化的统计测度",
     "check": "中国式现代化的统计测度"},
    {"year": 2024, "edition": 10, "article": "36972",
     "title": "大数据与人工智能时代的统计研究",
     "check": "大数据与人工智能时代的统计研究"},
    {"year": 2025, "edition": 11, "article": "37047",
     "title": "统计创新应用 数据引领未来",
     "check": "统计创新应用"},
    {"year": 2026, "edition": 12, "article": "37119",
     "title": "服务国家战略 创新统计赋能",
     "check": "服务国家战略"},
)

# Theme-interpretation material. Only the 2022 edition publishes a text
# version -- several professors spelling out suggested research directions
# under that year's theme; every other edition ships video behind QR codes,
# which leaves nothing to collect.
INTERPRETATIONS: tuple[dict[str, Any], ...] = (
    {"year": 2022, "article": "36782",
     "check": "主题解读一：易东"},
)

DIV_TOKEN_RE = re.compile(r"<div\b|</div\s*>", re.I)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def article_body(page: str) -> str:
    """Return the inner HTML of the CMS article container.

    The container is ``<div class="context">`` on every notice; the close tag is
    found by walking div tokens at equal depth, because the body itself nests
    divs freely.
    """
    opening = re.search(r'<div\s+class="context"[^>]*>', page)
    if not opening:
        raise RuntimeError("notice page carries no context container")
    depth = 1
    for token in DIV_TOKEN_RE.finditer(page, opening.end()):
        depth += 1 if token.group(0).lower().startswith("<div") else -1
        if depth == 0:
            return page[opening.end():token.start()]
    raise RuntimeError("context container never closes")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def discover() -> dict[str, Any]:
    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for index, edition in enumerate(EDITIONS):
        notice_url = f"{BASE}/dstz/{edition['article']}.jhtml"
        payload = fetch(notice_url)
        page = payload.decode("utf-8", "replace")
        flat = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", page))
        if re.sub(r"\s+", "", edition["check"]) not in flat:
            raise RuntimeError(
                f"tjjmds {edition['year']}: pinned theme not found on {notice_url}"
            )
        snapshot = SOURCE_ROOT / f"tjjmds-{edition['year']}.html"
        snapshot.write_bytes(payload)
        body = article_body(page).encode("utf-8")
        body_path = SOURCE_ROOT / f"tjjmds-{edition['year']}-body.html"
        body_path.write_bytes(body)
        records.append({
            "id": f"tjjmds-{edition['year']}",
            "year": edition["year"],
            "edition": edition["edition"],
            "title": edition["title"],
            "notice_url": notice_url,
            "page_path": snapshot.relative_to(ROOT).as_posix(),
            "page_bytes": len(payload),
            "page_sha256": sha256_bytes(payload),
            "body_path": body_path.relative_to(ROOT).as_posix(),
            "body_bytes": len(body),
            "body_sha256": sha256_bytes(body),
        })
        if index + 1 < len(EDITIONS):
            time.sleep(6.1)  # stay well under ten requests per minute
    interpretations: list[dict[str, Any]] = []
    for extra in INTERPRETATIONS:
        time.sleep(6.1)
        page_url = f"{BASE}/dsdt/{extra['article']}.jhtml"
        payload = fetch(page_url)
        page = payload.decode("utf-8", "replace")
        flat = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", page))
        if re.sub(r"\s+", "", extra["check"]) not in flat:
            raise RuntimeError(
                f"tjjmds {extra['year']} interpretation: marker not found on {page_url}"
            )
        snapshot = SOURCE_ROOT / f"tjjmds-{extra['year']}-jiedu.html"
        snapshot.write_bytes(payload)
        body = article_body(page).encode("utf-8")
        body_path = SOURCE_ROOT / f"tjjmds-{extra['year']}-jiedu-body.html"
        body_path.write_bytes(body)
        interpretations.append({
            "year": extra["year"],
            "page_url": page_url,
            "page_path": snapshot.relative_to(ROOT).as_posix(),
            "page_bytes": len(payload),
            "page_sha256": sha256_bytes(payload),
            "body_path": body_path.relative_to(ROOT).as_posix(),
            "body_bytes": len(body),
            "body_sha256": sha256_bytes(body),
        })
    manifest = {
        "schema_version": "1.0.0",
        "source_id": "tjjmds_official",
        "records": records,
        "interpretations": interpretations,
        "stats": {
            "edition_count": len(records),
            "interpretation_count": len(interpretations),
            "notice_bytes": sum(item["page_bytes"] for item in records),
        },
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("TJJMDS_DISCOVERY_OK " + json.dumps(manifest["stats"], ensure_ascii=False))
    return manifest


def verify() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("source_id") != "tjjmds_official":
        raise RuntimeError("Unexpected tjjmds manifest source_id")
    records = {item["id"]: item for item in manifest["records"]}
    expected = {f"tjjmds-{edition['year']}" for edition in EDITIONS}
    if set(records) != expected:
        raise RuntimeError(f"tjjmds editions differ: {sorted(set(records) ^ expected)}")
    if {item["year"] for item in manifest.get("interpretations", [])} != {
            extra["year"] for extra in INTERPRETATIONS}:
        raise RuntimeError("tjjmds interpretation years differ")
    for item in manifest["records"] + manifest.get("interpretations", []):
        for prefix in ("page", "body"):
            path = ROOT / item[f"{prefix}_path"]
            data = path.read_bytes()
            if len(data) != item[f"{prefix}_bytes"] or sha256_bytes(data) != item[f"{prefix}_sha256"]:
                raise RuntimeError(f"tjjmds snapshot mismatch: {path}")
    print("TJJMDS_VERIFY_OK " + json.dumps(manifest["stats"], ensure_ascii=False))
    return manifest["stats"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("discover", "verify", "all"))
    args = parser.parse_args()
    if args.command in {"discover", "all"}:
        discover()
    if args.command in {"verify", "all"}:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
