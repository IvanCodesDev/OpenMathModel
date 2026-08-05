#!/usr/bin/env python3
"""Download and extract complete CPMCM problem statements from zhanwen/MathModel.

The Git tree snapshot is treated as the discovery record. Selected DOCX files are
stored under datasets/raw, while normalized document blocks are written to the
interim layer and referenced assets are copied into the web application's public
directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import shutil
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from PIL import Image
from io import BytesIO


ROOT = Path(__file__).resolve().parents[2]
TREE_PATH = ROOT / "datasets/raw/sources/github/zhanwen-MathModel-tree.json"
RAW_ROOT = ROOT / "datasets/raw/sources/github/zhanwen-MathModel"
FILES_ROOT = RAW_ROOT / "files"
MANIFEST_PATH = RAW_ROOT / "source-manifest.json"
INTERIM_PATH = ROOT / "datasets/interim/github_zhanwen_mathmodel/full-problems.json"
ASSET_ROOT = ROOT / "apps/web/public/problem-assets"
REPOSITORY = "zhanwen/MathModel"
BRANCH = "master"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
COMMIT = "cd5be91735ebf11d5ee52eb170e86a6d07131977"

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_documents(tree: dict[str, Any]) -> list[dict[str, Any]]:
    pattern = re.compile(r"^国赛试题/(2023|2024)年研究生数学建模竞赛试题(?:/|$)")
    selected: list[dict[str, Any]] = []
    for item in tree["tree"]:
        path = item.get("path", "")
        if item.get("type") != "blob" or not pattern.match(path) or not path.lower().endswith(".docx"):
            continue
        year = int(pattern.match(path).group(1))
        letter_match = re.search(r"/([A-F])题/", path)
        if not letter_match:
            letter_match = re.search(r"([A-F])题", PurePosixPath(path).name)
        if not letter_match:
            continue
        letter = letter_match.group(1)
        name = PurePosixPath(path).name
        is_support = bool(re.search(r"附件|附录|说明|通知|概念|定义", name))
        selected.append({
            "path": path,
            "size": item.get("size", 0),
            "git_blob_sha": item["sha"],
            "year": year,
            "letter": letter,
            "role": "supporting_document" if is_support else "problem_statement",
        })
    selected.sort(key=lambda item: (item["year"], item["letter"], item["role"] != "problem_statement", item["path"]))
    expected = {(year, letter) for year in (2023, 2024) for letter in "ABCDEF"}
    found = {(item["year"], item["letter"]) for item in selected if item["role"] == "problem_statement"}
    if found != expected:
        raise RuntimeError(f"Expected 12 primary statements, found {sorted(found)}")
    return selected


def raw_url(path: str) -> str:
    quoted = urllib.parse.quote(path, safe="/")
    return f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{quoted}"


def blob_url(path: str) -> str:
    quoted = urllib.parse.quote(path, safe="/")
    return f"{REPOSITORY_URL}/blob/{COMMIT}/{quoted}"


def download_documents(force: bool = False) -> dict[str, Any]:
    tree = json.loads(TREE_PATH.read_text(encoding="utf-8-sig"))
    documents = selected_documents(tree)
    records: list[dict[str, Any]] = []
    for index, document in enumerate(documents, start=1):
        destination = FILES_ROOT / PurePosixPath(document["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if force or not destination.exists() or destination.stat().st_size != document["size"]:
            request = urllib.request.Request(
                raw_url(document["path"]),
                headers={"User-Agent": "OpenMathModelDatasetBot/0.1", "Accept": "application/octet-stream"},
            )
            with urllib.request.urlopen(request, timeout=90) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
        if destination.stat().st_size != document["size"]:
            raise RuntimeError(f"Size mismatch for {document['path']}")
        record = {
            **document,
            "source_url": blob_url(document["path"]),
            "download_url": raw_url(document["path"]),
            "stored_path": destination.relative_to(ROOT).as_posix(),
            "sha256": sha256(destination),
        }
        records.append(record)
        print(f"DOWNLOAD {index:02d}/{len(documents):02d} {document['year']}{document['letter']} {document['role']} {document['size']} {document['path']}")
    manifest = {
        "schema_version": "1.0.0",
        "source_id": "github_zhanwen_mathmodel",
        "repository": REPOSITORY_URL,
        "branch": BRANCH,
        "commit": COMMIT,
        "tree_sha": tree["sha"],
        "collected_at": now_iso(),
        "summary": {
            "documents": len(records),
            "problem_statements": sum(item["role"] == "problem_statement" for item in records),
            "supporting_documents": sum(item["role"] == "supporting_document" for item in records),
            "bytes": sum(item["size"] for item in records),
        },
        "records": records,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        name = local_name(node.tag)
        if name == "t" and node.text:
            parts.append(node.text)
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    text = "".join(parts).replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def paragraph_kind(paragraph: ET.Element, text: str) -> tuple[str, int | None]:
    style_node = paragraph.find("./w:pPr/w:pStyle", NS)
    style = "" if style_node is None else style_node.attrib.get(f"{{{NS['w']}}}val", "")
    heading_match = re.search(r"(?:heading|标题)\s*([1-6])", style, re.I)
    if heading_match:
        return "heading", int(heading_match.group(1))
    if style.lower() in {"title", "subtitle", "题目", "主标题", "副标题"}:
        return "heading", 1 if style.lower() in {"title", "题目", "主标题"} else 2
    if len(text) <= 80 and re.match(r"^(?:[一二三四五六七八九十]+[、.]|\d+(?:\.\d+)*[、. ]|问题\s*[一二三四五六七八九十\d]+|附录|附件)", text):
        level = 3 if re.match(r"^\d+\.\d+", text) else 2
        return "heading", level
    numbering = paragraph.find("./w:pPr/w:numPr", NS)
    if numbering is not None:
        return "list_item", None
    return "paragraph", None


def relationship_map(archive: zipfile.ZipFile) -> dict[str, str]:
    root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
    return {
        item.attrib["Id"]: item.attrib["Target"]
        for item in root
        if item.attrib.get("TargetMode") != "External" and "Id" in item.attrib and "Target" in item.attrib
    }


def extract_docx(record: dict[str, Any], problem_id: str, asset_index: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    source = ROOT / record["stored_path"]
    blocks: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    with zipfile.ZipFile(source) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        relationships = relationship_map(archive)
        body = document.find("w:body", NS)
        if body is None:
            return blocks, assets, asset_index
        for child in body:
            name = local_name(child.tag)
            if name == "p":
                text = element_text(child)
                if text:
                    kind, level = paragraph_kind(child, text)
                    block: dict[str, Any] = {"type": kind, "text": text}
                    if level is not None:
                        block["level"] = level
                    blocks.append(block)
                for image_node in child.findall(".//a:blip", NS):
                    rel_id = image_node.attrib.get(f"{{{NS['r']}}}embed")
                    target = relationships.get(rel_id or "")
                    if not target:
                        continue
                    archive_path = str(PurePosixPath("word") / target).replace("word/../", "")
                    if archive_path not in archive.namelist():
                        continue
                    extension = PurePosixPath(archive_path).suffix.lower() or ".bin"
                    asset_index += 1
                    payload = archive.read(archive_path)
                    if extension in {".emf", ".wmf", ".tif", ".tiff"}:
                        with Image.open(BytesIO(payload)) as image:
                            converted = BytesIO()
                            image.convert("RGBA").save(converted, format="PNG")
                            payload = converted.getvalue()
                        extension = ".png"
                    filename = f"{asset_index:03d}{extension}"
                    asset_path = ASSET_ROOT / problem_id / filename
                    asset_path.parent.mkdir(parents=True, exist_ok=True)
                    asset_path.write_bytes(payload)
                    web_path = f"/problem-assets/{problem_id}/{filename}"
                    alt = text[:80] if text else f"{record['year']} {record['letter']} 题插图 {asset_index}"
                    blocks.append({"type": "image", "src": web_path, "alt": alt})
                    assets.append({
                        "path": asset_path.relative_to(ROOT).as_posix(),
                        "url": web_path,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    })
            elif name == "tbl":
                rows: list[list[str]] = []
                for row in child.findall("./w:tr", NS):
                    cells = [element_text(cell) for cell in row.findall("./w:tc", NS)]
                    if any(cells):
                        rows.append(cells)
                if rows:
                    blocks.append({"type": "table", "rows": rows})
    return blocks, assets, asset_index


def classify_problem(title: str) -> tuple[str, list[str], list[str]]:
    rules = [
        (("WLAN", "网络", "吞吐量"), "网络与通信建模", ["网络建模", "性能优化"]),
        (("DFT", "矩阵", "整数分解"), "数学与算法", ["矩阵分解", "数值逼近"]),
        (("评审", "竞赛"), "评价与决策", ["评价模型", "统计分析"]),
        (("双碳", "路径规划"), "规划优化", ["碳排放", "多目标规划"]),
        (("脑卒中", "诊疗"), "医学数据建模", ["临床预测", "机器学习"]),
        (("降水", "预报"), "气象预测", ["时空预测", "数据融合"]),
        (("风电", "功率", "调度"), "能源系统优化", ["优化调度", "新能源"]),
        (("磁性", "磁芯损耗"), "工程数据建模", ["损耗预测", "参数优化"]),
        (("地理", "大数据"), "地理空间建模", ["空间分析", "大数据"]),
        (("高速公路", "应急车道"), "交通系统建模", ["交通预测", "决策优化"]),
        (("X射线", "脉冲星", "光子"), "空间科学建模", ["时间序列", "参数估计"]),
    ]
    for keywords, problem_type, directions in rules:
        if any(keyword.lower() in title.lower() for keyword in keywords):
            return problem_type, directions, list(keywords)
    return "综合建模", ["综合分析"], ["数学建模"]


def extract_paper_inventory() -> list[dict[str, Any]]:
    tree = json.loads(TREE_PATH.read_text(encoding="utf-8-sig"))
    prefix = "国赛论文/"
    papers: list[dict[str, Any]] = []
    for item in tree["tree"]:
        path = item.get("path", "")
        if item.get("type") != "blob" or not path.startswith(prefix) or not path.lower().endswith(".pdf"):
            continue
        year_match = re.search(r"(20\d{2})年", path)
        if not year_match:
            continue
        year = int(year_match.group(1))
        letter_match = re.search(r"/([A-F])(?:题)?/", path)
        stem = PurePosixPath(path).stem
        letter = letter_match.group(1) if letter_match else (stem[0].upper() if stem and stem[0].upper() in "ABCDEF" else "?")
        award = "数模之星提名奖" if "数模之星" in path else "优秀论文"
        problem_id = f"cpmcm-{year}-{letter.lower()}" if year in {2023, 2024} and letter != "?" else None
        path_id = hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]
        papers.append({
            "id": f"cpmcm-paper-{year}-{letter.lower()}-{path_id}",
            "title": stem,
            "record_type": "paper",
            "problem_id": problem_id,
            "problem_code": f"{year} CPMCM {letter}",
            "competition": "中国研究生数学建模竞赛",
            "category": award,
            "year": year,
            "award": award,
            "distinctions": [award] if award != "优秀论文" else [],
            "institution": None,
            "team_id": stem if re.fullmatch(r"[A-F]?\d{4,}", stem, re.I) else None,
            "models": [],
            "innovation": "已建立逐篇来源索引，模型方法等待 PDF 全文解析。",
            "summary": f"{year} 年中国研究生数学建模竞赛 {letter} 题论文；来源文件：{PurePosixPath(path).name}。",
            "source_id": "github_zhanwen_mathmodel",
            "source_url": blob_url(path),
            "full_text_url": blob_url(path),
            "source_status": "community_repository_snapshot",
            "access_scope": "linked_content",
            "source_file_bytes": item.get("size", 0),
            "source_git_blob_sha": item["sha"],
        })
    return sorted(papers, key=lambda item: (-item["year"], item["problem_code"], item["title"]))


def extract_problems() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if ASSET_ROOT.exists():
        for year in (2023, 2024):
            for letter in "ABCDEF":
                target = ASSET_ROOT / f"cpmcm-{year}-{letter.lower()}"
                if target.exists():
                    shutil.rmtree(target)
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for record in manifest["records"]:
        grouped.setdefault((record["year"], record["letter"]), []).append(record)
    problems: list[dict[str, Any]] = []
    all_assets: list[dict[str, Any]] = []
    for year, letter in sorted(grouped, key=lambda key: (-key[0], key[1])):
        records = grouped[(year, letter)]
        primary = next(item for item in records if item["role"] == "problem_statement")
        supporting = [item for item in records if item["role"] == "supporting_document"]
        title = PurePosixPath(primary["path"]).stem
        problem_id = f"cpmcm-{year}-{letter.lower()}"
        blocks: list[dict[str, Any]] = []
        assets: list[dict[str, Any]] = []
        asset_index = 0
        ordered_records = [primary, *supporting]
        for document_index, record in enumerate(ordered_records):
            if document_index:
                blocks.append({"type": "document_break", "title": PurePosixPath(record["path"]).stem})
            document_blocks, document_assets, asset_index = extract_docx(record, problem_id, asset_index)
            blocks.extend(document_blocks)
            assets.extend(document_assets)
        text = "\n".join(block.get("text", "") for block in blocks)
        problem_type, directions, keywords = classify_problem(title)
        source_documents = [{
            "title": PurePosixPath(item["path"]).name,
            "role": item["role"],
            "url": item["source_url"],
            "bytes": item["size"],
            "sha256": item["sha256"],
            "git_blob_sha": item["git_blob_sha"],
        } for item in ordered_records]
        problems.append({
            "id": problem_id,
            "code": f"{year} CPMCM {letter}",
            "title": title,
            "competition": "中国研究生数学建模竞赛",
            "category": "研究生赛",
            "year": year,
            "problem_type": problem_type,
            "modeling_directions": directions,
            "keywords": keywords,
            "data_requirement": "题面含数据附件说明" if re.search(r"附件|数据", text) else "题面内给定数据",
            "status": "完整题面",
            "summary": next((block["text"] for block in blocks if block["type"] == "paragraph" and len(block.get("text", "")) >= 35), title),
            "source_id": "github_zhanwen_mathmodel",
            "source_url": primary["source_url"],
            "source_status": "community_repository_snapshot",
            "access_scope": "stored_content",
            "attachments": [{"title": item["title"], "url": item["url"], "kind": "problem"} for item in source_documents],
            "content_format": "ordered_docx_blocks",
            "content_status": "complete",
            "content_character_count": len(text),
            "content_block_count": len(blocks),
            "content_blocks": blocks,
            "source_documents": source_documents,
        })
        all_assets.extend(assets)
        print(f"EXTRACT {problem_id} blocks={len(blocks)} chars={len(text)} images={len(assets)} documents={len(records)}")
    papers = extract_paper_inventory()
    result = {
        "schema_version": "1.0.0",
        "source_id": "github_zhanwen_mathmodel",
        "repository": REPOSITORY_URL,
        "commit": manifest["commit"],
        "generated_at": manifest["collected_at"],
        "stats": {
            "problem_count": len(problems),
            "document_count": len(manifest["records"]),
            "content_block_count": sum(item["content_block_count"] for item in problems),
            "content_character_count": sum(item["content_character_count"] for item in problems),
            "asset_count": len(all_assets),
            "asset_bytes": sum(item["bytes"] for item in all_assets),
            "paper_inventory_count": len(papers),
        },
        "problems": problems,
        "papers": papers,
        "assets": all_assets,
    }
    INTERIM_PATH.parent.mkdir(parents=True, exist_ok=True)
    INTERIM_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def verify() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    result = json.loads(INTERIM_PATH.read_text(encoding="utf-8"))
    if manifest["summary"]["problem_statements"] != 12 or result["stats"]["problem_count"] != 12:
        raise RuntimeError("The complete 2023–2024 A–F problem set is not present")
    for record in manifest["records"]:
        path = ROOT / record["stored_path"]
        if not path.exists() or path.stat().st_size != record["size"] or sha256(path) != record["sha256"]:
            raise RuntimeError(f"Raw source verification failed: {record['path']}")
    for problem in result["problems"]:
        if problem["content_status"] != "complete" or problem["content_character_count"] < 400 or problem["content_block_count"] < 5:
            raise RuntimeError(f"Incomplete extracted content: {problem['id']}")
    if result["stats"].get("paper_inventory_count") != 685 or len(result.get("papers", [])) != 685:
        raise RuntimeError("Expected 685 repository paper PDF records")
    for asset in result["assets"]:
        path = ROOT / asset["path"]
        if not path.exists() or path.stat().st_size != asset["bytes"] or sha256(path) != asset["sha256"]:
            raise RuntimeError(f"Asset verification failed: {asset['path']}")
    summary = {**manifest["summary"], **result["stats"], "commit": manifest["commit"]}
    print("MATHMODEL_FULL_PROBLEMS_VERIFY_OK " + json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("download", "extract", "all", "verify"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.command in {"download", "all"}:
        download_documents(force=args.force)
    if args.command in {"extract", "all"}:
        extract_problems()
    if args.command in {"verify", "all"}:
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
