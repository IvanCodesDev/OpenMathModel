from omm_agent_skills import validate


def test_scalars_and_bool_integer_distinction():
    assert validate("x", {"type": "string"}) == []
    assert validate(1, {"type": "integer"}) == []
    assert validate(True, {"type": "integer"}) != []
    assert validate(True, {"type": "boolean"}) == []
    assert validate(1.5, {"type": "number"}) == []
    assert validate(None, {"type": "null"}) == []


def test_required_and_nested_properties():
    schema = {
        "type": "object",
        "required": ["name", "tags"],
        "properties": {
            "name": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    assert validate({"name": "a", "tags": ["x"]}, schema) == []

    problems = validate({"tags": ["x", 3]}, schema)
    assert any("missing required property 'name'" in p for p in problems)
    assert any("$.tags[1]" in p for p in problems)


def test_type_mismatch_reports_path_and_stops_descending():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    problems = validate([1, 2], schema)
    assert problems == ["$: expected object, got list"]


def test_enum():
    assert validate("A", {"enum": ["A", "B"]}) == []
    assert validate("C", {"enum": ["A", "B"]}) != []


def test_unsupported_type_is_reported_not_crashing():
    problems = validate("x", {"type": "unicorn"})
    assert "unsupported schema type" in problems[0]
