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
        self.specs[spec.name] = spec

    def with_allowlist(self, names: Iterable[str]) -> "ToolRegistry":
        return ToolRegistry(specs=dict(self.specs), allowlist=frozenset(names))

    def resolve(self, name: str) -> ToolSpec:
        if self.allowlist is not None and name not in self.allowlist:
            raise ToolNotAllowed(f"tool {name!r} is not on the allowlist")
        spec = self.specs.get(name)
        if spec is None:
            raise ToolNotAllowed(f"tool {name!r} is not registered")
        return spec

    @staticmethod
    def validate_args(spec: ToolSpec, arguments: Mapping[str, Any]) -> str | None:
        missing = [key for key in spec.required_args if key not in arguments]
        if missing:
            return f"missing required arguments: {', '.join(sorted(missing))}"
        return None
