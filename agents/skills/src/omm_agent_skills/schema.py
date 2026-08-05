"""Minimal JSON-schema-subset validator for structured LLM outputs.

Supports exactly what the prompt IO schemas use today: ``type`` (object,
array, string, number, integer, boolean, null), ``required``, ``properties``,
``items`` and ``enum``. It exists to gate model output at the node boundary
without pulling a third-party dependency into the agent domain; if/when
packages/contracts standardizes a schema library, this module is the single
swap point.
"""

from __future__ import annotations

from typing import Any

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # bool is an int subclass in Python; exclude it from number/integer.
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable violations; empty list means valid."""
    problems: list[str] = []

    expected_type = schema.get("type")
    if expected_type is not None:
        check = _TYPE_CHECKS.get(expected_type)
        if check is None:
            problems.append(f"{path}: unsupported schema type {expected_type!r}")
            return problems
        if not check(value):
            problems.append(
                f"{path}: expected {expected_type}, got {type(value).__name__}"
            )
            return problems  # deeper checks are meaningless on a type mismatch

    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} not in enum {schema['enum']!r}")

    if expected_type == "object":
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing required property {key!r}")
        for key, sub_schema in (schema.get("properties") or {}).items():
            if key in value:
                problems.extend(validate(value[key], sub_schema, f"{path}.{key}"))

    if expected_type == "array":
        items = schema.get("items")
        if items:
            for index, item in enumerate(value):
                problems.extend(validate(item, items, f"{path}[{index}]"))

    return problems
