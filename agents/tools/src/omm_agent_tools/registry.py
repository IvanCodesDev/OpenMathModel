"""Tool registry: the single gate through which nodes reach external actions.

Dependency rule 5 (PROJECT_STRUCTURE.md): high-risk/high-cost abilities are
called through ONE uniform execution interface and never scattered inside
prompts. The registry owns which tools exist, their risk class and their
per-call timeout; the invoker (invoker.py) owns recording and enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from omm_agent_core import ToolResult

#: A handler receives (arguments, call context) and returns a ToolResult.
ToolHandler = Callable[[dict[str, Any], "ToolCallContext"], ToolResult]

#: Permission tiers in ascending privilege (design doc §4.3): a caller bound
#: to tier T may only invoke tools whose tier ranks ≤ T. Minimal grant per
#: node/subagent; a subagent's tier never exceeds its parent's.
TIERS: tuple[str, ...] = ("readonly", "workspace_write", "execute", "spawn")


def tier_rank(tier: str) -> int:
    """Rank a tier for comparison; unknown names are assembly defects."""
    try:
        return TIERS.index(tier)
    except ValueError:
        raise ValueError(f"unknown tool tier {tier!r}; expected one of {TIERS}") from None


@dataclass(frozen=True)
class ToolCallContext:
    run_id: str
    step_id: str
    tool_name: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    risk: str = "low"  # "low" | "medium" | "high"
    timeout_s: float = 30.0
    #: Names of required argument keys; validated before the handler runs.
    required_args: tuple[str, ...] = ()
    #: Permission tier this tool demands from its caller (§4.3).
    tier: str = "readonly"


class ToolNotAllowed(Exception):
    pass


@dataclass
class ToolRegistry:
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    #: When set, only these tool names may run — independent of registration,
    #: so a compromised prompt cannot summon a registered-but-not-allowed tool.
    allowlist: frozenset[str] | None = None

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self.specs:
            raise ValueError(f"tool {spec.name!r} already registered")
        tier_rank(spec.tier)  # unknown tier is an assembly defect: fail at registration
        self.specs[spec.name] = spec

    def with_allowlist(self, names: Iterable[str]) -> "ToolRegistry":
        return ToolRegistry(specs=dict(self.specs), allowlist=frozenset(names))

    def resolve(self, name: str, caller_max_tier: str | None = None) -> ToolSpec:
        if self.allowlist is not None and name not in self.allowlist:
            raise ToolNotAllowed(f"tool {name!r} is not on the allowlist")
        spec = self.specs.get(name)
        if spec is None:
            raise ToolNotAllowed(f"tool {name!r} is not registered")
        if caller_max_tier is not None and tier_rank(spec.tier) > tier_rank(caller_max_tier):
            # E240: tier violations are assembly defects (D2.1), reported with
            # the code in-band because ToolResult carries no error_code field.
            raise ToolNotAllowed(
                f"[E240] tool {name!r} requires tier {spec.tier!r}, "
                f"caller is limited to {caller_max_tier!r}"
            )
        return spec

    @staticmethod
    def validate_args(spec: ToolSpec, arguments: Mapping[str, Any]) -> str | None:
        missing = [key for key in spec.required_args if key not in arguments]
        if missing:
            return f"missing required arguments: {', '.join(sorted(missing))}"
        return None
