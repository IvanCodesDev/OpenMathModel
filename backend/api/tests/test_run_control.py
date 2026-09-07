"""对话即控制面（ADR-0018）：任务页聊天框里的一句话真正驱动运行。

单元部分只测纯函数（合法动作集 / 本地规则 / 选项点名 / 判定回复解析 / 提示词块），
不碰数据库；端到端部分走真实的托管对话轮：把运行推到 FAILED，再用
POST /api/chat/turns 说「继续啊」，断言运行被重试、备注落库、事件流首条是
action 回执、轮视图 meta.actions 持久化；取消走「提案 → 确认」两轮。

上游模型一律用 httpx.MockTransport 模拟；判定模型与对话模型是同一个 mock，
handler 按请求正文分流（判定请求正文里有「运行控制判定器」字样）。
"""

from __future__ import annotations

import json

import httpx
from conftest import approve_when_asked, run_status_is, wait_until
from sqlalchemy import select

from omm_api import llm as llm_module
from omm_api.orm import ApprovalRequestRow, RunNoteRow
from omm_api.run_control import (
    ControlDecision,
    GateOption,
    RunControlContext,
    _parse_judge_reply,
    decide,
    decide_locally,
    explicit_stage,
    legal_actions,
    match_option,
    pending_proposal_of,
    prompt_block,
    reply_rules,
)

# ── 夹具：审批门形态与上下文 ──────────────────────────────────────────────────

G1_OPTIONS = [
    GateOption("approve", "采用当前方案", "确认方案并进入实验阶段"),
    GateOption("reject", "退回重做方案", "重新执行建模方案阶段并再次确认"),
]
G1_MULTI_OPTIONS = [
    GateOption("approve", "采用推荐方案：方案 A（线性规划）", recommended=True),
    GateOption("adopt:plan_b", "改用备选：方案 B（启发式搜索）"),
    GateOption("reject", "退回重做方案"),
]
G2_OPTIONS = [
    GateOption("adopt_cleaned", "采用清洗结果", recommended=True),
    GateOption("use_raw", "沿用原始数据"),
]
REVISION_OPTIONS = [
    GateOption(f"redo:{stage}", f"从「{label}」重做", recommended=(stage == "PAPER_WRITING"))
    for stage, label in (
        ("PROBLEM_ANALYSIS", "题意解析"),
        ("DATA_PREPARATION", "数据准备"),
        ("MODEL_PLANNING", "建模方案"),
        ("EXPERIMENTING", "实验运行"),
        ("VALIDATING", "结果验证"),
        ("PAPER_WRITING", "论文撰写"),
    )
] + [GateOption("reject", "撤回本次修改要求")]


def _ctx(status: str, node: str = "EXPERIMENTING", **extra) -> RunControlContext:
    return RunControlContext(run_id="run_x", status=status, node=node, **extra)


def _gate(options: list[GateOption], **extra) -> RunControlContext:
    return _ctx(
        "WAITING_APPROVAL",
        node="MODEL_PLANNING",
        approval_id="appr_1",
        approval_title="确认建模方案",
        options=options,
        **extra,
    )


# ── 合法动作集 ───────────────────────────────────────────────────────────────


def test_legal_actions_follow_state_machine() -> None:
    assert legal_actions("FAILED", False, [], 0) == {"retry", "redo"}
    assert legal_actions("PAUSED", False, [], 0) == {"resume", "cancel", "redo"}
    assert legal_actions("RUNNING", False, [], 0) == {"pause", "cancel", "redo"}
    assert legal_actions("QUEUED", False, [], 0) == {"cancel"}, "还没开始，没有可重做的东西"
    assert legal_actions("WAITING_APPROVAL", True, G1_OPTIONS, 0) == {"approve", "cancel", "reject", "redo"}
    assert legal_actions("WAITING_APPROVAL", True, G2_OPTIONS, 0) == {"approve", "cancel", "redo"}
    assert legal_actions("WAITING_APPROVAL", True, REVISION_OPTIONS, 0, revision_gate=True) == {
        "approve",
        "cancel",
        "reject",
    }, "修订门上六个起点已是选项，不另开 redo"
    assert legal_actions("WAITING_APPROVAL", False, [], 0) == frozenset(), "没有待审批行就没有可选动作"
    assert legal_actions("COMPLETED", False, [], 0) == {"revision"}
    assert legal_actions("COMPLETED", False, [], 3) == frozenset(), "修订轮数用完"
    assert legal_actions("CANCELLED", False, [], 0) == frozenset()
    assert "redo" not in _gate(REVISION_OPTIONS, revision_gate_round=1).legal
    assert "redo" in _gate(G1_OPTIONS).legal


# ── 本地规则：失败运行 ───────────────────────────────────────────────────────


def test_failed_run_retry_phrases() -> None:
    ctx = _ctx("FAILED")
    for text in ("继续啊", "怎么又失败了，快继续想办法", "重试", "再跑一次", "重新执行一下", "retry"):
        decision = decide_locally(text, ctx)
        assert decision.kind == "retry" and decision.source == "rule", text


