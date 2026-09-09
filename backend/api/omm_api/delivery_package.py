"""交付包导出（DeliveryManifest 写侧第一步）：把成果清单打成用户能拿走的 zip。

读侧投影（``stage_outputs._delivery_manifest``）已经把「文件 + 哈希 + 一致性结果」算出来了；
这里只做打包：``manifest.json``（DeliveryManifest 契约 JSON 原样）、``SHA256SUMS``（GNU
coreutils 格式，逐文件）、``README.txt``（中英对照：题目 / run id / 交付状态 / 五项检查 /
生成时间 / 核验方法）、``package.json``（包级事实：收录了哪些文件、哪些产物因内容与登记哈希
不一致或不可读而被排除、核验计数）、``verification-report.md``（打包时逐产物核验的人可读报告，
中英对照）以及 ``files/<kind>/<name>`` 下的产物本体。

两条纪律：**逐文件重算 sha256 与登记值核对**，对不上的不进包、如实记在 ``package.json`` 与
校验报告（交付物不能混进一个内容漂移过的文件）；**可复现打包**——同一份 manifest 与内容两次
打包字节相同（zip 条目时间戳统一取 manifest 的 ``updated_at``，条目按固定顺序写入，README 与
报告只由 manifest / 内容字节 / 该时间戳生成）。
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from omm_contracts import DeliveryManifest

__all__ = ["DeliveryPackage", "build_delivery_package", "package_filename"]

#: 2 = 多了 ``verification-report.md``、README 中英对照、``package.json`` 多 ``verification`` / ``readme_languages``。
PACKAGE_FORMAT_VERSION = 2
_FILES_DIR = "files"
REPORT_FILE = "verification-report.md"
README_LANGUAGES = ("zh-CN", "en")
_STATUS_LABELS = {
    "not_ready": "未就绪",
    "pending_confirmation": "待确认交付",
    "confirmed": "已确认交付",
    "returned_for_revision": "已退回修改",
    "unattended": "无人值守（未经 G4 确认）",
}
# 英文措辞与前端 en-US 词表逐字一致（成果页交付记录分页），两处口径不分叉。
_STATUS_LABELS_EN = {
    "not_ready": "Not delivered",
    "pending_confirmation": "Awaiting delivery confirmation",
    "confirmed": "Delivery confirmed",
    "returned_for_revision": "Returned for revision",
    "unattended": "Published without a final-draft gate (G4)",
}
_CHECK_LABELS_EN = {
    "paper_artifact_ready": "Paper draft artifact readable and hash verified",
    "audit_clean": "Final-draft audit has no findings",
    "figures_delivered": "Every inserted figure has a downloadable artifact",
    "metrics_in_paper": "Experiment metrics appear in the paper",
    "validation_reported": "Validation verdict present",
}
_FINDING_KIND_LABELS = {
    "unsourced_number": ("无出处数值", "unsourced numbers"),
    "phantom_figure": ("图引用不实", "phantom figure references"),
    "phantom_table": ("表引用不实", "phantom table references"),
    "unverified_citation": ("引用未经验证", "unverified citations"),
}
# 逐产物核验结论四态：(package.json 里的 reason / 报告里的中文 / 英文)
_VERDICT_VERIFIED = ("verified", "通过，已收录", "Verified, included")
_VERDICT_MISMATCH = ("内容与登记哈希不一致", "哈希不一致，未收录", "Hash mismatch, excluded")
_VERDICT_UNREADABLE = ("内容对象不可读", "内容不可读，未收录", "Unreadable, excluded")
_VERDICT_NOT_READY = ("产物没有可下载的内容对象（status 非 READY 或内容缺失）", "未就绪，未收录", "Not ready, skipped")


@dataclass(frozen=True)
class DeliveryPackage:
    filename: str
    content: bytes
    #: 收录的产物文件（zip 内路径，按写入顺序）。
    included: tuple[str, ...] = ()
    #: 被排除的产物：{artifact_id, name, reason}。
    integrity_failures: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def package_filename(run_id: str) -> str:
    return f"delivery-{run_id[:16]}.zip"


def _parse_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime(1980, 1, 1, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # zip 的 DOS 时间戳从 1980 起、精度 2 秒
    return max(parsed, datetime(1980, 1, 1, tzinfo=timezone.utc))


def _zip_datetime(stamp: datetime) -> tuple[int, int, int, int, int, int]:
    local = stamp.astimezone(timezone.utc)
    return (local.year, local.month, local.day, local.hour, local.minute, local.second - local.second % 2)


def _plain(value: Any) -> Any:
    """契约生成的 RootModel（RunId / Timestamp）→ 原值；其余原样。"""
    return getattr(value, "root", value)


def _enum_value(value: Any) -> str:
    """契约生成的 pydantic 枚举 → 字符串值（str(Enum) 会给 `Kind.paper`）。"""
    return str(getattr(value, "value", value))


def _safe_name(name: str) -> str:
    cleaned = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = "".join(ch if ch not in '<>:"|?*' else "_" for ch in cleaned)
    return cleaned or "artifact"


def _utc(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_label_en(check: Any) -> str:
    return _CHECK_LABELS_EN.get(_enum_value(check.id), str(check.label))


def _readme(manifest: DeliveryManifest, generated_at: datetime, included: int, failures: int) -> str:
    """README.txt：中文段 + 英文段，同一组事实；``detail`` / 备注是后端事实文本，原样不译。"""
    delivery = manifest.delivery
    generated = _utc(generated_at)
    lines = [
        "OpenMathModel 交付包",
        "",
        f"题目：{manifest.problem_title or '（未命名）'}",
        f"运行 ID：{_plain(manifest.run_id)}",
        f"生成时间：{generated}",
        "",
    ]
    if manifest.paper_citation is not None:
        lines.append(f"论文题目：{manifest.paper_citation.title}")
    if delivery is not None:
        status = _enum_value(delivery.status)
        lines.append(f"交付状态：{_STATUS_LABELS.get(status, status)}")
        if delivery.confirmed_at:
            lines.append(f"确认时间：{_plain(delivery.confirmed_at)}")
        if delivery.comment:
            lines.append(f"备注：{delivery.comment}")
        lines.append("")
        lines.append("一致性检查：")
        for check in delivery.checks:
            mark = "通过" if check.passed else "未过"
            lines.append(f"- [{mark}] {check.label}：{check.detail}")
    lines.extend([
        "",
        f"文件：{included} 个已收录（files/ 目录，逐文件哈希见 SHA256SUMS）；{failures} 个因内容与登记哈希不一致或不可读而未收录（见 package.json）。",
        "manifest.json 是成果清单（delivery-manifest.v1 契约），SHA256SUMS 可用 `sha256sum -c SHA256SUMS` 核验；",
        f"逐产物核验结论（登记哈希 / 实际哈希 / 是否收录）见 {REPORT_FILE}。",
        "",
        "-" * 40,
        "",
        "OpenMathModel delivery package",
        "",
        f"Problem: {manifest.problem_title or '(untitled)'}",
        f"Run ID: {_plain(manifest.run_id)}",
        f"Generated at: {generated}",
        "",
    ])
    if manifest.paper_citation is not None:
        lines.append(f"Paper title: {manifest.paper_citation.title}")
    if delivery is not None:
        status = _enum_value(delivery.status)
        lines.append(f"Delivery status: {_STATUS_LABELS_EN.get(status, status)}")
        if delivery.confirmed_at:
            lines.append(f"Confirmed at: {_plain(delivery.confirmed_at)}")
        if delivery.comment:
            lines.append(f"Comment: {delivery.comment}")
        lines.append("")
        lines.append("Consistency checks:")
        for check in delivery.checks:
            mark = "PASS" if check.passed else "FAIL"
            lines.append(f"- [{mark}] {_check_label_en(check)}: {check.detail}")
    lines.extend([
        "",
        f"Files: {included} included under files/ (per-file hashes in SHA256SUMS); "
        f"{failures} excluded because the content did not match the registered hash or could not be read (see package.json).",
        "manifest.json is the delivery manifest (delivery-manifest.v1 contract). Verify the files with "
        "`sha256sum -c SHA256SUMS` (Linux / macOS) or `Get-FileHash -Algorithm SHA256 <file>` (Windows PowerShell).",
        f"Per-artifact verification (registered vs. actual hash, included or not) is in {REPORT_FILE}.",
        "",
    ])
    return "\n".join(lines)


def _finding_kinds(by_kind: Mapping[str, Any]) -> tuple[str, str]:
    """``findings_by_kind`` → （中文列举, 英文列举），只列 > 0 的类别，固定按契约顺序、未知 kind 排后原样。"""
    known = [kind for kind in _FINDING_KIND_LABELS if int(by_kind.get(kind, 0) or 0) > 0]
    unknown = sorted(kind for kind, count in by_kind.items() if kind not in _FINDING_KIND_LABELS and int(count or 0) > 0)
    zh = "、".join(f"{_FINDING_KIND_LABELS[k][0]} {int(by_kind[k])} 处" for k in known)
    en = ", ".join(f"{int(by_kind[k])} {_FINDING_KIND_LABELS[k][1]}" for k in known)
    if unknown:
        extra_zh = "、".join(f"{k} {int(by_kind[k])} 处" for k in unknown)
        extra_en = ", ".join(f"{int(by_kind[k])} {k}" for k in unknown)
        zh = f"{zh}、{extra_zh}" if zh else extra_zh
        en = f"{en}, {extra_en}" if en else extra_en
    return zh or "无", en or "none"


def _md_cell(value: Any) -> str:
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|").strip() or "—"


def _verification_report(
    manifest: DeliveryManifest,
    generated_at: datetime,
    rows: list[dict[str, Any]],
) -> str:
    """verification-report.md：打包时逐产物核验的人可读版（中英对照）。

    ``rows`` 是打包循环按产物顺序记下的核验事实（name / kind / artifact_id / registered / actual /
    verdict / path），报告只排版、不再判定。
    """
    delivery = manifest.delivery
    verified = sum(1 for row in rows if row["verdict"] is _VERDICT_VERIFIED)
    mismatched = sum(1 for row in rows if row["verdict"] is _VERDICT_MISMATCH)
    unreadable = sum(1 for row in rows if row["verdict"] is _VERDICT_UNREADABLE)
    not_ready = sum(1 for row in rows if row["verdict"] is _VERDICT_NOT_READY)
    downloadable = verified + mismatched + unreadable
    generated = _utc(generated_at)

    lines = [
        "# 交付包校验报告 / Delivery package verification report",
        "",
        f"- 运行 ID / Run ID: `{_plain(manifest.run_id)}`",
        f"- 题目 / Problem: {manifest.problem_title or '（未命名）/ (untitled)'}",
        f"- 生成时间 / Generated at: {generated}",
        f"- 成果清单更新时间 / Manifest updated at: {_plain(manifest.updated_at)}",
    ]
    if delivery is not None:
        status = _enum_value(delivery.status)
        lines.append(
            f"- 交付状态 / Delivery status: {_STATUS_LABELS.get(status, status)} / {_STATUS_LABELS_EN.get(status, status)}"
        )
    if downloadable == 0:
        verdict_zh = "成果清单里没有可下载的产物，files/ 为空"
        verdict_en = "The manifest lists no downloadable artifacts; files/ is empty"
    elif mismatched == 0 and unreadable == 0:
        verdict_zh = f"全部 {downloadable} 个可下载产物哈希核验通过，已收录"
        verdict_en = f"All {downloadable} downloadable artifacts passed hash verification and were included"
    else:
        verdict_zh = (
            f"{downloadable} 个可下载产物中 {verified} 个哈希核验通过并收录；"
            f"{mismatched} 个内容与登记哈希不一致、{unreadable} 个内容不可读，均未收录"
        )
        verdict_en = (
            f"{verified} of {downloadable} downloadable artifacts passed hash verification and were included; "
            f"{mismatched} mismatched the registered hash and {unreadable} could not be read — none of those were included"
        )
    if not_ready:
        verdict_zh += f"；另有 {not_ready} 个产物未就绪（无可下载内容），跳过"
        verdict_en += f"; {not_ready} more artifact(s) were not ready (no downloadable content) and were skipped"
    lines.extend([
        f"- 结论 / Verdict: {verdict_zh} / {verdict_en}",
        "",
        "## 逐产物核验 / Per-artifact verification",
        "",
        "| 产物 / Artifact | 类型 / Kind | 登记哈希 / Registered SHA-256 | 实际哈希 / Actual SHA-256 | 结论 / Result |",
        "|---|---|---|---|---|",
    ])
    for row in rows:
        _, zh, en = row["verdict"]
        result = f"{zh} `{row['path']}` / {en}" if row["path"] else f"{zh} / {en}"
        lines.append(
            f"| {_md_cell(row['name'])} | {_md_cell(row['kind'])} | {_md_cell(row['registered'])} | "
            f"{_md_cell(row['actual'])} | {result} |"
        )
    if not rows:
        lines.append("| — | — | — | — | 成果清单为空 / Manifest has no artifacts |")

    if delivery is not None:
        lines.extend(["", "## 一致性检查 / Consistency checks", ""])
        if delivery.checks:
            for check in delivery.checks:
                mark = "通过 / PASS" if check.passed else "未过 / FAIL"
                lines.append(f"- [{mark}] {check.label} / {_check_label_en(check)} — {check.detail}")
        else:
            lines.append("- 无 / none")
        lines.extend(["", "## 终稿审计 / Final-draft audit", ""])
        audit = delivery.audit
        if audit is None:
            lines.append("- 论文未做终稿审计（旧运行）/ The paper was not audited (legacy run)")
        else:
            kinds_zh, kinds_en = _finding_kinds(dict(audit.findings_by_kind or {}))
            lines.extend([
                f"- 审计发现 / Findings: {audit.findings_total}（{kinds_zh}）/ {audit.findings_total} ({kinds_en})",
                f"- 冻结数字 / Frozen numbers: {audit.frozen_numbers_total}",
                f"- 图件 / Figures: {audit.figures_total}（已插入 {audit.figures_inserted}）/ {audit.figures_total} ({audit.figures_inserted} inserted)",
                f"- 文献 / References: {audit.references_total}（已引用 {audit.references_cited}）/ {audit.references_total} ({audit.references_cited} cited)",
            ])
        lines.append(
            f"- 清单口径 / Manifest counts: 产物 {delivery.files_total}、可下载 {delivery.files_ready}、带登记哈希 {delivery.files_hashed} "
            f"/ {delivery.files_total} artifacts, {delivery.files_ready} downloadable, {delivery.files_hashed} with a registered hash"
        )

    lines.extend([
        "",
        "## 如何复核 / How to re-verify",
        "",
        "- Linux / macOS: `sha256sum -c SHA256SUMS`",
        "- Windows PowerShell: `Get-FileHash -Algorithm SHA256 files\\<kind>\\<file>`，与 SHA256SUMS 中对应行比对 / compare with the matching line in SHA256SUMS",
        "- `manifest.json` 是成果清单（delivery-manifest.v1 契约），`package.json` 是机器可读的收录 / 排除记录 "
        "/ `manifest.json` is the delivery manifest (delivery-manifest.v1 contract); `package.json` is the machine-readable inclusion / exclusion record",
        "- 本报告由代码在打包时按登记哈希与实际字节生成，只核文件是否原样、不评内容质量；不经模型 "
        "/ This report is generated by code at packaging time from the registered hashes and the actual bytes; it verifies that files are intact and does not judge content quality. No model is involved.",
        "",
    ])
    return "\n".join(lines)


def build_delivery_package(
    manifest: DeliveryManifest,
    read_content: Callable[[str], Optional[bytes]],
    generated_at: datetime | None = None,
    file_names: Mapping[str, str] | None = None,
) -> DeliveryPackage:
    """成果清单 + 内容读取回调 → 交付包（内存 zip）。

    ``read_content(artifact_id)`` 返回产物字节；None 表示没有可读的内容对象（登记为 READY 却读不到
    也算）。只收录 ``download_url`` 非空的产物；内容 sha256 与登记值不一致的不进包。
    ``file_names`` 给每个产物在包里的文件名（登记名可能是「建模论文草稿」这样的展示名，真实文件名
    在内容 URI 尾部，由调用方给）；缺省用清单里的 name。
    """
    names = dict(file_names or {})
    stamp = generated_at or _parse_timestamp(_plain(manifest.updated_at))
    zip_time = _zip_datetime(stamp)
    payload = manifest.model_dump(mode="json")

    entries: list[tuple[str, bytes]] = []
    sums: list[str] = []
    files: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []
    used_paths: set[str] = set()

    for artifact in sorted(manifest.artifacts, key=lambda item: (_enum_value(item.kind), str(item.name), str(item.id))):
        info: dict[str, Any] = {"artifact_id": artifact.id, "name": artifact.name, "kind": _enum_value(artifact.kind)}
        registered = str(artifact.sha256 or "").lower()
        row: dict[str, Any] = {**info, "registered": registered or None, "actual": None, "path": None}
        report_rows.append(row)
        if not artifact.download_url:
            row["verdict"] = _VERDICT_NOT_READY
            skipped.append({**info, "reason": _VERDICT_NOT_READY[0]})
            continue
        content = read_content(artifact.id)
        if content is None:
            row["verdict"] = _VERDICT_UNREADABLE
            failures.append({**info, "reason": _VERDICT_UNREADABLE[0]})
            continue
        digest = hashlib.sha256(content).hexdigest()
        row["actual"] = digest
        if registered and digest != registered:
            row["verdict"] = _VERDICT_MISMATCH
            failures.append({
                **info,
                "reason": _VERDICT_MISMATCH[0],
                "registered_sha256": registered,
                "actual_sha256": digest,
            })
            continue
        file_name = _safe_name(names.get(artifact.id) or artifact.name)
        path = f"{_FILES_DIR}/{_enum_value(artifact.kind)}/{file_name}"
        if path in used_paths:
            path = f"{_FILES_DIR}/{_enum_value(artifact.kind)}/{artifact.id[:8]}_{file_name}"
        used_paths.add(path)
        row["verdict"] = _VERDICT_VERIFIED
        row["path"] = path
        entries.append((path, content))
        sums.append(f"{digest}  {path}")
        files.append({
            **info,
            "path": path,
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": artifact.media_type,
        })

    package_info = {
        "format_version": PACKAGE_FORMAT_VERSION,
        "run_id": str(_plain(manifest.run_id)),
        "generated_at": _utc(stamp),
        "manifest_updated_at": _plain(manifest.updated_at),
        "delivery_status": (
            _enum_value(manifest.delivery.status) if manifest.delivery is not None else None
        ),
        "readme_languages": list(README_LANGUAGES),
        "verification": {
            "report": REPORT_FILE,
            "verified": len(files),
            "mismatched": sum(1 for row in report_rows if row["verdict"] is _VERDICT_MISMATCH),
            "unreadable": sum(1 for row in report_rows if row["verdict"] is _VERDICT_UNREADABLE),
            "not_ready": len(skipped),
        },
        "files": files,
        "integrity_failures": failures,
        "skipped_not_ready": skipped,
    }
    readme = _readme(manifest, stamp, len(files), len(failures))
    report = _verification_report(manifest, stamp, report_rows)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        def write(name: str, data: bytes) -> None:
            entry = zipfile.ZipInfo(name, date_time=zip_time)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)

        write("README.txt", readme.encode("utf-8"))
        write("manifest.json", (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        write("package.json", (json.dumps(package_info, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        write("SHA256SUMS", ("\n".join(sums) + ("\n" if sums else "")).encode("utf-8"))
        write(REPORT_FILE, report.encode("utf-8"))
        for path, content in entries:
            write(path, content)

    return DeliveryPackage(
        filename=package_filename(str(_plain(manifest.run_id))),
        content=buffer.getvalue(),
        included=tuple(path for path, _ in entries),
        integrity_failures=tuple(failures),
    )


def summarize_package(package: DeliveryPackage) -> Mapping[str, Any]:
    """响应头 / 日志用的包摘要。"""
    return {
        "files": len(package.included),
        "integrity_failures": len(package.integrity_failures),
        "bytes": len(package.content),
        "sha256": package.sha256,
    }
