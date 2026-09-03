"""五类页面正文投影端点：GET /v1/task-runs/{run_id}/stage-outputs。

正文来自 run_domain_events 的 STEP_SUCCEEDED 输出；真实链路复用
test_task_runs_llm_nodes 的 MockTransport 六阶段桩（同一份 stub 输出），
覆盖：全链跑完后五类正文可读且通过契约校验、未完成阶段的空值行为、
越权访问 404。
"""

from __future__ import annotations

from conftest import (
    API,
    approve_when_asked,
    confirm_delivery,
    create_project,
    create_run,
    register_user,
    run_status_is,
    wait_until,
)

from omm_api.stage_outputs import StageState, _robustness_report, _validation_report
from omm_contracts.v1.experiment_summary import ValidationReport
from test_task_runs_llm_nodes import (
    ANALYSIS_OUTPUT,
    EXPERIMENT_OUTPUT,
    PAPER_OUTPUT,
    PLANNING_OUTPUT,
    PREPARATION_OUTPUT,
    VALIDATION_OUTPUT,
    _configure_llm,
)


def _stage_outputs(client, run_id: str) -> dict:
    response = client.get(f"{API}/task-runs/{run_id}/stage-outputs")
    assert response.status_code == 200, response.text
    return response.json()


def test_stage_outputs_readable_after_full_llm_chain(client, monkeypatch, validate_contract):
    """全链真实节点跑完后，五类正文都应存在并通过各自契约校验。"""
    project = create_project(client)
    _configure_llm(client, monkeypatch)
    run = create_run(client, project["id"], goal="优化共享单车调度")

    approve_when_asked(client, run["id"], option_id="approve")
    confirm_delivery(client, run["id"])
    wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))

    payload = _stage_outputs(client, run["id"])
    assert payload["run_id"] == run["id"]

    problem_frame = payload["problem_frame"]
    validate_contract("problem-frame.schema.json", problem_frame)
    assert problem_frame["run_id"] == run["id"]
    assert problem_frame["title"] == ANALYSIS_OUTPUT["title"]
    assert problem_frame["objectives"] == ANALYSIS_OUTPUT["objectives"]
    assert problem_frame["subquestions"] == ANALYSIS_OUTPUT["subquestions"]
    assert "viability" not in problem_frame, "准入判定等过程字段不得进入投影"

    dataset_profile = payload["dataset_profile"]
    validate_contract("dataset-profile.schema.json", dataset_profile)
    assert dataset_profile["run_id"] == run["id"]
    assert dataset_profile["profile_summary"] == PREPARATION_OUTPUT["profile_summary"]
    assert dataset_profile["missing_value_strategy"] == PREPARATION_OUTPUT["missing_value_strategy"]
    assert dataset_profile["datasets"][0]["name"] == PREPARATION_OUTPUT["datasets"][0]["name"]

    plan_proposal = payload["plan_proposal"]
    validate_contract("plan-proposal.schema.json", plan_proposal)
    assert plan_proposal["recommended_plan_id"] == PLANNING_OUTPUT["recommended_plan_id"]
    assert len(plan_proposal["plans"]) == len(PLANNING_OUTPUT["plans"])
    assert "llm_attempts" not in plan_proposal, "过程杂项字段不得进入投影"

    experiment_summary = payload["experiment_summary"]
    validate_contract("experiment-summary.schema.json", experiment_summary)
    assert experiment_summary["approach_summary"] == EXPERIMENT_OUTPUT["approach_summary"]
    assert experiment_summary["metrics"] == {"rmse": 0.5}
    assert experiment_summary["validation"] is not None
    assert experiment_summary["validation"]["verdict"] == VALIDATION_OUTPUT["verdict"]
    assert experiment_summary["validation"]["validation_summary"] == VALIDATION_OUTPUT["validation_summary"]
    # 稳健性复跑在沙盒里真跑（stub 模型发出三项全过的检验脚本）：判定数字来自
    # 标记行，投影只带契约七键——过程字段留在活动流，不进正文契约
    robustness = experiment_summary["validation"]["robustness"]
    assert robustness is not None, "验证节点沙盒化后，全链里稳健性复跑应真实执行并进投影"
    assert robustness["executed"] is True and robustness["status"] == "passed"
    assert robustness["checks_total"] == 3 and robustness["checks_failed"] == 0
    assert [check["id"] for check in robustness["checks"]] == ["sensitivity", "bootstrap", "baseline"]
    assert all(check["passed"] is True for check in robustness["checks"])
    assert robustness["checks"][0] == {
        "id": "sensitivity",
        "name": "需求率扰动",
        "passed": True,
        "value": 0.05,
        "threshold": 0.2,
        "detail": "在阈值内",
    }
    assert robustness["summary_text"] == "沙盒复跑稳健性检查 3 项，通过 3 项，全部达标。"
    assert robustness["reason"] == ""
    for key in ("attempts", "llm_calls", "summary", "failed_checks", "final_code_artifact", "produced_artifacts"):
        assert key not in robustness, f"过程字段 {key} 不得进入投影"

    document_draft = payload["document_draft"]
    validate_contract("document-draft.schema.json", document_draft)
    assert document_draft["title"] == PAPER_OUTPUT["title"]
    assert document_draft["keywords"] == PAPER_OUTPUT["keywords"]
    assert document_draft["version"] == 1
    # H5 数字冻结：清单（值 + 出处）与终稿审计发现随草稿进契约投影
    frozen = {entry["id"]: entry for entry in document_draft["frozen_numbers"]}
    # 指标来自沙盒标记行（不在 stub 的 EXPERIMENT_OUTPUT 里），与上面投影断言同一口径
    assert frozen["metrics.rmse"]["value"] == 0.5
    assert frozen["metrics.rmse"]["source_stage"] == "EXPERIMENTING"
    assert frozen["metrics.rmse"]["source_path"] == "metrics.rmse"
    assert any(key.startswith("robustness.") for key in frozen), "稳健性复跑数值也冻结"
    assert document_draft["audit_findings"] == [], "桩章节只引用 rmse=0.5，审计应干净"

    delivery_manifest = payload["delivery_manifest"]
    validate_contract("delivery-manifest.schema.json", delivery_manifest)
    assert delivery_manifest["problem_title"] == ANALYSIS_OUTPUT["title"]
    assert delivery_manifest["key_metrics"] == {"rmse": 0.5}
    assert delivery_manifest["validation_verdict"] == VALIDATION_OUTPUT["verdict"]
    assert delivery_manifest["paper_citation"] is not None
    assert delivery_manifest["paper_citation"]["title"] == PAPER_OUTPUT["title"]
    paper_artifact_ids = {a["id"] for a in delivery_manifest["artifacts"] if a["kind"] == "paper"}
    assert paper_artifact_ids, "论文草稿应作为产物出现在成果清单里"
    assert delivery_manifest["paper_citation"]["artifact_id"] in paper_artifact_ids
    table_artifacts = [a for a in delivery_manifest["artifacts"] if a["kind"] == "table"]
    assert table_artifacts and table_artifacts[0]["producer_node"] == "EXPERIMENTING"


