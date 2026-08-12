#!/usr/bin/env python3
"""Discover official Electric Cup problem metadata without bypassing downloads.

The organiser publishes one detail page per edition and two attachment links
per edition.  The attachment endpoint currently presents an interactive
verification page, so this recipe records the official titles, notice URLs and
opaque file identifiers while deliberately rejecting HTML responses as problem
archives.  This keeps discovery repeatable and prevents a verification page
from being mistaken for a RAR/ZIP file.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "datasets/raw/sources/full-problem-archives/electric-cup"
MANIFEST = SOURCE_ROOT / "manifest.json"
BASE = "https://shumo.neepu.edu.cn"
USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"

# There was no edition in 2020.  These stable university-CMS article ids cover
# every published edition from 2017 through 2026.
EDITIONS = {
    2017: 1579,
    2018: 1589,
    2019: 1599,
    2021: 1609,
    2022: 1619,
    2023: 1629,
    2024: 1639,
    2025: 1649,
    2026: 2171,
}

ATTACHMENT_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']*download\.jsp[^"\']*)["\'][^>]*>(.*?)</a>',
    re.I | re.S,
)


def clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def fetch(url: str, destination: Path) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response:
        body = response.read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    return body


def parse_edition(year: int, article_id: int, body: bytes) -> list[dict[str, Any]]:
    text = body.decode("utf-8", "replace")
    notice_url = f"{BASE}/info/1121/{article_id}.htm"
    records = []
    for href, label_html in ATTACHMENT_RE.findall(text):
        label = clean(label_html)
        match = re.match(r"([AB])题[：:]\s*(.+?)\.(rar|zip)$", label, re.I)
        if not match:
            continue
        letter, title, extension = match.groups()
        url = urljoin(notice_url, html.unescape(href))
        file_id = re.search(r"[?&]wbfileid=([A-F0-9]+)", url, re.I)
        if not file_id:
            raise RuntimeError(f"Missing wbfileid in {url}")
        records.append({
            "id": f"electric-cup-{year}-{letter.lower()}",
            "year": year,
            "letter": letter.upper(),
            "title": title.strip(),
            "notice_url": notice_url,
            "download_url": url,
            "file_id": file_id.group(1).upper(),
            "archive_format": extension.lower(),
            "access_status": "interactive_verification_required",
        })
    if [item["letter"] for item in records] != ["A", "B"]:
        raise RuntimeError(f"Electric Cup {year}: expected A/B attachments, found {records}")
    return records


def discover(cache_dir: Path | None = None) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    notices = []
    for index, (year, article_id) in enumerate(EDITIONS.items()):
        notice_url = f"{BASE}/info/1121/{article_id}.htm"
        cached = cache_dir / f"detail-{article_id}.htm" if cache_dir else None
        if cached and cached.is_file():
            body = cached.read_bytes()
        else:
            target = SOURCE_ROOT / "notices" / f"{year}.html"
            body = fetch(notice_url, target)
            if index + 1 < len(EDITIONS):
                time.sleep(6.1)  # source policy: at most ten requests/minute
        digest = hashlib.sha256(body).hexdigest()
        notices.append({"year": year, "url": notice_url, "sha256": digest, "bytes": len(body)})
        records.extend(parse_edition(year, article_id, body))

    manifest = {
        "schema_version": "1.0.0",
        "source_id": "electric_cup_official",
        "generated_from": f"{BASE}/ljjs/ljst.htm",
        "notices": notices,
        "problems": records,
        "stats": {
            "edition_count": len(EDITIONS),
            "problem_count": len(records),
            "downloadable_archive_count": 0,
            "interactive_verification_count": len(records),
        },
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("ELECTRIC_CUP_DISCOVERY_OK " + json.dumps(manifest["stats"], ensure_ascii=False))
    return manifest


def verify() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected_ids = {
        f"electric-cup-{year}-{letter.lower()}"
        for year in EDITIONS
        for letter in "AB"
    }
    actual_ids = {item["id"] for item in manifest["problems"]}
    if actual_ids != expected_ids:
        raise RuntimeError(f"Electric Cup ids differ: {sorted(actual_ids ^ expected_ids)}")
    if any(item["access_status"] != "interactive_verification_required" for item in manifest["problems"]):
        raise RuntimeError("Unexpected Electric Cup attachment access status")
    print("ELECTRIC_CUP_DISCOVERY_VERIFY_OK " + json.dumps(manifest["stats"], ensure_ascii=False))
    return manifest["stats"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("discover", "verify"))
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    if args.command == "discover":
        discover(args.cache_dir)
    else:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
