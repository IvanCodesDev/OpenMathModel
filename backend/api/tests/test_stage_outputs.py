"""五类页面正文投影端点：GET /v1/task-runs/{run_id}/stage-outputs。

正文来自 run_domain_events 的 STEP_SUCCEEDED 输出；真实链路复用
test_task_runs_llm_nodes 的 MockTransport 六阶段桩（同一份 stub 输出），
覆盖：全链跑完后五类正文可读且通过契约校验、未完成阶段的空值行为、
越权访问 404。
"""

from __future__ import annotations

from datetime import datetime, timezone

from conftest import (
    API,
    approve_when_asked,
    confirm_delivery,
    create_project,
    create_run,
    pending_approval,
    register_user,
    run_status_is,
    wait_until,
)

from omm_api.orm import ApprovalRequestRow
from omm_api.stage_outputs import (
    StageState,
    _audit_findings,
    _cleaning_report,
    _dataset_profile,
    _document_draft,
    _figures,
    _plan_proposal,
    _references,
    _review_report,
    _robustness_report,
    _validation_report,
)
from omm_contracts import DeliveryManifest
from omm_contracts.v1.dataset_profile import CleaningReport
from omm_contracts.v1.experiment_summary import ReviewReport, ValidationReport
from test_task_runs_llm_nodes import (
    ANALYSIS_OUTPUT,
    CLEANING_OUTPUT,
    EXPERIMENT_OUTPUT,
    PAPER_FIGURE_FILE,
    PAPER_OUTPUT,
    PLANNING_OUTPUT,
    PREPARATION_OUTPUT,
    REVIEW_OUTPUT,
    VALIDATION_OUTPUT,
    _configure_llm,
    _stage_csv_attachment,
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
    # 本链没有下发数据文件：清洗如实「未执行」并给原因（不是 null——null 留给该字段
    # 出现之前的运行），数字 0、审稿 null
    cleaning = dataset_profile["cleaning"]
    assert cleaning["executed"] is False and cleaning["status"] is None and cleaning["review"] is None
    assert cleaning["reason"] == "工作区没有已下发的数据文件，无需清洗"
    assert (cleaning["rows_before"], cleaning["rows_after"], cleaning["attempts"]) == (0, 0, 0)

    plan_proposal = payload["plan_proposal"]
    validate_contract("plan-proposal.schema.json", plan_proposal)
    assert plan_proposal["recommended_plan_id"] == PLANNING_OUTPUT["recommended_plan_id"]
    assert len(plan_proposal["plans"]) == len(PLANNING_OUTPUT["plans"])
    assert "llm_attempts" not in plan_proposal, "过程杂项字段不得进入投影"
    # H3 切片 2：归约后的规范化把假设表 / 符号表带进契约；模型的毛病（$ 定界、
    # 枚举别名、「方案 A」/「Plan B」写法）在节点归一化后已成契约形状
    assert [(entry["id"], entry["scope"]) for entry in plan_proposal["assumptions"]] == [
        ("G1", "global"), ("G2", "global"), ("A1", "A"), ("B1", "B"),
    ]
    assert plan_proposal["assumptions"][2]["impact"] == "high"
    assert [(entry["symbol"], entry["plan_id"]) for entry in plan_proposal["symbols"]] == [
        ("i \\in \\mathcal{I}", None), ("d_i", None), ("x_i", "A"), ("z", "A"), ("\\mathcal{N}(s)", "B"),
    ]
    assert plan_proposal["symbols"][2] == {
        "symbol": "x_i", "kind": "variable", "definition": "调度点 i 是否设站",
        "unit": None, "range": "{0,1}", "plan_id": "A",
    }
    # H3 切片 6：方案卡带实现语言（归约桩没写 → 节点按唯一可用语言 python 补齐）；
    # G1 决策台账进投影：approve = 采用推荐案 A，与实验阶段实际所用方案一致
    assert [plan["language"] for plan in plan_proposal["plans"]] == ["python", "python"]
    decision = plan_proposal["decision"]
    g1 = next(
        item for item in client.get(f"{API}/task-runs/{run['id']}/approvals").json()["items"]
        if item["decision_type"] == "confirm_plan"
    )
    assert decision == {
        "approval_id": g1["id"],
        "option_id": "approve",
        "chosen_plan_id": "A",
        # 动作层记的是账户标识原值（与 /approvals 的 resolution.actor 同一份）
        "actor": g1["resolution"]["actor"],
        "comment": None,
        "resolved_at": g1["resolution"]["resolved_at"],
    }
    assert decision["actor"] and decision["actor"] != "user"

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
        # H3 切片 3：检查回指方案阶段标为「待检验」的全局假设 G2（假设表下游消费）
        "assumption_id": "G2",
    }
    assert [check["assumption_id"] for check in robustness["checks"]] == ["G2", None, None]
    assert robustness["summary_text"] == "沙盒复跑稳健性检查 3 项，通过 3 项，全部达标。"
    assert robustness["reason"] == ""
    for key in (
        "attempts", "llm_calls", "summary", "failed_checks", "final_code_artifact",
        "produced_artifacts", "assumption_coverage", "uncovered_focus",
    ):
        assert key not in robustness, f"过程字段 {key} 不得进入投影"
    # H4 切片 12：三条审稿链里进这页的两条——实验代码审稿挂顶层、检验脚本审稿挂
    # 稳健性报告；桩审稿人一轮 accept + 一条 minor 意见，节点确定性复跑一致
    expected_review = {
        "executed": True,
        "verdict": "accept",
        "rounds": 1,
        "findings": REVIEW_OUTPUT["findings"],
        "blockers": 0,
        "summary": REVIEW_OUTPUT["summary"],
        "stalemate": False,
        "rerun_consistent": True,
        "reason": "",
    }
    assert experiment_summary["review"] == expected_review
    assert robustness["review"] == expected_review
    for key in ("llm_calls", "rerun"):
        assert key not in experiment_summary["review"], f"审稿过程字段 {key} 不得进入投影"

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
    # H5 切片 18：论文阶段按总编规划补画的图（沙盒真跑、SVG 产物）进同一张图件清单，
    # 来源 PAPER_WRITING、检验章已插入；产物带 figure kind 与哈希
    assert document_draft["figures"] == [{
        "number": 1, "name": PAPER_FIGURE_FILE, "artifact_id": document_draft["figures"][0]["artifact_id"],
        "caption": "实验指标柱状图", "source_stage": "PAPER_WRITING", "inserted": True,
    }]
    figure_artifacts = [a for a in delivery_manifest["artifacts"] if a["kind"] == "figure"]
    assert [a["id"] for a in figure_artifacts] == [document_draft["figures"][0]["artifact_id"]]
    assert figure_artifacts[0]["producer_node"] == "PAPER_WRITING" and figure_artifacts[0]["download_url"]
    # H5 切片 17：文件 + 哈希 + 一致性结果——每个产物带登记哈希；用户在 G4「确认交付」后
    # 交付记录 confirmed，五项确定性检查全过（论文产物可读、审计 0 发现、已插入的一张图有
    # 可下载产物、实验指标 rmse=0.5 出现在正文、检验结论在场）
    assert all(len(a["sha256"]) == 64 for a in delivery_manifest["artifacts"]), "产物哈希齐全"
    delivery = delivery_manifest["delivery"]
    # 审批接口不透出 evidence.gate，按 G4 的选项 id 认门（与 conftest.confirm_delivery 同口径）
    g4 = next(
        item for item in client.get(f"{API}/task-runs/{run['id']}/approvals").json()["items"]
        if [option["id"] for option in item["options"]] == ["confirm_delivery", "redo:PAPER_WRITING"]
    )
    assert delivery["status"] == "confirmed"
    assert delivery["approval_id"] == g4["id"]
    assert delivery["confirmed_at"] == g4["resolution"]["resolved_at"]
    assert delivery["paper_version"] == 1 and delivery["comment"] is None
    assert delivery["audit"] == {
        "findings_total": 0, "findings_by_kind": {},
        "frozen_numbers_total": len(document_draft["frozen_numbers"]),
        "figures_total": 1, "figures_inserted": 1, "references_total": 0, "references_cited": 0,
    }
    assert [(check["id"], check["passed"]) for check in delivery["checks"]] == [
        ("paper_artifact_ready", True), ("audit_clean", True), ("figures_delivered", True),
        ("metrics_in_paper", True), ("validation_reported", True),
    ]
    assert delivery["checks"][2]["detail"] == "1 / 1 张已插入图件有可下载产物"
    assert delivery["checks"][3]["detail"].startswith("1 / 1 个实验指标出现在论文正文或摘要中")
    assert delivery["files_total"] == len(delivery_manifest["artifacts"])
    assert delivery["files_hashed"] == delivery["files_total"]
    assert delivery["files_ready"] == sum(1 for a in delivery_manifest["artifacts"] if a["download_url"])


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
    assert manifest["delivery"] is None, "没有真实论文草稿就没有交付记录（不装作有）"
    assert all(len(a["sha256"]) == 64 for a in manifest["artifacts"]), "模拟链产物同样带登记哈希"


