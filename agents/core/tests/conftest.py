from dataclasses import dataclass

import pytest

from omm_agent_core import (
    FixedClock,
    InMemoryArtifactStore,
    InMemoryEventSink,
    NodeResult,
    NodeServices,
    SequentialIdGenerator,
    TaskRunEngine,
    TaskState,
    WORK_SEQUENCE,
)


@dataclass
class ScriptedNode:
    """Node whose behaviour is scripted per attempt for deterministic tests."""

    state: TaskState
    results: list[NodeResult]
    calls: int = 0

    def run(self, ctx, services):  # noqa: ANN001 - protocol signature
        self.calls += 1
        index = min(self.calls - 1, len(self.results) - 1)
        return self.results[index]


def echo_node(state: TaskState) -> ScriptedNode:
    return ScriptedNode(
        state=state,
        results=[NodeResult.succeeded(outputs={"echo": state.value})],
    )


@pytest.fixture()
def harness():
    def build(node_overrides: dict[TaskState, object] | None = None):
        sink = InMemoryEventSink()
        nodes = {state: echo_node(state) for state in WORK_SEQUENCE}
        if node_overrides:
            nodes.update(node_overrides)
        clock = FixedClock()
        ids = SequentialIdGenerator()
        services = NodeServices(
            clock=clock, ids=ids, artifacts=InMemoryArtifactStore()
        )
        engine = TaskRunEngine(
            sink=sink, clock=clock, ids=ids, nodes=nodes, services=services
        )
        return engine, sink, nodes

    return build
