"""设置中心「数据与隐私」的服务端实现：设置存储、任务保留与文件缓存清理。

设置面：users.privacy_settings 存九个面板项（privacy_settings_of 解析，缺键回落
默认值）。通知与本机历史开关的行为闸门在前端，但设置本体存服务端：换浏览器或
清缓存后打开设置面板即可回填，与 max_concurrent_runs / usage_settings 的裁决一致。

清理面：「任务保留时间」与「文件缓存」由 RetentionThread 周期执行：

- 任务保留：删除已结束（COMPLETED/FAILED/CANCELLED）且超过保留期的 TaskRun
  及其步骤、事件、审批、领域事件与产物行；产物二进制在库内无其他引用时一并
  从内容存储删除。「任务完成后删除」保留 1 小时缓冲——用户刚看完成果页就抽走
  数据的体验不可接受，缓冲后由下一轮清扫收走。
- 文件缓存：删除超期的附件正文抽取缓存（artifact_texts）。缓存按需重建，删除
  只影响下次读取的耗时，不丢任何原始文件。

运行中/排队/等待审批的任务永不清理；llm_usage_records 是审计历史，不随任务删除；
项目级附件（run_id 为空）不属于任务保留范围。
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from omm_contracts import TERMINAL_TASK_RUN_STATUSES

from .blobstore import LocalContentStore
from .config import Settings
from .db import Database
from .models import User, utcnow
from .orm import (
    AgentEventRow,
    ApprovalRequestRow,
    ArtifactRow,
    ArtifactTextRow,
    DomainEventRow,
    ProjectRow,
    StepRunRow,
    TaskRunRow,
)

logger = logging.getLogger("omm.privacy")

#: privacy_settings 缺省值：与设置面板的初始开关状态一一对应。
DEFAULT_PRIVACY_SETTINGS: dict = {
    "save_history": True,
    "local_first": True,
    "model_training": False,
    "retention": "forever",
    "file_cache": "days_30",
    "notify_task_done": True,
    "notify_budget": True,
    "notify_security": True,
    "email_digest": False,
}

#: 任务保留策略 → 天数；forever 不清理，on_complete 走小时级缓冲。
RETENTION_DAYS: dict[str, int] = {"days_90": 90, "days_30": 30}

#: 文件缓存策略 → 天数；on_close 表示任务结束后即可清理该任务的抽取缓存。
FILE_CACHE_DAYS: dict[str, int] = {"days_30": 30, "days_7": 7}

#: 「任务完成后删除」的缓冲：完成后先留 1 小时给用户看成果页。
ON_COMPLETE_GRACE = timedelta(hours=1)

_TERMINAL_STATUSES = tuple(status.value for status in TERMINAL_TASK_RUN_STATUSES)


def privacy_settings_of(user: User) -> dict:
    """用户的隐私设置：未设置的键回落缺省值（历史行可能只存了部分键）。"""
    stored = user.privacy_settings if isinstance(user.privacy_settings, dict) else {}
    merged = dict(DEFAULT_PRIVACY_SETTINGS)
    merged.update({k: stored[k] for k in DEFAULT_PRIVACY_SETTINGS if k in stored})
    return merged


# ── 清扫实现 ────────────────────────────────────────────────────────────────


def _expired_run_ids(session: Session, user: User, retention: str) -> list[str]:
    """按保留策略挑出该用户已结束且超期的 run id；运行中的任务永不入选。"""
    if retention in RETENTION_DAYS:
        cutoff = utcnow() - timedelta(days=RETENTION_DAYS[retention])
    elif retention == "on_complete":
        cutoff = utcnow() - ON_COMPLETE_GRACE
    else:
        return []

    rows = session.execute(
        select(TaskRunRow.id, TaskRunRow.ended_at, TaskRunRow.updated_at)
        .join(ProjectRow, ProjectRow.id == TaskRunRow.project_id)
        .where(
            ProjectRow.owner == user.id,
            TaskRunRow.status.in_(_TERMINAL_STATUSES),
        )
    ).all()
    # ended_at 理应存在；历史数据缺失时退回 updated_at，避免永远清不掉。
    return [run_id for run_id, ended_at, updated_at in rows if (ended_at or updated_at) < cutoff]


def _delete_artifacts(
    session: Session,
    artifact_rows: list,
    blobs: Optional[LocalContentStore],
) -> None:
    """删除一批产物行（(id, sha256) 元组）及正文缓存；内容对象无引用后回收。"""
    artifact_ids = [row_id for row_id, _ in artifact_rows]
    candidate_digests = {sha for _, sha in artifact_rows if sha}
    if artifact_ids:
        session.execute(
            delete(ArtifactTextRow).where(ArtifactTextRow.artifact_id.in_(artifact_ids))
        )
        session.execute(delete(ArtifactRow).where(ArtifactRow.id.in_(artifact_ids)))
        session.flush()
    # 内容寻址存储天然去重：只删库内已无任何 Artifact 引用的对象。
    if blobs is not None and candidate_digests:
        still_referenced = set(
            session.scalars(
                select(ArtifactRow.sha256).where(ArtifactRow.sha256.in_(candidate_digests))
            )
        )
        for digest in candidate_digests - still_referenced:
            blobs.delete(digest)


def _delete_runs(session: Session, run_ids: list[str], blobs: Optional[LocalContentStore]) -> int:
    """删除一批 run 及其全部从属行；返回删除的 run 数。调用方负责 commit。"""
    if not run_ids:
        return 0

    artifact_rows = session.execute(
        select(ArtifactRow.id, ArtifactRow.sha256).where(ArtifactRow.run_id.in_(run_ids))
    ).all()
    _delete_artifacts(session, list(artifact_rows), blobs)
    for model in (ApprovalRequestRow, AgentEventRow, StepRunRow, DomainEventRow):
        session.execute(delete(model).where(model.run_id.in_(run_ids)))
    session.execute(delete(TaskRunRow).where(TaskRunRow.id.in_(run_ids)))
    session.flush()
    return len(run_ids)


def purge_project(
    session: Session,
    project: ProjectRow,
    blobs: Optional[LocalContentStore] = None,
) -> dict:
    """删除项目及其全部运行、产物与内容对象（侧栏「删除任务」的执行体）。

    llm_usage_records 是审计历史，保留不动。调用方负责 commit。
    """
    run_ids = list(
        session.scalars(select(TaskRunRow.id).where(TaskRunRow.project_id == project.id))
    )
    deleted_runs = _delete_runs(session, run_ids, blobs)
    # 项目级附件（run_id 为空）不属于任何 run，此处一并清理。
    leftover = session.execute(
        select(ArtifactRow.id, ArtifactRow.sha256).where(ArtifactRow.project_id == project.id)
    ).all()
    _delete_artifacts(session, list(leftover), blobs)
    session.delete(project)
    session.flush()
    return {"runs": deleted_runs, "artifacts": len(leftover)}


def _sweep_file_cache(session: Session, user: User, file_cache: str) -> int:
    """按文件缓存策略删除该用户的附件正文抽取缓存；返回删除行数。"""
    owned_artifacts = (
        select(ArtifactRow.id)
        .join(ProjectRow, ProjectRow.id == ArtifactRow.project_id)
        .where(ProjectRow.owner == user.id)
    )
    if file_cache in FILE_CACHE_DAYS:
        cutoff = utcnow() - timedelta(days=FILE_CACHE_DAYS[file_cache])
        statement = delete(ArtifactTextRow).where(
            ArtifactTextRow.artifact_id.in_(owned_artifacts),
            ArtifactTextRow.created_at < cutoff,
        )
    elif file_cache == "on_close":
        closed_artifacts = (
            select(ArtifactRow.id)
            .join(ProjectRow, ProjectRow.id == ArtifactRow.project_id)
            .join(TaskRunRow, TaskRunRow.id == ArtifactRow.run_id)
            .where(
                ProjectRow.owner == user.id,
                TaskRunRow.status.in_(_TERMINAL_STATUSES),
            )
        )
        statement = delete(ArtifactTextRow).where(ArtifactTextRow.artifact_id.in_(closed_artifacts))
    else:
        return 0
    result = session.execute(statement)
    return int(result.rowcount or 0)


def run_retention_sweep(session: Session, blobs: Optional[LocalContentStore] = None) -> dict:
    """对全部用户执行一轮保留清扫；返回 {runs, texts} 删除计数。调用方负责 commit。"""
    deleted_runs = 0
    deleted_texts = 0
    for user in session.scalars(select(User).where(User.privacy_settings.is_not(None))):
        settings = privacy_settings_of(user)
        deleted_runs += _delete_runs(
            session, _expired_run_ids(session, user, str(settings["retention"])), blobs
        )
        deleted_texts += _sweep_file_cache(session, user, str(settings["file_cache"]))
    return {"runs": deleted_runs, "texts": deleted_texts}


class RetentionThread:
    """后台清扫线程：周期执行任务保留与文件缓存策略（与 RunnerThread 同构）。

    首轮清扫延后一个周期：进程刚启动时用户多半正要继续上次的工作，
    先让页面恢复，再做清理。
    """

    def __init__(self, db: Database, settings: Settings, blobs: LocalContentStore) -> None:
        self._db = db
        self._blobs = blobs
        self._interval = settings.retention_sweep_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="omm-retention-sweep", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _sweep_once(self) -> None:
        session = self._db.session_factory()
        try:
            counts = run_retention_sweep(session, self._blobs)
            session.commit()
            if counts["runs"] or counts["texts"]:
                logger.info(
                    "retention sweep: removed %s runs, %s cached texts",
                    counts["runs"],
                    counts["texts"],
                )
        except Exception:  # 清扫失败不允许杀死线程
            session.rollback()
            logger.exception("retention sweep failed")
        finally:
            session.close()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._sweep_once()