def _paper_state(outputs: dict, step_id: str = "step_paper_1", count: int = 1) -> StageState:
    state = StageState()
    state.at = datetime(2026, 9, 7, 5, 40, tzinfo=timezone.utc)
    state.count = count
    state.step_id = step_id
    state.outputs = outputs
    return state


def _g4_row(step_id: str, status: str = "RESOLVED", option_id: str | None = "confirm_delivery", comment: str | None = None):
    from omm_api.orm import ApprovalRequestRow

    return ApprovalRequestRow(
        id="appr_8b1d6f2a3c4e5f60718293a4b5c6d7e8",
        run_id=_RUN_ID,
        decision_type="generic",
        title="论文草稿已生成",
        options=[{"id": "confirm_delivery", "label": "确认交付"}, {"id": "redo:PAPER_WRITING", "label": "退回修改"}],
        evidence={"note": "论文草稿已生成", "requested_by_step": step_id, "gate": "G4"},
        status=status,
        requested_at=datetime(2026, 9, 7, 5, 30, tzinfo=timezone.utc),
        resolution=(
            {"option_id": option_id, "resolved_at": "2026-09-07T05:40:00.000000Z", "actor": "user", "comment": comment}
            if option_id is not None else None
        ),
    )


def test_delivery_record_replays_g4_and_runs_deterministic_checks(validate_contract):
    """交付记录：G4 审批 → 状态 / 确认时间 / 备注；五项检查如实 passed 与依据；论文缺席 → null。"""
    from omm_api.stage_outputs import _delivery_record

    paper_id = "art_9f8e7d6c5b4a30211203a4b5c6d7e8f9"
    fig_id = "art_1a2b3c4d5e6f708192a3b4c5d6e7f809"
    artifacts = [
        {"id": paper_id, "kind": "paper", "name": "paper-draft.md", "media_type": "text/markdown", "size_bytes": 10,
         "status": "READY", "producer_node": "PAPER_WRITING", "download_url": f"/api/v1/artifacts/{paper_id}/download",
         "sha256": "fcde2b2edba56bf408601fb721fe9b5c338d10ee429ea04fae5511b68fbf8fb9"},
        {"id": fig_id, "kind": "figure", "name": "fit.png", "media_type": "image/png", "size_bytes": 10,
         "status": "READY", "producer_node": "EXPERIMENTING", "download_url": None, "sha256": None},
    ]
    outputs = {
        **PAPER_OUTPUT,
        "abstract": "贪心基线 rmse=0.12。",
        "sections": [{"heading": "5 求解", "content": "见图 1。\n\n![图 1 拟合](fit.png)\n\nrmse=0.12，但 mae 没写。"}],
        "frozen_numbers": [
            {"id": "metrics.rmse", "label": "rmse", "value": 0.12, "source_stage": "EXPERIMENTING", "source_path": "metrics.rmse"},
            {"id": "metrics.mae", "label": "mae", "value": 3.0, "source_stage": "EXPERIMENTING", "source_path": "metrics.mae"},
            {"id": "robustness.x", "label": "x", "value": 0.2, "source_stage": "VALIDATING", "source_path": "robustness.checks[0].value"},
        ],
        "audit_findings": [
            {"scope": "第1章《5 求解》", "kind": "unsourced_number", "numbers": ["0.87"], "detail": "无出处"},
            {"scope": "第1章《5 求解》", "kind": "phantom_table", "numbers": ["表 2"], "detail": "幽灵表"},
        ],
        "figures": [
            {"number": 1, "name": "fit.png", "artifact_id": fig_id, "caption": "拟合", "source_stage": "EXPERIMENTING", "inserted": True},
            {"number": 2, "name": "conv.svg", "artifact_id": "art_2", "caption": "", "source_stage": "EXPERIMENTING", "inserted": False},
        ],
        "references": [
            {"number": 1, "title": "T", "text": "T[Z].", "url": None, "source": "plan_citation", "card_id": None, "cited": True},
        ],
    }
    paper = _paper_state(outputs)

    # 已确认交付 + 三项检查不过（审计有发现 / 已插入图件的产物不可下载 / mae 没出现在正文）
    record = _delivery_record(paper, [_g4_row("step_paper_1", comment=" 直接交付 ")], artifacts, paper_id, "pass")
    assert (record["status"], record["confirmed_at"], record["comment"], record["approval_id"]) == (
        "confirmed", "2026-09-07T05:40:00.000000Z", "直接交付", "appr_8b1d6f2a3c4e5f60718293a4b5c6d7e8",
    )
    assert record["paper_version"] == 1
    assert record["audit"] == {
        "findings_total": 2, "findings_by_kind": {"unsourced_number": 1, "phantom_table": 1},
        "frozen_numbers_total": 3, "figures_total": 2, "figures_inserted": 1, "references_total": 1, "references_cited": 1,
    }
    assert [(c["id"], c["passed"]) for c in record["checks"]] == [
        ("paper_artifact_ready", True), ("audit_clean", False), ("figures_delivered", False),
        ("metrics_in_paper", False), ("validation_reported", True),
    ]
    assert record["checks"][0]["detail"] == "paper-draft.md（fcde2b2edba5…）"
    assert record["checks"][1]["detail"] == "终稿审计发现 2 处（phantom_table 1 处、unsourced_number 1 处）"
    assert record["checks"][2]["detail"] == "0 / 1 张已插入图件有可下载产物；缺：fit.png"
    assert record["checks"][3]["detail"] == "1 / 2 个实验指标出现在论文正文或摘要中；未出现：metrics.mae"
    assert record["checks"][4]["detail"] == "检验结论：pass"
    assert (record["files_total"], record["files_ready"], record["files_hashed"]) == (2, 1, 1)

    # 状态回放：挂起 / 退回 / 过期 / 没挂 G4（无人值守）/ 审批是别趟 step 的不算
    assert _delivery_record(paper, [_g4_row("step_paper_1", status="PENDING", option_id=None)], artifacts, paper_id, None)["status"] == "pending_confirmation"
    returned = _delivery_record(paper, [_g4_row("step_paper_1", option_id="redo:PAPER_WRITING", comment="重写")], artifacts, paper_id, None)
    assert (returned["status"], returned["confirmed_at"], returned["comment"]) == ("returned_for_revision", None, "重写")
    assert _delivery_record(paper, [_g4_row("step_paper_1", status="EXPIRED", option_id=None)], artifacts, paper_id, None)["status"] == "not_ready"
    unattended = _delivery_record(paper, [_g4_row("step_paper_0")], artifacts, paper_id, None)
    assert unattended["status"] == "unattended" and unattended["approval_id"] is None
    assert unattended["checks"][4] == {"id": "validation_reported", "label": "检验结论在场", "passed": False, "detail": "检验阶段没有给出结论"}

    # 未审计的旧运行：audit null、audit_clean 不过；论文产物缺失点名
    legacy = _delivery_record(_paper_state(dict(PAPER_OUTPUT)), [], [], None, None)
    assert legacy["audit"] is None
    assert legacy["checks"][0] == {"id": "paper_artifact_ready", "label": "论文草稿产物可读且哈希对得上", "passed": False, "detail": "论文草稿产物缺失或内容对象不可读"}
    assert legacy["checks"][1]["detail"] == "论文未做终稿审计"
    assert legacy["checks"][2]["detail"] == "本次运行的论文没有插入图件"
    assert legacy["checks"][3]["detail"] == "冻结清单里没有实验指标"

    # 没有真实论文（sim / 未到论文阶段）→ null
    assert _delivery_record(None, [], artifacts, None, "pass") is None
    assert _delivery_record(_paper_state({"label": "写入建模报告草稿（模拟）"}), [], artifacts, None, "pass") is None

    # 整份 DeliveryManifest 过 JSON Schema（含 sha256 与 delivery）
    manifest = DeliveryManifest(
        run_id=_RUN_ID, problem_title="题", artifacts=artifacts, key_metrics={"rmse": 0.12},
        validation_verdict="pass", paper_citation=None, delivery=record, updated_at="2026-09-07T05:40:00.000000Z",
    ).model_dump(mode="json")
    validate_contract("delivery-manifest.schema.json", manifest)


