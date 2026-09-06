"""生成者-评审者（§8.4）纯函数件：复跑核对、终答归一化、材料拼接。"""

import math

from omm_agent_skills import (
    REVIEW_MAX_ROUNDS,
    REVIEW_PROMPT_ID,
    REVIEWER_KNOWLEDGE_TOOL_NAMES,
    REVIEWER_MAX_TOOL_ROUNDS,
    REVIEWER_TOOL_NAMES,
    compare_metrics,
    normalize_verdict,
    reviewer_tool_brief,
)
from omm_agent_skills.review import (
    REVIEW_MAX_FINDINGS,
    findings_material,
    rerun_material,
    review_feedback,
    verdict_summary_text,
)


def test_review_constants_are_the_design_values():
    assert REVIEW_PROMPT_ID == "experiment_review.default"
    assert REVIEW_MAX_ROUNDS == 2
    assert REVIEWER_MAX_TOOL_ROUNDS == 3
    assert REVIEWER_TOOL_NAMES == ("ws_read", "ws_list")
    assert REVIEWER_KNOWLEDGE_TOOL_NAMES == ("knowledge_search", "knowledge_read")
    assert "python_run" not in REVIEWER_TOOL_NAMES and "ws_write" not in REVIEWER_TOOL_NAMES


# -- compare_metrics -------------------------------------------------------------


def test_compare_metrics_accepts_identical_and_float_noise_only():
    assert compare_metrics({"rmse": 0.12, "n": 8}, {"rmse": 0.12, "n": 8}) == (True, [])
    assert compare_metrics({"rmse": 0.12}, {"rmse": 0.12 + 1e-12}) == (True, [])
    assert compare_metrics({"rmse": 1e6}, {"rmse": 1e6 * (1 + 1e-8)}) == (True, [])
    assert compare_metrics({"x": math.nan}, {"x": math.nan}) == (True, [])


def test_compare_metrics_reports_every_difference_by_key():
    consistent, diffs = compare_metrics(
        {"rmse": 0.12, "mae": 0.5, "label": "a"},
        {"rmse": 0.3, "label": "b", "extra": 1},
    )
    assert consistent is False
    assert diffs == [
        "extra：首跑缺失，复跑 1",
        "label：首跑 'a'，复跑 'b'",
        "mae：首跑 0.5，复跑缺失",
        "rmse：首跑 0.12，复跑 0.3",
    ]


def test_compare_metrics_treats_a_missing_marker_line_as_not_reproducible():
    assert compare_metrics({"rmse": 0.12}, {}) == (False, ["复跑未打印 OMM_METRICS_JSON 标记行"])


def test_compare_metrics_does_not_confuse_bools_with_numbers():
    consistent, diffs = compare_metrics({"ok": True}, {"ok": 1})
    assert consistent is False and diffs == ["ok：首跑 True，复跑 1"]


# -- normalize_verdict -----------------------------------------------------------


def test_normalize_verdict_keeps_a_reject_only_when_it_names_a_blocker():
    rejected = normalize_verdict({
        "verdict": "REJECT",
        "findings": [
            {"id": "R2", "severity": "minor", "issue": "命名"},
            {"severity": "critical", "location": "baseline", "issue": "口径不一致", "fix": "同一切分"},
        ],
        "summary": " 不可信 ",
    })
    assert rejected["verdict"] == "reject" and rejected["blockers"] == 1
    # blocker 排前、缺 id 按序号补、别名收敛、fix 别名归 fix_hint
    assert rejected["findings"][0] == {
        "id": "R2",
        "severity": "blocker",
        "location": "baseline",
        "issue": "口径不一致",
        "fix_hint": "同一切分",
    }
    assert rejected["findings"][1]["id"] == "R2" and rejected["findings"][1]["severity"] == "minor"
    assert rejected["summary"] == "不可信"

    softened = normalize_verdict({
        "verdict": "reject",
        "findings": [{"severity": "major", "issue": "基线太弱"}],
        "summary": "",
    })
    assert softened["verdict"] == "accept" and softened["blockers"] == 0
    assert softened["findings"][0]["severity"] == "major"


