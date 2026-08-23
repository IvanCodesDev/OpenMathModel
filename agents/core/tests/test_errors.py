"""Error taxonomy (D2.1): stable codes, catalog completeness, payload shape."""

from __future__ import annotations

import pytest
from omm_agent_core.errors import CATALOG, AgentError, Disposition, ErrorCode


def test_every_code_has_a_catalog_entry():
    assert set(CATALOG) == set(ErrorCode)
    for code, info in CATALOG.items():
        assert info.code is code
        assert info.owner in {
            "gateway", "loop", "toolbus", "budget", "scheduler", "supervisor",
        }
        assert info.summary


def test_code_values_are_stable():
    """The string values are persisted in events — changing one is a breaking
    change and must be caught here, not discovered in an event log."""
    expected = {
        "LLM_NETWORK": "E110",
        "LLM_SCHEMA_VIOLATION": "E120",
        "LLM_CONTENT_REFUSAL": "E130",
        "LLM_PROVIDER_QUOTA": "E140",
        "TOOL_BAD_ARGS": "E210",
        "TOOL_TIMEOUT": "E220",
        "TOOL_CRASH": "E230",
        "TOOL_TIER_DENIED": "E240",
        "TOOL_IDEMPOTENCY_CONFLICT": "E250",
        "BUDGET_RUN": "E310",
        "BUDGET_NODE": "E320",
        "BUDGET_LOOP": "E330",
        "LOOP_NO_PROGRESS": "E331",
        "LOOP_TOOL_FAIL_STREAK": "E332",
        "BUDGET_SUBAGENT": "E340",
        "GRAPH_ILLEGAL_TRANSITION": "E410",
        "GRAPH_READS_UNSATISFIED": "E420",
        "GRAPH_ITERATION_LIMIT": "E430",
        "GRAPH_JOIN_FAILED": "E440",
        "SUBAGENT_SPAWN_INVALID": "E510",
        "SUBAGENT_ENVELOPE_INVALID": "E520",
        "SUBAGENT_TIMEOUT_REAPED": "E530",
        "SUBAGENT_DEPTH_VIOLATION": "E540",
    }
    assert {member.name: member.value for member in ErrorCode} == expected


def test_dispositions_follow_d21_defaults():
    assert CATALOG[ErrorCode.BUDGET_RUN].disposition is Disposition.BUDGET_GATE
    assert CATALOG[ErrorCode.TOOL_TIER_DENIED].disposition is Disposition.DEFECT
    assert CATALOG[ErrorCode.GRAPH_ITERATION_LIMIT].disposition is Disposition.FORCED_GATE
    assert CATALOG[ErrorCode.SUBAGENT_TIMEOUT_REAPED].disposition is Disposition.PARENT_POLICY
    # repairable tool failures come back as inner-loop observations
    for code in (ErrorCode.TOOL_BAD_ARGS, ErrorCode.TOOL_TIMEOUT, ErrorCode.TOOL_CRASH):
        assert CATALOG[code].disposition is Disposition.OBSERVATION


def test_agent_error_message_and_payload():
    err = AgentError(
        ErrorCode.BUDGET_RUN,
        "tokens 用尽",
        context={"total_tokens": 1_500_001},
    )
    assert "E310" in str(err)
    assert "tokens 用尽" in str(err)
    assert err.info.owner == "budget"

    payload = err.to_payload()
    assert payload["error_code"] == "E310"
    assert payload["error_context"] == {"total_tokens": 1_500_001}
    # context must be a copy, not a live reference
    err.context["total_tokens"] = 0
    assert payload["error_context"]["total_tokens"] == 1_500_001


def test_agent_error_without_context_omits_field():
    payload = AgentError(ErrorCode.LLM_NETWORK).to_payload()
    assert "error_context" not in payload
    with pytest.raises(KeyError):
        _ = CATALOG["not-a-code"]  # type: ignore[index]
