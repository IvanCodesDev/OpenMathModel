#!/usr/bin/env python3
"""Fetch, verify and safely expand official MathorCup statement archives.

The organiser's ZIP files use two different legacy filename encodings: the
2018 archive stores UTF-8 bytes without the ZIP UTF-8 flag, while later
archives use GBK bytes without the flag.  ``zipfile.extractall`` therefore
creates mojibake paths on Windows.  This recipe decodes each member explicitly,
rejects path traversal, pins every downloaded SHA-256, and stages the corrected
2026 D problem separately so the ingester can prefer it over the original.
"""

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
SOURCE_ROOT = ARCHIVE_ROOT / "mathorcup"
EXTRACTED_ROOT = ARCHIVE_ROOT / "extracted-v2"
MANIFEST = SOURCE_ROOT / "manifest.json"
USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"


ARCHIVES: tuple[dict[str, Any], ...] = (
    {
        "id": "mathorcup-2018",
        "year": 2018,
        "notice_url": "https://mathorcup.org/detail/2250",
        "url": "https://mathorcup.org/uploads/files/20190303/1551582180301081.zip",
        "filename": "mathorcup-2018.zip",
        "sha256": "9b7ba20a25957c840ea48dc3f28c01f012185b46ba9656b945058c7050274109",
        "bytes": 2_364_775,
    },
    {
        "id": "mathorcup-2023",
        "year": 2023,
        "notice_url": "https://mathorcup.org/detail/2417",
        "url": "https://mathorcup.org/uploads/files/20230413/1681372678943642.zip",
        "filename": "mathorcup-2023.zip",
        "sha256": "4edde879efe20e99b685a0f90767d5ea6bd022b1c3c797ecf2352a2c78153407",
        "bytes": 26_042_614,
    },
    {
        "id": "mathorcup-2024",
        "year": 2024,
        "notice_url": "https://mathorcup.org/detail/2438",
        "url": "https://mathorcup.org/uploads/files/20240414/1713070010384989.zip",
        "filename": "mathorcup-2024.zip",
        "sha256": "4bfae69d792a26f442093b31fb3a64f613b51b8937f2a45b503d2c20a2c3d3ea",
        "bytes": 21_111_203,
    },
    {
        "id": "mathorcup-2026",
        "year": 2026,
        "notice_url": "https://mathorcup.org/detail/2487",
        "url": "https://mathorcup.org/uploads/files/20260417/1776383723989262.zip",
        "filename": "mathorcup-2026.zip",
        "sha256": "950ea755182a0f4d6ba32fa9c904817a042ff367f9f2bcf7512dc6e7fe7621d8",
        "bytes": 2_755_960,
    },
    {
        "id": "mathorcup-2026-d-fixed",
        "year": 2026,
        "notice_url": "https://mathorcup.org/detail/2488",
        "url": "https://mathorcup.org/uploads/files/20260417/1776438403640865.zip",
        "filename": "mathorcup-2026-d-fixed.zip",
        "sha256": "d417f99dd790d576b89f9c7b8b0fd7ba22ff324414884f801c07431b0e2a039a",
        "bytes": 156_307,
    },
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive(record: dict[str, Any], path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size != record["bytes"]:
        raise RuntimeError(
            f"Archive size mismatch for {record['id']}: "
            f"expected {record['bytes']}, found {path.stat().st_size}"
        )
    actual = sha256(path)
    if actual != record["sha256"]:
        raise RuntimeError(
            f"Archive SHA-256 mismatch for {record['id']}: "
            f"expected {record['sha256']}, found {actual}"
        )


def fetch(record: dict[str, Any]) -> Path:
    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    target = SOURCE_ROOT / record["filename"]
    if target.is_file():
        validate_archive(record, target)
        print(f"REUSE {record['id']} bytes={target.stat().st_size}")
        return target
    request = urllib.request.Request(record["url"], headers={"User-Agent": USER_AGENT})
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        validate_archive(record, temporary)
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"FETCH {record['id']} bytes={target.stat().st_size}")
    return target


def decoded_name(info: ZipInfo) -> str:
    name = info.filename.replace("\\", "/")
    if info.flag_bits & 0x800:
        return name
    raw = name.encode("cp437")
    for encoding in ("utf-8", "gb18030"):
        try:
            candidate = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        # Wrong single-byte decodes can succeed while preserving box-drawing
        # mojibake; require either CJK text or a fully ASCII member name.
        if candidate.isascii() or any("\u3400" <= char <= "\u9fff" for char in candidate):
            return candidate
    return name


def safe_destination(root: Path, member_name: str) -> Path:
    parts = PurePosixPath(member_name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"Unsafe ZIP member path: {member_name!r}")
    candidate = root.joinpath(*parts)
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise RuntimeError(f"ZIP member escapes destination: {member_name!r}")
    return candidate


def expand(record: dict[str, Any]) -> dict[str, Any]:
    archive = SOURCE_ROOT / record["filename"]
    validate_archive(record, archive)
    destination = EXTRACTED_ROOT / record["id"]
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    members: list[dict[str, Any]] = []
    with ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = decoded_name(info)
            lowered = name.lower()
            if lowered.startswith("__macosx/") or "/__macosx/" in lowered or Path(name).name.startswith("._"):
                continue
            target = safe_destination(destination, name)
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
    print(f"EXPAND {record['id']} files={len(members)}")
    return {"id": record["id"], "directory": destination.relative_to(ROOT).as_posix(), "members": members}


def write_manifest(expansions: list[dict[str, Any]]) -> dict[str, Any]:
    expansion_by_id = {item["id"]: item for item in expansions}
    records = []
    for source in ARCHIVES:
        archive = SOURCE_ROOT / source["filename"]
        validate_archive(source, archive)
        records.append({
            **source,
            "path": archive.relative_to(ROOT).as_posix(),
            "extracted": expansion_by_id.get(source["id"]),
        })
    manifest = {
        "schema_version": "1.0.0",
        "source_id": "mathorcup_official",
        "records": records,
        "stats": {
            "archive_count": len(records),
            "archive_bytes": sum(item["bytes"] for item in records),
            "extracted_file_count": sum(len(item["members"]) for item in expansions),
        },
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("source_id") != "mathorcup_official":
        raise RuntimeError("Unexpected MathorCup manifest source_id")
    expected_ids = {item["id"] for item in ARCHIVES}
    records = {item["id"]: item for item in manifest["records"]}
    if set(records) != expected_ids:
        raise RuntimeError(f"MathorCup manifest ids differ: {sorted(set(records) ^ expected_ids)}")
    for pinned in ARCHIVES:
        record = records[pinned["id"]]
        archive = ROOT / record["path"]
        validate_archive(pinned, archive)
        extracted = record.get("extracted") or {}
        if not extracted.get("members"):
            raise RuntimeError(f"No extracted files recorded for {pinned['id']}")
        for member in extracted["members"]:
            path = ROOT / member["path"]
            if not path.is_file() or path.stat().st_size != member["bytes"] or sha256(path) != member["sha256"]:
                raise RuntimeError(f"Extracted member mismatch: {path}")
    print("MATHORCUP_STAGE_VERIFY_OK " + json.dumps(manifest["stats"], ensure_ascii=False))
    return manifest["stats"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "expand", "verify", "all"))
    args = parser.parse_args()
    if args.command in {"fetch", "all"}:
        for record in ARCHIVES:
            fetch(record)
    if args.command in {"expand", "all"}:
        expansions = [expand(record) for record in ARCHIVES]
        print(json.dumps(write_manifest(expansions)["stats"], ensure_ascii=False))
    if args.command in {"verify", "all"}:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
