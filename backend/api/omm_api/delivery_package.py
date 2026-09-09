"""交付包导出（DeliveryManifest 写侧第一步）：把成果清单打成用户能拿走的 zip。

读侧投影（``stage_outputs._delivery_manifest``）已经把「文件 + 哈希 + 一致性结果」算出来了；
这里只做打包：``manifest.json``（DeliveryManifest 契约 JSON 原样）、``SHA256SUMS``（GNU
coreutils 格式，逐文件）、``README.txt``（题目 / run id / 交付状态 / 五项检查 / 生成时间）、
``package.json``（包级事实：收录了哪些文件、哪些产物因内容与登记哈希不一致或不可读而被排除）
以及 ``files/<kind>/<name>`` 下的产物本体。

两条纪律：**逐文件重算 sha256 与登记值核对**，对不上的不进包、如实记在 ``package.json``
（交付物不能混进一个内容漂移过的文件）；**可复现打包**——同一份 manifest 与内容两次打包字节
相同（zip 条目时间戳统一取 manifest 的 ``updated_at``，条目按固定顺序写入）。
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

PACKAGE_FORMAT_VERSION = 1
_FILES_DIR = "files"
_STATUS_LABELS = {
    "not_ready": "未就绪",
    "pending_confirmation": "待确认交付",
    "confirmed": "已确认交付",
    "returned_for_revision": "已退回修改",
    "unattended": "无人值守（未经 G4 确认）",
}


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


def _readme(manifest: DeliveryManifest, generated_at: datetime, included: int, failures: int) -> str:
    delivery = manifest.delivery
    lines = [
        "OpenMathModel 交付包",
        "",
        f"题目：{manifest.problem_title or '（未命名）'}",
        f"运行 ID：{_plain(manifest.run_id)}",
        f"生成时间：{generated_at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
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
        "manifest.json 是成果清单（delivery-manifest.v1 契约），SHA256SUMS 可用 `sha256sum -c SHA256SUMS` 核验。",
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
    used_paths: set[str] = set()

    for artifact in sorted(manifest.artifacts, key=lambda item: (_enum_value(item.kind), str(item.name), str(item.id))):
        info: dict[str, Any] = {"artifact_id": artifact.id, "name": artifact.name, "kind": _enum_value(artifact.kind)}
        if not artifact.download_url:
            skipped.append({**info, "reason": "产物没有可下载的内容对象（status 非 READY 或内容缺失）"})
            continue
        content = read_content(artifact.id)
        if content is None:
            failures.append({**info, "reason": "内容对象不可读"})
            continue
        digest = hashlib.sha256(content).hexdigest()
        registered = str(artifact.sha256 or "").lower()
        if registered and digest != registered:
            failures.append({
                **info,
                "reason": "内容与登记哈希不一致",
                "registered_sha256": registered,
                "actual_sha256": digest,
            })
            continue
        file_name = _safe_name(names.get(artifact.id) or artifact.name)
        path = f"{_FILES_DIR}/{_enum_value(artifact.kind)}/{file_name}"
        if path in used_paths:
            path = f"{_FILES_DIR}/{_enum_value(artifact.kind)}/{artifact.id[:8]}_{file_name}"
        used_paths.add(path)
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
        "generated_at": stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_updated_at": _plain(manifest.updated_at),
        "delivery_status": (
            _enum_value(manifest.delivery.status) if manifest.delivery is not None else None
        ),
        "files": files,
        "integrity_failures": failures,
        "skipped_not_ready": skipped,
    }
    readme = _readme(manifest, stamp, len(files), len(failures))

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
