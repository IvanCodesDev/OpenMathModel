"""从 schemas/<version> 生成 Pydantic v2 模型（确定性输出）。

用法（使用 packages/contracts/.venv 或任何装有 requirements-dev.txt 的 Python）:
    python scripts/generate_python.py            # 生成/覆盖 src/omm_contracts/v1/
    python scripts/generate_python.py --check    # 只比对不落盘，不一致退出码 1（CI 用）
    python scripts/generate_python.py --verify   # fixtures 双路验证（valid 必须通过）

约定：
- jsonschema（validate.py）是权威校验门禁；Pydantic 模型是服务端的类型便利层。
- invalid fixtures 若被 Pydantic 接受，只告警不阻断（Pydantic 略松于 JSON Schema 属预期）。
- 生成风格与 datamodel-code-generator 版本绑定（requirements-dev.txt 锁定）；升级工具须
  重新生成并把 diff 一并提交，CI 的 --check 会拦截生成物与 Schema/工具不同步。
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
from pathlib import Path

CONTRACTS_ROOT = Path(__file__).resolve().parent.parent
VERSION = "v1"
SCHEMA_DIR = CONTRACTS_ROOT / "schemas" / VERSION
OUT_DIR = CONTRACTS_ROOT / "src" / "omm_contracts" / VERSION

HEADER = (
    f"# 本文件由 scripts/generate_python.py 从 schemas/{VERSION} 生成，禁止手改。\n"
    "# 重新生成：packages/contracts/.venv/Scripts/python scripts/generate_python.py"
)


def snake(key: str) -> str:
    return key.replace("-", "_")


def load_keys() -> list[tuple[str, str]]:
    """[(schema-key, 主类型名)]，按文件名排序保证确定性。"""
    pairs: list[tuple[str, str]] = []
    for schema_path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        key = schema_path.name[: -len(".schema.json")]
        schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
        pairs.append((key, schema.get("title") or key))
    if not pairs:
        raise FileNotFoundError(f"no schemas found under {SCHEMA_DIR}")
    return pairs


def generate_all(target: Path) -> None:
    from datamodel_code_generator import DataModelType, InputFileType, PythonVersion, generate

    target.mkdir(parents=True, exist_ok=True)
    pairs = load_keys()
    for key, _title in pairs:
        generate(
            SCHEMA_DIR / f"{key}.schema.json",
            input_file_type=InputFileType.JsonSchema,
            output=target / f"{snake(key)}.py",
            output_model_type=DataModelType.PydanticV2BaseModel,
            target_python_version=PythonVersion.PY_310,
            disable_timestamp=True,
            use_union_operator=True,
            use_schema_description=True,
            use_double_quotes=True,
            custom_file_header=HEADER,
        )
    init_lines = [HEADER, ""]
    init_lines += [f"from .{snake(key)} import {title}" for key, title in pairs]
    init_lines += [
        "",
        "__all__ = [" + ", ".join(f'"{title}"' for _key, title in pairs) + "]",
        "",
    ]
    (target / "__init__.py").write_text("\n".join(init_lines), encoding="utf-8")


def check() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        expected_dir = Path(tmp) / VERSION
        generate_all(expected_dir)
        problems: list[str] = []
        expected_files = sorted(p.name for p in expected_dir.glob("*.py"))
        actual_files = sorted(p.name for p in OUT_DIR.glob("*.py")) if OUT_DIR.exists() else []
        for name in expected_files:
            if name not in actual_files:
                problems.append(f"missing: src/omm_contracts/{VERSION}/{name}")
            elif (expected_dir / name).read_text(encoding="utf-8") != (OUT_DIR / name).read_text(encoding="utf-8"):
                problems.append(f"stale: src/omm_contracts/{VERSION}/{name}")
        for name in actual_files:
            if name not in expected_files:
                problems.append(f"extraneous: src/omm_contracts/{VERSION}/{name}")
        if problems:
            print("CONTRACTS_PY_STALE 生成物与 Schema/工具不同步，请重跑 scripts/generate_python.py")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f'CONTRACTS_PY_OK {{"files":{len(expected_files)},"version":"{VERSION}"}}')
        return 0


def verify() -> int:
    sys.path.insert(0, str(CONTRACTS_ROOT / "src"))
    from pydantic import ValidationError

    pairs = dict(load_keys())
    failures: list[str] = []
    accepted_invalid: list[str] = []
    valid_count = 0
    rejected_invalid = 0

    fixture_root = CONTRACTS_ROOT / "fixtures" / VERSION
    for path in sorted((fixture_root / "valid").glob("*.json")):
        key = path.name.split(".", 1)[0]
        module = importlib.import_module(f"omm_contracts.{VERSION}.{snake(key)}")
        model = getattr(module, pairs[key])
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        try:
            model.model_validate(payload)
            valid_count += 1
        except ValidationError as exc:
            failures.append(f"valid/{path.name}: {exc.errors()[0]}")

    for path in sorted((fixture_root / "invalid").glob("*.json")):
        key = path.name.split(".", 1)[0]
        module = importlib.import_module(f"omm_contracts.{VERSION}.{snake(key)}")
        model = getattr(module, pairs[key])
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        try:
            model.model_validate(payload)
            accepted_invalid.append(f"invalid/{path.name}")
        except ValidationError:
            rejected_invalid += 1

    if failures:
        print(f'CONTRACTS_PY_VERIFY_FAILED {{"valid_failures":{len(failures)}}}')
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f'CONTRACTS_PY_VERIFY_OK {{"valid":{valid_count},"invalid_rejected":{rejected_invalid},'
        f'"invalid_accepted_by_pydantic":{len(accepted_invalid)}}}'
    )
    for name in accepted_invalid:
        print(f"  ~ warn（Pydantic 松于 JSON Schema，已由 validate.py 权威拦截）: {name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate/verify Pydantic models from contract schemas.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check()
    if args.verify:
        return verify()
    generate_all(OUT_DIR)
    print(f'CONTRACTS_PY_GENERATED {{"files":{len(list(OUT_DIR.glob("*.py")))},"version":"{VERSION}"}}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
