from __future__ import annotations

from conftest import API, create_run


def _force_run_status(client, run_id: str, status: str) -> None:
    """白盒设定运行状态：状态桶过滤测试不必跑完整个模拟工作流。"""
    from omm_api.orm import TaskRunRow

    with client.app.state.db.session_factory() as session:
        row = session.get(TaskRunRow, run_id)
        row.status = status
        session.commit()


def test_create_and_get_project(client, make_project, validate_contract):
    created = make_project("CUMCM 2024 A 题")
    validate_contract("project.schema.json", created)

    response = client.get(f"/api/v1/projects/{created['id']}")
    assert response.status_code == 200
    assert response.json()["name"] == "CUMCM 2024 A 题"
    assert response.headers.get("x-request-id", "").startswith("req_")


def test_create_project_validation_error_envelope(client, validate_contract):
    response = client.post("/api/v1/projects", json={"name": ""})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["request_id"].startswith("req_")
    validate_contract("error.schema.json", body)


def test_get_unknown_project_returns_error_envelope(client, validate_contract):
    response = client.get("/api/v1/projects/proj_doesnotexist00")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    validate_contract("error.schema.json", body)


def test_list_projects_pagination(client, make_project):
    for index in range(3):
        make_project(f"项目 {index}")
    response = client.get("/api/v1/projects", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_list_projects_include_stats(client, make_project, validate_contract):
    no_run = make_project("从未运行的项目")
    busy = make_project("有运行和产物的项目")
    create_run(client, busy["id"], goal="第一次基线运行", auto_start=False)
    latest = create_run(client, busy["id"], goal="第二次改进运行", auto_start=False)
    upload = client.post(
        f"{API}/projects/{busy['id']}/artifacts",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        data={"kind": "dataset"},
    )
    assert upload.status_code == 201, upload.text

    body = client.get(f"{API}/projects", params={"include": "stats"}).json()
    by_id = {item["id"]: item for item in body["items"]}

    assert by_id[no_run["id"]]["stats"] == {"latest_run": None, "artifact_count": 0}
    stats = by_id[busy["id"]]["stats"]
    assert stats["artifact_count"] == 1
    assert stats["latest_run"]["id"] == latest["id"]
    assert stats["latest_run"]["goal"] == "第二次改进运行"
    assert stats["latest_run"]["status"] == "QUEUED"
    validate_contract("project.schema.json", by_id[busy["id"]])
    validate_contract("project.schema.json", by_id[no_run["id"]])

    # 不带 include 时不计算 stats（null 或缺省，消费方按未提供处理）
    plain = client.get(f"{API}/projects").json()["items"]
    assert all(item.get("stats") is None for item in plain)


def test_list_projects_state_and_q_filters(client, make_project):
    plain = make_project("卫星轨道设计")
    active = make_project("城市水网优化")
    create_run(client, active["id"], goal="完成水网基线", auto_start=False)
    done = make_project("疫情推演复盘")
    finished = create_run(client, done["id"], goal="推演收尾", auto_start=False)
    _force_run_status(client, finished["id"], "COMPLETED")

    def listed(**params):
        return client.get(f"{API}/projects", params=params).json()

    act = listed(state="active")
    assert [item["id"] for item in act["items"]] == [active["id"]]
    assert act["total"] == 1

    fin = listed(state="done", include="stats")
    assert [item["id"] for item in fin["items"]] == [done["id"]]
    assert fin["items"][0]["stats"]["latest_run"]["status"] == "COMPLETED"

    # q 命中项目名 / 命中最新运行目标 / 不命中 / 通配符按字面处理
    assert [item["id"] for item in listed(q="卫星")["items"]] == [plain["id"]]
    assert [item["id"] for item in listed(q="水网基线")["items"]] == [active["id"]]
    assert listed(q="不存在的关键词")["total"] == 0
    assert listed(q="%")["total"] == 0


def test_list_projects_stats_pagination_total(client, make_project):
    for index in range(3):
        make_project(f"分页项目 {index}")
    body = client.get(f"{API}/projects", params={"include": "stats", "limit": 2}).json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert all(
        item["stats"] == {"latest_run": None, "artifact_count": 0}
        for item in body["items"]
    )


def test_list_projects_archived_with_stats(client, make_project):
    make_project("在列项目")
    target = make_project("归档项目")
    patched = client.patch(f"{API}/projects/{target['id']}", json={"archived": True})
    assert patched.status_code == 200, patched.text

    body = client.get(f"{API}/projects", params={"archived": "true", "include": "stats"}).json()
    assert [item["id"] for item in body["items"]] == [target["id"]]
    assert body["items"][0]["stats"] == {"latest_run": None, "artifact_count": 0}
