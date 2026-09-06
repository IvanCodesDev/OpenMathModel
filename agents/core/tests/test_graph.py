"""Graph v1（linear-v1）：图模型校验、D1.5 快照、调度器等价、影子对比、装配档位。

引擎级的等价证据在 conftest：`harness` 按 linear / graph 两种调度各跑一遍全部
引擎用例，收尾断言影子零分歧。这里补图模块自身的契约。
"""

import pytest

from omm_agent_core import (
    DEFAULT_GRAPH_MODE,
    GRAPH_MODES,
    LINEAR_V1_GATES,
    WORK_SEQUENCE,
    AgentError,
    ErrorCode,
    EventType,
    FixedClock,
    GateDecl,
    GraphEdge,
    GraphNode,
    GraphScheduler,
    GraphSpec,
    InMemoryArtifactStore,
    InMemoryEventSink,
    LinearScheduler,
    NodeResult,
    NodeServices,
    Scheduler,
    SequentialIdGenerator,
    ShadowComparator,
    StepRun,
    StepStatus,
    TaskRunEngine,
    TaskRunSnapshot,
    TaskState,
    linear_v1,
    resolve_graph_mode,
    schedulers_for_mode,
    stage_output_schema_id,
)

from conftest import ScriptedNode, echo_node


def node(node_id, state, **kwargs):
    return GraphNode(id=node_id, state=state, **kwargs)


def spec(nodes, edges=(), graph_id="t", version=1):
    return GraphSpec(id=graph_id, version=version, nodes=tuple(nodes), edges=tuple(edges))


def two_node_spec(**edge_kwargs):
    return spec(
        [
            node("a", TaskState.PROBLEM_ANALYSIS, writes=("a.v1",)),
            node("b", TaskState.DATA_PREPARATION, reads=("a.v1",)),
        ],
        [GraphEdge("a", "b", **edge_kwargs)],
    )


# -- linear_v1 与 D1.5 快照 ---------------------------------------------------------


def test_linear_v1_is_the_graph_form_of_work_sequence():
    graph = linear_v1()
    graph.validate()

    assert graph.workflow_version == "linear-v1"
    assert [n.state for n in graph.nodes] == list(WORK_SEQUENCE)
    assert [n.id for n in graph.nodes] == [s.value.lower() for s in WORK_SEQUENCE]
    assert graph.entry().state is TaskState.PROBLEM_ANALYSIS
    # 单链：每个节点至多一条顺序出边，末节点没有出边
    for previous, following in zip(graph.nodes, graph.nodes[1:]):
        (edge,) = graph.successors(previous.id)
        assert (edge.kind, edge.target) == ("seq", following.id)
    assert graph.successors(graph.nodes[-1].id) == ()
    # 闸门声明：G1 / G4 必停，G2 / G3 条件门；题意分析与实验没有门
    assert {n.state: n.gate for n in graph.nodes if n.gate} == LINEAR_V1_GATES
    assert graph.node_for_state(TaskState.PROBLEM_ANALYSIS).gate is None
    assert graph.node_for_state(TaskState.EXPERIMENTING).gate is None
    # reads = 上游全部产出（NodeContext.prior_outputs 口径），writes = 自身过渡 schema_id
    upstream = []
    for n in graph.nodes:
        assert list(n.reads) == upstream
        assert n.writes == (stage_output_schema_id(n.state),)
        upstream.append(n.writes[0])
    assert stage_output_schema_id(TaskState.MODEL_PLANNING) == "model-planning.outputs.v1"


def test_linear_v1_snapshot_has_the_d15_shape_and_round_trips():
    graph = linear_v1()
    raw = graph.to_dict()

    assert raw["id"] == "linear" and raw["version"] == 1 and raw["maps"] == []
    assert raw["nodes"][2] == {
        "id": "model_planning",
        "kind": "agent",
        "state": "MODEL_PLANNING",
        "reads": ["problem-analysis.outputs.v1", "data-preparation.outputs.v1"],
        "writes": ["model-planning.outputs.v1"],
        "gate": {"id": "G1", "always": True},
    }
    assert "gate" not in raw["nodes"][0] and "budget_profile" not in raw["nodes"][0]
    assert raw["edges"][0] == {"from": "problem_analysis", "to": "data_preparation", "kind": "seq"}
    assert len(raw["edges"]) == 5
    assert GraphSpec.from_dict(raw) == graph