def _validation_with(robustness: dict | None) -> dict:
    return {
        "verdict": "pass",
        "checks": [],
        "risks": [],
        "validation_summary": "结果可信",
        "robustness": robustness,
    }


def test_robustness_projection_fills_unexecuted_shape():
    """节点如实降级为「仅判读」时只给 {executed, reason}：投影补齐契约七键；
    没跑复跑就没有检验脚本可审，审稿键为 null。"""
    report = _robustness_report({"executed": False, "reason": "未配置工具端口，跳过稳健性复跑"})
    assert report == {
        "executed": False,
        "status": None,
        "summary_text": "",
        "checks": [],
        "checks_total": 0,
        "checks_failed": 0,
        "reason": "未配置工具端口，跳过稳健性复跑",
        "review": None,
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
                "assumption_id": "A1",
            },
            # name 缺省回落 id；value 是 bool 不算数字；threshold 允许文字口径；
            # assumption_id 非字符串（旧节点没有该键 / 模型给了数字）→ null
            {"id": "baseline", "name": "", "passed": True, "value": True, "threshold": "≥ 0.1", "assumption_id": 7},
            {"id": "", "passed": True, "value": 1},  # 缺 id → 剔除
            {"id": "bogus", "passed": "yes"},  # passed 非布尔 → 剔除
            "not-a-dict",
        ],
        "checks_total": 5,
        "checks_failed": 1,
        # 假设覆盖表是节点侧的派生数据（可由 plan-proposal.assumptions × checks.assumption_id
        # 推出），不进契约
        "assumption_coverage": [{"id": "A1", "check_ids": ["sensitivity"], "passed": False}],
        "uncovered_focus": ["A2"],
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
        "review",
    }
    assert report["review"] is None, "审稿环之前的运行没有 review 键 → null"
    assert report["checks"] == [
        {
            "id": "sensitivity",
            "name": "需求率扰动",
            "passed": False,
            "value": 0.25,
            "threshold": 0.2,
            "detail": "超出阈值",
            "assumption_id": "A1",
        },
        {
            "id": "baseline",
            "name": "baseline",
            "passed": True,
            "value": None,
            "threshold": "≥ 0.1",
            "detail": "",
            "assumption_id": None,
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


# -- 独立审稿结论（§8.4 生成者-评审者环）的投影 -----------------------------------------


def test_review_projection_fills_unexecuted_shape_and_keeps_legacy_null():
    """审稿没派出去（无监督者 / 预算不足 / 子代理未完成）：节点只给 {executed:false,
    reason, llm_calls[, rounds, rerun]} → 投影补齐契约九键；审稿环之前的运行没有该键
    → null，而不是编一个「未审稿」。"""
    skipped = _review_report({"executed": False, "reason": "未配置子代理监督者，跳过独立审稿", "llm_calls": 0})
    assert skipped == {
        "executed": False,
        "verdict": None,
        "rounds": 0,
        "findings": [],
        "blockers": 0,
        "summary": "",
        "stalemate": False,
        "rerun_consistent": None,
        "reason": "未配置子代理监督者，跳过独立审稿",
    }
    ReviewReport.model_validate(skipped)

    # 首轮审稿人没给出有效终答：节点记了 rounds 与复跑核对，但 executed=false —
    # verdict / findings / summary 不得从半成品里捞
    failed_round = _review_report(
        {
            "executed": False,
            "rounds": 1,
            "rerun": {"executed": True, "consistent": True, "metrics": {"rmse": 0.5}, "diff": [], "reason": ""},
            "reason": "审稿子代理未完成（failed）",
            "llm_calls": 2,
            "verdict": "reject",
            "findings": [{"id": "R1", "severity": "blocker", "issue": "半成品"}],
            "summary": "半成品",
        }
    )
    assert failed_round["executed"] is False and failed_round["rounds"] == 1
    assert failed_round["verdict"] is None and failed_round["findings"] == [] and failed_round["summary"] == ""
    assert failed_round["rerun_consistent"] is True
    ReviewReport.model_validate(failed_round)

    assert _review_report(None) is None
    assert _review_report("garbage") is None


def test_review_projection_strips_process_fields_cleans_findings_and_recounts():
    """已执行形状带过程字段（llm_calls、rerun 的 metrics / diff）：契约
    additionalProperties=false 会把接口打成 500 → 投影剔除；意见逐条清洗后 blockers
    按投影结果重算，不信节点给的计数。"""
    raw = {
        "executed": True,
        "rounds": 2,
        "verdict": "reject",
        "findings": [
            {"id": "R1", "severity": "blocker", "location": "robustness.py:perturb()", "issue": " 扰动只作用在训练集 ", "fix_hint": "同步扰动评估集"},
            # severity 枚举外 → minor；缺 id → 按序补 R2；location / fix_hint 缺省空串
            {"severity": "critical", "issue": "阈值来源未说明"},
            {"id": "R3", "severity": "major", "issue": ""},  # 无 issue → 剔除
            "not-a-dict",
            {"id": "R4", "severity": "BLOCKER", "issue": "大写枚举也认"},
        ],
        "blockers": 9,
        "summary": "扰动实现有缺陷",
        "rerun": {"executed": True, "consistent": False, "metrics": {"rmse": 0.7}, "diff": ["rmse: 0.5 → 0.7"], "reason": "复跑指标与首跑不一致"},
        "stalemate": True,
        "reason": "审稿 2 轮后仍有阻断性意见未解决",
        "llm_calls": 4,
    }
    report = _review_report(raw)
    assert set(report) == {
        "executed", "verdict", "rounds", "findings", "blockers", "summary", "stalemate", "rerun_consistent", "reason",
    }
    assert report["findings"] == [
        {"id": "R1", "severity": "blocker", "location": "robustness.py:perturb()", "issue": "扰动只作用在训练集", "fix_hint": "同步扰动评估集"},
        {"id": "R2", "severity": "minor", "location": "", "issue": "阈值来源未说明", "fix_hint": ""},
        {"id": "R4", "severity": "blocker", "location": "", "issue": "大写枚举也认", "fix_hint": ""},
    ]
    assert report["blockers"] == 2 and report["rounds"] == 2 and report["verdict"] == "reject"
    assert report["stalemate"] is True and report["rerun_consistent"] is False
    assert report["summary"] == "扰动实现有缺陷" and report["reason"] == raw["reason"]
    ReviewReport.model_validate(report)

    # 通过的形状：未复跑 → rerun_consistent null；verdict 不在枚举 → null；rounds 畸形 → 0
    accepted = _review_report(
        {
            "executed": True,
            "rounds": "1",
            "verdict": "approve",
            "findings": [],
            "blockers": 0,
            "summary": "实现忠实于方案",
            "rerun": {"executed": False, "reason": "剩余预算不足以复跑核对"},
            "stalemate": False,
            "reason": "",
            "llm_calls": 1,
        }
    )
    assert accepted["verdict"] is None and accepted["rounds"] == 0 and accepted["rerun_consistent"] is None
    assert accepted["stalemate"] is False and accepted["summary"] == "实现忠实于方案"
    ReviewReport.model_validate(accepted)

    # 检验脚本的审稿随稳健性报告投影为可选键，整份 validation 仍过契约模型
    robustness = _robustness_report(
        {
            "executed": True,
            "status": "passed",
            "summary_text": "沙盒复跑稳健性检查 1 项，通过 1 项，全部达标。",
            "checks": [{"id": "sensitivity", "name": "需求率扰动", "passed": True, "value": 0.05, "threshold": 0.2, "detail": ""}],
            "review": raw,
        }
    )
    assert robustness["review"] == report
    ValidationReport.model_validate(_validation_with(robustness))


def test_cleaning_projection_fills_unexecuted_shape_and_keeps_legacy_null():
    """清洗没跑（无工具 / 监督者 / 数据文件 / 预算、子代理未完成）：节点只给
    {executed:false, reason} → 投影补齐契约十一键（数字 0、列表空、status / review
    null，reason 原样）；该字段出现之前的运行没有 cleaning 键 → null。"""
    skipped = _cleaning_report({"executed": False, "reason": "工作区没有已下发的数据文件，无需清洗"})
    assert skipped == {
        "executed": False,
        "status": None,
        "reason": "工作区没有已下发的数据文件，无需清洗",
        "attempts": 0,
        "rows_before": 0,
        "rows_after": 0,
        "rows_deleted_ratio": 0.0,
        "imputed_columns": [],
        "imputed_target_columns": [],
        "summary": "",
        "review": None,
    }
    CleaningReport.model_validate(skipped)

    # 子代理未完成也是未执行：节点不会写影响面，即便有半成品键也不得捞出来
    aborted = _cleaning_report(
        {
            "executed": False,
            "reason": "清洗子代理未完成（failed，E301）；后续阶段按原始数据继续",
            "rows_before": 1200,
            "status": "failed",
            "review": {"executed": True, "verdict": "reject"},
        }
    )
    assert aborted["executed"] is False and aborted["status"] is None and aborted["review"] is None
    assert aborted["rows_before"] == 0 and aborted["reason"].startswith("清洗子代理未完成")
    CleaningReport.model_validate(aborted)

    assert _cleaning_report(None) is None
    assert _cleaning_report("garbage") is None


def test_cleaning_projection_strips_process_fields_and_normalizes(validate_contract):
    """已执行形状带过程字段（llm_calls / target_columns / 产物引用）：契约
    additionalProperties=false → 白名单剔除；数字按标记行取非负整数、删行比例缺失时
    按前后行数重算、status 越界归 failed、审稿走同一份 _review_report 清洗。"""
    raw = {
        "executed": True,
        "status": "passed",
        "attempts": 2,
        "llm_calls": 5,
        "summary": "合并两表后按区域中位数插补 demand，删除 96 行负值记录",
        "target_columns": ["demand"],
        "final_code_artifact": "art_123",
        "produced_artifacts": ["art_124", "art_125"],
        "rows_before": 1200,
        "rows_after": 1104,
        "rows_deleted_ratio": 0.08,
        "imputed_columns": ["demand", "", "  "],
        "imputed_target_columns": ["demand"],
        "review": {
            "executed": True,
            "rounds": 2,
            "verdict": "reject",
            "findings": [
                {"id": "R1", "severity": "blocker", "location": "cleaning.py:impute()", "issue": "目标列 demand 被插补", "fix_hint": "改为删行或标记"},
                {"id": "R2", "severity": "minor", "location": "", "issue": "删行数写死在打印语句里", "fix_hint": "用 len(df) 计算"},
            ],
            "blockers": 1,
            "summary": "目标列被越权插补，清洗产物不能作为建模样本",
            "rerun": {"executed": True, "consistent": True, "metrics": {"rows_after": 1104}, "diff": [], "reason": ""},
            "stalemate": True,
            "reason": "审稿 2 轮后仍有阻断性意见未解决",
            "llm_calls": 3,
        },
    }
    report = _cleaning_report(raw)
    assert set(report) == {
        "executed", "status", "reason", "attempts", "rows_before", "rows_after", "rows_deleted_ratio",
        "imputed_columns", "imputed_target_columns", "summary", "review",
    }
    assert report["status"] == "passed" and report["reason"] == "" and report["attempts"] == 2
    assert (report["rows_before"], report["rows_after"], report["rows_deleted_ratio"]) == (1200, 1104, 0.08)
    assert report["imputed_columns"] == ["demand"] and report["imputed_target_columns"] == ["demand"]
    assert report["summary"] == raw["summary"]
    assert report["review"] == _review_report(raw["review"])
    assert report["review"]["stalemate"] is True and report["review"]["blockers"] == 1
    assert "llm_calls" not in report["review"] and "rerun" not in report["review"]
    CleaningReport.model_validate(report)

    # 首波没过验收：没有审稿；比例缺失按行数重算；status 越界 → failed；坏数字归 0
    failed = _cleaning_report(
        {
            "executed": True,
            "status": "aborted",
            "attempts": True,
            "rows_before": 100,
            "rows_after": 80,
            "imputed_columns": "demand",
            "summary": "",
        }
    )
    assert failed["status"] == "failed" and failed["attempts"] == 0
    assert failed["rows_deleted_ratio"] == 0.2 and failed["imputed_columns"] == []
    assert failed["review"] is None
    CleaningReport.model_validate(failed)

    # 整份 DatasetProfile 带 cleaning 过契约（JSON Schema 权威校验）
    state = StageState()
    state.at = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    state.outputs = {**PREPARATION_OUTPUT, "cleaning": raw}
    profile = _dataset_profile(_RUN_ID, state)
    payload = profile.model_dump(mode="json")
    validate_contract("dataset-profile.schema.json", payload)
    assert payload["cleaning"] == report
    # 该字段出现之前的运行：没有 cleaning 键 → null（不是编一个「未执行」）
    state.outputs = dict(PREPARATION_OUTPUT)
    legacy = _dataset_profile(_RUN_ID, state).model_dump(mode="json")
    validate_contract("dataset-profile.schema.json", legacy)
    assert legacy["cleaning"] is None


def test_audit_findings_projection_keeps_audit_chain_kinds_and_drops_unknown(validate_contract):
    """审计链三条审计的四种 kind 都进契约；节点将来多出的未知 kind 逐条剔除
    （契约 enum 硬约束，透传会把 stage-outputs 打成 500）；缺键 → null、空数组原样。"""
    raw = [
        {"scope": "第3章《6 结果分析与检验》", "kind": "unsourced_number", "numbers": ["0.87"], "detail": "无出处"},
        {"scope": "第2章《5 模型建立与求解》", "kind": "phantom_figure", "numbers": ["图 1", "fit.png"], "detail": "幽灵图"},
        {"scope": "第2章《5 模型建立与求解》", "kind": "phantom_table", "numbers": ["表 1"], "detail": "幽灵表"},
        {"scope": "第4章《参考文献》", "kind": "unverified_citation", "numbers": ["[3]", "[5]"], "detail": "未验证"},
        {"scope": "摘要", "kind": "future_kind", "numbers": [], "detail": "投影不认识的类型"},
        "garbage",
    ]
    findings = _audit_findings(raw)
    assert [f["kind"] for f in findings] == [
        "unsourced_number", "phantom_figure", "phantom_table", "unverified_citation",
    ]
    assert findings[1]["numbers"] == ["图 1", "fit.png"] and findings[3]["numbers"] == ["[3]", "[5]"]
    assert _audit_findings(None) is None and _audit_findings([]) == []

    state = StageState()
    state.at = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    state.outputs = {**PAPER_OUTPUT, "frozen_numbers": [], "audit_findings": raw}
    payload = _document_draft(_RUN_ID, state).model_dump(mode="json")
    validate_contract("document-draft.schema.json", payload)
    assert [f["kind"] for f in payload["audit_findings"]] == [f["kind"] for f in findings]
    # 图件字段出现之前的运行：没有 figures 键 → null
    assert payload["figures"] is None


def test_figures_projection_whitelists_real_figures_and_drops_malformed(validate_contract):
    """真实图件清单进契约：编号 / 文件名 / 来源阶段畸形的条目剔除，artifact_id 空串归 null，
    inserted 强制布尔；缺键 → null、空数组原样（= 本次运行没有产出图件）。"""
    raw = [
        {"number": 1, "name": "fit_vs_baseline.png", "artifact_id": "art_fig1", "caption": "模型与基线的拟合对比",
         "source_stage": "EXPERIMENTING", "inserted": True},
        {"number": 2, "name": "convergence.svg", "artifact_id": "", "caption": "", "source_stage": "EXPERIMENTING",
         "inserted": "yes"},
        {"number": 3, "name": "sensitivity.png", "artifact_id": "art_fig3", "caption": "灵敏度",
         "source_stage": "VALIDATING", "inserted": False},
        # 论文阶段按总编规划补画的图（figure_render 第二步）：同一张清单、来源 PAPER_WRITING
        {"number": 4, "name": "station_dispatch.png", "artifact_id": "art_fig_paper", "caption": "各站点调度量分布",
         "source_stage": "PAPER_WRITING", "inserted": True},
        {"number": 0, "name": "zero.png", "artifact_id": "a", "caption": "", "source_stage": "EXPERIMENTING", "inserted": False},
        {"number": True, "name": "bool.png", "artifact_id": "a", "caption": "", "source_stage": "EXPERIMENTING", "inserted": False},
        {"number": 4, "name": "  ", "artifact_id": "a", "caption": "", "source_stage": "EXPERIMENTING", "inserted": False},
        {"number": 5, "name": "eda.png", "artifact_id": "a", "caption": "清洗前后分布", "source_stage": "DATA_PREPARATION", "inserted": False},
        "garbage",
    ]
    figures = _figures(raw)
    assert figures == [
        {"number": 1, "name": "fit_vs_baseline.png", "artifact_id": "art_fig1", "caption": "模型与基线的拟合对比",
         "source_stage": "EXPERIMENTING", "inserted": True},
        {"number": 2, "name": "convergence.svg", "artifact_id": None, "caption": "", "source_stage": "EXPERIMENTING",
         "inserted": True},
        {"number": 3, "name": "sensitivity.png", "artifact_id": "art_fig3", "caption": "灵敏度",
         "source_stage": "VALIDATING", "inserted": False},
        {"number": 4, "name": "station_dispatch.png", "artifact_id": "art_fig_paper", "caption": "各站点调度量分布",
         "source_stage": "PAPER_WRITING", "inserted": True},
    ]
    assert _figures(None) is None and _figures("oops") is None and _figures([]) == []

    state = StageState()
    state.at = datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)
    state.outputs = {**PAPER_OUTPUT, "frozen_numbers": [], "audit_findings": [], "figures": raw}
    payload = _document_draft(_RUN_ID, state).model_dump(mode="json")
    validate_contract("document-draft.schema.json", payload)
    assert payload["figures"] == figures
    # 引用库字段出现之前的运行：没有 references 键 → null
    assert payload["references"] is None


def test_references_projection_whitelists_verified_entries_and_drops_malformed(validate_contract):
    """本次运行的已验证引用库进契约：编号 / 标题 / 来源畸形的条目剔除，非 http(s) 链接归 null、
    card_id 空串归 null、cited 强制布尔；缺键 → null、空数组原样。"""
    raw = [
        {"number": 1, "title": "生产企业原材料的订购与运输", "text": "全国大学生数学建模竞赛 2021 2021 CUMCM C. 生产企业原材料的订购与运输[Z]. [来源](https://example.test/c)",
         "url": "https://example.test/c", "source": "plan_citation", "card_id": "problem:cumcm-2021-c", "cited": True},
        {"number": 2, "title": "机场出租车排队仿真", "text": "", "url": "javascript:alert(1)", "source": "user_reference",
         "card_id": "", "cited": "yes"},
        {"number": 0, "title": "编号为零", "text": "x", "url": None, "source": "plan_citation", "card_id": None, "cited": False},
        {"number": True, "title": "编号是布尔", "text": "x", "url": None, "source": "plan_citation", "card_id": None, "cited": False},
        {"number": 3, "title": "  ", "text": "x", "url": None, "source": "plan_citation", "card_id": None, "cited": False},
        {"number": 4, "title": "凭空写的", "text": "x", "url": None, "source": "made_up", "card_id": None, "cited": True},
        "garbage",
    ]
    references = _references(raw)
    assert references == [
        {"number": 1, "title": "生产企业原材料的订购与运输",
         "text": "全国大学生数学建模竞赛 2021 2021 CUMCM C. 生产企业原材料的订购与运输[Z]. [来源](https://example.test/c)",
         "url": "https://example.test/c", "source": "plan_citation", "card_id": "problem:cumcm-2021-c", "cited": True},
        {"number": 2, "title": "机场出租车排队仿真", "text": "机场出租车排队仿真", "url": None, "source": "user_reference",
         "card_id": None, "cited": True},
    ]
    assert _references(None) is None and _references("oops") is None and _references([]) == []

    state = StageState()
    state.at = datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc)
    state.outputs = {**PAPER_OUTPUT, "frozen_numbers": [], "audit_findings": [], "figures": [], "references": raw}
    payload = _document_draft(_RUN_ID, state).model_dump(mode="json")
    validate_contract("document-draft.schema.json", payload)
    assert payload["references"] == references