def test_normalize_verdict_defaults_unknown_words_to_accept_and_drops_junk_findings():
    result = normalize_verdict({
        "verdict": "maybe",
        "findings": ["not a dict", {"severity": "blocker"}, {"issue": "   "}, {"issue": "ok", "severity": "weird"}],
    })
    assert result["verdict"] == "accept"
    assert result["findings"] == [
        {"id": "R4", "severity": "minor", "location": "", "issue": "ok", "fix_hint": ""}
    ]
    assert normalize_verdict({})["findings"] == []


def test_normalize_verdict_caps_findings_and_keeps_blockers_first():
    raw = [{"id": f"m{i}", "severity": "minor", "issue": f"minor {i}"} for i in range(10)]
    raw.append({"id": "b", "severity": "blocker", "issue": "阻断"})
    result = normalize_verdict({"verdict": "reject", "findings": raw})
    assert len(result["findings"]) == REVIEW_MAX_FINDINGS
    assert result["findings"][0]["id"] == "b"
    assert result["verdict"] == "reject" and result["blockers"] == 1


# -- material --------------------------------------------------------------------


def test_reviewer_tool_brief_lists_only_the_granted_tools():
    plain = reviewer_tool_brief(REVIEWER_TOOL_NAMES)
    assert "- ws_read：" in plain and "- ws_list：" in plain
    assert "knowledge_search" not in plain and "python_run" not in plain
    assert "按「输出要求」输出终答 JSON" in plain
    assert f"至多 {REVIEWER_MAX_TOOL_ROUNDS} 次" in plain
    assert "至少给出一条 blocker" in plain

    with_knowledge = reviewer_tool_brief(REVIEWER_TOOL_NAMES + REVIEWER_KNOWLEDGE_TOOL_NAMES)
    assert "- knowledge_search：" in with_knowledge and "- knowledge_read：" in with_knowledge
    assert "标出处 id" in with_knowledge


def test_rerun_material_states_the_facts_for_each_outcome():
    assert rerun_material({"executed": False, "reason": "剩余预算不足以复跑核对"}) == "未复跑：剩余预算不足以复跑核对"
    assert "逐键一致" in rerun_material({"executed": True, "consistent": True})
    inconsistent = rerun_material({
        "executed": True,
        "consistent": False,
        "reason": "复跑指标与首跑不一致",
        "diff": ["rmse：首跑 0.12，复跑 0.3"],
    })
    assert "与首跑不一致" in inconsistent
    assert "- 复跑指标与首跑不一致" in inconsistent and "- rmse：首跑 0.12，复跑 0.3" in inconsistent


def test_findings_material_and_review_feedback_are_structured_not_transcripts():
    findings = [
        {"id": "R1", "severity": "blocker", "location": "baseline", "issue": "口径不一致", "fix_hint": "同一切分"},
        {"id": "R2", "severity": "minor", "issue": "命名"},
    ]
    assert findings_material(findings) == (
        "[R1｜blocker] baseline：口径不一致（修法：同一切分）\n[R2｜minor] 命名"
    )
    assert findings_material([]) == "无"

    feedback = review_feedback(findings, "暂不可信", {"executed": True, "consistent": False, "diff": ["rmse：首跑 0.12，复跑 0.3"]})
    assert feedback.startswith("## 审稿驳回意见")
    assert "必须逐条处理并重新运行" in feedback
    assert "[R1｜blocker] baseline：口径不一致" in feedback
    assert "审稿总结：暂不可信" in feedback
    assert "复跑核对：" in feedback and "rmse：首跑 0.12，复跑 0.3" in feedback
    quiet = review_feedback(findings, "", {"executed": True, "consistent": True})
    assert "审稿总结" not in quiet and "复跑核对" not in quiet


def test_verdict_summary_text_covers_accept_stalemate_and_skipped():
    assert verdict_summary_text({"executed": False, "reason": "未配置子代理监督者"}) == "未经独立审稿：未配置子代理监督者"
    assert verdict_summary_text({
        "executed": True, "rounds": 1, "verdict": "accept", "findings": [{}, {}], "blockers": 0,
    }) == "独立审稿通过（1 轮，2 条意见）"
    text = verdict_summary_text({
        "executed": True, "rounds": 2, "verdict": "reject", "blockers": 1,
        "stalemate": True, "reason": "审稿 2 轮后仍有阻断性意见未解决",
    })
    assert text == "独立审稿 2 轮后仍有 1 条阻断性意见未解决（审稿 2 轮后仍有阻断性意见未解决），交结果采用闸门裁定"