def test_edge_snapshot_only_carries_optional_fields_when_set():
    iter_edge = GraphEdge("validating", "experimenting", kind="iter", max_iters=2, when="not robust")
    assert iter_edge.to_dict() == {
        "from": "validating",
        "to": "experimenting",
        "kind": "iter",
        "when": "not robust",
        "max_iters": 2,
    }
    assert GraphEdge.from_dict(iter_edge.to_dict()) == iter_edge
    assert GateDecl.from_dict({"id": "G2"}) == GateDecl("G2", always=False)


# -- 装配期校验（§6.2）：E410 结构违约 / E420 reads 不可满足 ----------------------------


def defect_code(graph):
    with pytest.raises(AgentError) as info:
        graph.validate()
    return info.value.code


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(spec([]), id="empty"),
        pytest.param(
            spec([node("a", TaskState.PROBLEM_ANALYSIS), node("a", TaskState.DATA_PREPARATION)]),
            id="duplicate-node-id",
        ),
        pytest.param(
            spec([node("a", TaskState.PROBLEM_ANALYSIS), node("b", TaskState.PROBLEM_ANALYSIS)]),
            id="two-nodes-one-state",
        ),
        pytest.param(spec([node("a", TaskState.COMPLETED)]), id="non-work-state"),
        pytest.param(spec([node("a", TaskState.PROBLEM_ANALYSIS, kind="lane")]), id="bad-kind"),
        pytest.param(
            spec(
                [node("a", TaskState.PROBLEM_ANALYSIS), node("b", TaskState.DATA_PREPARATION)],
                [GraphEdge("a", "zzz")],
            ),
            id="dangling-edge",
        ),
        pytest.param(
            spec(
                [node("a", TaskState.PROBLEM_ANALYSIS), node("b", TaskState.DATA_PREPARATION)],
                [GraphEdge("a", "b", kind="jump")],
            ),
            id="bad-edge-kind",
        ),
        pytest.param(
            spec([node("a", TaskState.PROBLEM_ANALYSIS)], [GraphEdge("a", "a")]),
            id="seq-self-loop",
        ),
        pytest.param(
            spec(
                [node("a", TaskState.PROBLEM_ANALYSIS), node("b", TaskState.DATA_PREPARATION)],
                [GraphEdge("a", "b", kind="cond")],
            ),
            id="cond-without-when",
        ),
        pytest.param(
            spec(
                [node("a", TaskState.PROBLEM_ANALYSIS), node("b", TaskState.DATA_PREPARATION)],
                [GraphEdge("a", "b"), GraphEdge("b", "a", kind="iter")],
            ),
            id="iter-without-max-iters",
        ),
        pytest.param(
            spec([node("a", TaskState.PROBLEM_ANALYSIS), node("b", TaskState.DATA_PREPARATION)]),
            id="two-entries",
        ),
        pytest.param(
            spec(
                [
                    node("a", TaskState.PROBLEM_ANALYSIS),
                    node("b", TaskState.DATA_PREPARATION),
                    node("c", TaskState.MODEL_PLANNING),
                ],
                [GraphEdge("a", "b"), GraphEdge("b", "c"), GraphEdge("c", "b")],
            ),
            id="cycle-on-seq-edges",
        ),
        pytest.param(
            spec(
                [
                    node("a", TaskState.PROBLEM_ANALYSIS, gate=GateDecl("G1")),
                    node("b", TaskState.DATA_PREPARATION, gate=GateDecl("G1")),
                ],
                [GraphEdge("a", "b")],
            ),
            id="duplicate-gate-id",
        ),
        pytest.param(
            spec(
                [
                    node("a", TaskState.PROBLEM_ANALYSIS, gate=GateDecl("")),
                    node("b", TaskState.DATA_PREPARATION),
                ],
                [GraphEdge("a", "b")],
            ),
            id="gate-without-id",
        ),
    ],
)
def test_structural_defects_raise_e410(broken):
    assert defect_code(broken) is ErrorCode.GRAPH_ILLEGAL_TRANSITION


