"""服务端托管对话轮（ADR-0016）：后台生成、附着回放、暂停、落库与隐私联动。

上游一律用 httpx.MockTransport 模拟（经 omm_api.llm._transport_factory 注入），
流式正文用可阻塞的字节迭代器，精确控制「半截时停下 / 断开后继续」的时序。
GET /events 由 TestClient 缓冲到终态才返回，天然是等待生成结束的同步点。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from omm_api import llm as llm_module
from omm_api.chat_turns import INTERRUPTED, RUNNING
from omm_api.orm import ChatTurnRow, LlmUsageRow

MESSAGES = [{"role": "user", "content": "请给出问题一的建模思路"}]
SCOPE = "run_" + "0" * 32
CHAT_SCOPE = "chat_" + "1" * 32
#: 运行控制判定请求的识别特征（与 test_run_control 一致：判定提示词开头的自述）
JUDGE_MARKER = "运行控制判定器"


def _chunk(text: str) -> bytes:
    return f'data: {json.dumps({"choices": [{"delta": {"content": text}}]})}\n\n'.encode()


def _reasoning_chunk(text: str) -> bytes:
    return f'data: {json.dumps({"choices": [{"delta": {"reasoning_content": text}}]})}\n\n'.encode()


DONE = b"data: [DONE]\n\n"


def _install_transport(monkeypatch, handler) -> None:
    """上游 mock。任务页的每轮对话前会先出一次运行控制判定（ADR-0018/0019，非流式）：
    这里统一答 none，让 handler 只服务对话调用本身——否则判定会把阻塞流的前几段吃掉。"""

    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        if any(JUDGE_MARKER in str(m.get("content", "")) for m in body.get("messages", [])):
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"action":"none"}'}}]})
        return handler(request)

    monkeypatch.setattr(llm_module, "_transport_factory", lambda: httpx.MockTransport(route))


def _sse_response(chunks) -> httpx.Response:
    return httpx.Response(200, content=chunks, headers={"content-type": "text/event-stream"})


def _save_config(client) -> None:
    body = {
        "endpoints": [
            {
                "name": "主接口",
                "protocol": "openai",
                "base_url": "https://gateway.test/v1",
                "api_key": "sk-main",
                "model": "gpt-test",
            }
        ]
    }
    response = client.put("/api/account/llm-config", json=body)
    assert response.status_code == 200, response.text


def _own_run(client, make_run) -> str:
    return make_run(goal="托管对话轮测试")["id"]


def _events(text: str) -> list[dict]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


def _start(client, scope: str, **extra) -> dict:
    response = client.post(
        "/api/chat/turns",
        json={"messages": MESSAGES, "scope_id": scope, "text": MESSAGES[0]["content"], **extra},
    )
    assert response.status_code == 202, response.text
    return response.json()["turn"]


def _wait_terminal(client, turn_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turn = client.get(f"/api/chat/turns/{turn_id}").json()["turn"]
        if turn["status"] != RUNNING:
            return turn
        time.sleep(0.02)
    raise AssertionError(f"turn {turn_id} 未在 {timeout}s 内到终态")


class BlockingStream:
    """按段放行的上游正文：测试先拿到前几段，再决定何时放出后面的。"""

    def __init__(self, segments: list[list[bytes]]) -> None:
        self.segments = segments
        self.gates = [threading.Event() for _ in segments]
        self.gates[0].set()
        self.closed = threading.Event()

    def release(self, index: int) -> None:
        self.gates[index].set()

    def release_all(self) -> None:
        for gate in self.gates:
            gate.set()

    def __iter__(self):
        try:
            for gate, chunks in zip(self.gates, self.segments):
                gate.wait(timeout=10)
                yield from chunks
        finally:
            self.closed.set()


# ── 基本生命周期 ──────────────────────────────────────────────────────────────


def test_turn_runs_in_background_and_persists(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    _install_transport(
        monkeypatch,
        lambda request: _sse_response([_reasoning_chunk("想一想"), _chunk("先"), _chunk("建模"), DONE]),
    )
    _save_config(client)

    turn = _start(client, run_id, attachments=["赛题.pdf"])
    assert turn["scope_id"] == run_id and turn["opening"] is False
    assert turn["text"] == MESSAGES[0]["content"] and turn["attachments"] == ["赛题.pdf"]
    assert turn["persisted"] is True

    stream = client.get(f"/api/chat/turns/{turn['id']}/events?after=0")
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = _events(stream.text)
    kinds = [event["type"] for event in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert "reasoning" in kinds and "delta" in kinds
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1)), "seq 连续递增"

    final = _wait_terminal(client, turn["id"])
    assert final["status"] == "completed"
    assert final["reply"] == "先建模" and final["reasoning"] == "想一想"
    assert final["meta"]["endpoint"] == "主接口" and final["meta"]["host"] == "gateway.test"
    assert final["last_seq"] == len(events)

    listed = client.get(f"/api/chat/scopes/{run_id}/turns").json()["items"]
    assert [item["id"] for item in listed] == [turn["id"]]

    with client.app.state.db.session_factory() as session:
        row = session.get(ChatTurnRow, turn["id"])
        assert row is not None and row.status == "completed"
        assert row.reply == "先建模" and row.reasoning == "想一想"
        assert row.attachments == ["赛题.pdf"]

    with client.app.state.db.session_factory() as session:
        chat_usage = session.scalars(select(LlmUsageRow).where(LlmUsageRow.source == "chat")).all()
        assert len(chat_usage) == 1, "流式完成照常记一条用量"


def test_generation_continues_without_any_listener(client, make_run, monkeypatch):
    """核心诉求：页面走了（没人附着）生成也要跑完，回来时拿到完整回复。"""
    run_id = _own_run(client, make_run)
    stream = BlockingStream([[_chunk("上半")], [_chunk("下半"), DONE]])
    _install_transport(monkeypatch, lambda request: _sse_response(stream))
    _save_config(client)

    turn = _start(client, run_id)
    # 没有任何 SSE 观众；上游继续吐流。
    stream.release(1)
    final = _wait_terminal(client, turn["id"])
    assert final["status"] == "completed" and final["reply"] == "上半下半"

    # 迟到的附着：整段回放后收尾。
    events = _events(client.get(f"/api/chat/turns/{turn['id']}/events").text)
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "上半下半"
    assert events[-1]["type"] == "done"


def test_attach_with_after_replays_only_newer_events(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    stream = BlockingStream([[_chunk("一"), _chunk("二")], [_chunk("三"), DONE]])
    _install_transport(monkeypatch, lambda request: _sse_response(stream))
    _save_config(client)

    turn = _start(client, run_id)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/chat/turns/{turn['id']}").json()["turn"]
        if snapshot["reply"] == "一二":
            break
        time.sleep(0.02)
    assert snapshot["status"] == RUNNING and snapshot["reply"] == "一二"
    assert snapshot["last_seq"] == 3, "meta + 两段正文"

    stream.release(1)
    events = _events(client.get(f"/api/chat/turns/{turn['id']}/events?after={snapshot['last_seq']}").text)
    assert [event["seq"] for event in events] == [4, 5]
    assert events[0] == {"type": "delta", "text": "三", "seq": 4}
    assert events[1]["type"] == "done"


def test_stop_freezes_partial_reply_and_closes_upstream(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    stream = BlockingStream([[_chunk("已经写了一半")], [_chunk("不该出现"), DONE]])
    _install_transport(monkeypatch, lambda request: _sse_response(stream))
    _save_config(client)

    turn = _start(client, run_id)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/chat/turns/{turn['id']}").json()["turn"]["reply"] == "":
        time.sleep(0.02)

    stopped = client.post(f"/api/chat/turns/{turn['id']}/stop").json()["turn"]
    assert stopped["status"] == "stopped" and stopped["reply"] == "已经写了一半"
    assert stopped["meta"]["stopped"] is True

    events = _events(client.get(f"/api/chat/turns/{turn['id']}/events").text)
    assert events[-1] == {"type": "done", "stopped": True, "seq": events[-1]["seq"]}

    # 放出后续正文：工作线程看到已终态即退出并关上游，正文不再追加。
    stream.release(1)
    assert stream.closed.wait(timeout=5), "上游连接应被关闭"
    time.sleep(0.05)
    again = client.get(f"/api/chat/turns/{turn['id']}").json()["turn"]
    assert again["reply"] == "已经写了一半" and again["status"] == "stopped"

    with client.app.state.db.session_factory() as session:
        row = session.get(ChatTurnRow, turn["id"])
        assert row.status == "stopped" and row.reply == "已经写了一半"


def test_stop_before_any_output_is_generation_stopped_error(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    stream = BlockingStream([[], [_chunk("迟到"), DONE]])
    _install_transport(monkeypatch, lambda request: _sse_response(stream))
    _save_config(client)

    turn = _start(client, run_id)
    stopped = client.post(f"/api/chat/turns/{turn['id']}/stop").json()["turn"]
    assert stopped["status"] == "stopped" and stopped["reply"] == ""
    assert stopped["error"]["code"] == "GENERATION_STOPPED"
    events = _events(client.get(f"/api/chat/turns/{turn['id']}/events").text)
    assert events[-1]["type"] == "error" and events[-1]["code"] == "GENERATION_STOPPED"
    stream.release_all()


def test_upstream_error_before_output_marks_failed(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    _install_transport(monkeypatch, lambda request: httpx.Response(500, json={"error": "boom"}))
    _save_config(client)

    turn = _start(client, run_id)
    final = _wait_terminal(client, turn["id"])
    assert final["status"] == "failed" and final["reply"] == ""
    assert final["error"]["code"]
    events = _events(client.get(f"/api/chat/turns/{turn['id']}/events").text)
    assert events[-1]["type"] == "error" and events[-1]["code"] == final["error"]["code"]


def test_second_turn_in_same_scope_while_running_is_409(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    stream = BlockingStream([[_chunk("慢")], [DONE]])
    _install_transport(monkeypatch, lambda request: _sse_response(stream))
    _save_config(client)

    _start(client, run_id)
    response = client.post(
        "/api/chat/turns", json={"messages": MESSAGES, "scope_id": run_id, "text": "再问一个"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "CHAT_TURN_IN_PROGRESS"
    stream.release_all()


def test_opening_turn_has_no_user_text(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    _install_transport(monkeypatch, lambda request: _sse_response([_chunk("开场分析"), DONE]))
    _save_config(client)

    turn = _start(client, run_id, opening=True)
    assert turn["opening"] is True and turn["text"] == ""
    final = _wait_terminal(client, turn["id"])
    assert final["reply"] == "开场分析"


def test_trace_patch_roundtrip(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    _install_transport(monkeypatch, lambda request: _sse_response([_chunk("好"), DONE]))
    _save_config(client)

    turn = _start(client, run_id)
    _wait_terminal(client, turn["id"])
    trace = [{"title": "已解析并注入附件 ×1", "detail": "赛题.pdf", "elapsed_ms": 120}]
    patched = client.patch(f"/api/chat/turns/{turn['id']}", json={"trace": trace}).json()["turn"]
    assert patched["trace"] == trace
    listed = client.get(f"/api/chat/scopes/{run_id}/turns").json()["items"]
    assert listed[0]["trace"] == trace
    with client.app.state.db.session_factory() as session:
        assert session.get(ChatTurnRow, turn["id"]).trace == trace


# ── 归属与隔离 ────────────────────────────────────────────────────────────────


def test_run_scope_must_be_owned(client, monkeypatch):
    _install_transport(monkeypatch, lambda request: _sse_response([_chunk("x"), DONE]))
    _save_config(client)
    response = client.post(
        "/api/chat/turns", json={"messages": MESSAGES, "scope_id": SCOPE, "text": "谁的任务"}
    )
    assert response.status_code == 404
    assert client.get(f"/api/chat/scopes/{SCOPE}/turns").status_code == 404


def test_chat_scope_isolated_per_user(client, second_client, monkeypatch):
    from conftest import register_user

    _install_transport(monkeypatch, lambda request: _sse_response([_chunk("我的"), DONE]))
    _save_config(client)
    turn = _start(client, CHAT_SCOPE)
    _wait_terminal(client, turn["id"])

    register_user(second_client, "other-user@test.dev")
    assert second_client.get(f"/api/chat/scopes/{CHAT_SCOPE}/turns").json()["items"] == []
    assert second_client.get(f"/api/chat/turns/{turn['id']}").status_code == 404
    assert second_client.post(f"/api/chat/turns/{turn['id']}/stop").status_code == 404


def test_invalid_scope_id_is_422(client):
    response = client.post("/api/chat/turns", json={"messages": MESSAGES, "scope_id": "bogus", "text": "x"})
    assert response.status_code == 422


# ── 隐私与删除 ────────────────────────────────────────────────────────────────


def _privacy(client, **overrides) -> None:
    body = {
        "save_history": True,
        "local_first": True,
        "model_training": False,
        "retention": "forever",
        "file_cache": "days_30",
        "notify_task_done": True,
        "notify_budget": True,
        "notify_security": True,
        "email_digest": False,
        **overrides,
    }
    response = client.put("/api/account/privacy-settings", json=body)
    assert response.status_code == 200, response.text


def test_history_off_keeps_turn_in_memory_only(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    _install_transport(monkeypatch, lambda request: _sse_response([_chunk("不落盘"), DONE]))
    _save_config(client)
    _privacy(client, save_history=False)

    turn = _start(client, run_id)
    assert turn["persisted"] is False
    final = _wait_terminal(client, turn["id"])
    assert final["reply"] == "不落盘"
    # 重进仍能拿到（内存缓冲期内）……
    listed = client.get(f"/api/chat/scopes/{run_id}/turns").json()["items"]
    assert [item["id"] for item in listed] == [turn["id"]]
    # ……但库里没有任何一行。
    with client.app.state.db.session_factory() as session:
        assert session.scalars(select(ChatTurnRow)).all() == []


def test_turning_history_off_deletes_saved_turns(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    _install_transport(monkeypatch, lambda request: _sse_response([_chunk("会被清"), DONE]))
    _save_config(client)
    turn = _start(client, run_id)
    _wait_terminal(client, turn["id"])

    _privacy(client, save_history=False)
    assert client.get(f"/api/chat/scopes/{run_id}/turns").json()["items"] == []
    with client.app.state.db.session_factory() as session:
        assert session.get(ChatTurnRow, turn["id"]) is None


def test_delete_scope_stops_running_turn_and_clears_rows(client, make_run, monkeypatch):
    run_id = _own_run(client, make_run)
    stream = BlockingStream([[_chunk("半")], [DONE]])
    _install_transport(monkeypatch, lambda request: _sse_response(stream))
    _save_config(client)
    turn = _start(client, run_id)

    deleted = client.delete(f"/api/chat/scopes/{run_id}").json()["deleted"]
    assert deleted == 1
    assert client.get(f"/api/chat/scopes/{run_id}/turns").json()["items"] == []
    assert client.get(f"/api/chat/turns/{turn['id']}").status_code == 404
    stream.release_all()


def test_deleting_project_removes_run_chat_turns(client, make_run, monkeypatch):
    run = make_run(goal="随项目删除")
    _install_transport(monkeypatch, lambda request: _sse_response([_chunk("好"), DONE]))
    _save_config(client)
    turn = _start(client, run["id"])
    _wait_terminal(client, turn["id"])

    assert client.delete(f"/api/v1/projects/{run['project_id']}").status_code == 204
    with client.app.state.db.session_factory() as session:
        assert session.get(ChatTurnRow, turn["id"]) is None


# ── 进程重启 ──────────────────────────────────────────────────────────────────


def test_recover_marks_leftover_running_rows_interrupted(client, make_run):
    run_id = _own_run(client, make_run)
    now = datetime.now(timezone.utc)
    with client.app.state.db.session_factory() as session:
        me = client.get("/api/account/me").json()["user"]["id"]
        session.add(
            ChatTurnRow(
                id="cturn_" + "f" * 32,
                user_id=me,
                scope_id=run_id,
                status=RUNNING,
                opening=False,
                text="重启前的提问",
                reply="写到一半",
                reasoning="",
                meta={},
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    assert client.app.state.chat_turns.recover_interrupted() == 1
    turn = client.get("/api/chat/turns/cturn_" + "f" * 32).json()["turn"]
    assert turn["status"] == INTERRUPTED and turn["reply"] == "写到一半"
    assert turn["error"]["code"] == "GENERATION_INTERRUPTED"
    events = _events(client.get("/api/chat/turns/cturn_" + "f" * 32 + "/events").text)
    assert events == [{"type": "error", "code": "GENERATION_INTERRUPTED", "message": turn["error"]["message"]}]
