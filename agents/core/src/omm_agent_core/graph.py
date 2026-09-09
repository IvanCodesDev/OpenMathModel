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

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from .errors import AgentError, ErrorCode
from .models import StepStatus, TaskRunSnapshot
from .states import WORK_SEQUENCE, WORK_STATES, TaskState, next_work_state

NODE_KINDS: tuple[str, ...] = ("agent", "gate", "map", "join", "subgraph")
EDGE_KINDS: tuple[str, ...] = ("seq", "cond", "iter")
#: 回环边：不参与入口 / 拓扑序 / 可达性 / reads 的前向结构。iter = 人工回退的许可（Graph v2
#: 第一步），cond = 按本步结果自动回退（第三步）；两者都只允许指向源节点的上游或自身。
LOOP_EDGE_KINDS: frozenset[str] = frozenset({"iter", "cond"})


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


class IterationRefused(AgentError, ValueError):
    """迭代边不放行（没有这条边 = E410 / 轮次用尽 = E430）。

    既是稳定错误码（事件与评测按 code 归类），也是 ``ValueError``——引擎的 ``resolve_review`` /
    ``request_revision`` / ``redo`` 对前置条件不成立一律抛 ValueError，控制面按同一路径转成
    「不能这么做」的回答，而不是 500。
    """


@dataclass(frozen=True)
class IterationLicense:
    """一次放行的回退：走的哪条迭代边、这是第几轮、上限多少（Graph v2 第二步，D2.2）。

    ``taken`` = 目标阶段已成功通过的次数 − 1（已走轮次），``iteration`` = taken + 1（本次是第几轮），
    ``remaining`` = 放行本次后还剩几轮。引擎把 ``event_fields()`` 记进触发回退的事件
    （REVIEW_RESOLVED / REVISION_REQUESTED / RUN_REDO），v1 图没有许可、事件 payload 不变。
    """

    edge: GraphEdge
    graph: str
    taken: int
    limit: int
    #: 条件边自动回退（第三步）时由调度器填好目标阶段，引擎不必再查图；人工回退的许可为 None
    #: （目标是调用方自己给的）。
    target_state: TaskState | None = None

    @property
    def iteration(self) -> int:
        return self.taken + 1

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.iteration)

    @property
    def via_edge(self) -> str:
        return f"{self.edge.kind}:{self.edge.source}->{self.edge.target}"

    def event_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "via_edge": self.via_edge,
            "iteration": self.iteration,
            "max_iters": self.limit,
            "graph": self.graph,
        }
        if self.edge.kind == "cond" and self.edge.when:
            fields["when"] = self.edge.when
        return fields


# ── 条件表达式（§6.2：``when`` 是基于 StageOutput 字段的纯表达式）────────────────────
#
# 白名单子集，stdlib ``ast`` 解析、不 eval：``and / or / not``、六种比较与 ``in / not in``、
# 点路径 / 常量下标取值（缺键给 None、不抛）、字面量、``len()``。别的语法一律是装配缺陷
# （E410，``GraphSpec.validate`` 时报），运行期求值异常按 False——条件不成立就不放行，
# 一条写坏的条件边不能把运行拖进异常循环。

_CONDITION_COMPARATORS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_CONDITION_CALLS: dict[str, Callable[[Any], Any]] = {"len": lambda value: len(value) if value is not None else 0}


def _condition_tree(expression: str) -> ast.Expression:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"条件表达式语法错误：{exc.msg}") from None
    _check_condition_node(tree.body)
    return tree