def test_stage_outputs_null_before_stage_completes(client, make_run, tick):
    """未开始的运行：五类正文全部为 null，不是 404（运行本身是存在的）。"""
    run = make_run("完成基线建模")

    empty = _stage_outputs(client, run["id"])
    assert empty["run_id"] == run["id"]
    for key in (
        "problem_frame",
        "dataset_profile",
        "plan_proposal",
        "experiment_summary",
        "document_draft",
        "delivery_manifest",
    ):
        assert empty[key] is None, f"{key} 应为 null"

    # 未配置自定义 API：sim 节点完成 PROBLEM_ANALYSIS，但产出不含 title 等契约字段，
    # 六类正文（含成果清单）仍应保持 null。
    assert tick(run["id"]) == "RUNNING"
    still_empty = _stage_outputs(client, run["id"])
    for key in (
        "problem_frame",
        "dataset_profile",
        "plan_proposal",
        "experiment_summary",
        "document_draft",
        "delivery_manifest",
    ):
        assert still_empty[key] is None, f"{key} 应仍为 null（sim 节点无契约字段）"


def test_stage_outputs_completed_sim_chain_returns_nulls_not_500(
    client, make_run, validate_contract
):
    """完整跑完的模拟链（未配置自定义 API 的默认链路）：

    sim 节点只产出 {"label": ...}，没有任何契约实质字段——四类正文必须保持
    null（而不是空值兜底对象：空 plans 违反 minItems=1、空 verdict 违反 enum，
    会把接口打成 500）；成果清单因存在真实产物而存在，正文性字段全为 null。
    """
    run = make_run("完成基线建模")
    approve_when_asked(client, run["id"], option_id="approve")
    wait_until(client, run["id"], run_status_is(client, run["id"], "COMPLETED"))

    payload = _stage_outputs(client, run["id"])
    for key in ("dataset_profile", "plan_proposal", "experiment_summary", "document_draft"):
        assert payload[key] is None, f"{key} 应为 null（sim 节点无契约字段）"

    manifest = payload["delivery_manifest"]
    assert manifest is not None, "完成的模拟运行有真实产物，成果清单应存在"
    validate_contract("delivery-manifest.schema.json", manifest)
    assert manifest["problem_title"] is None
    assert manifest["key_metrics"] is None, "sim 实验不是真实实验，指标应为 null 而非空对象"
    assert manifest["validation_verdict"] is None
    assert manifest["paper_citation"] is None, "sim 论文阶段无标题等契约字段，引用应为 null"
    kinds = sorted(a["kind"] for a in manifest["artifacts"])
    assert kinds == ["figure", "report"], "成果清单应列出模拟链的两个真实产物"


