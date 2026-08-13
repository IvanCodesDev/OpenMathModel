#!/usr/bin/env python3
"""Repeatable Wave A collector for official mathematical modeling sources.

The collector uses only Python's standard library. It validates the source
registry, checks robots.txt, rate-limits requests, records HTTP validators,
writes immutable content-addressed raw objects, and emits one manifest per run.
Large runtime data remains under ignored raw/interim directories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = ROOT / "datasets" / "catalog" / "source-registry.json"
RAW_ROOT = ROOT / "datasets" / "raw"
INTERIM_ROOT = ROOT / "datasets" / "interim"
ATTACHMENT_SUFFIXES = {".pdf", ".zip", ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx", ".csv"}
HTML_HINTS = ("problem", "contest", "archive", "赛题", "题目", "竞赛")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def validate_registry(registry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if registry.get("schema_version") != "1.0.0":
        errors.append("schema_version must be 1.0.0")
    sources = registry.get("sources")
    if not isinstance(sources, list) or not sources:
        return errors + ["sources must be a non-empty array"]
    seen: set[str] = set()
    required = {
        "id", "name", "tier", "enabled", "official_status", "competition", "domains",
        "entrypoints", "allowed_paths", "denied_paths", "year_range", "crawl_policy", "license_record",
    }
    rights_fields = {
        "status", "rights_holder", "license_text_url", "allow_storage", "allow_indexing",
        "allow_redistribution", "allow_derivatives", "reviewed_at",
    }
    for index, source in enumerate(sources):
        prefix = f"sources[{index}]"
        missing = sorted(required - set(source))
        if missing:
            errors.append(f"{prefix}: missing {', '.join(missing)}")
            continue
        source_id = source["id"]
        if not re.fullmatch(r"[a-z0-9_]+", source_id):
            errors.append(f"{prefix}.id: invalid identifier")
        if source_id in seen:
            errors.append(f"{prefix}.id: duplicate {source_id}")
        seen.add(source_id)
        if source["tier"] not in {f"T{i}" for i in range(6)}:
            errors.append(f"{prefix}.tier: invalid tier")
        years = source["year_range"]
        if not isinstance(years.get("from"), int) or not isinstance(years.get("to"), int) or years["from"] > years["to"]:
            errors.append(f"{prefix}.year_range: invalid range")
        policy = source["crawl_policy"]
        if not 1 <= policy.get("requests_per_minute", 0) <= 120:
            errors.append(f"{prefix}.crawl_policy.requests_per_minute: out of range")
        if policy.get("concurrency") != 1:
            errors.append(f"{prefix}.crawl_policy.concurrency: Wave A collector requires 1")
        rights = source["license_record"]
        missing_rights = sorted(rights_fields - set(rights))
        if missing_rights:
            errors.append(f"{prefix}.license_record: missing {', '.join(missing_rights)}")
        # HTTPS is the rule. A source may declare plain_http_only when its
        # official site simply has no TLS endpoint (the statistical modelling
        # contest's site keeps port 443 closed), which keeps the exception in
        # the ledger instead of silently weakening the invariant.
        schemes = {"https", "http"} if source.get("plain_http_only") else {"https"}
        for entrypoint in source["entrypoints"]:
            parsed = urllib.parse.urlparse(entrypoint.get("url", ""))
            if parsed.scheme not in schemes or not parsed.netloc:
                errors.append(f"{prefix}.entrypoints: only absolute HTTPS URLs are accepted")
            if parsed.hostname not in source["domains"]:
                errors.append(f"{prefix}.entrypoints: domain is not registered: {parsed.hostname}")
    return errors


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append({"href": self._href, "text": " ".join("".join(self._text).split())})
            self._href = None
            self._text = []


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


class Collector:
    def __init__(self, source: dict[str, Any], years: set[int], max_pages: int, include_attachments: bool, max_bytes: int) -> None:
        self.source = source
        self.years = years
        self.max_pages = max_pages
        self.include_attachments = include_attachments
        self.max_bytes = max_bytes
        self.policy = source["crawl_policy"]
        self.interval = 60.0 / self.policy["requests_per_minute"]
        self.last_request = 0.0
        self.state_path = INTERIM_ROOT / source["id"] / "http-state.json"
        self.state: dict[str, Any] = load_json(self.state_path, {})
        self.errors: list[dict[str, str]] = []

    def _wait(self) -> None:
        remaining = self.interval - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)

    def _request(self, url: str, conditional: bool = True) -> FetchResult:
        headers = {"User-Agent": self.policy["user_agent"], "Accept": "text/html,application/xhtml+xml,application/pdf,application/zip;q=0.9,*/*;q=0.1"}
        prior = self.state.get(url, {}) if conditional else {}
        if prior.get("etag"):
            headers["If-None-Match"] = prior["etag"]
        if prior.get("last_modified"):
            headers["If-Modified-Since"] = prior["last_modified"]
        request = urllib.request.Request(url, headers=headers)
        retries = self.policy["max_retries"]
        for attempt in range(retries + 1):
            self._wait()
            try:
                with urllib.request.urlopen(request, timeout=self.policy["timeout_seconds"]) as response:
                    self.last_request = time.monotonic()
                    content_length = int(response.headers.get("Content-Length", "0") or 0)
                    if content_length > self.max_bytes:
                        raise ValueError(f"response exceeds --max-bytes ({content_length} > {self.max_bytes})")
                    body = response.read(self.max_bytes + 1)
                    if len(body) > self.max_bytes:
                        raise ValueError(f"response exceeds --max-bytes ({len(body)} > {self.max_bytes})")
                    return FetchResult(response.geturl(), response.status, {k.lower(): v for k, v in response.headers.items()}, body)
            except urllib.error.HTTPError as exc:
                self.last_request = time.monotonic()
                if exc.code == 304:
                    return FetchResult(url, 304, {k.lower(): v for k, v in exc.headers.items()}, b"")
                if exc.code not in {429, 500, 502, 503, 504} or attempt == retries:
                    raise
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                time.sleep(min(delay, 60))
            except (urllib.error.URLError, TimeoutError) as exc:
                self.last_request = time.monotonic()
                if attempt == retries:
                    raise exc
                time.sleep(min(2 ** attempt, 60))
        raise RuntimeError("retry loop exhausted")

    def check_robots(self) -> tuple[dict[str, str], urllib.robotparser.RobotFileParser | None]:
        first = urllib.parse.urlparse(self.source["entrypoints"][0]["url"])
        robots_url = urllib.parse.urlunparse((first.scheme, first.netloc, "/robots.txt", "", "", ""))
        checked = utc_now()
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            result = self._request(robots_url, conditional=False)
            parser.parse(result.body.decode("utf-8", errors="replace").splitlines())
            allowed = all(parser.can_fetch(self.policy["user_agent"], entry["url"]) for entry in self.source["entrypoints"])
            return {"url": robots_url, "status": "allowed" if allowed else "disallowed", "checked_at": checked, "detail": f"HTTP {result.status}"}, parser
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"url": robots_url, "status": "missing", "checked_at": checked, "detail": "HTTP 404; no robots rules published"}, None
            return {"url": robots_url, "status": "unavailable", "checked_at": checked, "detail": f"HTTP {exc.code}"}, None
        except Exception as exc:  # network and decoding failures are recorded in the manifest
            return {"url": robots_url, "status": "unavailable", "checked_at": checked, "detail": str(exc)}, None

    def _allowed_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in self.source["domains"]:
            return False
        if any(parsed.path.startswith(prefix) for prefix in self.source["denied_paths"]):
            return False
        return any(parsed.path.startswith(prefix) for prefix in self.source["allowed_paths"])

    def _normalize_link(self, base: str, href: str) -> str | None:
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            return None
        absolute = urllib.parse.urljoin(base, href)
        parsed = urllib.parse.urlparse(absolute)
        normalized = urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", "", parsed.query, ""))
        return normalized if self._allowed_url(normalized) else None

    def _is_attachment(self, url: str) -> bool:
        return Path(urllib.parse.urlparse(url).path.lower()).suffix in ATTACHMENT_SUFFIXES

    def _matches_year(self, url: str, text: str) -> bool:
        haystack = f"{url} {text}"
        return any(str(year) in haystack for year in self.years)

    def _should_follow_html(self, url: str, text: str, depth: int) -> bool:
        if depth >= 2:
            return False
        haystack = f"{url} {text}".lower()
        return self._matches_year(url, text) or any(hint in haystack for hint in HTML_HINTS)

    @staticmethod
    def _extension(content_type: str, url: str) -> str:
        content_type = content_type.split(";", 1)[0].strip().lower()
        known = {"text/html": ".html", "application/pdf": ".pdf", "application/zip": ".zip", "application/json": ".json"}
        return known.get(content_type) or Path(urllib.parse.urlparse(url).path).suffix[:10] or mimetypes.guess_extension(content_type) or ".bin"

    def _store_object(self, digest: str, extension: str, body: bytes) -> Path:
        target = RAW_ROOT / "objects" / digest[:2] / f"{digest}{extension}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(body)
            os.replace(temporary, target)
        return target

    def collect(self, allow_unavailable_robots: bool = False) -> tuple[Path, dict[str, Any]]:
        started = utc_now()
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        robots, robot_parser = self.check_robots()
        if robots["status"] == "disallowed":
            raise RuntimeError(f"robots.txt disallows a registered entrypoint: {robots['url']}")
        if robots["status"] == "unavailable" and not allow_unavailable_robots:
            raise RuntimeError(f"robots.txt check unavailable: {robots['detail']}")

        queue: deque[tuple[str, int]] = deque((entry["url"], 0) for entry in self.source["entrypoints"])
        queued = {url for url, _ in queue}
        visited: set[str] = set()
        records: list[dict[str, Any]] = []
        discovered: dict[str, dict[str, Any]] = {}
        summary = {"fetched": 0, "not_modified": 0, "failed": 0, "bytes": 0, "unique_content": 0, "discovered_links": 0}
        snapshot_dir = RAW_ROOT / "snapshots" / self.source["id"] / run_id
        manifest_path = snapshot_dir / "manifest.json"

        def checkpoint(run_status: str) -> dict[str, Any]:
            summary["discovered_links"] = len(discovered)
            current = {
                "schema_version": "1.0.0",
                "run_id": run_id,
                "source_id": self.source["id"],
                "started_at": started,
                "finished_at": utc_now(),
                "mode": "attachments" if self.include_attachments else "metadata",
                "run_status": run_status,
                "robots": robots,
                "records": records,
                "errors": self.errors,
                "summary": summary,
            }
            atomic_json(manifest_path, current)
            atomic_json(self.state_path, self.state)
            return current

        checkpoint("running")

        while queue and len(visited) < self.max_pages:
            url, depth = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            if robot_parser and not robot_parser.can_fetch(self.policy["user_agent"], url):
                self.errors.append({"url": url, "error": "robots_disallowed"})
                summary["failed"] += 1
                checkpoint("running")
                continue
            try:
                result = self._request(url)
            except Exception as exc:
                self.errors.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
                summary["failed"] += 1
                checkpoint("running")
                continue
            if result.status == 304:
                summary["not_modified"] += 1
                checkpoint("running")
                continue

            content_type = result.headers.get("content-type", "application/octet-stream")
            digest = hashlib.sha256(result.body).hexdigest()
            object_path = self._store_object(digest, self._extension(content_type, result.url), result.body)
            parsed_links: list[dict[str, Any]] = []
            if "html" in content_type.lower() or object_path.suffix == ".html":
                parser = LinkParser()
                parser.feed(result.body.decode("utf-8", errors="replace"))
                for raw_link in parser.links:
                    normalized = self._normalize_link(result.url, raw_link["href"])
                    if not normalized:
                        continue
                    item = {
                        "url": normalized,
                        "text": raw_link["text"],
                        "attachment": self._is_attachment(normalized),
                        "year_match": self._matches_year(normalized, raw_link["text"]),
                    }
                    parsed_links.append(item)
                    discovered.setdefault(normalized, item)
                    should_queue = self.include_attachments if item["attachment"] else self._should_follow_html(normalized, raw_link["text"], depth)
                    if should_queue and normalized not in queued:
                        if item["year_match"] and not item["attachment"]:
                            queue.appendleft((normalized, depth + 1))
                        else:
                            queue.append((normalized, depth + 1))
                        queued.add(normalized)

            relative_path = object_path.relative_to(ROOT).as_posix()
            record = {
                "url": result.url,
                "status": result.status,
                "content_type": content_type,
                "etag": result.headers.get("etag"),
                "last_modified": result.headers.get("last-modified"),
                "sha256": digest,
                "size_bytes": len(result.body),
                "fetched_at": utc_now(),
                "stored_path": relative_path,
                "links": parsed_links,
            }
            records.append(record)
            previous_digest = self.state.get(url, {}).get("sha256")
            summary["fetched"] += 1
            summary["bytes"] += len(result.body)
            summary["unique_content"] += int(previous_digest != digest)
            self.state[url] = {
                "etag": record["etag"], "last_modified": record["last_modified"],
                "sha256": digest, "stored_path": relative_path, "fetched_at": record["fetched_at"],
            }
            checkpoint("running")

        manifest = checkpoint("completed")
        inventory_path = INTERIM_ROOT / self.source["id"] / f"{run_id}-discovered-links.json"
        atomic_json(inventory_path, {"source_id": self.source["id"], "run_id": run_id, "items": sorted(discovered.values(), key=lambda item: item["url"])})
        return manifest_path, manifest


def parse_years(value: str) -> set[int]:
    if ":" in value:
        start, end = (int(part) for part in value.split(":", 1))
        if start > end:
            raise argparse.ArgumentTypeError("year range start must not exceed end")
        return set(range(start, end + 1))
    return {int(part) for part in value.split(",") if part}


def sources_by_id(registry: dict[str, Any], selected: str) -> Iterable[dict[str, Any]]:
    enabled = [source for source in registry["sources"] if source["enabled"]]
    if selected == "all":
        return enabled
    matches = [source for source in enabled if source["id"] == selected]
    if not matches:
        raise SystemExit(f"unknown or disabled source: {selected}")
    return matches


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate source registry invariants")
    collect = subparsers.add_parser("collect", help="collect immutable source snapshots")
    collect.add_argument("--source", default="all", help="source id or 'all'")
    collect.add_argument("--years", type=parse_years, help="YEAR,YEAR or START:END")
    collect.add_argument("--max-pages", type=int, default=25)
    collect.add_argument("--include-attachments", action="store_true")
    collect.add_argument("--max-bytes", type=int, default=100 * 1024 * 1024)
    collect.add_argument("--allow-unavailable-robots", action="store_true", help="record and continue when robots.txt cannot be reached")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    registry = load_json(args.registry)
    errors = validate_registry(registry)
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 2
    if args.command == "validate":
        print(json.dumps({"valid": True, "sources": len(registry["sources"]), "registry": str(args.registry)}, ensure_ascii=False))
        return 0

    outputs: list[dict[str, Any]] = []
    exit_code = 0
    for source in sources_by_id(registry, args.source):
        years = args.years or set(range(source["year_range"]["from"], source["year_range"]["to"] + 1))
        try:
            manifest_path, manifest = Collector(source, years, args.max_pages, args.include_attachments, args.max_bytes).collect(args.allow_unavailable_robots)
            outputs.append({"source_id": source["id"], "manifest": str(manifest_path), "summary": manifest["summary"]})
        except Exception as exc:
            outputs.append({"source_id": source["id"], "error": f"{type(exc).__name__}: {exc}"})
            exit_code = 1
    print(json.dumps(outputs, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
