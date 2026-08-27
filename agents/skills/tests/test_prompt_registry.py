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
        "data_preparation.default",
        "experiment_code.default",
        "model_planning.default",
        "paper_writing.default",
        "problem_analysis.default",
        "validating.default",
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