def _check_condition_node(node: ast.AST) -> None:
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _check_condition_node(value)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _check_condition_node(node.operand)
        return
    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _CONDITION_COMPARATORS:
                raise ValueError(f"条件表达式不支持比较符 {type(op).__name__}")
        for value in (node.left, *node.comparators):
            _check_condition_node(value)
        return
    if isinstance(node, ast.Constant):
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            return
        raise ValueError(f"条件表达式不支持字面量 {node.value!r}")
    if isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            _check_condition_node(element)
        return
    if isinstance(node, ast.Name):
        return
    if isinstance(node, ast.Attribute):
        _check_condition_node(node.value)
        return
    if isinstance(node, ast.Subscript):
        if not isinstance(node.slice, ast.Constant):
            raise ValueError("条件表达式的下标只能是常量")
        _check_condition_node(node.value)
        return
    if isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Name) and node.func.id in _CONDITION_CALLS) or node.keywords or len(node.args) != 1:
            raise ValueError("条件表达式只允许调用 len(x)")
        _check_condition_node(node.args[0])
        return
    raise ValueError(f"条件表达式不支持 {type(node).__name__}")


def _lookup(value: Any, key: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    if isinstance(value, (list, tuple)) and isinstance(key, int):
        return value[key] if -len(value) <= key < len(value) else None
    return getattr(value, str(key), None) if isinstance(key, str) and value is not None and not isinstance(value, (str, bytes)) else None


def _eval_condition_node(node: ast.AST, context: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = _eval_condition_node(value, context)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = _eval_condition_node(value, context)
            if result:
                return result
        return result
    if isinstance(node, ast.UnaryOp):
        return not _eval_condition_node(node.operand, context)
    if isinstance(node, ast.Compare):
        left = _eval_condition_node(node.left, context)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_condition_node(comparator, context)
            if not _CONDITION_COMPARATORS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_eval_condition_node(element, context) for element in node.elts)
    if isinstance(node, ast.Name):
        return context.get(node.id)
    if isinstance(node, ast.Attribute):
        return _lookup(_eval_condition_node(node.value, context), node.attr)
    if isinstance(node, ast.Subscript):
        return _lookup(_eval_condition_node(node.value, context), node.slice.value)  # type: ignore[attr-defined]
    if isinstance(node, ast.Call):
        return _CONDITION_CALLS[node.func.id](_eval_condition_node(node.args[0], context))  # type: ignore[attr-defined]
    raise ValueError(f"条件表达式不支持 {type(node).__name__}")


def validate_condition(expression: str) -> None:
    """装配期检查：语法 + 白名单；违约抛 ``ValueError``（调用方包成 E410）。"""
    if not str(expression or "").strip():
        raise ValueError("条件表达式为空")
    _condition_tree(str(expression))


def evaluate_condition(expression: str, context: Mapping[str, Any]) -> bool:
    """运行期求值：真值按 Python 口径；缺键 / 类型不匹配等一切异常按 False（不放行）。"""
    try:
        return bool(_eval_condition_node(_condition_tree(str(expression)).body, context))
    except Exception:  # noqa: BLE001 - 条件写坏只能让边不生效，不能让运行崩
        return False


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

    @property
    def iteration_edges(self) -> tuple[GraphEdge, ...]:
        return tuple(edge for edge in self.edges if edge.kind == "iter")

    @property
    def condition_edges(self) -> tuple[GraphEdge, ...]:
        """条件边（Graph v2 第三步）：按声明顺序——同一节点多条条件边时先声明的先判。"""
        return tuple(edge for edge in self.edges if edge.kind == "cond")

    def forward_edges(self, node_id: str) -> tuple[GraphEdge, ...]:
        """节点的前进出边（不含 iter / cond 回环边）。"""
        return tuple(edge for edge in self.successors(node_id) if edge.kind not in LOOP_EDGE_KINDS)

    def iteration_edge(self, source_state: TaskState, target_state: TaskState) -> GraphEdge | None:
        """从 ``source_state`` 的节点回到 ``target_state`` 的节点的迭代边（没有就是不许回）。"""
        source = self.node_for_state(source_state)
        target = self.node_for_state(target_state)
        if source is None or target is None:
            return None
        for edge in self.iteration_edges:
            if edge.source == source.id and edge.target == target.id:
                return edge
        return None

    def entry(self) -> GraphNode:
        """唯一入口 = 没有前向入边（回环边不算）的节点（``validate`` 保证恰好一个）。"""
        targets = {edge.target for edge in self.edges if edge.kind not in LOOP_EDGE_KINDS}
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
            if edge.kind not in LOOP_EDGE_KINDS and edge.source == edge.target:
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} 节点 {edge.source} 有非回环自环",
                    graph=graph, node_id=edge.source,
                )
            if edge.kind == "cond":
                if not edge.when:
                    raise _defect(
                        ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                        f"图 {graph} 条件边 {edge.source}→{edge.target} 缺 when",
                        graph=graph,
                    )
                try:
                    validate_condition(edge.when)
                except ValueError as exc:
                    raise _defect(
                        ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                        f"图 {graph} 条件边 {edge.source}→{edge.target} 的 when 非法：{exc}",
                        graph=graph, when=edge.when,
                    ) from None
            if edge.kind in LOOP_EDGE_KINDS and (edge.max_iters is None or edge.max_iters < 1):
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} {'迭代' if edge.kind == 'iter' else '条件'}边 {edge.source}→{edge.target} 的 max_iters 须 ≥ 1",
                    graph=graph,
                )

        entry = self.entry()
        forward = {node.id: [] for node in self.nodes}
        for edge in self.edges:
            if edge.kind not in LOOP_EDGE_KINDS:
                forward[edge.source].append(edge.target)

        # 除回环边外无环 + 全部节点从入口可达（不可达的节点永远不会跑，是缺陷不是配置）
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

        # 条件边只支持回退（目标是源节点的上游或自身）：向前的条件分叉随后续步，装配期就拒绝
        position = {node_id: index for index, node_id in enumerate(order)}
        for edge in self.condition_edges:
            if position[edge.target] > position[edge.source]:
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图 {graph} 条件边 {edge.source}→{edge.target} 指向下游：条件边只支持回退（分叉随后续步）",
                    graph=graph, node_id=edge.source,
                )

        # reads 可满足：每个读取的 schema_id 必须由某个上游（沿前向边）节点写出
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