def test_unreachable_node_is_a_defect_not_a_config():
    # a → b 之外还有个 c 挂在 b 后面但只经迭代边可达：从入口不可达 → 永远不会跑
    graph = spec(
        [
            node("a", TaskState.PROBLEM_ANALYSIS),
            node("b", TaskState.DATA_PREPARATION),
            node("c", TaskState.MODEL_PLANNING),
        ],
        [GraphEdge("a", "b"), GraphEdge("c", "b", kind="iter", max_iters=1)],
    )
    # c 没有非迭代入边 → 与 a 并列成了第二个入口
    assert defect_code(graph) is ErrorCode.GRAPH_ILLEGAL_TRANSITION


def test_unsatisfied_reads_raise_e420_and_upstream_writes_satisfy_them():
    graph = spec(
        [
            node("a", TaskState.PROBLEM_ANALYSIS, writes=("a.v1",)),
            node("b", TaskState.DATA_PREPARATION, reads=("a.v1", "ghost.v1")),
        ],
        [GraphEdge("a", "b")],
    )
    with pytest.raises(AgentError) as info:
        graph.validate()
    assert info.value.code is ErrorCode.GRAPH_READS_UNSATISFIED
    assert info.value.context["missing"] == ["ghost.v1"]

    # 传递可见：c 读 a 写出的东西，中间隔着 b 也算满足
    graph = spec(
        [
            node("a", TaskState.PROBLEM_ANALYSIS, writes=("a.v1",)),
            node("b", TaskState.DATA_PREPARATION),
            node("c", TaskState.MODEL_PLANNING, reads=("a.v1",)),
        ],
        [GraphEdge("a", "b"), GraphEdge("b", "c")],
    )
    graph.validate()
    # 迭代边不算上游：b 读 c 写的东西只经 c→b 迭代边回流 → 不满足
    graph = spec(
        [
            node("a", TaskState.PROBLEM_ANALYSIS),
            node("b", TaskState.DATA_PREPARATION, reads=("c.v1",)),
            node("c", TaskState.MODEL_PLANNING, writes=("c.v1",)),
        ],
        [GraphEdge("a", "b"), GraphEdge("b", "c"), GraphEdge("c", "b", kind="iter", max_iters=2)],
    )
    assert defect_code(graph) is ErrorCode.GRAPH_READS_UNSATISFIED


def test_iter_edges_are_allowed_by_the_spec_but_not_by_the_v1_scheduler():
    graph = spec(
        [
            node("a", TaskState.PROBLEM_ANALYSIS),
            node("b", TaskState.DATA_PREPARATION),
            node("c", TaskState.MODEL_PLANNING),
        ],
        [GraphEdge("a", "b"), GraphEdge("b", "c"), GraphEdge("c", "b", kind="iter", max_iters=2)],
    )
    graph.validate()  # 迭代边合法（v2 语义）……
    with pytest.raises(AgentError) as info:
        GraphScheduler(graph)  # ……但 v1 调度器装配期拒绝
    assert info.value.code is ErrorCode.GRAPH_ILLEGAL_TRANSITION
    assert "v1" in str(info.value)

    forked = spec(
        [
            node("a", TaskState.PROBLEM_ANALYSIS),
            node("b", TaskState.DATA_PREPARATION),
            node("c", TaskState.MODEL_PLANNING),
        ],
        [GraphEdge("a", "b"), GraphEdge("a", "c")],
    )
    forked.validate()
    with pytest.raises(AgentError):
        GraphScheduler(forked)  # 两条出边 = 分叉，v1 不支持


def test_unknown_node_lookup_is_e410():
    with pytest.raises(AgentError) as info:
        linear_v1().node("nowhere")
    assert info.value.code is ErrorCode.GRAPH_ILLEGAL_TRANSITION


# -- 调度器等价：两种调度器在同一份快照上给同一个答案 ---------------------------------------