def test_stage_outputs_carry_cleaning_and_its_review_after_real_cleaning(client, monkeypatch, validate_contract):
    """有数据文件下发时清洗沙盒真跑（stub 模型发脚本、python 沙箱执行、审稿人接受）：
    数据阶段一结束（挂在 G1 时）dataset_profile.cleaning 就应带影响面数字与审稿结论，
    过程字段不进契约。"""
    project = create_project(client)
    artifact_id = _stage_csv_attachment(
        client, project["id"], "orders.csv", "quarter,volume\n1,120.5\n2,130.0\n".encode("utf-8")
    )
    _configure_llm(client, monkeypatch)
    run = create_run(
        client,
        project["id"],
        goal="优化共享单车调度",
        params={"attachment_metadata": [{"name": "orders.csv", "artifact_id": artifact_id}]},
    )
    wait_until(client, run["id"], pending_approval(client, run["id"]))

    dataset_profile = _stage_outputs(client, run["id"])["dataset_profile"]
    validate_contract("dataset-profile.schema.json", dataset_profile)
    cleaning = dataset_profile["cleaning"]
    assert cleaning is not None, "有数据文件在场时清洗真实执行，结论应进投影"
    assert cleaning["executed"] is True and cleaning["status"] == "passed" and cleaning["reason"] == ""
    assert cleaning["attempts"] == 1
    assert (cleaning["rows_before"], cleaning["rows_after"], cleaning["rows_deleted_ratio"]) == (2, 2, 0.0)
    assert cleaning["imputed_columns"] == [] and cleaning["imputed_target_columns"] == []
    assert cleaning["summary"] == CLEANING_OUTPUT["summary"]
    assert cleaning["review"] == {
        "executed": True,
        "verdict": "accept",
        "rounds": 1,
        "findings": REVIEW_OUTPUT["findings"],
        "blockers": 0,
        "summary": REVIEW_OUTPUT["summary"],
        "stalemate": False,
        "rerun_consistent": True,
        "reason": "",
    }
    for key in ("llm_calls", "target_columns", "final_code_artifact", "produced_artifacts"):
        assert key not in cleaning, "过程字段 / 产物引用不进正文契约"


