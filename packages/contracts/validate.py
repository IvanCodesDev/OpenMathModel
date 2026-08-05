"""OpenMathModel contracts self-check.

用法:
    python packages/contracts/validate.py [--version v1]

检查项:
1. schemas/<version>/*.schema.json 均为合法 JSON Schema (draft 2020-12)。
2. fixtures/<version>/valid/**  必须全部通过对应 Schema 校验。
3. fixtures/<version>/invalid/** 必须全部校验失败（防止 Schema 过松）。
4. 同名公共 $defs 在所有 Schema 中保持一致（忽略 description），防止自包含副本漂移。

fixture 文件名约定: "<schema-key>.<case>.json"，schema-key 即
"<schema-key>.schema.json"。退出码 0 = 全部通过。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

CONTRACTS_ROOT = Path(__file__).resolve().parent


def load_schemas(version: str = "v1") -> dict[str, dict[str, Any]]:
    """Load all schemas of a contract version, keyed by schema-key (e.g. "task-run")."""
    schema_dir = CONTRACTS_ROOT / "schemas" / version
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(schema_dir.glob("*.schema.json")):
        key = path.name[: -len(".schema.json")]
        schemas[key] = json.loads(path.read_text(encoding="utf-8"))
    if not schemas:
        raise FileNotFoundError(f"no schemas found under {schema_dir}")
    return schemas


def get_validator(schemas: dict[str, dict[str, Any]], key: str) -> Draft202012Validator:
    return Draft202012Validator(schemas[key])


def validate_payload(schemas: dict[str, dict[str, Any]], key: str, payload: Any) -> list[str]:
    """Validate one payload; return human-readable error list (empty = pass)."""
    validator = get_validator(schemas, key)
    errors = []
    for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{location}: {err.message}")
    return errors


def _strip_descriptions(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(item) for item in node]
    return node


def check_shared_defs(schemas: dict[str, dict[str, Any]]) -> list[str]:
    """Ensure same-named $defs are structurally identical across all schemas."""
    problems: list[str] = []
    seen: dict[str, tuple[str, str]] = {}
    for key, schema in schemas.items():
        for def_name, def_body in (schema.get("$defs") or {}).items():
            canonical = json.dumps(_strip_descriptions(def_body), sort_keys=True, ensure_ascii=False)
            if def_name in seen:
                first_key, first_canonical = seen[def_name]
                if canonical != first_canonical:
                    problems.append(
                        f"$defs/{def_name} drifted: {first_key} vs {key}"
                    )
            else:
                seen[def_name] = (key, canonical)
    return problems


def run(version: str) -> int:
    schemas = load_schemas(version)
    failures: list[str] = []
    checked = 0

    for key, schema in schemas.items():
        checked += 1
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # jsonschema raises SchemaError subclasses
            failures.append(f"[schema] {key}: invalid schema: {exc}")

    for drift in check_shared_defs(schemas):
        failures.append(f"[defs] {drift}")

    fixture_root = CONTRACTS_ROOT / "fixtures" / version
    for expectation, folder in (("valid", "valid"), ("invalid", "invalid")):
        for path in sorted((fixture_root / folder).glob("*.json")):
            checked += 1
            key = path.name.split(".", 1)[0]
            if key not in schemas:
                failures.append(f"[fixture] {path.name}: unknown schema key '{key}'")
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            errors = validate_payload(schemas, key, payload)
            if expectation == "valid" and errors:
                failures.append(f"[fixture] {folder}/{path.name} should PASS but failed: {errors[0]}")
            if expectation == "invalid" and not errors:
                failures.append(f"[fixture] {folder}/{path.name} should FAIL but passed")

    if failures:
        print(f"CONTRACTS CHECK FAILED ({len(failures)} problem(s), {checked} item(s) checked):")
        for line in failures:
            print(f"  - {line}")
        return 1

    print(
        f"CONTRACTS CHECK PASSED: {len(schemas)} schema(s), "
        f"{checked - len(schemas)} fixture(s), shared $defs consistent."
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate OpenMathModel contract schemas and fixtures.")
    parser.add_argument("--version", default="v1", help="contract version folder, default v1")
    args = parser.parse_args()
    sys.exit(run(args.version))


if __name__ == "__main__":
    main()