def test_failed_run_questions_and_negations_are_not_commands() -> None:
    ctx = _ctx("FAILED")
    for text in ("为什么失败了", "失败原因是什么？", "不要重试", "先不要重跑，看看原因", "别急着重试", "继续说说这个错误"):
        assert decide_locally(text, ctx).kind == "none", text
        # 没有判定器时 decide 也不会凭空造动作
        assert decide(text, ctx, judge=None).kind == "none", text


# ── 本地规则：暂停 / 执行中 ─────────────────────────────────────────────────


def test_paused_and_running_phrases() -> None:
    paused = _ctx("PAUSED")
    assert decide_locally("恢复", paused).kind == "resume"
    assert decide_locally("继续跑吧", paused).kind == "resume"
    running = _ctx("RUNNING")
    assert decide_locally("先停一下", running).kind == "pause"
    assert decide_locally("暂停", running).kind == "pause"
    assert decide_locally("取消任务", running).kind == "cancel"
    assert decide_locally("取消", running).kind == "cancel", "没有退回选项的状态下裸「取消」= 取消任务"
    # RUNNING 时「继续」既不是 resume 也不是 retry：不合法就不判
    assert decide_locally("继续", running).kind == "none"
    # 补充要求不是命令
    assert decide_locally("把目标函数改成加权总成本", running).kind == "none"


# ── 本地规则：审批门 ─────────────────────────────────────────────────────────


def test_gate_approve_reject_and_named_options() -> None:
    g1 = _gate(G1_OPTIONS)
    approve = decide_locally("同意", g1)
    assert (approve.kind, approve.option_id) == ("approve", "approve")
    assert decide_locally("确认", g1).option_id == "approve"
    assert decide_locally("采用当前方案", g1).option_id == "approve"
    reject = decide_locally("退回重做", g1)
    assert (reject.kind, reject.option_id) == ("reject", "reject")
    assert decide_locally("换个方案", g1).kind == "reject"
    # 有退回选项的门上，裸「取消」拿不准（撤回还是取消任务）→ 交判定
    assert decide_locally("取消", g1).kind == "none"
    assert decide_locally("取消任务", g1).kind == "cancel"
    # 问句里的「可以」不是放行
    assert decide_locally("可以解释一下这个方案吗", g1).kind == "none"
    assert decide_locally("可以", g1).option_id == "approve"


def test_gate_letter_and_ordinal_pick_the_right_plan() -> None:
    gate = _gate(G1_MULTI_OPTIONS)
    assert decide_locally("用方案 B", gate).option_id == "adopt:plan_b"
    assert decide_locally("采用方案B", gate).option_id == "adopt:plan_b", "字母优先于推荐项别名「采用」"
    assert decide_locally("第二个", gate).option_id == "adopt:plan_b"
    assert decide_locally("就方案 A", gate).option_id == "approve"
    assert decide_locally("按推荐", gate).option_id == "approve"
    # 多个正向选项且只点了「方案」拿不准 → none
    assert decide_locally("这个方案", gate).kind == "none"


def test_gate_aliases_for_data_gate() -> None:
    gate = _gate(G2_OPTIONS)
    assert decide_locally("用清洗后的", gate).option_id == "adopt_cleaned"
    assert decide_locally("沿用原始数据", gate).option_id == "use_raw"
    assert decide_locally("继续", gate).option_id == "adopt_cleaned", "不点名时用唯一推荐项"


def test_revision_gate_picks_stage_or_preselected() -> None:
    gate = _gate(REVISION_OPTIONS, revision_gate_round=1)
    named = decide_locally("从数据准备重做", gate)
    assert (named.kind, named.option_id) == ("approve", "redo:DATA_PREPARATION")
    assert decide_locally("确认", gate).option_id == "redo:PAPER_WRITING", "预选项 = 唯一 recommended"
    assert decide_locally("重做", gate).kind == "none", "七个选项共有的「重做」不算点名"
    assert decide_locally("撤回", gate).kind == "reject"
    assert decide_locally("从题意解析和实验运行重做", gate).option_id == "redo:PROBLEM_ANALYSIS", "多阶段取最早"


# ── 本地规则：已完成运行 ────────────────────────────────────────────────────


def test_completed_run_revision_only_when_stage_named() -> None:
    done = _ctx("COMPLETED", node="PAPER_WRITING")
    decision = decide_locally("从数据准备重做", done)
    assert (decision.kind, decision.stage) == ("revision", "DATA_PREPARATION")
    assert decide_locally("把论文撰写部分重写一下", done).stage == "PAPER_WRITING"
    # 没点名阶段 / 是问句：本地不烧修订轮数，交判定（或回落对话）
    assert decide_locally("把结果改成表格给我看看", done).kind == "none"
    assert decide_locally("数据准备阶段做了什么？", done).kind == "none"
    exhausted = _ctx("COMPLETED", node="PAPER_WRITING", revision_rounds=3)
    assert decide_locally("从数据准备重做", exhausted).kind == "none"


# ── 本地规则：任意状态从阶段重做（ADR-0019） ────────────────────────────────


