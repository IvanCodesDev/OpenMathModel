"""论文导出执行面（ADR-0012 阶段 A）：Tectonic 编译 .tex → PDF。

开发链沿 RunnerThread 模式在 API 进程内消费队列，目标态随执行面迁往
backend/worker，接口契约不变。子进程隔离：``--untrusted``（禁 shell-escape）、
独立临时工作目录、超时强杀；未安装 Tectonic 时任务落 UNSUPPORTED，不假装成功。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from sqlalchemy import select

from omm_contracts import ArtifactKind, ArtifactStatus, PaperExportStatus

from .blobstore import ArtifactBlobStore
from .config import Settings
from .db import Database
from .events import append_event, lock_run
from .ids import new_id
from .orm import ArtifactRow, PaperExportRow
from .serialize import utcnow

logger = logging.getLogger("omm.paper_export")

UNSUPPORTED_HINT = (
    "服务端未安装 Tectonic 编译器，无法编译 PDF。安装 tectonic 并加入 PATH，"
    "或设置 OMM_TECTONIC_PATH 指向可执行文件；离线部署需先联网预热宏包缓存。"
    "在此之前可改用「导出 LaTeX (.tex)」本机编译。"
)


def find_tectonic(settings: Settings) -> Optional[str]:
    """定位 Tectonic 可执行文件：配置项优先，否则探测 PATH。"""
    if settings.tectonic_path:
        path = Path(settings.tectonic_path)
        return str(path) if path.is_file() else None
    return shutil.which("tectonic")


@dataclass
class CompileResult:
    ok: bool
    pdf: bytes = field(default=b"", repr=False)
    log_tail: str = ""


def run_tectonic(tectonic: str, source_tex: bytes, timeout_seconds: float) -> CompileResult:
    """在独立临时目录内编译一份 .tex；超时由 subprocess 强杀子进程。"""
    with TemporaryDirectory(prefix="omm-paper-") as workdir:
        (Path(workdir) / "main.tex").write_bytes(source_tex)
        try:
            proc = subprocess.run(
                [tectonic, "--untrusted", "--chatter", "minimal", "main.tex"],
                cwd=workdir,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return CompileResult(ok=False, log_tail=f"编译超时（{timeout_seconds:.0f} 秒），已终止")
        except OSError as exc:
            return CompileResult(ok=False, log_tail=f"编译器无法启动：{exc}")
        log = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace").strip()
        pdf_path = Path(workdir) / "main.pdf"
        if proc.returncode != 0 or not pdf_path.exists():
            return CompileResult(ok=False, log_tail=log or "编译失败且没有日志输出")
        return CompileResult(ok=True, pdf=pdf_path.read_bytes(), log_tail=log)


class PaperExportProcessor:
    """对单个导出任务执行一次完整处理；后台线程与测试共用（镜像 WorkflowAdvancer）。"""

    def __init__(self, db: Database, settings: Settings, blobs: ArtifactBlobStore) -> None:
        self._db = db
        self._settings = settings
        self._blobs = blobs

    def pending_ids(self) -> list[str]:
        session = self._db.session_factory()
        try:
            rows = session.execute(
                select(PaperExportRow.id)
                .where(PaperExportRow.status == PaperExportStatus.QUEUED.value)
                .order_by(PaperExportRow.created_at.asc())
            ).scalars()
            return list(rows)
        finally:
            session.close()

    def process(self, export_id: str) -> Optional[str]:
        """处理一个导出并返回终态；非 QUEUED 的任务原样返回当前状态（幂等）。"""
        session = self._db.session_factory()
        try:
            row = session.get(PaperExportRow, export_id)
            if row is None:
                return None
            if row.status != PaperExportStatus.QUEUED.value:
                return row.status
            row.status = PaperExportStatus.RUNNING.value
            row.started_at = utcnow()
            # 先提交 RUNNING：编译最长两分钟，期间 GET 轮询要能看到真实状态
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        status, artifact_id, detail = self._compile(export_id)

        session = self._db.session_factory()
        try:
            row = session.get(PaperExportRow, export_id)
            if row is None:
                return None
            row.status = status
            row.artifact_id = artifact_id
            row.detail = detail[:500] if detail else None
            row.ended_at = utcnow()
            if row.run_id:
                # 沿 run 事件流原位通知工作台；事件序列在行锁保护下分配
                lock_run(session, row.run_id)
                append_event(
                    session,
                    row.run_id,
                    "paper.export.finished",
                    {"export_id": row.id, "status": status, "artifact_id": artifact_id},
                )
            session.commit()
            return status
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _compile(self, export_id: str) -> tuple[str, Optional[str], Optional[str]]:
        """编译并落 PDF 产物，返回 (status, artifact_id, detail)。

        源内容按 export 行登记的 sha256 从 blobstore 取回；编译在会话之外执行，
        不占数据库连接。
        """
        session = self._db.session_factory()
        try:
            row = session.get(PaperExportRow, export_id)
            if row is None:
                return PaperExportStatus.FAILED.value, None, "导出记录不存在"
            source_sha256 = row.source_sha256
            source_artifact_id = row.source_artifact_id
            project_id = row.project_id
            run_id = row.run_id
            source_name = None
            if source_artifact_id:
                source = session.get(ArtifactRow, source_artifact_id)
                if source is not None:
                    source_name = source.name
        finally:
            session.close()

        tectonic = find_tectonic(self._settings)
        if tectonic is None:
            return PaperExportStatus.UNSUPPORTED.value, None, UNSUPPORTED_HINT

        handle = self._blobs.open(source_sha256) if source_sha256 else None
        if handle is None:
            return PaperExportStatus.FAILED.value, None, "tex 源内容对象缺失，无法编译"
        with handle:
            source_tex = handle.read()

        result = run_tectonic(tectonic, source_tex, self._settings.paper_export_timeout_seconds)
        if not result.ok:
            # detail 只留日志尾部（契约上限 500 字），完整日志进服务端日志
            logger.warning("paper export %s failed: %s", export_id, result.log_tail[-2000:])
            return PaperExportStatus.FAILED.value, None, result.log_tail[-500:]

        sha256, size = self._blobs.put(result.pdf)
        pdf_name = f"{Path(source_name).stem}.pdf" if source_name else "论文导出.pdf"
        session = self._db.session_factory()
        try:
            artifact = ArtifactRow(
                id=new_id("art"),
                project_id=project_id,
                run_id=run_id,
                kind=ArtifactKind.paper.value,
                name=pdf_name,
                uri=f"local://{sha256}",
                sha256=sha256,
                size_bytes=size,
                media_type="application/pdf",
                producer_step=None,
                inputs=[source_artifact_id] if source_artifact_id else [],
                status=ArtifactStatus.READY.value,
                created_at=utcnow(),
            )
            session.add(artifact)
            session.commit()
            return PaperExportStatus.READY.value, artifact.id, None
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class PaperExportThread:
    """后台编译消费线程：周期性处理排队中的导出（镜像 RunnerThread）。"""

    def __init__(self, processor: PaperExportProcessor, settings: Settings) -> None:
        self._processor = processor
        self._interval = settings.paper_export_poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="omm-paper-export", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for export_id in self._processor.pending_ids():
                    if self._stop.is_set():
                        break
                    self._processor.process(export_id)
            except Exception:  # 单个任务失败不允许杀死线程
                logger.exception("paper export tick failed")
            self._stop.wait(self._interval)
