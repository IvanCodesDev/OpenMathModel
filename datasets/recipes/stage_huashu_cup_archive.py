#!/usr/bin/env python3
"""Fetch and safely expand the organiser-published Huashu Cup archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = ROOT / "datasets/raw/sources/full-problem-archives"
SOURCE_ROOT = ARCHIVE_ROOT / "huashu-cup"
EXTRACTED_ROOT = ARCHIVE_ROOT / "extracted-v2"
USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"

# One entry per organiser-published bundle. The historical archive collects
# 2020-2025; each live edition ships separately once its contest closes, so the
# 2026 statements arrive as their own download rather than as an archive update.
# ``manifest`` is pinned per edition because the historical one predates the
# multi-edition layout and renaming it would strand the file already on disk.
EDITIONS: dict[str, dict[str, Any]] = {
    "historical": {
        "slug": "huashu-cup-historical",
        "manifest": "manifest.json",
        "url": "https://publicqn.saikr.com/2026/06/17/contest/db2972cc3178e62f2e7d59f675302d2f1781688084526.zip",
        "notice_url": "https://m.saikr.com/contest/notice_detail/44136",
        "sha256": "ef223b3fce5a5881975f9c9d178ac98dbff06e1477aef34a5a989fcaa61ab5fd",
        "bytes": 159_437_856,
        "member_count": 433,
    },
    "2026": {
        "slug": "huashu-cup-2026",
        "manifest": "manifest-2026.json",
        "url": "https://publicqn.saikr.com/contest/1786086963076/IkaHVScuDaJ97OPHF5R5RkiaFYXCVNo2.zip",
        "notice_url": "https://m.saikr.com/contest/notice_detail/46038",
        "sha256": "f3be40932aeafcaec92af6447a770cd4fb6f9d21321afe9f66c8453f29a48cbd",
        "bytes": 12_161_341,
        "member_count": 25,
    },
}


def archive_path(edition: dict[str, Any]) -> Path:
    return SOURCE_ROOT / f"{edition['slug']}.zip"


def extracted_path(edition: dict[str, Any]) -> Path:
    return EXTRACTED_ROOT / edition["slug"]


def manifest_path(edition: dict[str, Any]) -> Path:
    return SOURCE_ROOT / edition["manifest"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(edition: dict[str, Any]) -> None:
    archive = archive_path(edition)
    if (not archive.is_file() or archive.stat().st_size != edition["bytes"]
            or sha256(archive) != edition["sha256"]):
        raise RuntimeError(f"{edition['slug']} does not match pinned size and SHA-256")


def fetch(edition: dict[str, Any]) -> None:
    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    archive = archive_path(edition)
    if archive.is_file():
        validate(edition)
        print(f"REUSE {edition['slug']} bytes={edition['bytes']}")
        return
    temporary = archive.with_suffix(".zip.part")
    request = urllib.request.Request(edition["url"], headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=300) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(archive)
        validate(edition)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"FETCH {edition['slug']} bytes={edition['bytes']}")


def decoded_name(info: ZipInfo) -> str:
    name = info.filename.replace("\\", "/")
    if info.flag_bits & 0x800:
        return name
    raw = name.encode("cp437")
    for encoding in ("gb18030", "utf-8"):
        try:
            candidate = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if candidate.isascii() or any("\u3400" <= char <= "\u9fff" for char in candidate):
            return candidate
    raise RuntimeError(f"Cannot decode ZIP member {name!r}")


def destination(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"Unsafe ZIP path {name!r}")
    target = root.joinpath(*parts)
    resolved_root = root.resolve()
    resolved = target.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise RuntimeError(f"ZIP path escapes destination: {name!r}")
    return target


def expand(edition: dict[str, Any]) -> dict[str, Any]:
    validate(edition)
    root = extracted_path(edition)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    members = []
    with ZipFile(archive_path(edition)) as bundle:
        for info in bundle.infolist():
            name = decoded_name(info)
            target = destination(root, name)
            if info.is_dir() or name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            members.append({
                "path": target.relative_to(ROOT).as_posix(),
                "bytes": target.stat().st_size,
                "sha256": sha256(target),
            })
    if len(members) != edition["member_count"]:
        raise RuntimeError(
            f"{edition['slug']} expected {edition['member_count']} files, extracted {len(members)}"
        )
    manifest = {
        "schema_version": "1.0.0",
        "source_id": "huashu_cup_official",
        "notice_url": edition["notice_url"],
        "url": edition["url"],
        "path": archive_path(edition).relative_to(ROOT).as_posix(),
        "bytes": edition["bytes"],
        "sha256": edition["sha256"],
        "extracted": {"directory": root.relative_to(ROOT).as_posix(), "members": members},
        "stats": {
            "archive_count": 1,
            "archive_bytes": edition["bytes"],
            "extracted_file_count": len(members),
        },
    }
    manifest_path(edition).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("HUASHU_CUP_STAGE_OK " + json.dumps({"edition": edition["slug"], **manifest["stats"]},
                                              ensure_ascii=False))
    return manifest


def verify(edition: dict[str, Any]) -> dict[str, Any]:
    validate(edition)
    manifest = json.loads(manifest_path(edition).read_text(encoding="utf-8"))
    if (manifest.get("source_id") != "huashu_cup_official"
            or len(manifest["extracted"]["members"]) != edition["member_count"]):
        raise RuntimeError(f"Unexpected {edition['slug']} manifest")
    for item in manifest["extracted"]["members"]:
        path = ROOT / item["path"]
        if not path.is_file() or path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
            raise RuntimeError(f"{edition['slug']} extracted member mismatch: {path}")
    print("HUASHU_CUP_STAGE_VERIFY_OK " + json.dumps({"edition": edition["slug"], **manifest["stats"]},
                                                     ensure_ascii=False))
    return manifest["stats"]


def selected(name: str | None) -> list[dict[str, Any]]:
    if name is None:
        return list(EDITIONS.values())
    return [EDITIONS[name]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "expand", "verify", "all"))
    parser.add_argument("--edition", choices=tuple(EDITIONS), default=None,
                        help="restrict the run to one published bundle (default: all)")
    args = parser.parse_args()
    editions = selected(args.edition)
    for edition in editions:
        if args.command in {"fetch", "all"}:
            fetch(edition)
        if args.command in {"expand", "all"}:
            expand(edition)
        if args.command in {"verify", "all"}:
            verify(edition)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