def test_named_stage_redo_on_unfinished_runs() -> None:
    failed = _ctx("FAILED")  # 失败于实验运行
    named = decide_locally("从数据准备重做", failed)
    assert (named.kind, named.stage, named.source) == ("redo", "DATA_PREPARATION", "rule")
    assert decide_locally("建模方案重来一遍", failed).stage == "MODEL_PLANNING"
    # 点名的正是失败阶段 → 就是 retry（直接执行，不必再确认）
    assert decide_locally("把实验运行重做一遍", failed).kind == "retry"
    # 没点名阶段的「重新做」仍是 retry 候选，交判定模型带上下文看它到底要改哪一层
    assert decide_locally("换成随机森林重新做", failed).kind == "retry"
    # 问句不是命令
    assert decide_locally("数据准备阶段要不要重做？", failed).kind == "none"

    running = _ctx("RUNNING")
    assert decide_locally("从题意解析重做", running).kind == "redo"
    assert decide_locally("从数据准备重做", _ctx("PAUSED")).kind == "redo"

    # 节点自提闸门（G1）上点名更早的阶段 → redo；修订门上 → 选门上的 redo: 选项
    g1 = decide_locally("从数据准备重做", _gate(G1_OPTIONS))
    assert (g1.kind, g1.stage) == ("redo", "DATA_PREPARATION")
    revision_gate = decide_locally("从数据准备重做", _gate(REVISION_OPTIONS, revision_gate_round=1))
    assert (revision_gate.kind, revision_gate.option_id) == ("approve", "redo:DATA_PREPARATION")


def test_explicit_stage_and_option_matching_helpers() -> None:
    assert explicit_stage("从数据准备重做") == "DATA_PREPARATION"
    assert explicit_stage("把论文撰写和结果验证都重做") == "VALIDATING"
    assert explicit_stage("重来一遍") is None
    assert [o.id for o in match_option("方案b", G1_MULTI_OPTIONS)] == ["adopt:plan_b"]
    assert [o.id for o in match_option("第一种", G1_MULTI_OPTIONS)] == ["approve"]
    assert match_option("退回", G1_OPTIONS) == [], "reject 不参与正向匹配"


# ── 待确认提案 ─────────────────────────────────────────────────────────────


def test_pending_proposal_confirm_deny_and_supersede() -> None:
    proposal = {"kind": "cancel", "status": "proposed", "message": "要取消吗"}
    assert pending_proposal_of([{"kind": "retry", "status": "executed"}, proposal]) == proposal
    assert pending_proposal_of([proposal, {"kind": "retry", "status": "executed"}]) is None
    assert pending_proposal_of([{"kind": "retry", "status": "proposed"}]) is None, "retry 不需确认"
    redo = {"kind": "redo", "status": "proposed", "stage": "MODEL_PLANNING"}
    assert pending_proposal_of([redo]) == redo, "重做要确认"
    assert pending_proposal_of(None) is None

    ctx = _ctx("RUNNING", pending_proposal=proposal)
    for text in ("确认", "是的", "好", "取消吧", "确定取消任务"):
        assert decide_locally(text, ctx) == ControlDecision(kind="confirm", source="proposal"), text
    for text in ("算了", "不要", "不取消", "取消提案", "no"):
        assert decide_locally(text, ctx) == ControlDecision(kind="deny", source="proposal"), text
    # 其它文本：提案作废，按新意图处理
    assert decide_locally("先暂停一下", ctx).kind == "pause"
    assert decide_locally("为什么这么慢", ctx).kind == "none"


# ── 判定回复解析 ───────────────────────────────────────────────────────────


def test_judge_reply_parsing_only_accepts_legal_results() -> None:
    failed = _ctx("FAILED")
    assert _parse_judge_reply('{"action": "retry", "reason": "用户要求再跑"}', failed).kind == "retry"
    assert _parse_judge_reply('好的，判定结果：{"action":"retry"}', failed).kind == "retry"
    assert _parse_judge_reply('{"action": "cancel"}', failed).kind == "none", "FAILED 下 cancel 不合法"
    assert _parse_judge_reply('{"action": "none"}', failed).kind == "none"
    assert _parse_judge_reply("完全不是 JSON", failed).kind == "none"
    assert _parse_judge_reply("[1, 2]", failed).kind == "none"

    gate = _gate(G1_MULTI_OPTIONS)
    ok = _parse_judge_reply('{"action":"approve","option_id":"adopt:plan_b"}', gate)
    assert (ok.kind, ok.option_id, ok.source) == ("approve", "adopt:plan_b", "judge")
    assert _parse_judge_reply('{"action":"approve","option_id":"adopt:nope"}', gate).kind == "none"
    assert _parse_judge_reply('{"action":"approve"}', gate).kind == "none", "approve 必须点名选项"
    assert _parse_judge_reply('{"action":"approve","option_id":"reject"}', gate).kind == "none"
    assert _parse_judge_reply('{"action":"reject"}', gate).option_id == "reject"

    done = _ctx("COMPLETED", node="PAPER_WRITING")
    rev = _parse_judge_reply('{"action":"revision","stage":"data_preparation"}', done)
    assert (rev.kind, rev.stage) == ("revision", "DATA_PREPARATION")
    assert _parse_judge_reply('{"action":"revision","stage":"NOPE"}', done).stage is None


