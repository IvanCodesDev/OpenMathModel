"""BudgetGovernor: four-level hard budgets (design §4.7 — the single source).

Levels and defaults come straight from the §4.7 table; those numbers are
project decisions and get recalibrated by E5/E6, so they live in ONE place
(here) and everything else imports them. A budget is a HARD STOP, never a
warning: crossing a limit raises ``AgentError`` with the level's code and a
usage snapshot in the error context, so the UI and evals can show exactly
what was spent when the stop happened.

Enforcement split (§4.7): run/node ledgers are checked here at call sites
(gateway pre-check / sandbox charge); loop-level counters (E330/E331/E332)
are enforced by the loop engine in H1 and subagent slices (E340) by the
supervisor in H2 — their default NUMBERS still come from this module.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omm_agent_core.errors import AgentError, ErrorCode

__all__ = [
    "BudgetGovernor",
    "LoopBudget",
    "NodeBudget",
    "RunBudget",
    "SUBAGENT_MAX_FRACTION",
]


@dataclass(frozen=True)
class RunBudget:
    """Run level (§4.7 row 1): E310 → GB gate, a human adds budget or cancels."""

    max_total_tokens: int = 1_500_000
    max_llm_calls: int = 300
    max_sandbox_runs: int = 40
    max_wall_clock_s: float = 2 * 60 * 60


@dataclass(frozen=True)
class NodeBudget:
    """Node level (§4.7 row 2): E320 fails the node, error carries usage."""

    max_tokens: int = 300_000


@dataclass(frozen=True)
class LoopBudget:
    """Loop level defaults (§4.7 row 3); enforced by the loop engine (H1).

    max_turns: sandbox inner loops get 8, single-shot micro-skills get 1 —
    the assembler picks per node; 8 is the sandbox default here.
    """

    max_turns: int = 8
    repairs: int = 1  # R1: one structural repair per task
    no_progress_k: int = 3  # identical signatures before E331
    tool_fail_m: int = 3  # same-tool consecutive failures before E332


#: Subagent level (§4.7 row 4): a spawn may carry at most this fraction of
#: the parent's REMAINING budget; the supervisor (H2) enforces E340.
SUBAGENT_MAX_FRACTION = 0.25


class BudgetGovernor:
    """Run/node token-and-call ledgers with hard stops.

    Call sites use the pair: ``check_llm_call`` BEFORE hitting the provider
    (fail before spending), ``charge_llm`` after a reply (record what was
    spent). Sandbox runs are charged up front — a started run is spent money.
    """

    def __init__(
        self,
        run_budget: RunBudget | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budget = run_budget or RunBudget()
        self._clock = clock
        self._started = clock()
        self._total_tokens = 0
        self._llm_calls = 0
        self._sandbox_runs = 0
        self._node_budgets: dict[str, NodeBudget] = {}
        self._node_tokens: dict[str, int] = {}

    # -- node scopes -----------------------------------------------------------

    def open_node(self, node_id: str, budget: NodeBudget | None = None) -> None:
        self._node_budgets[node_id] = budget or NodeBudget()
        self._node_tokens.setdefault(node_id, 0)

    # -- checks & charges --------------------------------------------------------

    def check_llm_call(self, node_id: str | None = None) -> None:
        """Pre-flight: raise E310/E320 rather than spend over the line."""
        self._check_wall_clock()
        if self._llm_calls + 1 > self._budget.max_llm_calls:
            raise AgentError(
                ErrorCode.BUDGET_RUN,
                f"LLM 调用次数将超过上限 {self._budget.max_llm_calls}",
                context=self.snapshot(),
            )
        if self._total_tokens >= self._budget.max_total_tokens:
            raise AgentError(
                ErrorCode.BUDGET_RUN,
                f"tokens 已达运行上限 {self._budget.max_total_tokens}",
                context=self.snapshot(),
            )
        if node_id is not None:
            budget = self._node_budgets.get(node_id, NodeBudget())
            if self._node_tokens.get(node_id, 0) >= budget.max_tokens:
                raise AgentError(
                    ErrorCode.BUDGET_NODE,
                    f"节点 {node_id} tokens 已达上限 {budget.max_tokens}",
                    context={**self.snapshot(), "node_id": node_id},
                )

    def charge_llm(self, tokens: int, node_id: str | None = None) -> None:
        self._llm_calls += 1
        self._total_tokens += max(tokens, 0)
        if node_id is not None:
            self._node_tokens[node_id] = self._node_tokens.get(node_id, 0) + max(tokens, 0)

    def charge_sandbox_run(self) -> None:
        """Charged up front; the run that crosses the line never starts."""
        self._check_wall_clock()
        if self._sandbox_runs + 1 > self._budget.max_sandbox_runs:
            raise AgentError(
                ErrorCode.BUDGET_RUN,
                f"沙箱运行次数将超过上限 {self._budget.max_sandbox_runs}",
                context=self.snapshot(),
            )
        self._sandbox_runs += 1

    def _check_wall_clock(self) -> None:
        elapsed = self._clock() - self._started
        if elapsed > self._budget.max_wall_clock_s:
            raise AgentError(
                ErrorCode.BUDGET_RUN,
                f"运行墙钟已超上限 {self._budget.max_wall_clock_s:.0f}s",
                context=self.snapshot(),
            )

    # -- subagent slice (numbers only; enforcement is the supervisor's, H2) ----

    def subagent_slice(self) -> RunBudget:
        """At most 25% of REMAINING run budget for one spawn (§4.7 row 4)."""
        remaining_tokens = max(self._budget.max_total_tokens - self._total_tokens, 0)
        remaining_calls = max(self._budget.max_llm_calls - self._llm_calls, 0)
        remaining_runs = max(self._budget.max_sandbox_runs - self._sandbox_runs, 0)
        remaining_clock = max(
            self._budget.max_wall_clock_s - (self._clock() - self._started), 0.0
        )
        return RunBudget(
            max_total_tokens=int(remaining_tokens * SUBAGENT_MAX_FRACTION),
            max_llm_calls=int(remaining_calls * SUBAGENT_MAX_FRACTION),
            max_sandbox_runs=int(remaining_runs * SUBAGENT_MAX_FRACTION),
            max_wall_clock_s=remaining_clock * SUBAGENT_MAX_FRACTION,
        )

    # -- reporting ---------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Usage facts for error contexts, GB gates and run reports."""
        return {
            "total_tokens": self._total_tokens,
            "llm_calls": self._llm_calls,
            "sandbox_runs": self._sandbox_runs,
            "elapsed_s": round(self._clock() - self._started, 3),
            "limits": {
                "max_total_tokens": self._budget.max_total_tokens,
                "max_llm_calls": self._budget.max_llm_calls,
                "max_sandbox_runs": self._budget.max_sandbox_runs,
                "max_wall_clock_s": self._budget.max_wall_clock_s,
            },
        }
