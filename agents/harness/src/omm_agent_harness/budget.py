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

Opting out (added 2026-09-08): any dimension may be set to ``UNLIMITED`` and
that dimension stops being enforced — the ledger keeps counting (reports and
error contexts stay honest), only the hard stop is gone. ``UNLIMITED`` is
``math.inf`` on purpose rather than a negative sentinel: every comparison in
this module and in downstream consumers (``budgets.max_sandbox_runs < 1``,
``thread.join(max_wall_clock_s)``) then keeps working without special cases.
Non-finite values are NOT JSON-serializable though, so anything that reports a
limit outwards (``snapshot``, the supervisor's spawn audit) maps them to
``None`` — see ``_reported``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omm_agent_core.errors import AgentError, ErrorCode

__all__ = [
    "UNLIMITED",
    "BudgetGovernor",
    "LoopBudget",
    "NodeBudget",
    "RunBudget",
    "SUBAGENT_MAX_FRACTION",
    "is_unlimited",
]

#: "No hard stop on this dimension." Distinct from 0, which is a real quota
#: meaning "not even one" (``subagent_slice`` hands out 0 when the parent has
#: nothing left, and callers rely on that to skip work).
UNLIMITED: float = math.inf


def is_unlimited(limit: float) -> bool:
    return not math.isfinite(limit)


def _reported(limit: float) -> float | None:
    """Limit as it goes into JSON (error contexts, audits): ``inf`` → ``None``.

    ``json.dumps`` emits a bare ``Infinity`` for ``inf``, which is invalid JSON
    and gets rejected on the way into a ``jsonb`` column.
    """
    return None if is_unlimited(limit) else limit


@dataclass(frozen=True)
class RunBudget:
    """Run level (§4.7 row 1): E310 → GB gate, a human adds budget or cancels.

    The numbers below stay the §4.7 design table; the executor that actually
    runs tasks decides what to pass (``omm_api.engine_glue`` defaults every
    dimension to ``UNLIMITED``). ``int | float`` because ``UNLIMITED`` is inf.
    """

    max_total_tokens: int | float = 1_500_000
    max_llm_calls: int | float = 300
    max_sandbox_runs: int | float = 40
    max_wall_clock_s: float = 2 * 60 * 60


@dataclass(frozen=True)
class NodeBudget:
    """Node level (§4.7 row 2): E320 fails the node, error carries usage."""

    max_tokens: int | float = 300_000


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
        default_node_budget: NodeBudget | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budget = run_budget or RunBudget()
        # Nodes nobody opened explicitly used to silently fall back to the §4.7
        # table, so an executor that raised (or removed) its node cap still got
        # stopped at 300k on any node missing from its open_node list. The
        # fallback is now the caller's to choose.
        self._default_node_budget = default_node_budget or NodeBudget()
        self._clock = clock
        self._started = clock()
        self._total_tokens = 0
        self._llm_calls = 0
        self._sandbox_runs = 0
        self._node_budgets: dict[str, NodeBudget] = {}
        self._node_tokens: dict[str, int] = {}

    # -- node scopes -----------------------------------------------------------

    def open_node(self, node_id: str, budget: NodeBudget | None = None) -> None:
        self._node_budgets[node_id] = budget or self._default_node_budget
        self._node_tokens.setdefault(node_id, 0)

    # -- durable rebuild ---------------------------------------------------------

    def seed_usage(
        self,
        *,
        total_tokens: int = 0,
        llm_calls: int = 0,
        sandbox_runs: int = 0,
        node_tokens: dict[str, int] | None = None,
    ) -> None:
        """Seed ledgers from the executor's durable usage records.

        The governor itself is in-memory; executors that rebuild per advance
        (engine glue rebuilds from run.log events) call this once right after
        construction so limits keep holding across process restarts. Seeding
        REPLACES the ledgers — it is a rebuild entry point, not a charge.
        """
        self._total_tokens = max(int(total_tokens), 0)
        self._llm_calls = max(int(llm_calls), 0)
        self._sandbox_runs = max(int(sandbox_runs), 0)
        for node_id, tokens in (node_tokens or {}).items():
            self._node_tokens[node_id] = max(int(tokens), 0)

    # -- checks & charges --------------------------------------------------------

    def check_llm_call(self, node_id: str | None = None) -> None:
        """Pre-flight: raise E310/E320 rather than spend over the line.

        A dimension set to ``UNLIMITED`` is skipped here but still charged in
        ``charge_llm`` — usage reporting never depends on enforcement.
        """
        self._check_wall_clock()
        max_calls = self._budget.max_llm_calls
        if not is_unlimited(max_calls) and self._llm_calls + 1 > max_calls:
            raise AgentError(
                ErrorCode.BUDGET_RUN,
                f"LLM 调用次数将超过上限 {max_calls}",
                context=self.snapshot(),
            )
        max_tokens = self._budget.max_total_tokens
        if not is_unlimited(max_tokens) and self._total_tokens >= max_tokens:
            raise AgentError(
                ErrorCode.BUDGET_RUN,
                f"tokens 已达运行上限 {max_tokens}",
                context=self.snapshot(),
            )
        if node_id is not None:
            budget = self._node_budgets.get(node_id, self._default_node_budget)
            if (
                not is_unlimited(budget.max_tokens)
                and self._node_tokens.get(node_id, 0) >= budget.max_tokens
            ):
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
        max_runs = self._budget.max_sandbox_runs
        if not is_unlimited(max_runs) and self._sandbox_runs + 1 > max_runs:
            raise AgentError(
                ErrorCode.BUDGET_RUN,
                f"沙箱运行次数将超过上限 {max_runs}",
                context=self.snapshot(),
            )
        self._sandbox_runs += 1

    def _check_wall_clock(self) -> None:
        limit = self._budget.max_wall_clock_s
        if is_unlimited(limit):
            return
        elapsed = self._clock() - self._started
        if elapsed > limit:
            raise AgentError(
                ErrorCode.BUDGET_RUN,
                f"运行墙钟已超上限 {limit:.0f}s",
                context=self.snapshot(),
            )

    # -- subagent slice (numbers only; enforcement is the supervisor's, H2) ----

    def subagent_slice(self) -> RunBudget:
        """At most 25% of REMAINING run budget for one spawn (§4.7 row 4).

        A quarter of unlimited is still unlimited, so those dimensions pass
        ``UNLIMITED`` straight through — ``int(inf * .25)`` would raise, and a
        negative sentinel would make consumers that test ``< 1`` (the rerun
        check in the experiment node) think the child cannot afford anything.
        """
        return RunBudget(
            max_total_tokens=self._slice_of(
                self._budget.max_total_tokens, self._total_tokens
            ),
            max_llm_calls=self._slice_of(self._budget.max_llm_calls, self._llm_calls),
            max_sandbox_runs=self._slice_of(
                self._budget.max_sandbox_runs, self._sandbox_runs
            ),
            max_wall_clock_s=(
                UNLIMITED
                if is_unlimited(self._budget.max_wall_clock_s)
                else max(
                    self._budget.max_wall_clock_s - (self._clock() - self._started), 0.0
                )
                * SUBAGENT_MAX_FRACTION
            ),
        )

    @staticmethod
    def _slice_of(limit: int | float, used: int) -> int | float:
        if is_unlimited(limit):
            return UNLIMITED
        return int(max(limit - used, 0) * SUBAGENT_MAX_FRACTION)

    # -- reporting ---------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Usage facts for error contexts, GB gates and run reports.

        Usage is always the real tally (unlimited dimensions are still
        counted); a limit that is not enforced reports as ``None``.
        """
        return {
            "total_tokens": self._total_tokens,
            "llm_calls": self._llm_calls,
            "sandbox_runs": self._sandbox_runs,
            "elapsed_s": round(self._clock() - self._started, 3),
            "limits": {
                "max_total_tokens": _reported(self._budget.max_total_tokens),
                "max_llm_calls": _reported(self._budget.max_llm_calls),
                "max_sandbox_runs": _reported(self._budget.max_sandbox_runs),
                "max_wall_clock_s": _reported(self._budget.max_wall_clock_s),
            },
        }
