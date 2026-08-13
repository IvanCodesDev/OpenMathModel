#!/usr/bin/env python3
"""Fetch and pin the Teddy Cup statements the organiser hosts on its own domain.

Unlike the other contests in this corpus the Teddy Cup publishes one PDF per
problem rather than a per-edition archive, so there is nothing to expand: each
file is downloaded straight into the staging tree under a name the ingester can
read, and every byte count and SHA-256 is pinned here.

Only four editions can be staged. The 2017 and 2018 detail pages now redirect to
the site root and serve no statement, and from 2021 the contest moved to the
BdRace platform, which renders its statements through a single-page application
and keeps the PDFs behind registration. Those gaps are deliberate: the registry
records them rather than filling them from a mirror.

Every data set the statements refer to lives on pan.baidu.com, off the
organiser's own domain, so none of it is mirrored; the ingester links to the
edition page instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = ROOT / "datasets/raw/sources/full-problem-archives"
SOURCE_ROOT = ARCHIVE_ROOT / "tipdm-cup"
EXTRACTED_ROOT = ARCHIVE_ROOT / "extracted-v2"
MANIFEST = SOURCE_ROOT / "manifest.json"
USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"
MAX_ATTEMPTS = 4

# The 2015 edition ships all three problems as one paper, so it is staged as a
# single record the way the APMCM Wuyue Cup already is. ``letter`` is None there.
STATEMENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "tipdm-cup-2015", "year": 2015, "edition": 3, "letter": None,
        "title": "第三届“泰迪杯”全国大学生数据挖掘竞赛赛题",
        "notice_url": "https://www.tipdm.org/tzjingsai/613.jhtml",
        "url": "http://www.tipdm.org/u/cms/www/201504/08154419na3j.pdf",
        "bytes": 448_388,
        "sha256": "c10d452e5be6a342db8aae52f8482e81cc748a01061957285f2468b8de6bbf55",
    },
    {
        "id": "tipdm-cup-2016-a", "year": 2016, "edition": 4, "letter": "A",
        "title": "电商平台图片中文字的识别",
        "notice_url": "https://www.tipdm.org/tzjingsai/712.jhtml",
        "url": "http://www.tipdm.org/u/cms/www/201604/01110636qcnn.pdf",
        "bytes": 1_152_495,
        "sha256": "dab28d78900b6deef3a1b1742cb384024afaa66fd73c4cc329b1026608dfee6c",
    },
    {
        "id": "tipdm-cup-2016-b", "year": 2016, "edition": 4, "letter": "B",
        "title": "铁路旅客流量预测",
        "notice_url": "https://www.tipdm.org/tzjingsai/712.jhtml",
        "url": "http://www.tipdm.org/u/cms/www/201603/31192336trrw.pdf",
        "bytes": 313_813,
        "sha256": "a5c6112f9b978c8309eadb0d2e466f807bc238e50a43378add7199645c57795b",
    },
    {
        "id": "tipdm-cup-2016-c", "year": 2016, "edition": 4, "letter": "C",
        "title": "网络招聘信息的分析与挖掘",
        "notice_url": "https://www.tipdm.org/tzjingsai/712.jhtml",
        "url": "http://www.tipdm.org/u/cms/www/201604/28192551rzz9.pdf",
        "bytes": 156_827,
        "sha256": "e48a8a32a2d5c97bb7bf113b7a2e79f5b2389a7e8d9896b93726b707b962c01c",
    },
    {
        "id": "tipdm-cup-2019-a", "year": 2019, "edition": 7, "letter": "A",
        "title": "通过机器学习优化股票多因子模型",
        "notice_url": "https://www.tipdm.org/tzjingsai/1544.jhtml",
        "url": "https://www.tipdm.org/u/cms/www/201903/15214636gdsi.pdf",
        "bytes": 620_737,
        "sha256": "37289e8d81c9a60ae940bd91b4f6b8c78dd03ff90dce857474db686dbdf63324",
    },
    {
        "id": "tipdm-cup-2019-b", "year": 2019, "edition": 7, "letter": "B",
        "title": "直肠癌淋巴结转移的智能诊断",
        "notice_url": "https://www.tipdm.org/tzjingsai/1544.jhtml",
        "url": "https://www.tipdm.org/u/cms/www/201903/15214944i2k3.pdf",
        "bytes": 846_120,
        "sha256": "ecd207b562221b4e6925e2ead4385da23278346b83c37495c558b7d6c0ba8125",
    },
    {
        "id": "tipdm-cup-2019-c", "year": 2019, "edition": 7, "letter": "C",
        "title": "运输车辆安全驾驶行为的分析",
        "notice_url": "https://www.tipdm.org/tzjingsai/1544.jhtml",
        "url": "https://www.tipdm.org/u/cms/www/201904/01094717d2t3.pdf",
        "bytes": 621_095,
        "sha256": "e05bd7c598cca7cb978dcf1b19e299942d7d7f29b761353cb50bcde3f592e764",
    },
    {
        "id": "tipdm-cup-2020-a", "year": 2020, "edition": 8, "letter": "A",
        "title": "基于数据挖掘的上市公司高送转预测",
        "notice_url": "https://www.tipdm.org/tzbstysj/1637.jhtml",
        "url": "https://www.tipdm.org/u/cms/www/202004/01110941c82j.pdf",
        "bytes": 635_948,
        "sha256": "4f0d9e9e4c31e2d6e1381decbd09e6ad7563e8a72e8caf0d1a8e58522ee3b133",
    },
    {
        "id": "tipdm-cup-2020-b", "year": 2020, "edition": 8, "letter": "B",
        "title": "电力巡检智能缺陷检测",
        "notice_url": "https://www.tipdm.org/tzbstysj/1637.jhtml",
        "url": "https://www.tipdm.org/u/cms/www/202004/01111031vogd.pdf",
        "bytes": 763_618,
        "sha256": "87979967f7ba0d0e542db9314442c7b4bc0cbde0ee91ff4c112c538094041849",
    },
    {
        "id": "tipdm-cup-2020-c", "year": 2020, "edition": 8, "letter": "C",
        "title": "“智慧政务”中的文本挖掘应用",
        "notice_url": "https://www.tipdm.org/tzbstysj/1637.jhtml",
        "url": "https://www.tipdm.org/u/cms/www/202005/161045258k85.pdf",
        "bytes": 573_274,
        "sha256": "7fd01a0503c3f6403219636c2e558c4f126c65d2ac3d4a64e4da8c82fbe628a7",
    },
    {
        # The 2020 edition carried a fourth, separately announced problem.
        "id": "tipdm-cup-2020-t", "year": 2020, "edition": 8, "letter": "T",
        "title": "疫情通报文本中涉疫地点的自动提取（南都大数据研究院特别赛题）",
        "notice_url": "https://www.tipdm.org/tzbstysj/1640.jhtml",
        "url": "https://www.tipdm.org/u/cms/www/202005/051412370fi8.pdf",
        "bytes": 413_858,
        "sha256": "54f564e5739ae14702b614c0f17dad9bb8e9fa6b72cf66bc61efbb93a06b8fcc",
    },
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def staged_path(record: dict[str, Any]) -> Path:
    return EXTRACTED_ROOT / f"tipdm-cup-{record['year']}" / f"{record['id']}.pdf"


def validate(record: dict[str, Any], path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size != record["bytes"]:
        raise RuntimeError(
            f"Size mismatch for {record['id']}: expected {record['bytes']}, "
            f"found {path.stat().st_size}"
        )
    actual = sha256(path)
    if actual != record["sha256"]:
        raise RuntimeError(
            f"SHA-256 mismatch for {record['id']}: expected {record['sha256']}, found {actual}"
        )
    with path.open("rb") as handle:
        if handle.read(4) != b"%PDF":
            raise RuntimeError(f"{record['id']} is not a PDF")


def fetch(record: dict[str, Any]) -> Path:
    """Download one statement, retrying until the bytes match what is pinned.

    The organiser's server truncates a response now and then -- one attempt at
    the 2016 A statement returned 245 KB of a 1.1 MB file -- and a short read
    looks like success to ``copyfileobj``. Validating before the file is moved
    into place is what turns that into a retry instead of a corrupt statement.
    """
    target = staged_path(record)
    if target.is_file():
        validate(record, target)
        print(f"REUSE {record['id']} bytes={record['bytes']}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".pdf.part")
    request = urllib.request.Request(record["url"], headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
                validate(record, temporary)
                temporary.replace(target)
                print(f"FETCH {record['id']} bytes={record['bytes']} attempt={attempt}")
                return target
            except (RuntimeError, OSError) as error:
                last_error = error
                print(f"RETRY {record['id']} attempt={attempt}: {error}")
                time.sleep(min(2 ** attempt, 30))
    finally:
        temporary.unlink(missing_ok=True)
    raise RuntimeError(f"{record['id']} could not be downloaded intact: {last_error}")


def write_manifest() -> dict[str, Any]:
    records = []
    for record in STATEMENTS:
        path = staged_path(record)
        validate(record, path)
        records.append({**record, "path": path.relative_to(ROOT).as_posix()})
    manifest = {
        "schema_version": "1.0.0",
        "source_id": "tipdm_cup_official",
        "records": records,
        "stats": {
            "statement_count": len(records),
            "edition_count": len({item["year"] for item in records}),
            "statement_bytes": sum(item["bytes"] for item in records),
        },
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("source_id") != "tipdm_cup_official":
        raise RuntimeError("Unexpected Teddy Cup manifest source_id")
    staged = {item["id"]: item for item in manifest["records"]}
    expected = {item["id"] for item in STATEMENTS}
    if set(staged) != expected:
        raise RuntimeError(f"Teddy Cup ids differ: {sorted(set(staged) ^ expected)}")
    for record in STATEMENTS:
        validate(record, ROOT / staged[record["id"]]["path"])
    print("TIPDM_CUP_STAGE_VERIFY_OK " + json.dumps(manifest["stats"], ensure_ascii=False))
    return manifest["stats"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "verify", "all"))
    args = parser.parse_args()
    if args.command in {"fetch", "all"}:
        for record in STATEMENTS:
            fetch(record)
        print(json.dumps(write_manifest()["stats"], ensure_ascii=False))
    if args.command in {"verify", "all"}:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