_RUN_ID = "run_" + "0" * 32


def _planning_state(**extra) -> StageState:
    state = StageState()
    state.at = datetime(2026, 9, 5, 12, 30, tzinfo=timezone.utc)
    state.outputs = {
        "plans": [
            {"id": "A", "name": "整数规划", "approach": "MILP", "steps": ["建模"], "risks": []},
            {"id": "B", "name": "启发式", "approach": "贪心", "steps": ["迭代"], "risks": []},
        ],
        "recommended_plan_id": "A",
        "rationale": "精确解可行",
        **extra,
    }
    return state


def test_plan_tables_projection_absent_and_null_stay_null():
    """切片 2 之前的运行 / 无监督者的单次调用路径没有两表键；规范化失败节点写下
    null——两种情况投影都保持 null，不编空表。"""
    legacy = _plan_proposal(_RUN_ID, _planning_state())
    assert legacy.assumptions is None and legacy.symbols is None
    assert "assumptions" in legacy.model_dump() and legacy.model_dump()["assumptions"] is None

    degraded = _plan_proposal(_RUN_ID, _planning_state(assumptions=None, symbols=None))
    assert degraded.assumptions is None and degraded.symbols is None


def test_plan_tables_projection_drops_malformed_rows_and_refolds_scope(validate_contract):
    """契约 additionalProperties=false + 枚举硬约束：畸形行逐条剔除而不是打成 500；
    归属对不上现有方案的假设归全局、符号归共享。"""
    raw_assumptions = [
        {"id": "G1", "text": "需求服从泊松分布", "scope": "global", "basis": "题面", "impact": "medium", "status": "confirmed"},
        {"id": "A1", "text": "预算为硬约束", "scope": "A", "basis": "", "impact": "high", "status": "critical", "extra": "junk"},
        {"id": "C1", "text": "方案 C 已被归约掉", "scope": "C", "basis": "", "impact": "low", "status": "to_verify"},
        {"id": "", "text": "缺 id 剔除", "scope": "global", "basis": "", "impact": "low", "status": "confirmed"},
        {"id": "X1", "text": "", "scope": "global", "basis": "", "impact": "low", "status": "confirmed"},
        {"id": "X2", "text": "枚举越界剔除", "scope": "global", "basis": "", "impact": "severe", "status": "confirmed"},
        {"id": "X3", "text": "状态越界剔除", "scope": "global", "basis": "", "impact": "low", "status": "pending"},
        "not-a-dict",
    ]
    raw_symbols = [
        {"symbol": "$x_i$", "kind": "variable", "definition": "是否设站", "unit": "", "range": "{0,1}", "plan_id": "A"},
        {"symbol": "K", "kind": "parameter", "definition": "预算", "unit": "万元", "range": None, "plan_id": "C"},
        {"symbol": "\\mathcal{I}", "kind": "set", "definition": "候选点集合", "plan_id": None},
        {"symbol": "", "kind": "parameter", "definition": "空符号剔除"},
        {"symbol": "y", "kind": "parameter", "definition": ""},
        {"symbol": "w", "kind": "decision", "definition": "kind 越界剔除"},
        {"symbol": "v", "kind": "variable", "definition": "unit 不是字符串", "unit": 3, "range": ["a"], "plan_id": 7},
        42,
    ]
    proposal = _plan_proposal(_RUN_ID, _planning_state(assumptions=raw_assumptions, symbols=raw_symbols))
    payload = proposal.model_dump(mode="json")
    validate_contract("plan-proposal.schema.json", payload)

    assert payload["assumptions"] == [
        {"id": "G1", "text": "需求服从泊松分布", "scope": "global", "basis": "题面", "impact": "medium", "status": "confirmed"},
        {"id": "A1", "text": "预算为硬约束", "scope": "A", "basis": "", "impact": "high", "status": "critical"},
        {"id": "C1", "text": "方案 C 已被归约掉", "scope": "global", "basis": "", "impact": "low", "status": "to_verify"},
    ]
    assert payload["symbols"] == [
        {"symbol": "x_i", "kind": "variable", "definition": "是否设站", "unit": None, "range": "{0,1}", "plan_id": "A"},
        {"symbol": "K", "kind": "parameter", "definition": "预算", "unit": "万元", "range": None, "plan_id": None},
        {"symbol": "\\mathcal{I}", "kind": "set", "definition": "候选点集合", "unit": None, "range": None, "plan_id": None},
        {"symbol": "v", "kind": "variable", "definition": "unit 不是字符串", "unit": None, "range": None, "plan_id": None},
    ]