#: modeling-v2 迭代边的轮次上限：与控制面的修订轮数上限同一口径（三轮不成就该由人换路）。
MODELING_V2_MAX_ITERS = 3
#: modeling-v2 条件边（自动回实验）的轮次上限：设计文档 §9.1「validating → experimenting ≤ 2 自动回实验」。
MODELING_V2_AUTO_REDO_ITERS = 2
#: 自动回实验的触发条件：验证节点提的 G3 里系统自己推荐的就是「重做实验」（失败检查占比过半 /
#: 实验审稿僵持）——图替人执行这条推荐，最多两轮，轮次用尽（E430）才开 G3 交人。
MODELING_V2_AUTO_REDO_WHEN = "review.gate == 'G3' and review.impact.recommended == 'redo:EXPERIMENTING'"


def modeling_v2() -> GraphSpec:
    """Graph v2：linear-v1 的节点与顺序边 + **迭代边**（第一步，``iter``）+ **条件边**（第三步，``cond``）。

    今天「退回重做」（G4 的 redo:PAPER_WRITING、G3 的 redo:EXPERIMENTING、跑完后的修订轮、
    对话面的任意状态从阶段重做）都是引擎按事件直接把状态搬回去，图对此一无所知。这里把
    「从哪个节点允许回到哪个节点、最多几轮」写成图的一等事实：每个节点到它自己及全部上游
    节点各一条迭代边（重做 = 回到某阶段再顺序往下走），``max_iters`` 统一取修订轮数上限。
    调度器据此**放行或拒绝**回退（没有边 = E410；轮次用尽 = E430 强制交人裁），前进路径与
    linear-v1 逐字相同——影子等价证据照样成立。

    条件边 ``validating → experimenting``：验证节点要开 G3 且系统推荐「重做实验」时，图先自动
    回实验（≤ ``MODELING_V2_AUTO_REDO_ITERS`` 轮），轮次用尽（E430）才把 G3 交给人——D2.1
    「E430→G3」。推荐「接受并记录局限」的 G3 照旧直接开门（现有剧本控制流不变）。
    lane / join 仍是后续步。
    """
    base = linear_v1()
    edges: list[GraphEdge] = list(base.edges)
    for index, source in enumerate(base.nodes):
        for target in base.nodes[: index + 1]:
            edges.append(
                GraphEdge(source=source.id, target=target.id, kind="iter", max_iters=MODELING_V2_MAX_ITERS)
            )
    edges.append(
        GraphEdge(
            source=TaskState.VALIDATING.value.lower(),
            target=TaskState.EXPERIMENTING.value.lower(),
            kind="cond",
            when=MODELING_V2_AUTO_REDO_WHEN,
            max_iters=MODELING_V2_AUTO_REDO_ITERS,
        )
    )
    return GraphSpec(id="modeling", version=2, nodes=base.nodes, edges=tuple(edges))


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
    """按 GraphSpec 选目标：前进只认顺序边（每节点至多一条），回退按迭代边 / 条件边放行或拒绝。

    运行期不做 reads 检查：现引擎没有这一步，做了就不再等价；reads 可满足性在
    ``GraphSpec.validate`` 装配期证明一次即可（E420）。向前的条件分叉仍是后续步，装配期拒绝。
    回环边不参与前进选择（``select_target`` 与 linear-v1 逐字相同）：迭代边只在引擎要把运行
    搬回某阶段时经 ``license_iteration`` 裁定，条件边在每步成功后经 ``auto_iteration`` 按本步
    结果决定要不要自动回退——没有回环边的图（linear-v1）两者都不裁，行为与今天一致。
    """

    def __init__(self, spec: GraphSpec) -> None:
        spec.validate()
        for node in spec.nodes:
            forward = spec.forward_edges(node.id)
            if len(forward) > 1 or any(edge.kind != "seq" for edge in forward):
                raise _defect(
                    ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                    f"图调度器只支持每节点至多一条顺序出边，节点 {node.id} 不满足"
                    "（条件分叉随 Graph v2 后续步）",
                    graph=spec.workflow_version, node_id=node.id,
                )
        self.spec = spec

    def auto_iteration(
        self,
        snapshot: TaskRunSnapshot,
        state: TaskState,
        outputs: Mapping[str, Any] | None = None,
        review: Mapping[str, Any] | None = None,
        attempt: int | None = None,
    ) -> IterationLicense | None:
        """一步成功之后、闸门 / 前进之前的裁定：有条件边为真就自动回退（纯函数、不改快照）。

        按声明顺序看 ``state`` 节点的条件边，``when`` 在上下文 {outputs, review, state, attempt}
        上求值（缺键给 None、异常按 False）；第一条为真的边放行——已走轮次沿用迭代边口径
        （目标阶段已成功通过的次数 − 1，人工重做与自动回退共用一个有界计数），≥ ``max_iters``
        抛 E430（引擎据此放过自动回退、照旧开闸门交人 = D2.1「E430→G3」）。没有条件边或
        条件都不成立 → None。
        """
        node = self.spec.node_for_state(state)
        if node is None:
            return None
        context = {
            "outputs": dict(outputs or {}),
            "review": dict(review or {}),
            "state": state.value,
            "attempt": attempt,
        }
        graph = self.spec.workflow_version
        for edge in self.spec.condition_edges:
            if edge.source != node.id or not evaluate_condition(str(edge.when), context):
                continue
            target = self.spec.node(edge.target).state
            passes = sum(
                1 for step in snapshot.steps
                if step.state is target and step.status is StepStatus.SUCCEEDED
            )
            taken = max(0, passes - 1)
            limit = int(edge.max_iters or 0)
            if taken >= limit:
                raise IterationRefused(
                    ErrorCode.GRAPH_ITERATION_LIMIT,
                    f"条件边 {state.value}→{target.value} 的 {limit} 轮自动回退已用尽"
                    f"（{target.value} 已成功通过 {passes} 次），交闸门由人决定",
                    context={
                        "graph": graph, "from": state.value, "to": target.value,
                        "max_iters": limit, "taken": taken, "when": edge.when,
                    },
                )
            return IterationLicense(edge=edge, graph=graph, taken=taken, limit=limit, target_state=target)
        return None

    def license_iteration(
        self, snapshot: TaskRunSnapshot, source_state: TaskState, target_state: TaskState
    ) -> IterationLicense | None:
        """把运行从 ``source_state`` 搬回 ``target_state`` 前的裁定（纯函数、不改快照）。

        图上没有迭代边（v1）→ 不裁，返回 None；有迭代边的图 → 必须存在这条边（E410），且
        已走过的轮次 < ``max_iters``（E430，强制交人在闸门另作决定）。已走轮次 = 目标阶段
        已成功通过的次数 − 1（第一次成功是正常前进，之后每次成功都是一轮重做的结果）。
        放行时返回许可：这是第几轮、上限多少、走的哪条边——引擎把它记进触发回退的事件
        （D2.2 的 ``via_edge / iteration``），重放与审计据此可数轮次。
        """
        if not self.spec.iteration_edges:
            return None
        edge = self.spec.iteration_edge(source_state, target_state)
        graph = self.spec.workflow_version
        if edge is None:
            raise IterationRefused(
                ErrorCode.GRAPH_ILLEGAL_TRANSITION,
                f"图 {graph} 不允许从 {source_state.value} 回到 {target_state.value}（没有迭代边）",
                context={"graph": graph, "from": source_state.value, "to": target_state.value},
            )
        passes = sum(
            1 for step in snapshot.steps
            if step.state is target_state and step.status is StepStatus.SUCCEEDED
        )
        taken = max(0, passes - 1)
        limit = int(edge.max_iters or 0)
        if taken >= limit:
            raise IterationRefused(
                ErrorCode.GRAPH_ITERATION_LIMIT,
                f"迭代边 {source_state.value}→{target_state.value} 的 {limit} 轮已用尽"
                f"（{target_state.value} 已成功通过 {passes} 次），须由人在闸门另作决定",
                context={
                    "graph": graph, "from": source_state.value, "to": target_state.value,
                    "max_iters": limit, "taken": taken,
                },
            )
        return IterationLicense(edge=edge, graph=graph, taken=taken, limit=limit)

    def check_iteration(
        self, snapshot: TaskRunSnapshot, source_state: TaskState, target_state: TaskState
    ) -> GraphEdge | None:
        """``license_iteration`` 的边视图：放行给边、不裁给 None、拒绝抛 ``IterationRefused``。"""
        license = self.license_iteration(snapshot, source_state, target_state)
        return license.edge if license is not None else None

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
                # 只沿顺序边前进：迭代边 / 条件边是回退的许可，不是下一步
                out = self.spec.forward_edges(node.id)
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
GRAPH_MODES: tuple[str, ...] = ("off", "shadow", "linear-v1", "modeling-v2")
#: 缺省图驱动（§6.1 第二步「等价证明后切换默认」）：等价证据 = evals 12 剧本 off vs
#: linear-v1 控制流等价 + core 双调度器逐快照同答 + worker / API 全链；线性调度器留作
#: 影子，分歧照旧只进日志。``shadow`` / ``off`` 仍可显式选回；``modeling-v2``（Graph v2
#: 第一步：迭代边放行 / 拒绝回退）是可选档位，等价证据齐了再切缺省。
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
    if mode == "modeling-v2":
        return GraphScheduler(modeling_v2()), LinearScheduler()
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
    "IterationRefused",
    "IterationLicense",
    "LINEAR_V1_GATES",
    "LOOP_EDGE_KINDS",
    "LinearScheduler",
    "MODELING_V2_AUTO_REDO_ITERS",
    "MODELING_V2_AUTO_REDO_WHEN",
    "MODELING_V2_MAX_ITERS",
    "NODE_KINDS",
    "Scheduler",
    "SchedulingDivergence",
    "ShadowComparator",
    "evaluate_condition",
    "linear_v1",
    "modeling_v2",
    "resolve_graph_mode",
    "schedulers_for_mode",
    "stage_output_schema_id",
    "validate_condition",
]
