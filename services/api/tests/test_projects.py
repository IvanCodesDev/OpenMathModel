from __future__ import annotations


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
