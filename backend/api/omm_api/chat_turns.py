"""服务端托管对话轮（ADR-0016）：对话生成不随页面生死。

旧 /api/chat 是无状态代理流——浏览器一断连（切任务、刷新、关标签页），服务端
的上游请求随之取消，半截回复连同提问一起消失。这里把一轮对话变成**后台作业**：

- ``ChatTurnHub.start`` 建 ``chat_turns`` 行（「保存任务历史」关闭时只在内存），
  起一条守护线程消费 ``llm.stream_events``（含链内回退），事件按 seq 进内存缓冲，
  reply/reasoning 约每秒回写库，终态时定格。
- 页面经 ``ChatTurnHub.stream_live`` 附着直播：先回放 ``after`` 之后的缓冲，再阻塞
  等新事件；断开只影响观众，不影响生成。重进按 scope 拉全部轮，running 的轮拿到
  半截内容与 ``last_seq`` 原地续接。
- ``stop`` 是真正的「暂停生成」：立即定格 stopped 并向观众发终态事件，工作线程在
  下一个上游事件到来时退出并关掉上游连接。
- ``before_finish``（ADR-0020）：上游生成收尾之后、轮定格之前的一段收尾工作——对话即
  控制面把「回复结束后才执行的运行动作」挂在这里，产出的 ``action`` 事件排在终态事件
  之前。只在轮仍是 running 时调用：用户 stop 了就不再执行任何动作。
- 终态后缓冲保留 ``LIVE_RETENTION_S`` 供迟到的附着回放，之后只剩库里的定格文本；
  进程重启时遗留 running 的行标 interrupted（生成确实丢了，如实告知）。

约束：中枢是进程内状态，API 须单进程运行（与 RunnerThread 同一前提）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .db import Database
from .errors import ApiError
from .ids import new_id
from .orm import ChatTurnRow
from .serialize import iso_z

logger = logging.getLogger("omm.chat.turns")

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
STOPPED = "stopped"
INTERRUPTED = "interrupted"

#: 增量回写库的最小间隔（秒）：逐 token 写库既没必要也伤 SQLite 的单写位。
FLUSH_INTERVAL_S = 1.0
#: 终态后内存事件缓冲的保留时长（秒）。
LIVE_RETENTION_S = 10 * 60
#: 单轮事件缓冲上限：异常上游无限吐流时的内存保险丝。
MAX_EVENTS = 200_000
#: 进程重启时遗留 running 行的定格说明。
INTERRUPTED_MESSAGE = "服务重启，本轮生成中断；需要完整回答请重新发送"

EventProducer = Callable[[], Iterator[dict[str, Any]]]
DoneHook = Callable[[dict[str, Any], dict[str, Any]], None]
#: 收尾钩子：拿到上游的终态事件（done / error，含合成的），返回要排在终态之前的事件。
BeforeFinishHook = Callable[[dict[str, Any]], Iterable[dict[str, Any]]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LiveTurn:
    """一轮进行中（或刚结束、仍在缓冲期）的对话在内存里的全部状态。

    所有字段的读写都在 ``cond`` 锁内；``events`` 只追加，``seq`` = 下标 + 1。
    """

    def __init__(
        self,
        *,
        turn_id: str,
        user_id: str,
        scope_id: str,
        text: str,
        opening: bool,
        attachments: list[str],
        persist: bool,
    ) -> None:
        self.id = turn_id
        self.user_id = user_id
        self.scope_id = scope_id
        self.text = text
        self.opening = opening
        self.attachments = list(attachments)
        self.persist = persist
        self.status = RUNNING
        self.reply = ""
        self.reasoning = ""
        self.meta: dict[str, Any] = {}
        self.error: Optional[dict[str, str]] = None
        self.trace: Optional[list[dict[str, Any]]] = None
        self.feedback: Optional[str] = None
        self.created_at = _utcnow()
        self.updated_at = self.created_at
        self.ended_at: Optional[datetime] = None
        self.events: list[dict[str, Any]] = []
        self.stop_requested = False
        #: 终态时刻（monotonic），缓冲期计时起点；None = 仍在生成。
        self.retired_at: Optional[float] = None
        self.cond = threading.Condition()
        self.flush_lock = threading.Lock()

    # ── 锁内操作（调用方持有 cond） ──────────────────────────────────────

    def append_locked(self, event: dict[str, Any]) -> dict[str, Any]:
        stamped = {**event, "seq": len(self.events) + 1}
        self.events.append(stamped)
        self.updated_at = _utcnow()
        self.cond.notify_all()
        return stamped

    def finalize_locked(
        self,
        status: str,
        *,
        error: Optional[dict[str, str]] = None,
        terminal_event: dict[str, Any],
    ) -> bool:
        """第一次终态转换生效，之后的调用返回 False（stop 与工作线程可能同时到）。"""
        if self.status != RUNNING:
            return False
        self.status = status
        self.error = error
        self.ended_at = _utcnow()
        self.retired_at = time.monotonic()
        self.append_locked(terminal_event)
        return True

    def snapshot_locked(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope_id": self.scope_id,
            "status": self.status,
            "opening": self.opening,
            "text": self.text,
            "attachments": list(self.attachments),
            "reply": self.reply,
            "reasoning": self.reasoning,
            "meta": dict(self.meta),
            "error": dict(self.error) if self.error else None,
            "trace": list(self.trace) if self.trace else None,
            "feedback": self.feedback,
            # 附着直播的游标：视图里的 reply/reasoning 恰好包含前 last_seq 个事件。
            "last_seq": len(self.events),
            "live": True,
            "persisted": self.persist,
            "created_at": iso_z(self.created_at),
            "updated_at": iso_z(self.updated_at),
            "ended_at": iso_z(self.ended_at),
        }

    def snapshot(self) -> dict[str, Any]:
        with self.cond:
            return self.snapshot_locked()


def row_view(row: ChatTurnRow) -> dict[str, Any]:
    """库里定格的轮：没有事件缓冲，附着只会收到按状态合成的终态事件。"""
    return {
        "id": row.id,
        "scope_id": row.scope_id,
        "status": row.status,
        "opening": bool(row.opening),
        "text": row.text or "",
        "attachments": list(row.attachments or []),
        "reply": row.reply or "",
        "reasoning": row.reasoning or "",
        "meta": dict(row.meta or {}),
        "error": (
            {"code": row.error_code, "message": row.error_message or ""}
            if row.error_code
            else None
        ),
        "trace": list(row.trace) if row.trace else None,
        "feedback": row.feedback,
        "last_seq": 0,
        "live": False,
        "persisted": True,
        "created_at": iso_z(row.created_at),
        "updated_at": iso_z(row.updated_at),
        "ended_at": iso_z(row.ended_at),
    }


def terminal_event_for(view: dict[str, Any]) -> dict[str, Any]:
    """按定格状态合成终态事件：附着到一条已不在内存的轮时用它收尾。"""
    status = view["status"]
    meta = view.get("meta") or {}
    error = view.get("error") or {}
    if status == COMPLETED:
        event: dict[str, Any] = {"type": "done", "usage": meta.get("usage") or {}, "elapsed_ms": meta.get("elapsed_ms") or 0}
        if meta.get("error"):
            event.update({"partial": True, "error": meta["error"]})
        return event
    if status == STOPPED:
        if view.get("reply") or view.get("reasoning"):
            return {"type": "done", "stopped": True}
        return {"type": "error", "code": "GENERATION_STOPPED", "message": "已暂停生成"}
    if status == INTERRUPTED:
        return {
            "type": "error",
            "code": error.get("code") or "GENERATION_INTERRUPTED",
            "message": error.get("message") or INTERRUPTED_MESSAGE,
        }
    if status == RUNNING:
        # 库里 running 但内存没有：多进程部署或未走 recover_interrupted 的异常情形。
        return {"type": "error", "code": "GENERATION_INTERRUPTED", "message": INTERRUPTED_MESSAGE}
    return {
        "type": "error",
        "code": error.get("code") or "CHAT_FAILED",
        "message": error.get("message") or "回复生成失败",
    }


def sse_line(event: dict[str, Any]) -> str:
    seq = event.get("seq")
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}data: {json.dumps(event, ensure_ascii=False)}\n\n"


class ChatTurnHub:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._live: dict[str, LiveTurn] = {}

    # ── 启动恢复 / 退出 ───────────────────────────────────────────────────

    def recover_interrupted(self) -> int:
        """进程重启：上一进程里 running 的轮已经没人生成了，如实标 interrupted。"""
        now = _utcnow()
        with self._db.session_factory() as session:
            result = session.execute(
                update(ChatTurnRow)
                .where(ChatTurnRow.status == RUNNING)
                .values(
                    status=INTERRUPTED,
                    error_code="GENERATION_INTERRUPTED",
                    error_message=INTERRUPTED_MESSAGE,
                    ended_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            count = int(result.rowcount or 0)
        if count:
            logger.warning("chat turns: %d 条遗留 running 的轮标为 interrupted", count)
        return count

    def shutdown(self) -> None:
        """进程退出：正在生成的轮如实标 interrupted 并回写。"""
        with self._lock:
            lives = list(self._live.values())
        for live in lives:
            with live.cond:
                live.stop_requested = True
                if live.status == RUNNING:
                    error = {"code": "GENERATION_INTERRUPTED", "message": INTERRUPTED_MESSAGE}
                    live.finalize_locked(INTERRUPTED, error=error, terminal_event={"type": "error", **error})
            self._flush(live)

    # ── 发起 ──────────────────────────────────────────────────────────────

    def has_running(self, user_id: str, scope_id: str) -> bool:
        with self._lock:
            return any(
                live.user_id == user_id and live.scope_id == scope_id and live.status == RUNNING
                for live in self._live.values()
            )

    def start(
        self,
        *,
        user_id: str,
        scope_id: str,
        text: str,
        opening: bool,
        attachments: list[str],
        persist: bool,
        producer: EventProducer,
        on_done: Optional[DoneHook] = None,
        before_finish: Optional[BeforeFinishHook] = None,
    ) -> dict[str, Any]:
        live = LiveTurn(
            turn_id=new_id("cturn"),
            user_id=user_id,
            scope_id=scope_id,
            text=text,
            opening=opening,
            attachments=attachments,
            persist=persist,
        )
        if persist:
            with self._db.session_factory() as session:
                session.add(
                    ChatTurnRow(
                        id=live.id,
                        user_id=user_id,
                        scope_id=scope_id,
                        status=RUNNING,
                        opening=opening,
                        text=text,
                        attachments=list(attachments),
                        reply="",
                        reasoning="",
                        meta={},
                        created_at=live.created_at,
                        updated_at=live.updated_at,
                    )
                )
                session.commit()
        with self._lock:
            self._evict_retired_locked()
            self._live[live.id] = live
        worker = threading.Thread(
            target=self._run,
            args=(live, producer, on_done, before_finish),
            name=f"chat-turn-{live.id[-8:]}",
            daemon=True,
        )
        worker.start()
        return live.snapshot()

    def _run(
        self,
        live: LiveTurn,
        producer: EventProducer,
        on_done: Optional[DoneHook],
        before_finish: Optional[BeforeFinishHook] = None,
    ) -> None:
        iterator: Optional[Iterator[dict[str, Any]]] = None
        last_flush = time.monotonic()
        # 上游给出（或这里合成）的终态事件；None = 观众已 stop / 对话已删除，轮早已定格，
        # 不再有收尾工作——用户打断了这一轮，回复后才执行的动作随之作废。
        terminal: Optional[dict[str, Any]] = None
        try:
            iterator = iter(producer())
            for event in iterator:
                kind = event.get("type")
                if kind in ("done", "error"):
                    terminal = event
                    break
                with live.cond:
                    if live.status != RUNNING:
                        # 观众已 stop / 对话已删除：丢弃迟到的上游事件，退出并关连接。
                        break
                    if kind == "meta":
                        live.meta.update({k: v for k, v in event.items() if k != "type"})
                    elif kind == "action":
                        self._record_action_locked(live, event)
                    elif kind == "reasoning":
                        live.reasoning += str(event.get("text") or "")
                    elif kind == "delta":
                        live.reply += str(event.get("text") or "")
                    if len(live.events) >= MAX_EVENTS:
                        terminal = {"type": "error", "code": "CHAT_STREAM_TOO_LONG", "message": "回复事件过多，已截断"}
                        break
                    live.append_locked(event)
                if time.monotonic() - last_flush >= FLUSH_INTERVAL_S:
                    self._flush(live)
                    last_flush = time.monotonic()
            else:
                # 上游静默收尾（没有 done / error）：有正文按完成，否则按空回复失败。
                with live.cond:
                    if live.reply:
                        terminal = {"type": "done", "usage": {}, "elapsed_ms": 0}
                    else:
                        terminal = {"type": "error", "code": "EMPTY_REPLY", "message": "模型未返回内容"}
        except Exception as error:  # noqa: BLE001 - 任何异常都要定格成终态，观众不能永远等
            code = error.code if isinstance(error, ApiError) else "LLM_REQUEST_FAILED"
            message = error.message if isinstance(error, ApiError) else f"回复生成失败：{error}"
            logger.exception("chat turn %s 生成线程异常", live.id)
            terminal = {"type": "error", "code": code, "message": message}
        finally:
            if iterator is not None and hasattr(iterator, "close"):
                try:
                    iterator.close()  # type: ignore[union-attr]  生成器 GeneratorExit → 关上游连接
                except Exception:  # noqa: BLE001
                    logger.debug("chat turn %s 关闭上游迭代器失败", live.id, exc_info=True)
            if terminal is not None:
                self._finish(live, terminal, before_finish)
            self._flush(live)
            self._record_usage(live, on_done)

    @staticmethod
    def _record_action_locked(live: LiveTurn, event: dict[str, Any]) -> None:
        """运行控制回执（ADR-0018）：进 meta.actions 随轮落库，恢复时能重画轨迹行，
        下一轮据此知道有没有待确认的提案。"""
        actions = live.meta.setdefault("actions", [])
        actions.append({k: v for k, v in event.items() if k != "type"})

    def _finish(
        self,
        live: LiveTurn,
        terminal: dict[str, Any],
        before_finish: Optional[BeforeFinishHook],
    ) -> None:
        """上游收尾 → 收尾钩子 → 定格。钩子在锁外跑（它会读写数据库），且只在轮仍是
        running 时调用；钩子跑完若发现观众恰好在这几毫秒里 stop 了，动作已经发生，回执
        照样记进 meta（刷新页面能看到），只是不再改终态。"""
        tail: list[dict[str, Any]] = []
        if before_finish is not None:
            with live.cond:
                still_running = live.status == RUNNING
            if still_running:
                try:
                    tail = list(before_finish(terminal))
                except Exception:  # noqa: BLE001 - 收尾工作出错不能让轮悬着
                    logger.exception("chat turn %s 收尾钩子异常", live.id)
        with live.cond:
            for extra in tail:
                if extra.get("type") == "action":
                    self._record_action_locked(live, extra)
                live.append_locked(extra)
            if live.status != RUNNING:
                return
            if terminal.get("type") == "done":
                live.meta["usage"] = dict(terminal.get("usage") or {})
                live.meta["elapsed_ms"] = int(terminal.get("elapsed_ms") or 0)
                live.finalize_locked(COMPLETED, terminal_event=terminal)
            else:
                self._fail_locked(
                    live,
                    code=str(terminal.get("code") or "CHAT_FAILED"),
                    message=str(terminal.get("message") or "回复生成失败"),
                )

    @staticmethod
    def _fail_locked(live: LiveTurn, *, code: str, message: str) -> None:
        """出错定格：已有正文时正文照留、错误进 meta、按完成收口；否则按失败。"""
        error = {"code": code, "message": message}
        if live.reply:
            live.meta["error"] = error
            live.finalize_locked(COMPLETED, terminal_event={"type": "done", "partial": True, "error": error})
        else:
            live.finalize_locked(FAILED, error=error, terminal_event={"type": "error", **error})

    @staticmethod
    def _record_usage(live: LiveTurn, on_done: Optional[DoneHook]) -> None:
        if on_done is None:
            return
        with live.cond:
            if live.status != COMPLETED:
                return
            done_event = next((e for e in reversed(live.events) if e.get("type") == "done"), None)
            meta = dict(live.meta)
        if done_event is None or done_event.get("partial") or done_event.get("stopped"):
            return
        try:
            on_done(meta, done_event)
        except Exception:  # noqa: BLE001 - 记账绝不影响对话
            logger.exception("chat turn %s 用量记账失败", live.id)

    # ── 回写 ──────────────────────────────────────────────────────────────

    def _flush(self, live: LiveTurn) -> None:
        # 快照与写入在 flush_lock 内成对进行：工作线程与 stop() 同时回写时，
        # 后写的一定拿到更新的状态，不会被旧快照覆盖回 running。
        with live.flush_lock:
            with live.cond:
                if not live.persist:
                    return
                values = {
                    "status": live.status,
                    "reply": live.reply,
                    "reasoning": live.reasoning,
                    "meta": dict(live.meta),
                    "error_code": live.error["code"][:64] if live.error else None,
                    "error_message": live.error["message"][:2000] if live.error else None,
                    "trace": list(live.trace) if live.trace else None,
                    "feedback": live.feedback,
                    "updated_at": live.updated_at,
                    "ended_at": live.ended_at,
                }
            try:
                with self._db.session_factory() as session:
                    session.execute(update(ChatTurnRow).where(ChatTurnRow.id == live.id).values(**values))
                    session.commit()
            except Exception:  # noqa: BLE001 - 回写失败不打断生成，下一次再试
                logger.exception("chat turn %s 回写失败", live.id)

    # ── 读取 ──────────────────────────────────────────────────────────────

    def live_for(self, user_id: str, turn_id: str) -> Optional[LiveTurn]:
        with self._lock:
            live = self._live.get(turn_id)
        return live if live is not None and live.user_id == user_id else None

    def get(self, session: Session, user_id: str, turn_id: str) -> Optional[dict[str, Any]]:
        live = self.live_for(user_id, turn_id)
        if live is not None:
            return live.snapshot()
        row = session.get(ChatTurnRow, turn_id)
        if row is None or row.user_id != user_id:
            return None
        return row_view(row)

    def list_scope(self, session: Session, user_id: str, scope_id: str) -> list[dict[str, Any]]:
        """scope 下全部轮，按发起时间升序；内存里的覆盖库里滞后 ≤1s 的副本。"""
        rows = session.scalars(
            select(ChatTurnRow)
            .where(ChatTurnRow.user_id == user_id, ChatTurnRow.scope_id == scope_id)
            .order_by(ChatTurnRow.created_at, ChatTurnRow.id)
        ).all()
        views = {row.id: row_view(row) for row in rows}
        with self._lock:
            self._evict_retired_locked()
            lives = [
                live for live in self._live.values() if live.user_id == user_id and live.scope_id == scope_id
            ]
        for live in lives:
            views[live.id] = live.snapshot()
        return sorted(views.values(), key=lambda view: (view["created_at"], view["id"]))

    # ── 附着直播 ──────────────────────────────────────────────────────────

    @staticmethod
    def stream_live(live: LiveTurn, after: int, heartbeat_seconds: float) -> Iterator[str]:
        """回放 after 之后的缓冲并跟随直播；终态事件发完即收尾。

        after 超过缓冲长度（页面视图比缓冲还新，不可能；或参数伪造）时不会发
        任何事件就收尾，页面应回读视图确认状态。
        """
        cursor = max(0, after)
        while True:
            with live.cond:
                if cursor >= len(live.events) and live.status == RUNNING:
                    live.cond.wait(timeout=heartbeat_seconds)
                pending = live.events[cursor:]
                terminal = live.status != RUNNING
            for event in pending:
                yield sse_line(event)
                cursor = event["seq"]
            if terminal and cursor >= len(live.events):
                return
            if not pending:
                yield ": ping\n\n"

    # ── 停止 / 轨迹 / 删除 ────────────────────────────────────────────────

    def stop(self, session: Session, user_id: str, turn_id: str) -> Optional[dict[str, Any]]:
        live = self.live_for(user_id, turn_id)
        if live is None:
            return self.get(session, user_id, turn_id)
        with live.cond:
            live.stop_requested = True
            if live.status == RUNNING:
                if live.reply or live.reasoning:
                    live.meta["stopped"] = True
                    live.finalize_locked(STOPPED, terminal_event={"type": "done", "stopped": True})
                else:
                    error = {"code": "GENERATION_STOPPED", "message": "已暂停生成"}
                    live.finalize_locked(STOPPED, error=error, terminal_event={"type": "error", **error})
        self._flush(live)
        return live.snapshot()

    def set_trace(
        self, session: Session, user_id: str, turn_id: str, trace: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        live = self.live_for(user_id, turn_id)
        if live is not None:
            with live.cond:
                live.trace = list(trace)
                live.updated_at = _utcnow()
            self._flush(live)
            return live.snapshot()
        row = session.get(ChatTurnRow, turn_id)
        if row is None or row.user_id != user_id:
            return None
        row.trace = list(trace)
        row.updated_at = _utcnow()
        session.flush()
        return row_view(row)

    def set_feedback(
        self, session: Session, user_id: str, turn_id: str, feedback: Optional[str]
    ) -> Optional[dict[str, Any]]:
        """用户对一轮回复的评价（up / down / None 撤回）。

        与 set_trace 同一套落点：还在内存里的轮（生成中或刚结束的缓冲期）先改内存
        再回写，否则直接改库行。评价不参与生成，也不改 updated_at 之外的任何字段。
        """
        live = self.live_for(user_id, turn_id)
        if live is not None:
            with live.cond:
                live.feedback = feedback
                live.updated_at = _utcnow()
            self._flush(live)
            return live.snapshot()
        row = session.get(ChatTurnRow, turn_id)
        if row is None or row.user_id != user_id:
            return None
        row.feedback = feedback
        row.updated_at = _utcnow()
        session.flush()
        return row_view(row)

    def delete_scope(self, session: Session, user_id: str, scope_id: str) -> int:
        """删掉 scope 下全部轮：进行中的先停，库行一并清；返回删除的轮数。"""
        with self._lock:
            doomed = [
                live for live in self._live.values() if live.user_id == user_id and live.scope_id == scope_id
            ]
            for live in doomed:
                self._live.pop(live.id, None)
        memory_only = sum(1 for live in doomed if not live.persist)
        for live in doomed:
            with live.cond:
                live.stop_requested = True
                live.persist = False  # 行即将删除，工作线程不要再回写
                if live.status == RUNNING:
                    error = {"code": "GENERATION_STOPPED", "message": "对话已删除"}
                    live.finalize_locked(STOPPED, error=error, terminal_event={"type": "error", **error})
        result = session.execute(
            delete(ChatTurnRow).where(ChatTurnRow.user_id == user_id, ChatTurnRow.scope_id == scope_id)
        )
        session.flush()
        return int(result.rowcount or 0) + memory_only

    def delete_scopes(self, session: Session, user_id: str, scope_ids: list[str]) -> int:
        return sum(self.delete_scope(session, user_id, scope_id) for scope_id in scope_ids)

    def delete_user_turns(self, session: Session, user_id: str) -> int:
        """「保存任务历史」关闭时清空该用户全部对话记录（与前端清 localStorage 对齐）。"""
        with self._lock:
            scopes = sorted({live.scope_id for live in self._live.values() if live.user_id == user_id})
        count = self.delete_scopes(session, user_id, scopes)
        result = session.execute(delete(ChatTurnRow).where(ChatTurnRow.user_id == user_id))
        session.flush()
        return count + int(result.rowcount or 0)

    # ── 缓冲回收 ──────────────────────────────────────────────────────────

    def _evict_retired_locked(self) -> None:
        now = time.monotonic()
        for turn_id in [
            turn_id
            for turn_id, live in self._live.items()
            if live.retired_at is not None and now - live.retired_at >= LIVE_RETENTION_S
        ]:
            self._live.pop(turn_id, None)