def snapshot_in(state, steps=(), force_rerun=False):
    snap = TaskRunSnapshot(run_id="r", project_id="p", state=state, force_rerun=force_rerun)
    snap.steps = list(steps)
    return snap


def step(state, status, attempt=1):
    return StepRun(step_id=f"s_{state.value}_{attempt}", state=state, attempt=attempt, status=status)


def all_snapshots():
    yield snapshot_in(TaskState.CREATED)
    for index, state in enumerate(WORK_SEQUENCE):
        done_upstream = [step(s, StepStatus.SUCCEEDED) for s in WORK_SEQUENCE[:index]]
        yield snapshot_in(state, done_upstream)  # 还没开步（retry / 审批后恢复）
        yield snapshot_in(state, done_upstream + [step(state, StepStatus.FAILED)])
        yield snapshot_in(state, done_upstream + [step(state, StepStatus.RUNNING)])
        yield snapshot_in(state, done_upstream + [step(state, StepStatus.SUCCEEDED)])
        yield snapshot_in(state, done_upstream + [step(state, StepStatus.SUCCEEDED)], force_rerun=True)
        # 第二趟失败排在第一趟成功之后：以最近一步为准
        yield snapshot_in(
            state,
            done_upstream + [step(state, StepStatus.SUCCEEDED), step(state, StepStatus.FAILED, 2)],
        )


def test_graph_scheduler_agrees_with_linear_scheduler_on_every_snapshot():
    linear, graph = LinearScheduler(), GraphScheduler(linear_v1())
    checked = 0
    for snap in all_snapshots():
        before = snap.force_rerun
        assert graph.select_target(snap) is linear.select_target(snap), snap.state
        assert snap.force_rerun is before, "调度器是纯函数，不得改快照"
        checked += 1
    assert checked == 1 + 6 * len(WORK_SEQUENCE)
    assert isinstance(graph, Scheduler) and isinstance(linear, Scheduler)


def test_graph_scheduler_targets_follow_the_chain():
    graph = GraphScheduler(linear_v1())
    assert graph.select_target(snapshot_in(TaskState.CREATED)) is TaskState.PROBLEM_ANALYSIS
    done = snapshot_in(TaskState.MODEL_PLANNING, [step(TaskState.MODEL_PLANNING, StepStatus.SUCCEEDED)])
    assert graph.select_target(done) is TaskState.EXPERIMENTING
    last = snapshot_in(TaskState.PAPER_WRITING, [step(TaskState.PAPER_WRITING, StepStatus.SUCCEEDED)])
    assert graph.select_target(last) is TaskState.COMPLETED
    assert graph.select_target(snapshot_in(TaskState.PAPER_WRITING)) is TaskState.PAPER_WRITING


def test_graph_scheduler_rejects_states_outside_the_graph():
    partial = GraphScheduler(
        spec(
            [node("a", TaskState.PROBLEM_ANALYSIS), node("b", TaskState.DATA_PREPARATION)],
            [GraphEdge("a", "b")],
        )
    )
    with pytest.raises(AgentError) as info:
        partial.select_target(snapshot_in(TaskState.MODEL_PLANNING))
    assert info.value.code is ErrorCode.GRAPH_ILLEGAL_TRANSITION
    for scheduler in (partial, LinearScheduler()):
        with pytest.raises(RuntimeError):
            scheduler.select_target(snapshot_in(TaskState.NEEDS_REVIEW))


# -- 影子对比 --------------------------------------------------------------------------


class FixedScheduler:
    def __init__(self, target):
        self.target = target

    def select_target(self, snapshot):
        if isinstance(self.target, Exception):
            raise self.target
        return self.target


