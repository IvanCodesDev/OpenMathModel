#!/usr/bin/env python3
"""Snapshot the Teddy Cup statements the organiser publishes through its API.

From 2021 the contest runs on the organiser's own BdRace platform, which serves
each edition's problems as rich text from a public endpoint and keeps the PDFs
behind registration. The endpoint is the only public form of these statements,
so the response is stored verbatim and pinned, and each problem's body is
written out as its own HTML file.

Most of what the endpoint returns is not the whole statement. The platform cuts
the body off and puts a row of dots or an invitation to fetch the rest from a
WeChat account in its place -- twenty of the twenty-one problems published so
far end that way. Those are recorded here with ``complete`` false and never
reach the problem library, which requires a full statement; only the bodies
that carry neither marker are publishable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "datasets/raw/sources/full-problem-archives/tipdm-cup-bdrace"
MANIFEST = SOURCE_ROOT / "manifest.json"
BASE = "https://www.tipdm.org:10010"
QUESTION_ENDPOINT = "/api/competition/question/public/list/%s"
# Each edition's own page on the platform. It is a hash route, so it renders
# only after the bundle runs -- fine for the "view source" link, which opens in
# a browser. The route was confirmed against the 2023 edition; the neighbouring
# spellings the bundle also contains (competitionDetail, competitionInfo) all
# render the platform's 404 page.
COMPETITION_PAGE = "https://www.tipdm.org:10010/#/competition/%s"
USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"

# The platform's own competition ids. Two contests share the Teddy Cup name: the
# data-mining challenge and the shorter data-analysis skills contest.
EDITIONS: tuple[dict[str, Any], ...] = (
    {"slug": "tipdm-cup-2026", "year": 2026, "edition": 14, "track": "challenge",
     "id": "2011245275770961920"},
    {"slug": "tipdm-cup-2025", "year": 2025, "edition": 13, "track": "challenge",
     "id": "1891311201049296896"},
    {"slug": "tipdm-cup-2024", "year": 2024, "edition": 12, "track": "challenge",
     "id": "1734744522337984512"},
    {"slug": "tipdm-cup-2023", "year": 2023, "edition": 11, "track": "challenge",
     "id": "1620719578957127680"},
    {"slug": "tipdm-cup-2022", "year": 2022, "edition": 10, "track": "challenge",
     "id": "1481159137780998144"},
    {"slug": "tipdm-skills-2024", "year": 2024, "edition": 7, "track": "skills",
     "id": "1825410816212639744"},
    {"slug": "tipdm-skills-2023", "year": 2023, "edition": 6, "track": "skills",
     "id": "1694981063413243904"},
    {"slug": "tipdm-skills-2022", "year": 2022, "edition": 5, "track": "skills",
     "id": "1557899215680741376"},
)

TRACK_NAMES = {
    "challenge": "“泰迪杯”数据挖掘挑战赛",
    "skills": "“泰迪杯”数据分析技能赛",
}

# A paragraph of nothing but dots replaces the withheld part of a statement.
ELISION_RE = re.compile(r"^[.．。…\s·]{3,}$")
# The invitation that replaces it, or follows it.
SOLICITATION_RE = re.compile(r"(关注[^。]{0,20}公众号|领取赛题|把赛题发给客服|回复【客服】)")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def paragraphs(body: str) -> list[str]:
    """Flatten the body to its visible paragraphs, for the completeness test."""
    text = re.sub(r"(?i)</\s*(p|h[1-6]|li|div|blockquote)\s*>", "\n", body)
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("\ufeff", "")
    return [re.sub(r"\s+", " ", line).strip() for line in text.split("\n") if line.strip()]


def is_complete(body: str) -> bool:
    lines = paragraphs(body)
    if not lines:
        return False
    return not any(ELISION_RE.match(line) or SOLICITATION_RE.search(line) for line in lines)


def fetch(edition: dict[str, Any]) -> bytes:
    url = BASE + (QUESTION_ENDPOINT % edition["id"])
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": BASE + "/",
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def problem_id(edition: dict[str, Any], letter: str) -> str:
    return f"{edition['slug']}-{letter.lower()}"


def discover() -> dict[str, Any]:
    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    responses: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for index, edition in enumerate(EDITIONS):
        payload = fetch(edition)
        snapshot = SOURCE_ROOT / f"{edition['slug']}.json"
        snapshot.write_bytes(payload)
        responses.append({
            "slug": edition["slug"],
            "competition_id": edition["id"],
            "url": BASE + (QUESTION_ENDPOINT % edition["id"]),
            "path": snapshot.relative_to(ROOT).as_posix(),
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
        })
        document = json.loads(payload.decode("utf-8"))
        for question in document.get("data") or []:
            letter = (question.get("serialNumber") or "").strip().upper()
            body = question.get("description") or ""
            if not letter or not body:
                raise RuntimeError(f"{edition['slug']}: question without a letter or body")
            identifier = problem_id(edition, letter)
            target = SOURCE_ROOT / f"{identifier}.html"
            encoded = body.encode("utf-8")
            target.write_bytes(encoded)
            problems.append({
                "id": identifier,
                "year": edition["year"],
                "edition": edition["edition"],
                "track": edition["track"],
                "competition": TRACK_NAMES[edition["track"]],
                "letter": letter,
                "title": (question.get("name") or "").strip(),
                "question_unit": (question.get("questionUnit") or "").strip(),
                "source_url": COMPETITION_PAGE % edition["id"],
                "api_url": BASE + (QUESTION_ENDPOINT % edition["id"]),
                "path": target.relative_to(ROOT).as_posix(),
                "bytes": len(encoded),
                "sha256": sha256_bytes(encoded),
                "complete": is_complete(body),
            })
        if index + 1 < len(EDITIONS):
            time.sleep(6.1)  # source policy: at most ten requests per minute
    manifest = {
        "schema_version": "1.0.0",
        "source_id": "tipdm_cup_official",
        "collection": "bdrace_public_api",
        "responses": responses,
        "problems": problems,
        "stats": {
            "edition_count": len(EDITIONS),
            "problem_count": len(problems),
            "complete_count": sum(item["complete"] for item in problems),
            "elided_count": sum(not item["complete"] for item in problems),
        },
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("TIPDM_BDRACE_DISCOVERY_OK " + json.dumps(manifest["stats"], ensure_ascii=False))
    return manifest


def verify() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("collection") != "bdrace_public_api":
        raise RuntimeError("Unexpected BdRace manifest collection")
    slugs = {item["slug"] for item in manifest["responses"]}
    if slugs != {edition["slug"] for edition in EDITIONS}:
        raise RuntimeError(f"BdRace editions differ: {sorted(slugs)}")
    for record in manifest["responses"] + manifest["problems"]:
        path = ROOT / record["path"]
        if not path.is_file():
            raise RuntimeError(f"Missing BdRace snapshot {path}")
        data = path.read_bytes()
        if len(data) != record["bytes"] or sha256_bytes(data) != record["sha256"]:
            raise RuntimeError(f"BdRace snapshot mismatch {path}")
    for problem in manifest["problems"]:
        body = (ROOT / problem["path"]).read_text(encoding="utf-8")
        if is_complete(body) != problem["complete"]:
            raise RuntimeError(f"Completeness drifted for {problem['id']}")
    print("TIPDM_BDRACE_VERIFY_OK " + json.dumps(manifest["stats"], ensure_ascii=False))
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