def _g1_approval(
    *,
    step_id: str = "step_g1",
    option_id: str = "approve",
    comment: str | None = None,
    resolved_at: str = "2026-09-05T12:45:10.000000Z",
    status: str = "RESOLVED",
    decision_type: str = "confirm_plan",
    approval_id: str = "appr_1",
) -> ApprovalRequestRow:
    """一条（未落库的）G1 审批行：evidence.requested_by_step 指向产出方案的那一趟。"""
    return ApprovalRequestRow(
        id=approval_id,
        run_id=_RUN_ID,
        decision_type=decision_type,
        title="请确认建模方案",
        options=[{"id": "approve", "label": "采用推荐方案 A"}, {"id": "adopt:B", "label": "改用方案 B"}, {"id": "reject", "label": "退回重做方案"}],
        evidence={"note": "请确认建模方案", "requested_by_step": step_id, "gate": "G1"},
        status=status,
        requested_at=datetime(2026, 9, 5, 12, 30, 1, tzinfo=timezone.utc),
        resolution=None if status != "RESOLVED" else {
            "option_id": option_id, "actor": "user", "comment": comment, "resolved_at": resolved_at,
        },
    )


def test_plan_decision_projection_matches_the_approval_to_this_plan_version(validate_contract):
    """决策台账进 PlanProposal.decision（H3）：只认「对这一版方案」的正向确认，
    chosen_plan_id 复现下游选案规则，前端与实验阶段看到同一个方案。"""
    state = _planning_state()
    state.step_id = "step_g1"

    # 还没确认（审批 PENDING）/ 没有任何审批 / 旧运行没有 step id → null
    assert _plan_proposal(_RUN_ID, state).decision is None
    assert _plan_proposal(_RUN_ID, state, [_g1_approval(status="PENDING")]).decision is None
    legacy = _planning_state()
    assert _plan_proposal(_RUN_ID, legacy, [_g1_approval()]).decision is None

    # 采用推荐案：chosen = recommended；备注留空 → null
    approved = _plan_proposal(_RUN_ID, state, [_g1_approval(comment="  ")])
    payload = approved.model_dump(mode="json")
    validate_contract("plan-proposal.schema.json", payload)
    assert payload["decision"] == {
        "approval_id": "appr_1",
        "option_id": "approve",
        "chosen_plan_id": "A",
        "actor": "user",
        "comment": None,
        "resolved_at": "2026-09-05T12:45:10.000000Z",
    }
    # 旧运行的方案卡没有 language → null（消费者按 python 理解）
    assert [plan["language"] for plan in payload["plans"]] == [None, None]

    # 改用备选案 B，带备注原文
    adopted = _plan_proposal(_RUN_ID, state, [_g1_approval(option_id="adopt:B", comment="先跑基线")])
    assert adopted.decision.chosen_plan_id == "B" and adopted.decision.option_id == "adopt:B"
    assert adopted.decision.comment == "先跑基线"
    # adopt 指向已不存在的方案 id → 回到推荐案（与 chosen_plan 同规则）
    dangling = _plan_proposal(_RUN_ID, state, [_g1_approval(option_id="adopt:Z")])
    assert dangling.decision.chosen_plan_id == "A"