def test_shadow_records_target_divergence_and_calls_hook_without_raising():
    seen = []

    def hook(divergence):
        seen.append(divergence)
        raise RuntimeError("观测回调坏了也不能影响推进")

    comparator = ShadowComparator(
        LinearScheduler(), FixedScheduler(TaskState.PAPER_WRITING), on_divergence=hook
    )
    assert comparator.enabled
    snap = snapshot_in(TaskState.CREATED)
    snap.last_event_seq = 1
    comparator.compare_target(snap, TaskState.PROBLEM_ANALYSIS)

    assert [d.to_dict() for d in comparator.divergences] == [
        {
            "seq": 1,
            "state": "CREATED",
            "kind": "target",
            "primary": "PROBLEM_ANALYSIS",
            "shadow": "PAPER_WRITING",
            "detail": "",
        }
    ]
    assert seen == comparator.divergences
    # 一致时什么都不记
    comparator.compare_target(snapshot_in(TaskState.PAPER_WRITING), TaskState.PAPER_WRITING)
    assert len(comparator.divergences) == 1


def test_shadow_exception_is_recorded_as_error_divergence():
    comparator = ShadowComparator(LinearScheduler(), FixedScheduler(ValueError("boom")))
    comparator.compare_target(snapshot_in(TaskState.CREATED), TaskState.PROBLEM_ANALYSIS)
    (divergence,) = comparator.divergences
    assert divergence.kind == "error" and divergence.shadow is None
    assert divergence.detail == "ValueError: boom"


def test_shadow_disabled_records_nothing():
    comparator = ShadowComparator(LinearScheduler(), None)
    assert not comparator.enabled
    comparator.compare_target(snapshot_in(TaskState.CREATED), TaskState.PROBLEM_ANALYSIS)
    comparator.check_gate(snapshot_in(TaskState.PROBLEM_ANALYSIS), TaskState.PROBLEM_ANALYSIS, "G9")
    assert comparator.divergences == []


def test_gate_check_uses_the_graph_declarations():
    comparator = ShadowComparator(LinearScheduler(), GraphScheduler(linear_v1()))
    snap = snapshot_in(TaskState.MODEL_PLANNING)
    # 声明过的门（带 id 或不带 id 的旧式 needs_review）都不算分歧
    comparator.check_gate(snap, TaskState.MODEL_PLANNING, "G1")
    comparator.check_gate(snap, TaskState.MODEL_PLANNING, None)
    comparator.check_gate(snap, TaskState.DATA_PREPARATION, "G2")
    assert comparator.divergences == []
    # 图上没门的节点提了门 / 门号对不上 → undeclared_gate
    comparator.check_gate(snap, TaskState.PROBLEM_ANALYSIS, None)
    comparator.check_gate(snap, TaskState.VALIDATING, "G2")
    assert [(d.kind, d.state, d.primary, d.shadow) for d in comparator.divergences] == [
        ("undeclared_gate", "PROBLEM_ANALYSIS", None, None),
        ("undeclared_gate", "VALIDATING", "G2", "G3"),
    ]


def build_engine(nodes, scheduler=None, shadow=None, hook=None):
    sink = InMemoryEventSink()
    clock, ids = FixedClock(), SequentialIdGenerator()
    engine = TaskRunEngine(
        sink=sink,
        clock=clock,
        ids=ids,
        nodes=nodes,
        services=NodeServices(clock=clock, ids=ids, artifacts=InMemoryArtifactStore()),
        scheduler=scheduler,
        shadow=shadow,
        on_divergence=hook,
    )
    return engine, sink