def test_judge_reply_redo_needs_a_stage_that_has_something_to_redo() -> None:
    failed = _ctx("FAILED")  # 失败于实验运行
    redo = _parse_judge_reply('{"action":"redo","stage":"MODEL_PLANNING"}', failed, user_text="换成随机森林重新做")
    assert (redo.kind, redo.stage, redo.source) == ("redo", "MODEL_PLANNING", "judge")
    # 模型没给 / 给错阶段：按正文关键词推断起点（engine_glue.suggest_revision_stage）
    inferred = _parse_judge_reply('{"action":"redo","stage":"nope"}', failed, user_text="数据口径不对，重新准备数据")
    assert inferred.stage == "DATA_PREPARATION"
    # 点名的正是失败阶段 → 折成 retry（不必确认）
    assert _parse_judge_reply('{"action":"redo","stage":"EXPERIMENTING"}', failed).kind == "retry"
    # 要改的是还没开始的阶段：没有东西可重做，交备注
    assert _parse_judge_reply('{"action":"redo","stage":"PAPER_WRITING"}', failed).kind == "none"
    # 正文也推不出阶段 → none
    assert _parse_judge_reply('{"action":"redo"}', failed).kind == "none"
    # 修订门上 redo 不合法（六个起点是选项）
    assert _parse_judge_reply('{"action":"redo","stage":"DATA_PREPARATION"}', _gate(REVISION_OPTIONS, revision_gate_round=1)).kind == "none"
    # COMPLETED 上 redo 不合法（走 revision）
    assert _parse_judge_reply('{"action":"redo","stage":"DATA_PREPARATION"}', _ctx("COMPLETED", node="PAPER_WRITING")).kind == "none"


def test_decide_rules_are_a_fast_path_and_the_judge_sees_everything_else() -> None:
    """ADR-0019 §4：短句规则直判；其余一律出网带上下文；模型 none 时回落规则结果。"""
    calls: list[tuple[str, object]] = []
    answers: list[ControlDecision] = []

    def judge(text: str, ctx: RunControlContext, history) -> ControlDecision:
        calls.append((text, history))
        return answers.pop(0) if answers else ControlDecision(kind="none")

    failed = _ctx("FAILED")
    # 短句 + 规则命中 → 不出网
    assert decide("继续啊", failed, judge=judge).source == "rule"
    assert decide("从数据准备重做", failed, judge=judge) == ControlDecision(kind="redo", stage="DATA_PREPARATION", source="rule")
    assert calls == []
    # 没有任何词表命中的句子照样出网（不再有「气味词」门槛）
    answers.append(ControlDecision(kind="retry", source="judge"))
    assert decide("这个模型的假设合理吗", failed, judge=judge, history=[{"text": "上一句", "reply": "上一答"}]).kind == "retry"
    assert calls[-1] == ("这个模型的假设合理吗", [{"text": "上一句", "reply": "上一答"}])
    # 问句也交模型（它带着状态能分辨问与令）；模型说 none → 回落规则 → none
    assert decide("这个错误该怎么处理？", failed, judge=judge).kind == "none"
    # 长句里有规则词：规则只是候选，模型有更好判断时以模型为准
    answers.append(ControlDecision(kind="redo", stage="MODEL_PLANNING", source="judge"))
    long_sentence = "这个实验老是崩，我看还是换成随机森林重新做一遍比较靠谱"
    assert decide(long_sentence, failed, judge=judge).stage == "MODEL_PLANNING"
    # 模型拿不准（none）→ 回落规则命中的 retry，而不是什么都不做
    assert decide(long_sentence, failed, judge=judge) == ControlDecision(kind="retry", source="rule")
    # 超长文本不判定（粘贴的材料只落备注）
    assert decide("这次把它修好" + "。" * 800, failed, judge=judge).kind == "none"
    assert all(len(text) <= 800 for text, _ in calls)
    # 没有合法动作（已取消）→ 不出网
    before = len(calls)
    assert decide("重试", _ctx("CANCELLED"), judge=judge).kind == "none"
    assert len(calls) == before
    # 待确认提案的回应只看确认词，不出网
    proposal_ctx = _ctx("RUNNING", pending_proposal={"kind": "redo", "status": "proposed", "stage": "MODEL_PLANNING"})
    assert decide("确认", proposal_ctx, judge=judge).kind == "confirm"
    assert decide("算了", proposal_ctx, judge=judge).kind == "deny"
    assert len(calls) == before
    # 没有判定器（未配置接口 / sim）→ 规则结果就是最终结果
    assert decide("怎么又失败了，快继续想办法", failed, judge=None).kind == "retry"


# ── 提示词块 ───────────────────────────────────────────────────────────────


def test_prompt_block_states_status_actions_and_honesty_rule() -> None:
    ctx = _ctx("FAILED", failure_message="ModuleNotFoundError: scipy")
    block = prompt_block(ctx, [])
    assert "执行失败" in block and "实验运行" in block and "scipy" in block
    assert "「重试」" in block
    assert "本轮没有执行任何运行控制动作" in block

    after = _ctx("RUNNING")
    block = prompt_block(after, [{"kind": "retry", "status": "executed", "message": "已重试「实验运行」阶段"}])
    assert "- 已重试「实验运行」阶段" in block and "执行中" in block
    assert "不得声称执行了未列出的操作" in block

    gate = _gate(G1_MULTI_OPTIONS)
    block = prompt_block(gate, [])
    assert "待用户确认：确认建模方案" in block
    assert "方案 A（线性规划）（推荐）" in block

    done = _ctx("COMPLETED", node="PAPER_WRITING", revision_rounds=3)
    assert "修订轮数已用完" in prompt_block(done, [])