def _validation_with(robustness: dict | None) -> dict:
    return {
        "verdict": "pass",
        "checks": [],
        "risks": [],
        "validation_summary": "结果可信",
        "robustness": robustness,
    }


def test_robustness_projection_fills_unexecuted_shape():
    """节点如实降级为「仅判读」时只给 {executed, reason}：投影补齐契约七键。"""
    report = _robustness_report({"executed": False, "reason": "未配置工具端口，跳过稳健性复跑"})
    assert report == {
        "executed": False,
        "status": None,
        "summary_text": "",
        "checks": [],
        "checks_total": 0,
        "checks_failed": 0,
        "reason": "未配置工具端口，跳过稳健性复跑",
    }
    ValidationReport.model_validate(_validation_with(report))


def test_robustness_projection_strips_process_fields_and_recounts():
    """已执行形状带过程字段（契约 additionalProperties=false 会把接口打成 500）：
    投影剔除它们；畸形检查项剔除后计数重算，保住 checks_total == len(checks)。"""
    raw = {
        "executed": True,
        "status": "passed",
        "attempts": 2,
        "llm_calls": 3,
        "summary": "模型转述的总结",
        "failed_checks": [{"id": "sensitivity"}],
        "final_code_artifact": "art_" + "0" * 32,
        "produced_artifacts": ["checks.png"],
        "summary_text": "沙盒复跑稳健性检查 3 项，通过 2 项；未通过：需求率扰动（sensitivity：value 0.25，阈值 0.2）。",
        "checks": [
            {
                "id": "sensitivity",
                "name": "需求率扰动",
                "passed": False,
                "value": 0.25,
                "threshold": 0.2,
                "detail": "超出阈值",
            },
            # name 缺省回落 id；value 是 bool 不算数字；threshold 允许文字口径
            {"id": "baseline", "name": "", "passed": True, "value": True, "threshold": "≥ 0.1"},
            {"id": "", "passed": True, "value": 1},  # 缺 id → 剔除
            {"id": "bogus", "passed": "yes"},  # passed 非布尔 → 剔除
            "not-a-dict",
        ],
        "checks_total": 5,
        "checks_failed": 1,
    }
    report = _robustness_report(raw)
    assert set(report) == {
        "executed",
        "status",
        "summary_text",
        "checks",
        "checks_total",
        "checks_failed",
        "reason",
    }
    assert report["checks"] == [
        {
            "id": "sensitivity",
            "name": "需求率扰动",
            "passed": False,
            "value": 0.25,
            "threshold": 0.2,
            "detail": "超出阈值",
        },
        {
            "id": "baseline",
            "name": "baseline",
            "passed": True,
            "value": None,
            "threshold": "≥ 0.1",
            "detail": "",
        },
    ]
    assert report["checks_total"] == 2 and report["checks_failed"] == 1
    assert report["summary_text"] == raw["summary_text"]
    assert report["reason"] == ""
    ValidationReport.model_validate(_validation_with(report))


def test_robustness_projection_unfinished_sandbox_and_absent_field():
    """沙盒会话没跑成（status ≠ passed）：checks 为空、结论句如实说「未完成」；
    沙盒化之前的运行 / 模拟节点没有该键 → null，而不是编一个「未执行」。"""
    unfinished = _robustness_report(
        {
            "executed": True,
            "status": "failed",
            "attempts": 4,
            "checks": [],
            "checks_total": 0,
            "checks_failed": 0,
            "summary_text": "稳健性检查沙盒复跑未完成（failed），检验结论仅来自评审判读。",
        }
    )
    assert unfinished["executed"] is True and unfinished["status"] == "failed"
    assert unfinished["checks"] == [] and unfinished["checks_total"] == 0
    assert "未完成" in unfinished["summary_text"] and unfinished["reason"] == ""
    ValidationReport.model_validate(_validation_with(unfinished))

    assert _robustness_report(None) is None
    assert _robustness_report("garbage") is None

    legacy = StageState()
    legacy.outputs = {"verdict": "pass", "checks": [], "risks": [], "validation_summary": "旧运行"}
    assert _validation_report(legacy)["robustness"] is None
    ValidationReport.model_validate(_validation_report(legacy))


def test_stage_outputs_requires_ownership(client, second_client, make_run):
    """越权访问：他人任务一律 404，不泄露资源是否存在。"""
    run = make_run("归属校验")
    register_user(second_client, "stage-outputs-other@test.dev")

    response = second_client.get(f"{API}/task-runs/{run['id']}/stage-outputs")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"

    missing = client.get(f"{API}/task-runs/run_{'0' * 32}/stage-outputs")
    assert missing.status_code == 404
