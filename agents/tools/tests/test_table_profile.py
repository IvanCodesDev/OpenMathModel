"""table_profile：确定性画像的统计正确性、类型推断与边界。"""

from __future__ import annotations

import pytest

from omm_agent_tools import (
    RecordingInvoker,
    TaskWorkspace,
    ToolRegistry,
    profile_csv_text,
    table_profile_spec,
)

CSV = (
    "quarter,volume,region,note\n"
    "1,120.5,north,ok\n"
    "2,130.0,south,\n"
    "3,,north,fine\n"
    "4,150.5,east,ok\n"
)


def test_profile_statistics_are_deterministic_and_correct() -> None:
    profile = profile_csv_text(CSV)
    assert profile["rows"] == 4
    assert profile["truncated"] is False
    by_name = {column["name"]: column for column in profile["columns"]}

    quarter = by_name["quarter"]
    assert quarter["type"] == "int"
    assert (quarter["min"], quarter["max"], quarter["mean"]) == (1.0, 4.0, 2.5)

    volume = by_name["volume"]
    assert volume["type"] == "float"
    assert volume["missing"] == 1
    assert volume["mean"] == round((120.5 + 130.0 + 150.5) / 3, 4)

    region = by_name["region"]
    assert region["type"] == "str" and "min" not in region

    note = by_name["note"]
    assert note["missing"] == 1

    assert profile_csv_text(CSV) == profile, "同输入必产同画像"


def test_semicolon_delimiter_is_sniffed() -> None:
    profile = profile_csv_text("a;b\n1;2\n")
    assert profile["delimiter"] == ";"
    assert [column["name"] for column in profile["columns"]] == ["a", "b"]


def test_row_cap_truncates_honestly() -> None:
    body = "x\n" + "\n".join(str(i) for i in range(10))
    profile = profile_csv_text(body, max_rows=5)
    assert profile["rows"] == 5
    assert profile["truncated"] is True


def test_empty_csv_yields_empty_profile() -> None:
    assert profile_csv_text("") == {
        "rows": 0, "columns": [], "delimiter": ",", "truncated": False,
    }


@pytest.fixture()
def invoker(tmp_path) -> RecordingInvoker:
    workspace = TaskWorkspace(tmp_path, "run_profile")
    workspace.write_text("data/orders.csv", CSV)
    registry = ToolRegistry()
    registry.register(table_profile_spec(workspace))
    return RecordingInvoker(registry, lambda *_: None, caller_max_tier="readonly")


def test_tool_reads_workspace_and_reports_path(invoker) -> None:
    result = invoker.invoke("r", "s", "table_profile", {"path": "data/orders.csv"})
    assert result.ok
    assert result.output["path"] == "data/orders.csv"
    assert result.output["rows"] == 4


def test_tool_rejects_escape_and_missing_file(invoker) -> None:
    escaped = invoker.invoke("r", "s", "table_profile", {"path": "../secrets.csv"})
    assert not escaped.ok and "escapes workspace" in (escaped.error or "")
    missing = invoker.invoke("r", "s", "table_profile", {"path": "data/nope.csv"})
    assert not missing.ok and "不存在" in (missing.error or "")
