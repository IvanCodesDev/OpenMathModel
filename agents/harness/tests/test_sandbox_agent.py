"""沙盒 Agent 执行体（§7.1）：断言验收、修复波次、R2 预算与报告形状。

E4 起点：全部用 scripted chat + 假工具执行器驱动，断言的是控制流与报告
事实——尤其是"模型自述成功不算数"这条产品立身纪律。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from omm_agent_core.models import ArtifactRef, ToolResult
from omm_agent_harness import (
    Message,
    Reply,
    SandboxAssertion,
    SandboxTask,
    ToolCall,
    Usage,
    run_sandbox_task,
)

# -- scripted collaborators ----------------------------------------------------


def text_reply(content: str) -> Reply:
    return Reply(content=content, tool_calls=(), usage=Usage(10, 5, 3), model="stub")


def tool_reply(*calls: ToolCall) -> Reply:
    return Reply(content=None, tool_calls=tuple(calls), usage=Usage(10, 5, 3), model="stub")


def run_call(code: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="python_run", arguments={"code": code})


DONE = json.dumps({"summary": "已完成并自查通过"})


class ScriptedChat:
    def __init__(self, replies: Sequence[Reply]) -> None:
        self._replies = list(replies)
        self.calls = 0

    def __call__(self, messages: Sequence[Message]) -> Reply:
        self.calls += 1
        if not self._replies:
            raise AssertionError("chat called more times than scripted")
        return self._replies.pop(0)


def artifact(artifact_id: str, name: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        kind="table",
        uri=f"local://x/{name}",
        sha256="0" * 64,
        size=1,
        media_type="text/plain",
        producer_step="step_1",
    )


class FakeSandbox:
    """python_run 的脚本化执行器：按调用序返回预设结果。"""

    def __init__(self, results: Sequence[ToolResult]) -> None:
        self._results = list(results)
        self.codes: list[str] = []

    def __call__(self, calls: Sequence[ToolCall]) -> Sequence[ToolResult]:
        outcomes = []
        for call in calls:
            assert call.name == "python_run", f"unexpected tool {call.name}"
            self.codes.append(str(call.arguments.get("code")))
            outcomes.append(self._results.pop(0) if self._results else ToolResult(status="failed", error="script exhausted"))
        return outcomes


def ok_run(stdout: str, *artifacts: ArtifactRef) -> ToolResult:
    return ToolResult(status="succeeded", output={"stdout": stdout}, artifacts=tuple(artifacts))


ENV = {"runtime": "python", "version": "3.12.4", "deps_hash": "d" * 64}


def make_task(*assertions: SandboxAssertion, **overrides) -> SandboxTask:
    defaults = dict(
        task_id="t1",
        goal="构造数据并输出指标",
        system_prompt="你是沙盒执行工程师，只依据任务卡工作。",
        task_brief="生成 metrics.json 并打印指标标记行。",
        assertions=tuple(assertions),
        seeds={"random": 42},
    )
    defaults.update(overrides)
    return SandboxTask(**defaults)


def metrics_has_rmse() -> SandboxAssertion:
    def check(evidence) -> tuple[bool, str]:
        if "rmse" not in evidence.metrics:
            return False, "指标标记行缺少 rmse 键（未运行或脚本未打印 OMM_METRICS_JSON）"
        return True, f"rmse={evidence.metrics['rmse']}"

    return SandboxAssertion(id="a1", description="指标含 rmse", check=check)


def run(task, chat, sandbox, files=(), read=lambda p: ""):
    return run_sandbox_task(
        task,
        chat=chat,
        execute_tools=sandbox,
        workspace_files=lambda: list(files),
        read_text=read,
        env_fingerprint=ENV,
        publish_code=lambda code: "art_code_final",
    )


# -- 一波通过 --------------------------------------------------------------------


def test_single_wave_pass_produces_contract_shaped_report() -> None:
    chat = ScriptedChat([tool_reply(run_call("print(1)")), text_reply(DONE)])
    sandbox = FakeSandbox([
        ok_run('OMM_METRICS_JSON: {"rmse": 0.5}', artifact("art_m", "metrics.json"))
    ])
    report = run(make_task(metrics_has_rmse()), chat, sandbox)

    assert report["status"] == "passed"
    assert report["attempts"] == 1
    assert report["usage"]["runs"] == 1
    assert report["final_code_artifact"] == "art_code_final"
    assert report["produced_artifacts"] == ["art_m"]
    assert report["metrics_source_artifact"] == "art_m"
    assert report["assertions"] == [
        {"id": "a1", "passed": True, "detail": "rmse=0.5"}
    ]
    assert report["seeds"] == {"random": 42}
    assert report["env_fingerprint"] == ENV
    # 契约必填键齐备（sandbox-run-report.v1）
    assert set(report) == {
        "status", "attempts", "final_code_artifact", "produced_artifacts",
        "metrics_source_artifact", "assertions", "seeds", "env_fingerprint", "usage",
    }


# -- 模型自述成功不算数 -----------------------------------------------------------


def test_self_reported_success_is_rejected_by_assertions() -> None:
    """模型不跑代码直接说"完成"：断言评估失败 → 反馈修复 → 第二波真跑通过。"""
    chat = ScriptedChat([
        text_reply(DONE),  # 第一波：空口自述
        tool_reply(run_call('print("fix")')),  # 第二波：被反馈逼着真跑
        text_reply(DONE),
    ])
    sandbox = FakeSandbox([
        ok_run('OMM_METRICS_JSON: {"rmse": 0.4}', artifact("art_m2", "metrics.json"))
    ])
    report = run(make_task(metrics_has_rmse()), chat, sandbox)

    assert report["status"] == "passed"
    assert report["attempts"] == 2, "第一波自述被断言打回，第二波才通过"
    assert report["usage"]["runs"] == 1


def test_assertion_feedback_enters_next_wave_prompt() -> None:
    chat = ScriptedChat([
        text_reply(DONE),
        tool_reply(run_call("print(2)")),
        text_reply(DONE),
    ])
    sandbox = FakeSandbox([
        ok_run('OMM_METRICS_JSON: {"rmse": 0.3}', artifact("art_m3", "metrics.json"))
    ])

    seen: list[str] = []
    original_call = chat.__call__

    class SpyChat:
        def __call__(self, messages):
            seen.append("\n".join(m.content for m in messages))
            return original_call(messages)

    run(make_task(metrics_has_rmse()), SpyChat(), sandbox)
    assert "上一轮未通过验收" in seen[1]
    assert "缺少 rmse" in seen[1]


# -- R2 预算 ---------------------------------------------------------------------


def test_r2_budget_caps_sandbox_runs() -> None:
    """max_runs=2：第三次运行不会执行，任务收束为 failed。"""
    chat = ScriptedChat([
        tool_reply(run_call("bad1")), text_reply(DONE),  # 波1：跑1次，断言不过
        tool_reply(run_call("bad2")), tool_reply(run_call("bad3")), text_reply(DONE),  # 波2：跑第2次后第3次被预算拒绝
    ])
    sandbox = FakeSandbox([
        ok_run("no metrics"),
        ok_run("still none"),
        # 第三个结果不该被消费：预算拒绝发生在执行之前
        ok_run('OMM_METRICS_JSON: {"rmse": 0.1}'),
    ])
    report = run(make_task(metrics_has_rmse(), max_runs=2, max_waves=5), chat, sandbox)

    assert report["status"] == "failed"
    assert report["usage"]["runs"] == 2, "越线的那次运行不执行"
    assert len(sandbox.codes) == 2, "第三次调用未到达真实执行器"
    assert report["assertions"][0]["passed"] is False


def test_wave_limit_bounds_repair_loops() -> None:
    chat = ScriptedChat([
        text_reply(DONE), text_reply(DONE),  # 两波都空口自述
    ])
    sandbox = FakeSandbox([])
    report = run(make_task(metrics_has_rmse(), max_waves=2), chat, sandbox)
    assert report["status"] == "failed"
    assert report["attempts"] == 2
    assert report["usage"]["runs"] == 0


# -- 断言代码缺陷与证据面 -----------------------------------------------------------


def test_assertion_exception_counts_as_failure_with_detail() -> None:
    def broken(_evidence) -> tuple[bool, str]:
        raise RuntimeError("assertion bug")

    task = make_task(
        SandboxAssertion(id="ax", description="坏断言", check=broken), max_waves=1
    )
    chat = ScriptedChat([text_reply(DONE)])
    report = run(task, chat, FakeSandbox([]))
    assert report["status"] == "failed"
    assert "断言执行异常" in report["assertions"][0]["detail"]


def test_evidence_exposes_workspace_files_and_reader() -> None:
    def check(evidence) -> tuple[bool, str]:
        if "out/result.csv" not in evidence.files:
            return False, "缺少 out/result.csv"
        return True, evidence.read_text("out/result.csv")

    task = make_task(SandboxAssertion(id="af", description="产物齐", check=check))
    chat = ScriptedChat([tool_reply(run_call("write files")), text_reply(DONE)])
    sandbox = FakeSandbox([ok_run("done")])
    report = run(
        task, chat, sandbox,
        files=["out/result.csv"],
        read=lambda p: "value,1",
    )
    assert report["status"] == "passed"
    assert report["assertions"][0]["detail"] == "value,1"


def test_no_assertions_means_run_success_is_acceptance() -> None:
    """空断言清单 = 消费方显式声明只以运行成功为准（契约描述允许）。"""
    chat = ScriptedChat([tool_reply(run_call("print(9)")), text_reply(DONE)])
    sandbox = FakeSandbox([ok_run("ok")])
    report = run(make_task(), chat, sandbox)
    assert report["status"] == "passed"
    assert report["assertions"] == []
    assert report["metrics_source_artifact"] is None
