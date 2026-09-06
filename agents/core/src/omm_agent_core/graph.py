"""Graph v1（``linear-v1``）：六阶段 + 闸门的图化描述与调度器端口（设计文档 §6）。

三步走（§6.1）：linear-v1 = 现有六阶段 + G1 的图化描述 → 与现引擎影子等价（§6.5：
控制流事件序列等价，不比内容）→ 等价证明后切换默认 → 再启用 v2 特性（lane /
迭代边 / join）。本模块只落第一步的机件：

- ``GraphSpec``：Python DSL 声明的图 + 装配期校验 + D1.5 形状的 JSON 快照；
- ``Scheduler`` 端口：引擎「接下来跑哪个状态」的唯一决策点。``LinearScheduler``
  是原引擎 ``_select_target`` 原样搬出；``GraphScheduler`` 按图选目标；
- 影子对比：引擎同时问两个调度器，不一致只记 ``SchedulingDivergence``，永不改
  主路径、永不发事件（发了事件就自己破坏了要证明的等价）。

调度器是纯函数：不改快照。原 ``_select_target`` 在选中「重跑当前」时顺手清掉
``force_rerun``，现在由引擎在选定目标后清——两个调度器才能看到同一份快照。

事件枚举与 payload 键集不动（不加 ``node_id``）：v1 节点与状态一一对应，
``node_id`` 与 ``STATE_CHANGED.to`` 同义，加了只会破坏金轨迹的逐字节稳定；
lane / iteration 才需要它们（D2.2，随 v2）。stdlib-only（core 依赖规则）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from .errors import AgentError, ErrorCode
from .models import StepStatus, TaskRunSnapshot
from .states import WORK_SEQUENCE, WORK_STATES, TaskState, next_work_state

NODE_KINDS: tuple[str, ...] = ("agent", "gate", "map", "join", "subgraph")
EDGE_KINDS: tuple[str, ...] = ("seq", "cond", "iter")


def stage_output_schema_id(state: TaskState) -> str:
    """节点产出的 schema_id（stage_outputs 表的过渡口径，设计文档 §10.2）。

    六类页面契约（problem-frame.v1 等）由读侧投影组装，不是节点写出的形状；
    契约化时这里与 ``STAGE_OUTPUT_SCHEMA_IDS`` 一并换成正式 id。
    """
    return f"{state.value.lower().replace('_', '-')}.outputs.v1"


# ── 图模型（§6.2）───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateDecl:
    """节点上的闸门声明：``always`` = 必停门（G1 / G4），否则由节点按证据决定（G2 / G3）。"""

    id: str
    always: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "always": self.always}

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "GateDecl":
        return GateDecl(id=str(raw["id"]), always=bool(raw.get("always", False)))


@dataclass(frozen=True)
class GraphNode:
    id: str
    state: TaskState
    kind: str = "agent"
    #: 读 / 写的 StageOutput schema_id；调度器在装配期校验 reads 可满足。
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    gate: GateDecl | None = None
    budget_profile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "state": self.state.value,
            "reads": list(self.reads),
            "writes": list(self.writes),
        }
        if self.gate is not None:
            payload["gate"] = self.gate.to_dict()
        if self.budget_profile is not None:
            payload["budget_profile"] = self.budget_profile
        return payload

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "GraphNode":
        gate = raw.get("gate")
        return GraphNode(
            id=str(raw["id"]),
            state=TaskState(raw["state"]),
            kind=str(raw.get("kind", "agent")),
            reads=tuple(str(item) for item in raw.get("reads") or ()),
            writes=tuple(str(item) for item in raw.get("writes") or ()),
            gate=GateDecl.from_dict(gate) if gate else None,
            budget_profile=raw.get("budget_profile"),
        )


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    #: seq = 顺序；cond = 条件（``when`` 是基于 StageOutput 字段的纯表达式）；
    #: iter = 迭代边（``max_iters`` 超限强制转闸门）。后两种是 v2 语义。
    kind: str = "seq"
    when: str | None = None
    max_iters: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"from": self.source, "to": self.target, "kind": self.kind}
        if self.when is not None:
            payload["when"] = self.when
        if self.max_iters is not None:
            payload["max_iters"] = self.max_iters
        return payload

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "GraphEdge":
        max_iters = raw.get("max_iters")
        return GraphEdge(
            source=str(raw["from"]),
            target=str(raw["to"]),
            kind=str(raw.get("kind", "seq")),
            when=raw.get("when"),
            max_iters=int(max_iters) if max_iters is not None else None,
        )


def _defect(code: ErrorCode, detail: str, **context: Any) -> AgentError:
    return AgentError(code, detail, context=context)


@dataclass(frozen=True)
class GraphSpec:
    """一张图 = 版本化的宏观事实：接下来做什么要么写在这里，要么由人决定（原则 11）。"""

    id: str
    version: int
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...] = ()
    #: map / join 结构（子问题并行 lane）随 H4 定形，这里只原样透传快照。
    maps: tuple[dict[str, Any], ...] = ()

    @property
    def workflow_version(self) -> str:
        """``workflow_version`` 即图版本（§6.2），如 ``linear-v1``。"""
        return f"{self.id}-v{self.version}"

    # -- 查询 -------------------------------------------------------------------

    def node(self, node_id: str) -> GraphNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise _defect(
            ErrorCode.GRAPH_ILLEGAL_TRANSITION,
            f"图 {self.workflow_version} 没有节点 {node_id!r}",
            graph=self.workflow_version,
            node_id=node_id,
        )

    def node_for_state(self, state: TaskState) -> GraphNode | None:
        for node in self.nodes:
            if node.state is state:
                return node
        return None

    def successors(self, node_id: str) -> tuple[GraphEdge, ...]:
        return tuple(edge for edge in self.edges if edge.source == node_id)

    def entry(self) -> GraphNode:
        """唯一入口 = 没有非迭代入边的节点（``validate`` 保证恰好一个）。"""
        targets = {edge.target for edge in self.edges if edge.kind != "iter"}
        entries = [node for node in self.nodes if node.id not in targets]
        if len(entries) != 1:
            raise _defect(
                ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                f"图 {self.workflow_version} 应恰有一个入口节点，实际 {len(entries)} 个",
                graph=self.workflow_version,
                entries=[node.id for node in entries],
            )
        return entries[0]

    # -- 装配期校验（§6.2）--------------------------------------------------------

    def validate(self) -> None:
        """结构违约抛 E410、reads 不可满足抛 E420：都是装配缺陷，启动即报错。"""
        graph = self.workflow_version
        if not self.nodes:
            raise _defect(ErrorCode.GRAPH_ILLEGAL_TRANSITION, f"图 {graph} 没有节点", graph=graph)
        ids: set[str] = set()
        states: set[TaskState] = set()
        gates: set[str] = set()
        for node in self.nodes:
            if not node.id:
                raise _defect(ErrorCode.GRAPH_ILLEGAL_TRANSITION, f"图 {graph} 有空节点 id", graph=graph)
            if node.id in ids:
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION, f"图 {graph} 节点 id 重复：{node.id}",
                    graph=graph, node_id=node.id,
                )
            ids.add(node.id)
            if node.kind not in NODE_KINDS:
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} 节点 {node.id} 类型非法：{node.kind!r}",
                    graph=graph, node_id=node.id,
                )
            if node.state not in WORK_STATES:
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} 节点 {node.id} 绑定的 {node.state.value} 不是工作状态",
                    graph=graph, node_id=node.id,
                )
            if node.state in states:
                # v1 节点 ↔ 状态一一对应：同一状态两个节点，事件里的 state 就无法回指节点
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} 有两个节点绑定同一状态 {node.state.value}",
                    graph=graph, node_id=node.id,
                )
            states.add(node.state)
            if node.gate is not None:
                if not node.gate.id:
                    raise _defect(
                        ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                        f"图 {graph} 节点 {node.id} 的闸门没有 id",
                        graph=graph, node_id=node.id,
                    )
                if node.gate.id in gates:
                    raise _defect(
                        ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                        f"图 {graph} 闸门 id 重复：{node.gate.id}",
                        graph=graph, node_id=node.id,
                    )
                gates.add(node.gate.id)

        for edge in self.edges:
            if edge.kind not in EDGE_KINDS:
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} 边 {edge.source}→{edge.target} 类型非法：{edge.kind!r}",
                    graph=graph,
                )
            for endpoint in (edge.source, edge.target):
                if endpoint not in ids:
                    raise _defect(
                        ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                        f"图 {graph} 边 {edge.source}→{edge.target} 引用了不存在的节点 {endpoint!r}",
                        graph=graph, node_id=endpoint,
                    )
            if edge.kind != "iter" and edge.source == edge.target:
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} 节点 {edge.source} 有非迭代自环",
                    graph=graph, node_id=edge.source,
                )
            if edge.kind == "cond" and not edge.when:
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} 条件边 {edge.source}→{edge.target} 缺 when",
                    graph=graph,
                )
            if edge.kind == "iter" and (edge.max_iters is None or edge.max_iters < 1):
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} 迭代边 {edge.source}→{edge.target} 的 max_iters 须 ≥ 1",
                    graph=graph,
                )

        entry = self.entry()
        forward = {node.id: [] for node in self.nodes}
        for edge in self.edges:
            if edge.kind != "iter":
                forward[edge.source].append(edge.target)

        # 除迭代边外无环 + 全部节点从入口可达（不可达的节点永远不会跑，是缺陷不是配置）
        order = self._topological_order(forward, graph)
        reachable: set[str] = set()
        stack = [entry.id]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(forward[current])
        unreachable = sorted(ids - reachable)
        if unreachable:
            raise _defect(
                ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                f"图 {graph} 有从入口不可达的节点：{', '.join(unreachable)}",
                graph=graph, unreachable=unreachable,
            )

        # reads 可满足：每个读取的 schema_id 必须由某个上游（沿非迭代边）节点写出
        available: dict[str, set[str]] = {}
        nodes_by_id = {node.id: node for node in self.nodes}
        incoming: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        for source, targets in forward.items():
            for target in targets:
                incoming[target].append(source)
        for node_id in order:
            upstream: set[str] = set()
            for source in incoming[node_id]:
                upstream |= available[source]
            node = nodes_by_id[node_id]
            missing = [schema for schema in node.reads if schema not in upstream]
            if missing:
                raise _defect(
                    ErrorCode.GRAPH_READS_UNSATISFIED,
                    f"图 {graph} 节点 {node_id} 读取的 {', '.join(missing)} 没有上游节点写出",
                    graph=graph, node_id=node_id, missing=missing,
                )
            available[node_id] = upstream | set(node.writes)

    @staticmethod
    def _topological_order(forward: dict[str, list[str]], graph: str) -> list[str]:
        indegree = {node_id: 0 for node_id in forward}
        for targets in forward.values():
            for target in targets:
                indegree[target] += 1
        ready = [node_id for node_id, degree in indegree.items() if degree == 0]
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for target in forward[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if len(order) != len(forward):
            cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
            raise _defect(
                ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                f"图 {graph} 在非迭代边上成环：{', '.join(cyclic)}",
                graph=graph, cyclic=cyclic,
            )
        return order

    # -- JSON 快照（D1.5 形状）--------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "maps": [dict(item) for item in self.maps],
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "GraphSpec":
        return GraphSpec(
            id=str(raw["id"]),
            version=int(raw["version"]),
            nodes=tuple(GraphNode.from_dict(item) for item in raw.get("nodes") or ()),
            edges=tuple(GraphEdge.from_dict(item) for item in raw.get("edges") or ()),
            maps=tuple(dict(item) for item in raw.get("maps") or ()),
        )


#: linear-v1 的闸门声明：G1 / G4 必停，G2 / G3 由节点按证据决定（§11）。
LINEAR_V1_GATES: dict[TaskState, GateDecl] = {
    TaskState.DATA_PREPARATION: GateDecl("G2", always=False),
    TaskState.MODEL_PLANNING: GateDecl("G1", always=True),
    TaskState.VALIDATING: GateDecl("G3", always=False),
    TaskState.PAPER_WRITING: GateDecl("G4", always=True),
}


def linear_v1() -> GraphSpec:
    """现有六阶段 + 闸门的图化描述（§6.1 第一步）。

    reads 按 ``NodeContext.prior_outputs`` 的口径声明为上游全部产出（节点确实能
    读到全部上游），v2 再收窄到各节点实际读取集。
    """
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    upstream: list[str] = []
    previous: GraphNode | None = None
    for state in WORK_SEQUENCE:
        node = GraphNode(
            id=state.value.lower(),
            state=state,
            reads=tuple(upstream),
            writes=(stage_output_schema_id(state),),
            gate=LINEAR_V1_GATES.get(state),
        )
        if previous is not None:
            edges.append(GraphEdge(source=previous.id, target=node.id))
        nodes.append(node)
        upstream.append(stage_output_schema_id(state))
        previous = node
    return GraphSpec(id="linear", version=1, nodes=tuple(nodes), edges=tuple(edges))


# ── 调度器端口（§6.3）───────────────────────────────────────────────────────


@runtime_checkable
class Scheduler(Protocol):
    """引擎每次 advance 问一句「接下来跑哪个状态」；``TaskState.COMPLETED`` 表示收工。

    纯函数：读快照、不改快照、不发事件。
    """

    def select_target(self, snapshot: TaskRunSnapshot) -> TaskState: ...


def _latest_step(snapshot: TaskRunSnapshot, state: TaskState):
    for step in reversed(snapshot.steps):
        if step.state is state:
            return step
    return None


class LinearScheduler:
    """原引擎 ``_select_target`` 原样搬出：WORK_SEQUENCE 顺延、失败 / 重跑留在当前。"""

    def select_target(self, snapshot: TaskRunSnapshot) -> TaskState:
        if snapshot.state is TaskState.CREATED:
            return next_work_state(TaskState.CREATED)  # type: ignore[return-value]
        if snapshot.state in WORK_STATES:
            if snapshot.force_rerun:
                # RUN_RETRIED / 回退重做要求重跑当前状态（覆盖"最近步骤已 SUCCEEDED 则顺延"）
                return snapshot.state
            latest = _latest_step(snapshot, snapshot.state)
            if latest is not None and latest.status is StepStatus.SUCCEEDED:
                return next_work_state(snapshot.state)  # type: ignore[return-value]
            # No step yet (resumed via retry/review) or last attempt failed:
            # re-run the current state.
            return snapshot.state
        raise RuntimeError(f"advance called in unexpected state {snapshot.state}")


class GraphScheduler:
    """按 GraphSpec 选目标；v1 只认顺序边（cond / iter 是 v2 语义，装配期拒绝）。

    运行期不做 reads 检查：现引擎没有这一步，做了就不再等价；reads 可满足性在
    ``GraphSpec.validate`` 装配期证明一次即可（E420）。
    """

    def __init__(self, spec: GraphSpec) -> None:
        spec.validate()
        for node in spec.nodes:
            out = spec.successors(node.id)
            if len(out) > 1 or any(edge.kind != "seq" for edge in out):
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"v1 调度器只支持每节点至多一条顺序出边，节点 {node.id} 不满足"
                    "（条件边 / 迭代边随 Graph v2）",
                    graph=spec.workflow_version, node_id=node.id,
                )
        self.spec = spec

    def select_target(self, snapshot: TaskRunSnapshot) -> TaskState:
        if snapshot.state is TaskState.CREATED:
            return self.spec.entry().state
        if snapshot.state in WORK_STATES:
            node = self.spec.node_for_state(snapshot.state)
            if node is None:
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"运行处于 {snapshot.state.value}，但图 {self.spec.workflow_version} 没有对应节点",
                    graph=self.spec.workflow_version, state=snapshot.state.value,
                )
            if snapshot.force_rerun:
                return snapshot.state
            latest = _latest_step(snapshot, snapshot.state)
            if latest is not None and latest.status is StepStatus.SUCCEEDED:
                out = self.spec.successors(node.id)
                if not out:
                    return TaskState.COMPLETED
                return self.spec.node(out[0].target).state
            return snapshot.state
        raise RuntimeError(f"advance called in unexpected state {snapshot.state}")


# ── 影子对比（§6.5）──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SchedulingDivergence:
    """主 / 影子调度器在同一份快照上给出了不同答案（或影子抛了异常）。

    ``seq`` 是分歧发生时快照的 ``last_event_seq``——事件日志里那一处之后的下一步
    就是两边分道之处，重放日志到 seq 即可复现。
    """

    seq: int
    state: str
    kind: str  # "target" | "error" | "undeclared_gate"
    primary: str | None
    shadow: str | None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "state": self.state,
            "kind": self.kind,
            "primary": self.primary,
            "shadow": self.shadow,
            "detail": self.detail,
        }


DivergenceHook = Callable[[SchedulingDivergence], None]


class ShadowComparator:
    """引擎的影子对比机件：记分歧、回调，绝不抛出、绝不改主路径。"""

    def __init__(
        self,
        primary: Scheduler,
        shadow: Scheduler | None,
        on_divergence: DivergenceHook | None = None,
    ) -> None:
        self._primary = primary
        self._shadow = shadow
        self._hook = on_divergence
        self.divergences: list[SchedulingDivergence] = []

    @property
    def enabled(self) -> bool:
        return self._shadow is not None

    def _graph_spec(self) -> GraphSpec | None:
        for scheduler in (self._primary, self._shadow):
            spec = getattr(scheduler, "spec", None)
            if isinstance(spec, GraphSpec):
                return spec
        return None

    def _report(self, divergence: SchedulingDivergence) -> None:
        self.divergences.append(divergence)
        if self._hook is not None:
            try:
                self._hook(divergence)
            except Exception:  # noqa: BLE001 - 观测回调出错不得影响推进
                pass

    def compare_target(self, snapshot: TaskRunSnapshot, primary_target: TaskState) -> None:
        if self._shadow is None:
            return
        try:
            shadow_target = self._shadow.select_target(snapshot)
        except Exception as exc:  # noqa: BLE001 - 影子异常本身就是要记录的分歧
            self._report(
                SchedulingDivergence(
                    seq=snapshot.last_event_seq,
                    state=snapshot.state.value,
                    kind="error",
                    primary=primary_target.value,
                    shadow=None,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        if shadow_target is not primary_target:
            self._report(
                SchedulingDivergence(
                    seq=snapshot.last_event_seq,
                    state=snapshot.state.value,
                    kind="target",
                    primary=primary_target.value,
                    shadow=shadow_target.value,
                )
            )

    def check_gate(self, snapshot: TaskRunSnapshot, state: TaskState, gate_id: str | None) -> None:
        """节点提了闸门：图上该节点必须有声明（图是唯一宏观事实，原则 11）。"""
        spec = self._graph_spec()
        if spec is None:
            return
        node = spec.node_for_state(state)
        declared = node.gate if node is not None else None
        if declared is None or (gate_id is not None and declared.id != gate_id):
            self._report(
                SchedulingDivergence(
                    seq=snapshot.last_event_seq,
                    state=state.value,
                    kind="undeclared_gate",
                    primary=gate_id,
                    shadow=declared.id if declared is not None else None,
                    detail=f"图 {spec.workflow_version} 节点 {node.id if node else state.value} 未声明该闸门",
                )
            )


# ── 装配档位（§4.9）：OMM_GRAPH=off|shadow|linear-v1 ──────────────────────────

GRAPH_MODE_ENV = "OMM_GRAPH"
GRAPH_MODES: tuple[str, ...] = ("off", "shadow", "linear-v1")
#: 缺省图驱动（§6.1 第二步「等价证明后切换默认」）：等价证据 = evals 12 剧本 off vs
#: linear-v1 控制流等价 + core 双调度器逐快照同答 + worker / API 全链；线性调度器留作
#: 影子，分歧照旧只进日志。``shadow`` / ``off`` 仍可显式选回；``modeling-v2`` 随 H4
#: 才成为合法值。
DEFAULT_GRAPH_MODE = "linear-v1"


def resolve_graph_mode(raw: str | None) -> tuple[str, str | None]:
    """归一环境变量取值；非法值按缺省处理并返回一句警告（拼写错误不得静默换档）。"""
    value = (raw or "").strip().lower() or DEFAULT_GRAPH_MODE
    if value in GRAPH_MODES:
        return value, None
    return (
        DEFAULT_GRAPH_MODE,
        f"{GRAPH_MODE_ENV}={raw!r} 不是合法档位（{'|'.join(GRAPH_MODES)}），按 {DEFAULT_GRAPH_MODE} 处理",
    )


def schedulers_for_mode(mode: str) -> tuple[Scheduler, Scheduler | None]:
    """档位 → (主调度器, 影子)。图驱动时线性当影子：等价证据双向留。"""
    if mode == "off":
        return LinearScheduler(), None
    if mode == "shadow":
        return LinearScheduler(), GraphScheduler(linear_v1())
    if mode == "linear-v1":
        return GraphScheduler(linear_v1()), LinearScheduler()
    raise ValueError(f"unknown graph mode {mode!r}")


__all__ = [
    "DEFAULT_GRAPH_MODE",
    "DivergenceHook",
    "EDGE_KINDS",
    "GRAPH_MODES",
    "GRAPH_MODE_ENV",
    "GateDecl",
    "GraphEdge",
    "GraphNode",
    "GraphScheduler",
    "GraphSpec",
    "LINEAR_V1_GATES",
    "LinearScheduler",
    "NODE_KINDS",
    "Scheduler",
    "SchedulingDivergence",
    "ShadowComparator",
    "linear_v1",
    "resolve_graph_mode",
    "schedulers_for_mode",
    "stage_output_schema_id",
]