def test_engine_shadow_never_touches_the_event_log_but_reports_divergence():
    """负对照：影子是一张缺了验证阶段的图 → 记分歧；事件日志与无影子时逐字节相同。"""
    skipping = spec(
        [
            node("problem_analysis", TaskState.PROBLEM_ANALYSIS),
            node("data_preparation", TaskState.DATA_PREPARATION),
            node("model_planning", TaskState.MODEL_PLANNING),
            node("experimenting", TaskState.EXPERIMENTING),
            node("paper_writing", TaskState.PAPER_WRITING),
        ],
        [
            GraphEdge("problem_analysis", "data_preparation"),
            GraphEdge("data_preparation", "model_planning"),
            GraphEdge("model_planning", "experimenting"),
            GraphEdge("experimenting", "paper_writing"),
        ],
    )
    nodes = {state: echo_node(state) for state in WORK_SEQUENCE}
    reported = []
    engine, sink = build_engine(nodes, shadow=GraphScheduler(skipping), hook=reported.append)
    snapshot, _ = engine.create_run("p")
    engine.run_until_blocked(snapshot)

    baseline_engine, baseline_sink = build_engine(
        {state: echo_node(state) for state in WORK_SEQUENCE}
    )
    baseline, _ = baseline_engine.create_run("p")
    baseline_engine.run_until_blocked(baseline)
    assert [e.to_dict() for e in sink.events] == [e.to_dict() for e in baseline_sink.events]
    assert baseline_engine.shadow_divergences == []

    # 实验做完：主说 VALIDATING、影子说 PAPER_WRITING；随后运行处于 VALIDATING，
    # 影子图里没有这个状态 → error 分歧
    kinds = [(d.kind, d.state, d.primary, d.shadow) for d in engine.shadow_divergences]
    assert kinds[0] == ("target", "EXPERIMENTING", "VALIDATING", "PAPER_WRITING")
    assert kinds[1][:3] == ("error", "VALIDATING", "PAPER_WRITING")
    assert "E410" in engine.shadow_divergences[1].detail
    assert reported == engine.shadow_divergences
    assert isinstance(engine.scheduler, LinearScheduler)


def test_engine_flags_gate_raised_by_a_node_the_graph_declares_gateless():
    nodes = {state: echo_node(state) for state in WORK_SEQUENCE}
    nodes[TaskState.PROBLEM_ANALYSIS] = ScriptedNode(
        state=TaskState.PROBLEM_ANALYSIS,
        results=[NodeResult.needs_review(reason="题意存疑", review_meta={"gate": "G0"})],
    )
    engine, sink = build_engine(nodes, scheduler=GraphScheduler(linear_v1()), shadow=LinearScheduler())
    snapshot, _ = engine.create_run("p")
    engine.run_until_blocked(snapshot)

    assert snapshot.state is TaskState.NEEDS_REVIEW
    assert [e.event_type for e in sink.events][-1] is EventType.REVIEW_REQUESTED
    (divergence,) = engine.shadow_divergences
    assert (divergence.kind, divergence.state, divergence.primary) == (
        "undeclared_gate",
        "PROBLEM_ANALYSIS",
        "G0",
    )


# -- 装配档位 OMM_GRAPH ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, DEFAULT_GRAPH_MODE),
        ("", DEFAULT_GRAPH_MODE),
        ("off", "off"),
        (" Shadow ", "shadow"),
        ("LINEAR-V1", "linear-v1"),
    ],
)
def test_resolve_graph_mode_normalizes_valid_values(raw, expected):
    assert resolve_graph_mode(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["modeling-v2", "linear", "on", "graph"])
def test_resolve_graph_mode_falls_back_with_a_warning_on_unknown_values(raw):
    mode, warning = resolve_graph_mode(raw)
    assert mode == DEFAULT_GRAPH_MODE
    assert warning and raw in warning and "|".join(GRAPH_MODES) in warning


def test_schedulers_for_mode_pairs_primary_and_shadow():
    primary, shadow = schedulers_for_mode("off")
    assert isinstance(primary, LinearScheduler) and shadow is None
    primary, shadow = schedulers_for_mode("shadow")
    assert isinstance(primary, LinearScheduler) and isinstance(shadow, GraphScheduler)
    assert shadow.spec.workflow_version == "linear-v1"
    primary, shadow = schedulers_for_mode("linear-v1")
    assert isinstance(primary, GraphScheduler) and isinstance(shadow, LinearScheduler)
    with pytest.raises(ValueError):
        schedulers_for_mode("modeling-v2")


def test_default_mode_is_graph_driven_with_the_linear_engine_as_shadow():
    """§6.1 第二步：等价证明后切换默认——缺省图驱动，线性调度器留作影子。"""
    assert DEFAULT_GRAPH_MODE == "linear-v1"
    primary, shadow = schedulers_for_mode(resolve_graph_mode(None)[0])
    assert isinstance(primary, GraphScheduler) and isinstance(shadow, LinearScheduler)