def test_reply_rules_hand_off_to_the_run_after_executed_actions() -> None:
    """运行接手后的回复只做交接：「按典型参数继续」触发 retry 时，模型不能在对话里替运行算题
    （2026-09-07 截图：回复气泡里逐项算分，下方执行步骤同时在跑——两份工作并行）。"""
    handoff = reply_rules([{"kind": "retry", "status": "executed", "message": "已重试「实验运行」阶段"}])
    assert "运行已经接手" in handoff and "不要自己算题" in handoff and "执行步骤" in handoff
    assert "不得声称执行了未列出的操作" in handoff
    # 交接口径不再要求「再回答用户的问题」式的展开
    assert "再回答用户的问题" not in handoff
    for kind in ("resume", "redo", "approve", "reject", "revision"):
        assert reply_rules([{"kind": kind, "status": "executed"}]) == handoff, kind

    # 提案待确认：说明代价、请用户确认，同样不替运行干活
    proposed = reply_rules([{"kind": "redo", "status": "proposed", "stage": "MODEL_PLANNING"}])
    assert "尚未执行" in proposed and "「确认」" in proposed and "不要自己算题" in proposed

    # 没有动作 / 动作被状态机拒绝 / 提案被放弃 / 只是暂停或取消：默认口径，照常回答问题
    default = reply_rules([])
    assert "再回答用户的问题" in default and "可用指令" in default
    assert reply_rules([{"kind": "retry", "status": "rejected"}]) == default
    assert reply_rules([{"kind": "cancel", "status": "dismissed"}]) == default
    assert reply_rules([{"kind": "pause", "status": "executed"}]) == default
    assert reply_rules([{"kind": "cancel", "status": "executed"}]) == default

    # prompt_block 末行就是这条口径
    assert prompt_block(_ctx("RUNNING"), [{"kind": "retry", "status": "executed", "message": "x"}]).endswith(handoff)
    assert prompt_block(_ctx("FAILED"), []).endswith(default)


# ── 端到端：托管轮 ────────────────────────────────────────────────────────────

JUDGE_MARKER = "运行控制判定器"


def _sse(*texts: str) -> httpx.Response:
    chunks = [
        f'data: {json.dumps({"choices": [{"delta": {"content": text}}]})}\n\n'.encode() for text in texts
    ] + [b"data: [DONE]\n\n"]
    return httpx.Response(200, content=chunks, headers={"content-type": "text/event-stream"})


def _install_chat_mock(monkeypatch, *, judge_reply='{"action":"none"}') -> list[dict]:
    """对话回复固定为「收到」，判定请求按 judge_reply 回；返回所有被捕获的请求正文。

    judge_reply 可以是固定字串，也可以是「判定提示词 → 回答」的函数（按这句话是什么来答）。
    保存了模型配置后引擎节点也会走模型：这里把装配档位钉在 sim，让运行本身
    仍按模拟链推进，mock 只服务对话与判定两类调用。
    """
    monkeypatch.setenv("OMM_AGENT_NODES", "sim")
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        captured.append(body)
        judge_messages = [m for m in body.get("messages", []) if JUDGE_MARKER in str(m.get("content", ""))]
        if judge_messages:
            prompt = str(judge_messages[0]["content"])
            answer = judge_reply(prompt) if callable(judge_reply) else judge_reply
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": answer}}], "usage": {"total_tokens": 20}},
            )
        return _sse("收到", "。")

    monkeypatch.setattr(llm_module, "_transport_factory", lambda: httpx.MockTransport(handler))
    return captured


