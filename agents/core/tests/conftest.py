from dataclasses import dataclass

import pytest

from omm_agent_core import (
    FixedClock,
    GraphScheduler,
    InMemoryArtifactStore,
    InMemoryEventSink,
    LinearScheduler,
    NodeResult,
    NodeServices,
    SequentialIdGenerator,
    TaskRunEngine,
    TaskState,
    WORK_SEQUENCE,
    linear_v1,
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


#: 引擎测试在两种调度下各跑一遍（§6.5 影子等价）：``linear`` = 历史线性推进 +
#: linear-v1 图当影子；``graph`` = linear-v1 图驱动 + 线性当影子。每个用例的断言
#: 两边都得成立，且 fixture 收尾时影子不得记下任何分歧。
SCHEDULER_MODES = ("linear", "graph")


def schedulers_for(mode: str):
    if mode == "graph":
        return GraphScheduler(linear_v1()), LinearScheduler()
    return LinearScheduler(), GraphScheduler(linear_v1())


@pytest.fixture(params=SCHEDULER_MODES)
def harness(request):
    built: list[TaskRunEngine] = []

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
        scheduler, shadow = schedulers_for(request.param)
        engine = TaskRunEngine(
            sink=sink,
            clock=clock,
            ids=ids,
            nodes=nodes,
            services=services,
            scheduler=scheduler,
            shadow=shadow,
        )
        built.append(engine)
        return engine, sink, nodes

    yield build

    for engine in built:
        assert engine.shadow_divergences == [], [
            divergence.to_dict() for divergence in engine.shadow_divergences
        ]
