"""契约向后兼容检查器（CI 阻断破坏性变更）。

用法:
    python packages/contracts/check_compat.py                 # 比对 schemas/v1 与 baseline/v1.baseline.json
    python packages/contracts/check_compat.py --freeze        # 冻结当前 schemas 为新基线（有意破坏性变更须显式重冻结并过评审）
    python packages/contracts/check_compat.py --current-dir D # 指定被检目录（测试用）

判定基准：旧消费者 + 新生产者（消费者按 README 规则容忍未知枚举值与新增字段）。

BREAKING（退出码 1）:
    - Schema / property / $defs 条目被删除
    - type / $ref / title 变化（title 决定生成的 TS 类型名）
    - enum 删除既有取值；const 变化
    - required 新增条目
    - pattern / format 变化
    - 数值边界收紧（minLength/minimum/minItems 提高；maxLength/maximum/maxItems 降低）
    - additionalProperties 由开放变为 false
    - oneOf/anyOf 删除既有备选分支

ADDITIVE（允许）:
    - 新 Schema / 新可选 property / enum 新增取值 / required 减少
    - 约束放宽（max* 提高、min* 降低、约束关键字整体移除、additionalProperties 放开）
    - oneOf/anyOf 新增备选分支

无基线时退出码 3（提示先 --freeze）。description/examples/$comment 忽略。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CONTRACTS_ROOT = Path(__file__).resolve().parent
VERSION = "v1"
IGNORED_KEYS = {"description", "examples", "$comment"}

# 约束移除 = 放宽，允许
RELAXABLE_KEYS = {
    "pattern", "format", "const", "maxLength", "maximum", "maxItems",
    "minLength", "minimum", "minItems", "additionalProperties",
}
# 数值边界：方向敏感
UPPER_BOUNDS = {"maxLength", "maximum", "maxItems"}   # 降低 = 收紧 = BREAKING
LOWER_BOUNDS = {"minLength", "minimum", "minItems"}   # 提高 = 收紧 = BREAKING


def canon(node: Any) -> str:
    def strip(n: Any) -> Any:
        if isinstance(n, dict):
            return {k: strip(v) for k, v in n.items() if k not in IGNORED_KEYS}
        if isinstance(n, list):
            return [strip(i) for i in n]
        return n
    return json.dumps(strip(node), sort_keys=True, ensure_ascii=False)


def norm_type(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return sorted(str(v) for v in value)
    return [json.dumps(value)]


def compare_node(base: Any, cur: Any, ptr: str, problems: list[str]) -> None:
    if not isinstance(base, dict) or not isinstance(cur, dict):
        if canon(base) != canon(cur):
            problems.append(f"{ptr}: value changed")
        return

    for key, base_val in base.items():
        if key in IGNORED_KEYS:
            continue
        here = f"{ptr}/{key}"

        if key not in cur:
            if key in RELAXABLE_KEYS:
                continue  # 约束移除 = 放宽
            problems.append(f"{here}: removed (BREAKING)")
            continue
        cur_val = cur[key]

        if key == "title":
            if base_val != cur_val:
                problems.append(f"{here}: title changed '{base_val}' -> '{cur_val}'（TS 类型名随之变化，BREAKING）")
        elif key == "type":
            if norm_type(base_val) != norm_type(cur_val):
                problems.append(f"{here}: type changed {base_val} -> {cur_val}")
        elif key == "$ref":
            if base_val != cur_val:
                problems.append(f"{here}: $ref changed {base_val} -> {cur_val}")
        elif key == "enum":
            removed = [v for v in base_val if v not in cur_val]
            if removed:
                problems.append(f"{here}: enum values removed {removed}")
        elif key == "required":
            added = [v for v in cur_val if v not in base_val]
            if added:
                problems.append(f"{here}: required entries added {added}")
        elif key in ("properties", "$defs"):
            for name, sub in base_val.items():
                if name not in cur_val:
                    problems.append(f"{here}/{name}: removed (BREAKING)")
                else:
                    compare_node(sub, cur_val[name], f"{here}/{name}", problems)
        elif key in ("oneOf", "anyOf"):
            cur_canons = {canon(alt) for alt in cur_val}
            for i, alt in enumerate(base_val):
                if canon(alt) not in cur_canons:
                    problems.append(f"{here}[{i}]: alternative removed or changed (BREAKING)")
        elif key in UPPER_BOUNDS:
            if isinstance(base_val, (int, float)) and isinstance(cur_val, (int, float)) and cur_val < base_val:
                problems.append(f"{here}: tightened {base_val} -> {cur_val}")
        elif key in LOWER_BOUNDS:
            if isinstance(base_val, (int, float)) and isinstance(cur_val, (int, float)) and cur_val > base_val:
                problems.append(f"{here}: tightened {base_val} -> {cur_val}")
        elif key == "additionalProperties":
            base_open = base_val is not False
            cur_open = cur_val is not False
            if base_open and not cur_open:
                problems.append(f"{here}: additionalProperties open -> false (BREAKING)")
            elif isinstance(base_val, dict) and isinstance(cur_val, dict):
                compare_node(base_val, cur_val, here, problems)
        elif key in ("pattern", "format", "const"):
            if base_val != cur_val:
                problems.append(f"{here}: {key} changed {base_val!r} -> {cur_val!r}")
        elif key == "items":
            compare_node(base_val, cur_val, here, problems)
        elif key == "prefixItems":
            if len(cur_val) < len(base_val):
                problems.append(f"{here}: prefixItems shortened")
            for i, sub in enumerate(base_val[: len(cur_val)]):
                compare_node(sub, cur_val[i], f"{here}[{i}]", problems)
        else:
            if canon(base_val) != canon(cur_val):
                problems.append(f"{here}: constraint changed")


def load_schemas(schema_dir: Path) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(schema_dir.glob("*.schema.json")):
        # utf-8-sig：兼容 Windows 工具链写入的 BOM
        schemas[path.name[: -len(".schema.json")]] = json.loads(path.read_text(encoding="utf-8-sig"))
    if not schemas:
        raise FileNotFoundError(f"no schemas found under {schema_dir}")
    return schemas


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenMathModel contract compatibility gate.")
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--freeze", action="store_true", help="freeze current schemas as the new baseline")
    parser.add_argument("--current-dir", default=None, help="override schemas dir under test")
    args = parser.parse_args()

    schema_dir = Path(args.current_dir) if args.current_dir else CONTRACTS_ROOT / "schemas" / args.version
    baseline_path = CONTRACTS_ROOT / "baseline" / f"{args.version}.baseline.json"
    current = load_schemas(schema_dir)

    if args.freeze:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({"version": args.version, "schemas": current}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f'CONTRACTS_BASELINE_FROZEN {{"schemas":{len(current)},"version":"{args.version}"}}')
        return 0

    if not baseline_path.exists():
        print(f'CONTRACTS_COMPAT_NO_BASELINE {{"hint":"run with --freeze first","path":"{baseline_path.as_posix()}"}}')
        return 3

    baseline = json.loads(baseline_path.read_text(encoding="utf-8-sig"))["schemas"]
    problems: list[str] = []
    for key, base_schema in baseline.items():
        if key not in current:
            problems.append(f"{key}: schema removed (BREAKING)")
            continue
        compare_node(base_schema, current[key], key, problems)

    if problems:
        print(f'CONTRACTS_COMPAT_BROKEN {{"problems":{len(problems)}}}')
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f'CONTRACTS_COMPAT_OK {{"schemas":{len(current)},"baseline":"{args.version}"}}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
