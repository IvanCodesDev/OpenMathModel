"""BudgetGovernor: §4.7 defaults are the contract; every stop is hard."""

from __future__ import annotations

import pytest
from omm_agent_core.errors import AgentError, ErrorCode
from omm_agent_harness.budget import (
    SUBAGENT_MAX_FRACTION,
    BudgetGovernor,
    LoopBudget,
    NodeBudget,
    RunBudget,
)


class SteppingClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_defaults_match_design_table_4_7():
    run = RunBudget()
    assert run.max_total_tokens == 1_500_000
    assert run.max_llm_calls == 300
    assert run.max_sandbox_runs == 40
    assert run.max_wall_clock_s == 7_200

    assert NodeBudget().max_tokens == 300_000

    loop = LoopBudget()
    assert (loop.max_turns, loop.repairs, loop.no_progress_k, loop.tool_fail_m) == (8, 1, 3, 3)

    assert SUBAGENT_MAX_FRACTION == 0.25


def test_llm_call_count_hard_stop_is_e310():
    governor = BudgetGovernor(RunBudget(max_llm_calls=2))
    for _ in range(2):
        governor.check_llm_call()
        governor.charge_llm(tokens=10)

    with pytest.raises(AgentError) as excinfo:
        governor.check_llm_call()
    assert excinfo.value.code is ErrorCode.BUDGET_RUN
    assert excinfo.value.context["llm_calls"] == 2  # usage snapshot travels with the stop


def test_token_hard_stop_is_e310():
    governor = BudgetGovernor(RunBudget(max_total_tokens=100))
    governor.check_llm_call()
    governor.charge_llm(tokens=100)

    with pytest.raises(AgentError) as excinfo:
        governor.check_llm_call()
    assert excinfo.value.code is ErrorCode.BUDGET_RUN
    assert excinfo.value.context["total_tokens"] == 100


def test_node_budget_stop_is_e320_and_scoped():
    governor = BudgetGovernor()
    governor.open_node("model_planning", NodeBudget(max_tokens=50))
    governor.check_llm_call(node_id="model_planning")
    governor.charge_llm(tokens=50, node_id="model_planning")

    with pytest.raises(AgentError) as excinfo:
        governor.check_llm_call(node_id="model_planning")
    assert excinfo.value.code is ErrorCode.BUDGET_NODE
    assert excinfo.value.context["node_id"] == "model_planning"

    governor.check_llm_call(node_id="paper_writing")  # other nodes unaffected
    governor.check_llm_call()  # run level unaffected


def test_sandbox_runs_charged_up_front():
    governor = BudgetGovernor(RunBudget(max_sandbox_runs=1))
    governor.charge_sandbox_run()

    with pytest.raises(AgentError) as excinfo:
        governor.charge_sandbox_run()
    assert excinfo.value.code is ErrorCode.BUDGET_RUN
    assert excinfo.value.context["sandbox_runs"] == 1


def test_wall_clock_stop_is_e310():
    clock = SteppingClock()
    governor = BudgetGovernor(RunBudget(max_wall_clock_s=60), clock=clock)
    governor.check_llm_call()

    clock.now = 61.0
    with pytest.raises(AgentError) as excinfo:
        governor.check_llm_call()
    assert excinfo.value.code is ErrorCode.BUDGET_RUN
    assert excinfo.value.context["elapsed_s"] == 61.0


def test_subagent_slice_is_quarter_of_remaining():
    clock = SteppingClock()
    governor = BudgetGovernor(
        RunBudget(max_total_tokens=1000, max_llm_calls=100, max_sandbox_runs=40,
                  max_wall_clock_s=100),
        clock=clock,
    )
    governor.charge_llm(tokens=200)
    clock.now = 20.0

    piece = governor.subagent_slice()
    assert piece.max_total_tokens == 200  # 25% of 800 remaining
    assert piece.max_llm_calls == 24  # 25% of 99 remaining, floored
    assert piece.max_sandbox_runs == 10
    assert piece.max_wall_clock_s == 20.0  # 25% of 80s remaining


def test_snapshot_reports_usage_and_limits():
    governor = BudgetGovernor(RunBudget(max_total_tokens=100))
    governor.charge_llm(tokens=30)
    snapshot = governor.snapshot()
    assert snapshot["total_tokens"] == 30
    assert snapshot["llm_calls"] == 1
    assert snapshot["limits"]["max_total_tokens"] == 100
