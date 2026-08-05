"""sim-0.1 节点序列与转换辅助。

status（生命周期，schemas/v1 task-run.status）与 current_node（领域阶段）两轴分离，
对齐规划文档 §12.3 与 v1 契约。T5 由 agents/core 的类型化状态机接管本模块规则
（保持函数签名不变）。
"""

from __future__ import annotations

NODE_CREATED = "CREATED"
NODE_COMPLETED = "COMPLETED"

# 阶段推进顺序（current_node 的中间取值；不含 CREATED/COMPLETED 两个端点）
STAGES: list[str] = [
    "PROBLEM_ANALYSIS",
    "DATA_PREPARATION",
    "MODEL_PLANNING",
    "EXPERIMENTING",
    "VALIDATING",
    "PAPER_WRITING",
]
STAGE_SET = frozenset(STAGES)

# 完成 MODEL_PLANNING 后需要人工确认方案（G3.5 的 sim-0.1 简化）
APPROVAL_GATE_AFTER = "MODEL_PLANNING"

STAGE_LABELS: dict[str, str] = {
    "PROBLEM_ANALYSIS": "题意解析",
    "DATA_PREPARATION": "数据准备",
    "MODEL_PLANNING": "建模方案",
    "EXPERIMENTING": "实验运行",
    "VALIDATING": "结果验证",
    "PAPER_WRITING": "论文撰写",
}


def next_stage(stage: str) -> str | None:
    """返回下一阶段；最后一个阶段返回 None（进入 COMPLETED）。"""
    idx = STAGES.index(stage)
    return STAGES[idx + 1] if idx + 1 < len(STAGES) else None
