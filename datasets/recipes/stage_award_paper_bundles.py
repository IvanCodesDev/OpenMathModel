#!/usr/bin/env python3
"""Stage the award-paper bundles the organisers publish on their contest platform.

华数杯、APMCM 与 MathorCup 都不在自己的官网挂单篇论文，而是由主办方在报名平台
（赛氪）的「历年优秀论文」公告里发布一个压缩包。这份 recipe 只做三件事：

1. 按公告链接下载压缩包，并用固定的字节数与 SHA-256 校验（内容一变即失败）；
2. 解压出 PDF，按 <赛事>/<年份>/<题组>/<编号>.pdf 归一化落到原始层；
3. 读每篇论文的封面，产出题目、参赛编号、摘要与关键词的结构化索引。

压缩包与解压结果都留在 datasets/raw（不入库、不再分发），产品侧只发布索引和
指向主办方公告的来源链接。RAR 由系统自带的 bsdtar 解开：Windows 10 以后的
tar.exe 与各发行版的 libarchive 都能读 RAR，仓库因此不引入额外二进制依赖。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from paper_covers import derive_models, keyword_note, page_lines, parse_cover, summarize


ROOT = Path(__file__).resolve().parents[2]
#: 各赛事解压后的论文与其他 recipe 的产物并列放在 paper-archives/<赛事>/<年份>/<题组>/，
#: 开发服务器的 /paper-files/archive/… 路由按同一层级直接取文件。
ARCHIVE_ROOT = ROOT / "datasets/raw/sources/paper-archives"
OUTPUT = ROOT / "datasets/interim/award_paper_bundles/papers.json"
USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"

#: 每个合集：主办方公告页、公告里的附件直链、固定校验和，以及把目录名换算成
#: 届次/年份/题组的规则。届次到年份用「第一届的年份」推算，避免逐届硬编码。
BUNDLES: tuple[dict[str, Any], ...] = (
    {
        "key": "huashu-cup",
        "source_id": "huashu_cup_official",
        "competition": "华数杯全国大学生数学建模竞赛",
        "problem_prefix": "huashu-cup",
        "code_label": "华数杯",
        "archive": "huashu-cup-papers.zip",
        "notice_url": "https://www.saikr.com/c/nd/44347",
        "download_url": "https://publicqn.saikr.com/2026/06/22/contest/4fdef20d540b9bb0b3e4df15ebae95d41782122066118.zip",
        "bytes": 39964756,
        "sha256": "7fd094e46ad169262a6c93fbf60dc871664ce942c4e284119b6339ea0e6f2585",
        "first_edition_year": 2020,
    },
    {
        "key": "apmcm",
        "source_id": "apmcm_problems",
        "competition": "APMCM 亚太地区大学生数学建模竞赛",
        "problem_prefix": "apmcm",
        "code_label": "APMCM",
        "archive": "apmcm-papers.zip",
        "notice_url": "https://www.saikr.com/c/nd/33722",
        "download_url": "https://publicqn.saikr.com/2025/09/17/contest/af023705c9871be91f23cd3d694f02f41758072718312.zip",
        "bytes": 53701632,
        "sha256": "1b45488b7f9c82c0b39478668d40d76db864b73db9199b44fd14ebc8cadcf6a9",
    },
    {
        "key": "apmcm-cn",
        "source_id": "apmcm_problems",
        "competition": "APMCM 亚太地区大学生数学建模竞赛（中文赛项）",
        "problem_prefix": "apmcm",
        "problem_infix": "cn",
        "code_label": "APMCM 中文",
        "archive": "apmcm-cn-papers.zip",
        "notice_url": "https://www.saikr.com/c/nd/31675",
        "download_url": "https://publicqn.saikr.com/2025/06/06/contest/1e468486597a382974e7f1e7503a6c341749202787507.zip",
        "bytes": 10759092,
        "sha256": "01f8b2f764ce584badf480bb6deb695380b3a4f24ebb7a38b5ebff26f10bbe7c",
    },
    {
        "key": "mathorcup",
        "source_id": "mathorcup_official",
        "competition": "MathorCup 数学应用挑战赛",
        "legacy_competition": "MathorCup 高校数学建模挑战赛",
        "legacy_until": 2023,
        "problem_prefix": "mathorcup",
        "code_label": "MathorCup",
        "archive": "mathorcup-papers.rar",
        "notice_url": "https://www.saikr.com/c/nd/39400",
        "download_url": "https://publicqn.saikr.com/2026/02/09/contest/a2314c164af9390e586c10b7b2e074051770637499572.rar",
        "bytes": 124521368,
        "sha256": "37a1b57cb9e8319143d51345cb8ea12f5115c769ca23744d21907edadd6fb776",
        "first_edition_year": 2011,
    },
)

EDITION_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
EDITION_RE = re.compile(r"第([〇零一二三四五六七八九十百]+)届")
YEAR_RE = re.compile(r"(20\d{2})\s*年?")
LETTER_RE = re.compile(r"(?:^|[^A-Za-z])([A-F])\s*题")
LEADING_LETTER_RE = re.compile(r"^([A-F])(\d{4,})")
SERIAL_RE = re.compile(r"(\d{4,})")
TRAILING_INDEX_RE = re.compile(r"[-_ ]?(\d)\s*$")


def chinese_number(text: str) -> int:
    """Read 第X届 ordinals up to 第九十九届."""
    text = text.replace("〇", "零")
    if "十" not in text:
        return sum(EDITION_DIGITS.get(char, 0) for char in text)
    tens, _, ones = text.partition("十")
    return (EDITION_DIGITS.get(tens, 1) if tens else 1) * 10 + (EDITION_DIGITS.get(ones, 0) if ones else 0)


def member_year(bundle: dict[str, Any], member: str) -> int | None:
    year_match = YEAR_RE.search(member)
    if year_match:
        return int(year_match.group(1))
    edition_match = EDITION_RE.search(member)
    if edition_match and bundle.get("first_edition_year"):
        return bundle["first_edition_year"] + chinese_number(edition_match.group(1)) - 1
    return None


def member_letter(member: str) -> str:
    name = member.rsplit("/", 1)[-1]
    match = LETTER_RE.search(member)
    if match:
        return match.group(1).upper()
    match = LEADING_LETTER_RE.match(name)
    return match.group(1).upper() if match else ""


def member_slug(member: str, letter: str, used: set[tuple[Any, ...]], scope: tuple[Any, ...]) -> str:
    name = member.rsplit("/", 1)[-1].rsplit(".", 1)[0].strip()
    serial = SERIAL_RE.search(name.replace(f"{letter}题", "").replace("年", " "))
    if serial and len(serial.group(1)) >= 4:
        candidate = f"{letter or 'X'}{serial.group(1)}"
    else:
        index = TRAILING_INDEX_RE.search(name)
        candidate = f"{letter or 'X'}{index.group(1) if index else '1'}"
    slug = candidate
    counter = 1
    while (*scope, slug) in used:
        counter += 1
        slug = f"{candidate}-{counter}"
    used.add((*scope, slug))
    return slug


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def download(bundle: dict[str, Any], attempts: int = 12) -> Path:
    """Fetch the bundle with resume; the CDN drops long transfers mid-stream."""
    target = ARCHIVE_ROOT / bundle["archive"]
    if target.exists() and target.stat().st_size == bundle["bytes"]:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    for attempt in range(attempts):
        have = part.stat().st_size if part.exists() else 0
        if have >= bundle["bytes"]:
            break
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        request = urllib.request.Request(bundle["download_url"], headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=180) as response, part.open("ab") as handle:
                shutil.copyfileobj(response, handle, 262144)
        except Exception as error:  # network flake; resume from what landed
            print(f"BUNDLE_RETRY {bundle['key']} {type(error).__name__} at {part.stat().st_size if part.exists() else 0}")
            time.sleep(2 * (attempt + 1))
    size = part.stat().st_size if part.exists() else 0
    if size != bundle["bytes"]:
        raise RuntimeError(f"{bundle['key']}: incomplete download {size}/{bundle['bytes']}")
    part.replace(target)
    return target


def verify_archive(bundle: dict[str, Any]) -> Path:
    target = ARCHIVE_ROOT / bundle["archive"]
    if not target.exists():
        raise FileNotFoundError(f"Missing staged bundle: {target}")
    payload = target.read_bytes()
    if len(payload) != bundle["bytes"]:
        raise RuntimeError(f"{bundle['key']}: size mismatch {len(payload)} != {bundle['bytes']}")
    digest = sha256_bytes(payload)
    if digest != bundle["sha256"]:
        raise RuntimeError(f"{bundle['key']}: sha256 mismatch {digest} != {bundle['sha256']}")
    return target


def zip_members(archive_path: Path) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                continue
            name = info.filename
            if not info.flag_bits & 0x800:
                # Windows zip tools store GBK bytes; zipfile decodes them as cp437.
                try:
                    name = name.encode("cp437").decode("gbk")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass
            members.append((name, archive.read(info)))
    return members


def rar_members(archive_path: Path) -> list[tuple[str, bytes]]:
    """Unpack a RAR through bsdtar, then read the files back off disk."""
    staging = ARCHIVE_ROOT / f".{archive_path.stem}-rar"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["tar", "-xf", str(archive_path), "-C", str(staging)],
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(
            "bsdtar could not unpack the RAR bundle "
            f"({result.stderr.decode('utf-8', 'replace')[:200]}). Install bsdtar/libarchive with RAR support."
        )
    members = [
        (path.relative_to(staging).as_posix(), path.read_bytes())
        for path in sorted(staging.rglob("*.pdf"))
    ]
    return members


def extract(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    archive_path = verify_archive(bundle)
    members = zip_members(archive_path) if archive_path.suffix.lower() == ".zip" else rar_members(archive_path)
    destination_root = ARCHIVE_ROOT / bundle["key"]
    if destination_root.exists():
        shutil.rmtree(destination_root)
    staged: list[dict[str, Any]] = []
    used_slugs: set[tuple[Any, ...]] = set()
    for member, payload in sorted(members):
        year = member_year(bundle, member)
        letter = member_letter(member)
        if not year:
            print(f"BUNDLE_SKIP {bundle['key']} {member} (no edition or year in path)")
            continue
        slug = member_slug(member, letter, used_slugs, (year, letter))
        destination = destination_root / str(year) / (letter or "X") / f"{slug}.pdf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        staged.append({
            "member": member,
            "year": year,
            "letter": letter,
            "slug": slug,
            "path": destination.relative_to(ROOT).as_posix(),
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
        })
    print(f"BUNDLE_EXTRACT {bundle['key']} staged={len(staged)}")
    if archive_path.suffix.lower() != ".zip":
        shutil.rmtree(ARCHIVE_ROOT / f".{archive_path.stem}-rar", ignore_errors=True)
    return staged


def competition_name(bundle: dict[str, Any], year: int) -> str:
    if bundle.get("legacy_competition") and year <= bundle.get("legacy_until", 0):
        return bundle["legacy_competition"]
    return bundle["competition"]


def problem_id(bundle: dict[str, Any], year: int, letter: str) -> str | None:
    if not letter:
        return None
    infix = bundle.get("problem_infix")
    parts = [bundle["problem_prefix"], str(year)] + ([infix] if infix else []) + [letter.lower()]
    return "-".join(parts)


def route_path(bundle: dict[str, Any], record: dict[str, Any]) -> str:
    return f"/paper-files/archive/{bundle['key']}/{record['year']}/{record['letter'] or 'X'}/{record['slug']}.pdf"


def build() -> dict[str, Any]:
    papers: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    bundle_stats: list[dict[str, Any]] = []
    for bundle in BUNDLES:
        staged = extract(bundle)
        parsed = 0
        for record in staged:
            path = ROOT / record["path"]
            try:
                lines, page_count = page_lines(path)
                fields = parse_cover(lines)
            except Exception as error:
                skipped.append(f"{record['member']} ({type(error).__name__}: {error})")
                continue
            year = record["year"]
            letter = record["letter"]
            competition = competition_name(bundle, year)
            code = f"{year} {bundle['code_label']}{(' ' + letter) if letter else ''}"
            paper_id = f"{bundle['key']}-paper-{year}-{(letter or 'x').lower()}-{record['slug'].lower()}"
            title = fields["title"]
            if title:
                parsed += 1
            papers[paper_id] = {
                "title": title,
                "record_type": "paper",
                "problem_id": problem_id(bundle, year, letter),
                "problem_code": code,
                "competition": competition,
                "category": "优秀论文",
                "year": year,
                "award": "优秀论文",
                "distinctions": [],
                "institution": fields["institution"] or None,
                "team_id": fields["team_id"] or record["slug"],
                "keywords": fields["keywords"],
                "models": derive_models(title, fields["abstract"], fields["keywords"]),
                "summary": summarize(fields["abstract"]) or f"{code} 主办方发布的优秀论文全文。",
                "innovation": keyword_note(fields["keywords"]) if title
                else "主办方合集内的优秀论文全文，封面未提供可解析的题目。",
                "page_count": page_count,
                "source_id": bundle["source_id"],
                "source_url": bundle["notice_url"],
                "bundle_url": bundle["download_url"],
                "local_pdf_path": route_path(bundle, record),
                "source_file_bytes": record["bytes"],
                "source_sha256": record["sha256"],
                "archive_member": record["member"],
            }
        bundle_stats.append({
            "key": bundle["key"],
            "competition": bundle["competition"],
            "staged": len(staged),
            "with_title": parsed,
        })

    result = {
        "schema_version": "1.0.0",
        "source_id": "saikr_award_paper_bundles",
        "bundles": [
            {
                "key": bundle["key"],
                "source_id": bundle["source_id"],
                "archive": bundle["archive"],
                "notice_url": bundle["notice_url"],
                "download_url": bundle["download_url"],
                "bytes": bundle["bytes"],
                "sha256": bundle["sha256"],
            }
            for bundle in BUNDLES
        ],
        "stats": {
            "paper_count": len(papers),
            "with_title": sum(1 for item in papers.values() if item["title"]),
            "with_keywords": sum(1 for item in papers.values() if item["keywords"]),
            "with_models": sum(1 for item in papers.values() if item["models"]),
            "skipped_count": len(skipped),
            "bundles": bundle_stats,
        },
        "skipped": skipped,
        "papers": papers,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def verify() -> dict[str, Any]:
    for bundle in BUNDLES:
        verify_archive(bundle)
    data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    for paper_id, record in data["papers"].items():
        path = ROOT / record["local_pdf_path"].replace("/paper-files/archive/", "datasets/raw/sources/paper-archives/")
        if not path.exists() or path.stat().st_size != record["source_file_bytes"]:
            raise RuntimeError(f"Staged PDF missing or resized: {paper_id}")
        if sha256_bytes(path.read_bytes()) != record["source_sha256"]:
            raise RuntimeError(f"Staged PDF hash mismatch: {paper_id}")
    stats = data["stats"]
    ratio = stats["with_title"] / max(1, stats["paper_count"])
    if ratio < 0.8:
        raise RuntimeError(f"Cover recognition too low: {ratio:.0%}")
    print("AWARD_PAPER_BUNDLES_VERIFY_OK " + json.dumps(stats, ensure_ascii=False))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "build", "verify", "all"))
    args = parser.parse_args()
    if args.command in {"fetch", "all"}:
        for bundle in BUNDLES:
            path = download(bundle)
            verify_archive(bundle)
            print(f"BUNDLE_READY {bundle['key']} {path.stat().st_size} bytes")
            sys.stdout.flush()
    if args.command in {"build", "all"}:
        result = build()
        print(json.dumps(result["stats"], ensure_ascii=False))
    if args.command in {"verify", "all"}:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
