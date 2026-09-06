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
    register_user,
    run_status_is,
    wait_until,
)

from omm_api.orm import ApprovalRequestRow
from omm_api.stage_outputs import (
    StageState,
    _plan_proposal,
    _review_report,
    _robustness_report,
    _validation_report,
)
from omm_contracts.v1.experiment_summary import ReviewReport, ValidationReport
from test_task_runs_llm_nodes import (
    ANALYSIS_OUTPUT,
    EXPERIMENT_OUTPUT,
    PAPER_OUTPUT,
    PLANNING_OUTPUT,
    PREPARATION_OUTPUT,
    REVIEW_OUTPUT,
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