def test_plan_decision_projection_ignores_other_gates_and_stale_rounds():
    """退回重做后是新趟、新审批：旧趟的确认、拒绝、G2 / 修订门都不算这一版的决策。"""
    state = _planning_state(plans=[
        {"id": "A", "name": "整数规划", "approach": "MILP", "steps": ["建模"], "risks": [], "language": "Python"},
    ])
    state.step_id = "step_round2"
    rows = [
        _g1_approval(step_id="step_round1", option_id="adopt:B", approval_id="appr_old"),   # 上一趟的确认
        _g1_approval(step_id="step_round1", option_id="reject", approval_id="appr_rejected"),
        _g1_approval(step_id="step_round2", option_id="reject", approval_id="appr_reject2"),  # 拒绝不落台账
        _g1_approval(step_id="step_round2", decision_type="generic", approval_id="appr_revision"),
        _g1_approval(step_id="step_round2", decision_type="data_gate", option_id="adopt_cleaned", approval_id="appr_g2"),
    ]
    assert _plan_proposal(_RUN_ID, state, rows).decision is None

    rows.append(_g1_approval(step_id="step_round2", approval_id="appr_new", resolved_at="2026-09-05T13:00:00.000000Z"))
    proposal = _plan_proposal(_RUN_ID, state, rows)
    assert proposal.decision.approval_id == "appr_new" and proposal.decision.chosen_plan_id == "A"
    # 方案卡语言透传为小写标识
    assert proposal.plans[0].language == "python"


def test_stage_outputs_requires_ownership(client, second_client, make_run):
    """越权访问：他人任务一律 404，不泄露资源是否存在。"""
    run = make_run("归属校验")
    register_user(second_client, "stage-outputs-other@test.dev")

    response = second_client.get(f"{API}/task-runs/{run['id']}/stage-outputs")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"

    missing = client.get(f"{API}/task-runs/run_{'0' * 32}/stage-outputs")
    assert missing.status_code == 404