def _save_config(client) -> None:
    response = client.put(
        "/api/account/llm-config",
        json={
            "endpoints": [
                {
                    "name": "主接口",
                    "protocol": "openai",
                    "base_url": "https://gateway.test/v1",
                    "api_key": "sk-main",
                    "model": "gpt-test",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text


def _events(text: str) -> list[dict]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


def _say(client, scope: str, text: str) -> tuple[dict, list[dict]]:
    """发一轮托管对话并同步等到终态（GET /events 缓冲到 done 才返回）。"""
    response = client.post(
        "/api/chat/turns",
        json={"messages": [{"role": "user", "content": text}], "scope_id": scope, "text": text},
    )
    assert response.status_code == 202, response.text
    turn = response.json()["turn"]
    events = _events(client.get(f"/api/chat/turns/{turn['id']}/events?after=0").text)
    final = client.get(f"/api/chat/turns/{turn['id']}").json()["turn"]
    return final, events


def _failed_run(client, make_run, tick) -> str:
    run_id = make_run("实验容错验证 [fail:experiment]")["id"]
    tick(run_id, times=3)  # 到 G1
    approved = client.post(f"/api/v1/task-runs/{run_id}/actions", json={"action": "approve"})
    assert approved.status_code == 200, approved.text
    assert tick(run_id) == "FAILED"
    return run_id


def _is_judge_call(body: dict) -> bool:
    return any(JUDGE_MARKER in str(m.get("content", "")) for m in body.get("messages", []))


def _system_prompt(captured: list[dict]) -> str:
    chat_calls = [b for b in captured if not _is_judge_call(b)]
    assert chat_calls, "应有一次对话模型调用"
    return str(chat_calls[-1]["messages"][0]["content"])


def test_chat_retries_failed_run_and_injects_note(client, make_run, tick, monkeypatch):
    captured = _install_chat_mock(monkeypatch)
    _save_config(client)
    run_id = _failed_run(client, make_run, tick)

    final, events = _say(client, run_id, "怎么又失败了，快继续想办法")

    assert events[0]["type"] == "action", events[:2]
    action = events[0]
    assert action["kind"] == "retry" and action["status"] == "executed"
    assert action["stage"] == "EXPERIMENTING" and action["stage_label"] == "实验运行"
    assert action["note_id"]
    assert events[-1]["type"] == "done"
    assert final["status"] == "completed" and final["reply"] == "收到。"
    # 回执随轮落库：刷新页面按 meta.actions 重画
    assert final["meta"]["actions"] == [{k: v for k, v in action.items() if k not in ("type", "seq")}]

    run = client.get(f"/api/v1/task-runs/{run_id}").json()
    assert run["status"] == "RUNNING" and run["failure"] is None
    with client.app.state.db.session_factory() as session:
        notes = session.scalars(select(RunNoteRow).where(RunNoteRow.run_id == run_id)).all()
        assert [n.text for n in notes] == ["怎么又失败了，快继续想办法"]
        assert notes[0].scope == "global"

    # 模型拿到的系统提示词带状态块，且状态是执行后的最新值
    system = _system_prompt(captured)
    assert "【当前运行状态】" in system and "执行中" in system
    assert "已重试「实验运行」阶段" in system
    # 这句超过短句阈值：先出网判定（模型答 none），再回落规则命中的 retry（ADR-0019 §4）
    judge_calls = [body for body in captured if _is_judge_call(body)]
    assert len(judge_calls) == 1
    judge_prompt = judge_calls[0]["messages"][0]["content"]
    assert "怎么又失败了，快继续想办法" in judge_prompt and "失败于此" in judge_prompt

    # 第 2 次尝试成功并走完
    assert tick(run_id) == "RUNNING"
    tick(run_id, times=3)
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "COMPLETED"


def test_chat_question_on_failed_run_is_plain_chat_with_status_context(client, make_run, tick, monkeypatch):
    captured = _install_chat_mock(monkeypatch)
    _save_config(client)
    run_id = _failed_run(client, make_run, tick)

    final, events = _say(client, run_id, "为什么失败了？")

    assert [e["type"] for e in events if e["type"] == "action"] == []
    assert final["status"] == "completed" and "actions" not in final["meta"]
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "FAILED"
    system = _system_prompt(captured)
    assert "执行失败" in system and "本轮没有执行任何运行控制动作" in system
    # 终态 + 问句：既不重试，也不落备注
    with client.app.state.db.session_factory() as session:
        assert session.scalars(select(RunNoteRow).where(RunNoteRow.run_id == run_id)).all() == []


def test_chat_cancel_needs_confirmation_across_turns(client, make_run, tick, monkeypatch):
    _install_chat_mock(monkeypatch)
    _save_config(client)
    run_id = make_run("对话取消验证")["id"]
    assert tick(run_id) == "RUNNING"

    first, events = _say(client, run_id, "取消任务")
    proposal = events[0]
    assert proposal["type"] == "action" and proposal["kind"] == "cancel" and proposal["status"] == "proposed"
    assert proposal["confirm_hint"]
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "RUNNING", "提案不改状态"
    assert first["meta"]["actions"][0]["status"] == "proposed"

    second, events = _say(client, run_id, "确认")
    executed = events[0]
    assert executed["kind"] == "cancel" and executed["status"] == "executed"
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "CANCELLED"

    # 已取消：再说「重试」没有合法动作，只是普通对话
    third, events = _say(client, run_id, "重试")
    assert [e for e in events if e["type"] == "action"] == []
    assert third["status"] == "completed"


def test_chat_cancel_proposal_can_be_dismissed(client, make_run, tick, monkeypatch):
    _install_chat_mock(monkeypatch)
    _save_config(client)
    run_id = make_run("对话放弃取消")["id"]
    assert tick(run_id) == "RUNNING"

    _say(client, run_id, "不做了")
    final, events = _say(client, run_id, "算了")
    assert events[0]["kind"] == "cancel" and events[0]["status"] == "dismissed"
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "RUNNING"

    # 提案只对紧接着的一轮有效：再说「确认」不会突然取消
    final, events = _say(client, run_id, "确认")
    assert [e for e in events if e["type"] == "action"] == []
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "RUNNING"


def test_chat_picks_gate_option_by_name(client, make_run, tick, monkeypatch):
    _install_chat_mock(monkeypatch)
    _save_config(client)
    run_id = make_run("对话审批验证")["id"]
    tick(run_id, times=3)
    run = client.get(f"/api/v1/task-runs/{run_id}").json()
    assert run["status"] == "WAITING_APPROVAL"

    final, events = _say(client, run_id, "同意，按这个方案做")
    action = events[0]
    assert action["type"] == "action" and action["kind"] == "approve" and action["status"] == "executed"
    assert action["option_id"] == "approve" and action["option_label"] == "采用当前方案"
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "RUNNING"
    approvals = client.get(f"/api/v1/task-runs/{run_id}/approvals").json()["items"]
    resolved = [a for a in approvals if a["id"] == action["approval_id"]][0]
    assert resolved["status"] == "RESOLVED"
    assert resolved["resolution"]["option_id"] == "approve"
    assert resolved["resolution"]["comment"] == "同意，按这个方案做"


def test_chat_revision_on_completed_run_opens_gate_with_named_stage(client, make_run, tick, monkeypatch):
    _install_chat_mock(monkeypatch)
    _save_config(client)
    run_id = make_run("对话修订验证")["id"]
    approve_when_asked(client, run_id)
    wait_until(client, run_id, run_status_is(client, run_id, "COMPLETED"))

    final, events = _say(client, run_id, "从数据准备重做，特征要加上时间窗口")
    action = events[0]
    assert action["type"] == "action" and action["kind"] == "revision" and action["status"] == "executed"
    assert action["round"] == 1 and action["stage"] == "DATA_PREPARATION" and action["note_id"]

    run = client.get(f"/api/v1/task-runs/{run_id}").json()
    assert run["status"] == "WAITING_APPROVAL"
    approvals = client.get(f"/api/v1/task-runs/{run_id}/approvals").json()["items"]
    gate = [a for a in approvals if a["id"] == action["approval_id"]][0]
    assert gate["status"] == "PENDING"
    assert [o["id"] for o in gate["options"] if o.get("recommended")] == ["redo:DATA_PREPARATION"], "点名阶段被预选"

    # 下一句「确认」批准预选起点 → 重跑
    final, events = _say(client, run_id, "确认")
    assert events[0]["kind"] == "approve" and events[0]["option_id"] == "redo:DATA_PREPARATION"
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "RUNNING"


def test_chat_uses_weak_model_judge_when_rules_are_unsure(client, make_run, tick, monkeypatch):
    def judge_reply(prompt: str) -> str:
        # 只对「这次把它修好」判 retry；前一句问话按 none 答（模拟真实判定）
        return '{"action":"retry","reason":"要求修好"}' if "用户这句话：这次把它修好" in prompt else '{"action":"none"}'

    captured = _install_chat_mock(monkeypatch, judge_reply=judge_reply)
    _save_config(client)
    run_id = _failed_run(client, make_run, tick)

    # 先聊一句，让判定器有上文可带
    _say(client, run_id, "为什么失败了？")
    final, events = _say(client, run_id, "这次把它修好")
    assert events[0]["type"] == "action" and events[0]["kind"] == "retry" and events[0]["status"] == "executed"
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "RUNNING"
    judge_calls = [b for b in captured if _is_judge_call(b)]
    assert len(judge_calls) == 2 and all(b.get("stream") is not True for b in judge_calls)
    prompt = judge_calls[-1]["messages"][0]["content"]
    assert "当前允许的动作" in prompt
    # 最近对话随判定请求下发（ADR-0019 §4）
    assert "最近对话" in prompt and "用户：为什么失败了？" in prompt and "助手：收到。" in prompt


def _judge_redo(stage: str) -> str:
    return json.dumps({"action": "redo", "stage": stage, "reason": "要换模型"})


def test_chat_redo_from_earlier_stage_on_failed_run_is_proposed_then_executed(client, make_run, tick, monkeypatch):
    """ADR-0019：失败运行上一句没有任何词表词的修改要求 → 模型判成 redo → 提案 → 确认 → 真正回退重做。"""
    _install_chat_mock(monkeypatch, judge_reply=_judge_redo("MODEL_PLANNING"))
    _save_config(client)
    run_id = _failed_run(client, make_run, tick)

    first, events = _say(client, run_id, "这个实验老是崩，我看还是换成随机森林比较靠谱")
    proposal = events[0]
    assert proposal["type"] == "action" and proposal["kind"] == "redo" and proposal["status"] == "proposed"
    assert proposal["stage"] == "MODEL_PLANNING" and proposal["stage_label"] == "建模方案"
    assert proposal["text"] == "这个实验老是崩，我看还是换成随机森林比较靠谱"
    assert "重新计费" in proposal["message"] and proposal["confirm_hint"]
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "FAILED", "提案不改状态"
    with client.app.state.db.session_factory() as session:
        assert session.scalars(select(RunNoteRow).where(RunNoteRow.run_id == run_id)).all() == [], "提案阶段不落备注"

    second, events = _say(client, run_id, "确认")
    executed = events[0]
    assert executed["kind"] == "redo" and executed["status"] == "executed"
    assert executed["stage"] == "MODEL_PLANNING" and executed["note_id"]
    run = client.get(f"/api/v1/task-runs/{run_id}").json()
    assert run["status"] == "RUNNING" and run["current_node"] == "MODEL_PLANNING" and run["failure"] is None
    with client.app.state.db.session_factory() as session:
        notes = session.scalars(select(RunNoteRow).where(RunNoteRow.run_id == run_id)).all()
        # 落的是提案里带过来的原话，不是「确认」
        assert [n.text for n in notes] == ["这个实验老是崩，我看还是换成随机森林比较靠谱"]

    # 下个 tick 从建模方案起第 2 趟；上游两段不动
    assert tick(run_id) == "WAITING_APPROVAL"
    steps = client.get(f"/api/v1/task-runs/{run_id}/steps").json()["items"]
    attempts = {}
    for step in steps:
        attempts.setdefault(step["node"], []).append(step["attempt"])
    assert attempts["PROBLEM_ANALYSIS"] == [1] and attempts["DATA_PREPARATION"] == [1]
    assert attempts["MODEL_PLANNING"] == [1, 2]


def test_chat_redo_on_gate_supersedes_the_pending_approval(client, make_run, tick, monkeypatch):
    """等待方案确认（G1）时点名更早的阶段：规则直判 redo 提案；确认后门作废、从数据准备重跑。"""
    _install_chat_mock(monkeypatch)
    _save_config(client)
    run_id = make_run("对话回退验证")["id"]
    tick(run_id, times=3)
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "WAITING_APPROVAL"
    gate = [a for a in client.get(f"/api/v1/task-runs/{run_id}/approvals").json()["items"] if a["status"] == "PENDING"][0]

    first, events = _say(client, run_id, "从数据准备重做")
    assert events[0]["kind"] == "redo" and events[0]["status"] == "proposed" and events[0]["stage"] == "DATA_PREPARATION"
    assert "作废" in events[0]["message"]

    second, events = _say(client, run_id, "确认")
    assert events[0]["kind"] == "redo" and events[0]["status"] == "executed"
    run = client.get(f"/api/v1/task-runs/{run_id}").json()
    assert run["status"] == "RUNNING" and run["current_node"] == "DATA_PREPARATION"
    approvals = client.get(f"/api/v1/task-runs/{run_id}/approvals").json()["items"]
    superseded = [a for a in approvals if a["id"] == gate["id"]][0]
    assert superseded["status"] == "CANCELLED" and superseded["resolution"] is None, "作废不是决策"
    with client.app.state.db.session_factory() as session:
        row = session.get(ApprovalRequestRow, gate["id"])
        assert row.evidence["superseded_by"]["kind"] == "redo"
        assert row.evidence["superseded_by"]["target_state"] == "DATA_PREPARATION"

    assert tick(run_id) == "RUNNING"
    steps = client.get(f"/api/v1/task-runs/{run_id}/steps").json()["items"]
    data_steps = sorted(s["attempt"] for s in steps if s["node"] == "DATA_PREPARATION")
    assert data_steps == [1, 2]


def test_chat_redo_proposal_can_be_dismissed_and_future_stage_changes_stay_notes(client, make_run, tick, monkeypatch):
    _install_chat_mock(monkeypatch, judge_reply=_judge_redo("PAPER_WRITING"))
    _save_config(client)
    run_id = make_run("对话备注验证")["id"]
    assert tick(run_id) == "RUNNING"  # 题意解析已完成，下一 tick 进数据准备

    # 模型判成「从论文撰写重做」，但论文阶段还没开始：没有东西可重做 → 静默落备注
    final, events = _say(client, run_id, "论文最后用英文写，图表要放附录")
    assert [e for e in events if e["type"] == "action"] == []
    with client.app.state.db.session_factory() as session:
        notes = session.scalars(select(RunNoteRow).where(RunNoteRow.run_id == run_id)).all()
        assert [n.text for n in notes] == ["论文最后用英文写，图表要放附录"]

    # 点名已完成阶段 → 提案；「算了」放弃，状态不变
    _say(client, run_id, "从题意解析重做")
    final, events = _say(client, run_id, "算了")
    assert events[0]["kind"] == "redo" and events[0]["status"] == "dismissed"
    assert "从「题意解析」重做" in events[0]["message"]
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "RUNNING"


def test_chat_judge_failure_falls_back_to_plain_chat(client, make_run, tick, monkeypatch):
    monkeypatch.setenv("OMM_AGENT_NODES", "sim")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        if any(JUDGE_MARKER in str(m.get("content", "")) for m in body.get("messages", [])):
            return httpx.Response(500, json={"error": "boom"})
        return _sse("收到")

    monkeypatch.setattr(llm_module, "_transport_factory", lambda: httpx.MockTransport(handler))
    _save_config(client)
    run_id = _failed_run(client, make_run, tick)

    final, events = _say(client, run_id, "这次把它修好")
    assert [e for e in events if e["type"] == "action"] == []
    assert final["status"] == "completed" and final["reply"] == "收到"
    assert client.get(f"/api/v1/task-runs/{run_id}").json()["status"] == "FAILED"


def test_chat_scope_without_run_skips_control(client, monkeypatch):
    captured = _install_chat_mock(monkeypatch)
    _save_config(client)
    scope = "chat_" + "1" * 32

    final, events = _say(client, scope, "重试")
    assert [e for e in events if e["type"] == "action"] == []
    assert final["status"] == "completed"
    assert "【当前运行状态】" not in _system_prompt(captured)
