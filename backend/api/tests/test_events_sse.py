from __future__ import annotations


def _run_to_completion(client, make_run, tick) -> str:
    run = make_run()
    run_id = run["id"]
    tick(run_id, times=4)
    client.post(f"/api/v1/task-runs/{run_id}/actions", json={"action": "approve"})
    tick(run_id, times=4)
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "COMPLETED"
    return run_id


def _read_sse_blocks(response) -> list[dict]:
    """把 SSE 流解析为 [{id, event, data}]；读到 stream.end 停止。"""
    blocks: list[dict] = []
    current: dict = {}
    for line in response.iter_lines():
        if line == "":
            if current:
                blocks.append(current)
                if current.get("event") == "stream.end":
                    break
                current = {}
            continue
        if line.startswith(":"):
            continue  # 心跳注释
        key, _, value = line.partition(":")
        current[key.strip()] = value.strip()
    return blocks


def test_event_sequences_monotonic_without_gaps(
    client, make_run, tick, validate_contract
):
    run_id = _run_to_completion(client, make_run, tick)
    items = client.get(f"/api/v1/task-runs/{run_id}/events/history").json()["items"]
    sequences = [event["sequence"] for event in items]
    assert sequences == list(range(1, len(sequences) + 1))

    types = {event["type"] for event in items}
    assert "run.created" in types
    assert "run.status_changed" in types
    assert "approval.requested" in types
    assert "artifact.published" in types
    validate_contract("agent-event.schema.json", items[0])


def test_history_supports_after_filter(client, make_run, tick):
    run_id = _run_to_completion(client, make_run, tick)
    all_items = client.get(f"/api/v1/task-runs/{run_id}/events/history").json()["items"]
    last = all_items[-1]["sequence"]

    partial = client.get(
        f"/api/v1/task-runs/{run_id}/events/history", params={"after": last - 3}
    ).json()["items"]
    assert [event["sequence"] for event in partial] == [last - 2, last - 1, last]


def test_sse_replays_history_and_ends_on_terminal(client, make_run, tick):
    run_id = _run_to_completion(client, make_run, tick)
    total = len(client.get(f"/api/v1/task-runs/{run_id}/events/history").json()["items"])

    with client.stream("GET", f"/api/v1/task-runs/{run_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        blocks = _read_sse_blocks(response)

    data_blocks = [b for b in blocks if b.get("event") != "stream.end"]
    assert len(data_blocks) == total
    assert blocks[-1]["event"] == "stream.end"
    assert data_blocks[0]["id"] == "1"
    assert data_blocks[0]["event"] == "run.created"


def test_sse_resumes_from_last_event_id_header(client, make_run, tick):
    run_id = _run_to_completion(client, make_run, tick)
    items = client.get(f"/api/v1/task-runs/{run_id}/events/history").json()["items"]
    last = items[-1]["sequence"]

    with client.stream(
        "GET",
        f"/api/v1/task-runs/{run_id}/events",
        headers={"Last-Event-ID": str(last - 2)},
    ) as response:
        blocks = _read_sse_blocks(response)

    data_blocks = [b for b in blocks if b.get("event") != "stream.end"]
    assert [b["id"] for b in data_blocks] == [str(last - 1), str(last)]


def test_events_for_unknown_run(client):
    response = client.get("/api/v1/task-runs/run_missing000000/events/history")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
