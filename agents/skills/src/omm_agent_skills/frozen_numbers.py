"""数字冻结清单与数值审计（H5 切片 1；§9 硬规则「正文数字只来自产物注入」的落地）。

清单从上游各阶段的**结构化产出**里确定性抽取「值 + 出处」，不经模型转述：

- EXPERIMENTING ``metrics``：沙盒标记行注入的实验指标；
- VALIDATING ``robustness.checks[]``：稳健性复跑逐项的实测值与阈值（G3 的判定依据）；
- DATA_PREPARATION ``cleaning``：清洗影响面统计（清洗前后行数、删行比例）；
- MODEL_PLANNING 选中方案：``approach`` / ``steps`` 文本里出现的数值，带上下文片段作标签
  （方案没有结构化参数字段，这是确定性抽取能做到的上限）。

用法：清单渲染成表进论文各章的材料（模型只准引用清单与材料里的数字）、原样进
DocumentDraft（论文页与审计链消费）；审计把正文里的数值 token 与「冻结值 ∪ 材料里出现的
数值」对账，对不上的即为无出处数字。题面常数不进清单（用户拍板）、靠材料文本放行。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from omm_agent_core import TaskState

#: 正文 / 材料里的数值 token（无符号；负号与百分号不参与对账）。
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")

#: 方案文本抽数的上限：方案里的数字多半是步骤序号与量级描述，再多只是噪声。
PLAN_NUMBER_LIMIT = 12
#: 方案数值的上下文片段长度（单侧），让人看得出这个数在说什么。
_PLAN_CONTEXT_CHARS = 16
#: 审计取样上限：报得再多也是同一个问题，卡片与警告只需要样例。
AUDIT_SAMPLE_LIMIT = 8

FINDING_UNSOURCED_NUMBER = "unsourced_number"


def normalize_number_token(token: str) -> str | None:
    """数值 token 的对账口径：``0.50`` ≡ ``0.5``、``1200.0`` ≡ ``1200``；不是数就 None。"""
    try:
        value = Decimal(str(token).strip())
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    text = format(abs(value.normalize()), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entry(
    entry_id: str,
    label: str,
    value: int | float,
    stage: TaskState,
    path: str,
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "label": label,
        "value": value,
        "source_stage": stage.value,
        "source_path": path,
    }


def _metric_entries(experiment: Mapping[str, Any]) -> list[dict[str, Any]]:
    metrics = experiment.get("metrics")
    if not isinstance(metrics, Mapping):
        return []
    entries = []
    for name, value in metrics.items():
        if not _is_number(value):
            continue
        key = str(name).strip()
        if not key:
            continue
        entries.append(
            _entry(
                f"metrics.{key}",
                f"实验指标 {key}",
                value,
                TaskState.EXPERIMENTING,
                f"metrics.{key}",
            )
        )
    return entries


def _robustness_entries(validation: Mapping[str, Any]) -> list[dict[str, Any]]:
    robustness = validation.get("robustness")
    if not isinstance(robustness, Mapping) or not robustness.get("executed"):
        return []
    entries = []
    for index, check in enumerate(robustness.get("checks") or []):
        if not isinstance(check, Mapping):
            continue
        check_id = str(check.get("id") or f"check{index}").strip() or f"check{index}"
        name = str(check.get("name") or check_id)
        path = f"robustness.checks[{index}]"
        if _is_number(check.get("value")):
            entries.append(
                _entry(
                    f"robustness.{check_id}.value",
                    f"稳健性检查「{name}」实测值",
                    check["value"],
                    TaskState.VALIDATING,
                    f"{path}.value",
                )
            )
        threshold = check.get("threshold")
        if _is_number(threshold):
            entries.append(
                _entry(
                    f"robustness.{check_id}.threshold",
                    f"稳健性检查「{name}」阈值",
                    threshold,
                    TaskState.VALIDATING,
                    f"{path}.threshold",
                )
            )
        elif isinstance(threshold, str):
            # 文字阈值（如「≤ 0.05」）：把其中的数值逐个冻结，正文引用阈值时才对得上账
            for order, token in enumerate(NUMBER_PATTERN.findall(threshold)):
                entries.append(
                    _entry(
                        f"robustness.{check_id}.threshold.{order}",
                        f"稳健性检查「{name}」阈值（{threshold.strip()}）",
                        _token_value(token),
                        TaskState.VALIDATING,
                        f"{path}.threshold",
                    )
                )
    return entries


_CLEANING_FIELDS = (
    ("rows_before", "清洗前数据行数"),
    ("rows_after", "清洗后数据行数"),
    ("rows_deleted_ratio", "清洗删行比例"),
)


def _cleaning_entries(preparation: Mapping[str, Any]) -> list[dict[str, Any]]:
    cleaning = preparation.get("cleaning")
    if not isinstance(cleaning, Mapping) or not cleaning.get("executed"):
        return []
    entries = []
    for key, label in _CLEANING_FIELDS:
        value = cleaning.get(key)
        if _is_number(value):
            entries.append(
                _entry(
                    f"cleaning.{key}",
                    label,
                    value,
                    TaskState.DATA_PREPARATION,
                    f"cleaning.{key}",
                )
            )
    return entries


def _token_value(token: str) -> int | float:
    return float(token) if "." in token else int(token)


def _plan_entries(planning: Mapping[str, Any]) -> list[dict[str, Any]]:
    """选中方案文本里的数值（步骤序号这类一位数不计，与审计口径一致）。"""
    plans = [plan for plan in planning.get("plans") or [] if isinstance(plan, Mapping)]
    if not plans:
        return []
    recommended = planning.get("recommended_plan_id")
    plan = next((p for p in plans if p.get("id") == recommended), plans[0])
    plan_id = str(plan.get("id") or "").strip() or "0"
    name = str(plan.get("name") or plan_id)
    fragments: list[tuple[str, str]] = [("approach", str(plan.get("approach") or ""))]
    for index, step in enumerate(plan.get("steps") or []):
        fragments.append((f"steps[{index}]", str(step)))
    entries: list[dict[str, Any]] = []
    for path, text in fragments:
        for match in NUMBER_PATTERN.finditer(text):
            token = match.group(0)
            if len(token) < 2:
                continue
            start = max(0, match.start() - _PLAN_CONTEXT_CHARS)
            end = min(len(text), match.end() + _PLAN_CONTEXT_CHARS)
            context = text[start:end].replace("\n", " ").strip()
            entries.append(
                _entry(
                    f"plan.{plan_id}.{path}.{len(entries)}",
                    f"方案「{name}」{path}：…{context}…",
                    _token_value(token),
                    TaskState.MODEL_PLANNING,
                    f"plans[{plan_id}].{path}",
                )
            )
            if len(entries) >= PLAN_NUMBER_LIMIT:
                return entries
    return entries


def build_frozen_numbers(prior_outputs: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """四类来源 → 冻结清单（顺序固定：指标、稳健性、清洗统计、方案参数）。"""
    entries: list[dict[str, Any]] = []
    entries += _metric_entries(prior_outputs.get(TaskState.EXPERIMENTING.value) or {})
    entries += _robustness_entries(prior_outputs.get(TaskState.VALIDATING.value) or {})
    entries += _cleaning_entries(prior_outputs.get(TaskState.DATA_PREPARATION.value) or {})
    entries += _plan_entries(prior_outputs.get(TaskState.MODEL_PLANNING.value) or {})
    return entries


def render_frozen_numbers(entries: Sequence[Mapping[str, Any]]) -> str:
    """清单 → 提示词材料（Markdown 表）：模型只准引用表中数值，且不得改写。"""
    if not entries:
        return "（无：上游阶段没有产出可冻结的数字，正文只能引用材料中已有的数值）"
    lines = [
        "正文中的数值只准引用本表与材料中已有的数字，引用时保持数值原样（不换算、不四舍五入）：",
        "",
        "| 编号 | 数值 | 含义 | 出处 |",
        "| --- | --- | --- | --- |",
    ]
    for entry in entries:
        lines.append(
            f"| {entry['id']} | {_format_value(entry['value'])} | {entry['label']} "
            f"| {entry['source_stage']}.{entry['source_path']} |"
        )
    return "\n".join(lines)


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        text = repr(value)
        return text
    return str(value)


def number_tokens(*texts: str) -> set[str]:
    """文本里出现的全部数值（归一化）。"""
    tokens: set[str] = set()
    for text in texts:
        for token in NUMBER_PATTERN.findall(text or ""):
            canonical = normalize_number_token(token)
            if canonical is not None:
                tokens.add(canonical)
    return tokens


def allowed_number_tokens(entries: Iterable[Mapping[str, Any]], *material_texts: str) -> set[str]:
    """审计允许集 = 冻结值 ∪ 材料文本里出现的数值。"""
    allowed = number_tokens(*material_texts)
    for entry in entries:
        canonical = normalize_number_token(_format_value(entry.get("value")))
        if canonical is not None:
            allowed.add(canonical)
    return allowed


def unsourced_numbers(text: str, allowed: set[str]) -> list[str]:
    """正文里对不上允许集的数值（原样 token）。

    一位数不计（章节号 / 序号 / 列表编号会大量误报），同一数值只报一次，
    最多取样 AUDIT_SAMPLE_LIMIT 个。
    """
    missing: list[str] = []
    seen: set[str] = set()
    for token in NUMBER_PATTERN.findall(text or ""):
        if len(token) < 2:
            continue
        canonical = normalize_number_token(token)
        if canonical is None or canonical in seen or canonical in allowed:
            continue
        seen.add(canonical)
        missing.append(token)
        if len(missing) >= AUDIT_SAMPLE_LIMIT:
            break
    return missing


def audit_document(
    sections: Sequence[Mapping[str, Any]],
    abstract: str,
    allowed: set[str],
    abstract_allowed: set[str] | None = None,
) -> list[dict[str, Any]]:
    """整篇终稿的数字审计 → 审计发现列表（进 G4 卡片与 DocumentDraft）。

    每章一条、摘要一条；没有发现就是空列表（= 0 违规，G4 推荐确认交付）。
    """
    findings: list[dict[str, Any]] = []
    for index, section in enumerate(sections, start=1):
        heading = str(section.get("heading") or f"第 {index} 章")
        numbers = unsourced_numbers(str(section.get("content") or ""), allowed)
        if numbers:
            findings.append(_finding(f"第{index}章《{heading}》", numbers))
    scope_allowed = abstract_allowed if abstract_allowed is not None else allowed
    numbers = unsourced_numbers(abstract, scope_allowed)
    if numbers:
        findings.append(_finding("摘要", numbers))
    return findings


def _finding(scope: str, numbers: list[str]) -> dict[str, Any]:
    return {
        "scope": scope,
        "kind": FINDING_UNSOURCED_NUMBER,
        "numbers": list(numbers),
        "detail": (
            f"{scope}有 {len(numbers)} 个数值不在冻结清单与材料中"
            f"（{'、'.join(numbers[:3])}{'…' if len(numbers) > 3 else ''}）"
        ),
    }
