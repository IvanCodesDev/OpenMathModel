import pytest

from omm_agent_skills import (
    PromptFormatError,
    PromptRenderError,
    load_default_registry,
    parse_prompt_text,
)

VALID = """---
id: demo.default
stage: EXPERIMENTING
variant: default
version: 3
input_schema: {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
output_schema: {"type": "object", "required": ["ok"]}
---
Hello {{name}}, config={{config}}.
"""


def test_parse_valid_prompt():
    template = parse_prompt_text(VALID, "demo.prompt.md")
    assert template.id == "demo.default"
    assert template.stage == "EXPERIMENTING"
    assert template.version == 3
    assert template.input_schema["required"] == ["name"]
    assert template.placeholders() == {"name", "config"}


def test_render_fills_and_serializes_non_strings():
    template = parse_prompt_text(VALID)
    text = template.render({"name": "建模者", "config": {"lr": 0.1}})
    assert "Hello 建模者" in text
    assert '"lr": 0.1' in text.replace("'", '"')


def test_render_missing_variable_raises():
    template = parse_prompt_text(VALID)
    with pytest.raises(PromptRenderError, match="config"):
        template.render({"name": "x"})


@pytest.mark.parametrize(
    "mutation, message",
    [
        (VALID.replace("---\n", "", 1), "opening"),
        (VALID.replace("id: demo.default\n", ""), "missing 'id'"),
        (VALID.replace('{"type": "object", "required": ["name"]', '{bad json', 1), "invalid JSON"),
    ],
)
def test_parse_errors(mutation, message):
    with pytest.raises(PromptFormatError, match=message):
        parse_prompt_text(mutation)


def test_parse_requires_closing_fence_and_body():
    with pytest.raises(PromptFormatError, match="closing"):
        parse_prompt_text("---\nid: x\n")
    headers = "---\nid: x\nstage: S\nvariant: v\nversion: 1\n---\n"
    with pytest.raises(PromptFormatError, match="empty prompt body"):
        parse_prompt_text(headers)


def test_default_registry_loads_all_stage_prompts():
    registry = load_default_registry()
    assert registry.ids() == [
        "data_cleaning.sandbox",
        "data_preparation.default",
        "experiment_code.default",
        "experiment_code.sandbox",
        "model_planning.default",
        "model_planning.formalize",
        "model_planning.proposer",
        "model_planning.reduce",
        "paper_finalize.default",
        "paper_outline.default",
        "paper_section.default",
        "paper_writing.default",
        "problem_analysis.default",
        "validating.default",
        "validating.sandbox",
    ]

    # Every placeholder must be declared in the input schema, for every stage.
    for prompt_id in registry.ids():
        template = registry.get(prompt_id)
        assert template.placeholders() <= set(
            template.input_schema.get("properties", {})
        ), f"{prompt_id}: every placeholder must be declared in the input schema"

    planning = registry.get("model_planning.default")
    assert planning.stage == "MODEL_PLANNING"
    assert "plans" in planning.output_schema["required"]

    experiment = registry.get("experiment_code.default")
    assert experiment.stage == "EXPERIMENTING"
    assert "code" in experiment.output_schema["required"]


def test_sandbox_prompts_are_agent_task_cards_not_single_shot_templates():
    """沙盒执行体的两个模板：终答由内环校验，模板只负责角色与任务口径。

    单发模板（``experiment_code.default``）要求模型一次吐出 ``code``；沙盒
    模板下代码经 ``python_run`` 工具轮真跑，模板绝不能再要求整段代码，否则
    模型会把脚本塞进终答而永远不运行。
    """
    registry = load_default_registry()

    cleaning = registry.get("data_cleaning.sandbox")
    assert cleaning.stage == "DATA_PREPARATION"
    assert cleaning.placeholders() == {"preparation_plan", "data_files"}
    assert "cleaned/" in cleaning.body
    assert "OMM_METRICS_JSON" in cleaning.body

    experiment = registry.get("experiment_code.sandbox")
    assert experiment.stage == "EXPERIMENTING"
    assert "code" not in experiment.output_schema.get("required", [])
    assert {"approach_summary", "progress_note"} <= set(
        experiment.output_schema.get("required", [])
    )

    robustness = registry.get("validating.sandbox")
    assert robustness.stage == "VALIDATING"
    assert "code" not in robustness.output_schema.get("required", [])
    # 任务卡必须带实验脚本正文与风险点：复跑不是重新发明实验
    assert {"experiment_code", "risk_points", "metrics"} <= robustness.placeholders()
    assert "OMM_METRICS_JSON" in robustness.body and '"checks"' in robustness.body
    assert "禁止为了通过而事后放宽阈值" in robustness.body
