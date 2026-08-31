"""EngineLlmPort 传输层瞬态重试（整次调用粒度的自愈）。

真实事故：论文章节流式生成中途上游断连（peer closed connection），一次调用
失败直接把整个任务打成 FAILED。修复后端口对 _should_fall_back 认定的瞬态类
（断连/超时/限流）按整次调用重试（至多 ENGINE_CALL_MAX_ATTEMPTS 次），事件流
里每次尝试都有 llm_call_started / llm_call_failed 收尾对，前端据此把同一行
标成「第 N 次尝试」并清空半截增量。

三条边界：瞬态失败重试后恢复、确定性失败绝不重试、重试耗尽如实上抛。
纯单元测试：打桩 stream_complete_with_fallback，不出网、不起应用。
"""

from __future__ import annotations

import pytest
from omm_api import llm as llm_module
from omm_api.errors import ApiError
from omm_api.llm import (
    ENGINE_CALL_MAX_ATTEMPTS,
    ChatOutcome,
    EngineLlmPort,
    LlmConfig,
    LlmEndpoint,
)

ENDPOINT = LlmEndpoint(
    id="ep-test",
    name="测试接口",
    protocol="openai",
    base_url="https://llm.test/v1",
    model="gpt-test",
)


class _Template:
    def render(self, variables: dict) -> str:
        return "渲染后的提示词"


class _Registry:
    def get(self, prompt_id: str) -> _Template:
        return _Template()


class _Budget:
    """记录预算门每次尝试都被预检（重试不许绕过失控保护）。"""

    def __init__(self) -> None:
        self.checks = 0
        self.charges = 0

    def check_llm_call(self, node_id) -> None:
        self.checks += 1

    def charge_llm(self, tokens, node_id) -> None:
        self.charges += 1


def _outcome(text: str = '{"ok": true}') -> ChatOutcome:
    return ChatOutcome(
        text=text,
        model="gpt-test",
        endpoint=ENDPOINT,
        usage={"prompt_tokens": 10, "completion_tokens": 20},
        elapsed_ms=120,
    )


def _make_port(events: list[dict], budget: _Budget | None = None) -> EngineLlmPort:
    return EngineLlmPort(
        LlmConfig(endpoints=(ENDPOINT,), active_endpoint_id=ENDPOINT.id, stream=True),
        _Registry(),
        on_event=events.append,
        budget=budget,
    )


def _patch_stream(monkeypatch, results: list) -> list[float]:
    """按序弹出 results：异常则抛出，否则作为成果返回。返回退避记录。"""
    queue = list(results)

    def fake_stream(config, messages, on_delta=None, **kwargs):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps: list[float] = []
    monkeypatch.setattr(llm_module, "stream_complete_with_fallback", fake_stream)
    monkeypatch.setattr(llm_module.time, "sleep", sleeps.append)
    return sleeps


def _kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


def test_transient_stream_failure_retries_and_recovers(monkeypatch) -> None:
    """断连一次 → 自动重来一次调用并成功；事件流有完整的失败收尾对。"""
    events: list[dict] = []
    budget = _Budget()
    disconnect = ApiError(
        502, "LLM_UNREACHABLE", "无法连接接口「DeepSeek」：peer closed connection"
    )
    sleeps = _patch_stream(monkeypatch, [disconnect, _outcome()])

    text = _make_port(events, budget).complete("experiment_code", {})

    assert text == '{"ok": true}'
    kinds = _kinds(events)
    # 第 1 次：started + failed；第 2 次：started + llm_call 摘要
    assert kinds.count("llm_call_started") == 2
    assert kinds.count("llm_call_failed") == 1
    assert kinds[-1] == "llm_call"
    # 失败收尾对先于第二次开始（前端靠这个顺序把行清空重走）
    assert kinds.index("llm_call_failed") < len(kinds) - 1 - kinds[::-1].index("llm_call_started")
    assert sleeps == [2.0]
    # 每次尝试都过预算门；成功只记一次账
    assert budget.checks == 2 and budget.charges == 1


def test_non_transient_failure_does_not_retry(monkeypatch) -> None:
    """确定性失败（上游 4xx/5xx 信封、结构违约类）绝不烧重试预算。"""
    events: list[dict] = []
    upstream = ApiError(502, "LLM_UPSTREAM_ERROR", "接口「X」返回 HTTP 500：boom")
    sleeps = _patch_stream(monkeypatch, [upstream, _outcome()])

    with pytest.raises(ApiError) as excinfo:
        _make_port(events).complete("experiment_code", {})

    assert excinfo.value.code == "LLM_UPSTREAM_ERROR"
    kinds = _kinds(events)
    assert kinds.count("llm_call_started") == 1
    assert kinds.count("llm_call_failed") == 1
    assert sleeps == []


def test_no_balance_failure_does_not_retry(monkeypatch) -> None:
    """402 余额不足是确定性失败：链内回退已试过备用接口，整次重试只会原样再撞。"""
    events: list[dict] = []
    no_balance = ApiError(
        402, "LLM_NO_BALANCE", "接口「DeepSeek」余额不足（HTTP 402）：Insufficient Balance"
    )
    sleeps = _patch_stream(monkeypatch, [no_balance, _outcome()])

    with pytest.raises(ApiError) as excinfo:
        _make_port(events).complete("experiment_code", {})

    assert excinfo.value.code == "LLM_NO_BALANCE"
    kinds = _kinds(events)
    assert kinds.count("llm_call_started") == 1
    assert kinds.count("llm_call_failed") == 1
    assert sleeps == []


def test_retries_exhausted_raises_last_error(monkeypatch) -> None:
    """连环瞬态失败：至多 ENGINE_CALL_MAX_ATTEMPTS 次，之后如实上抛。"""
    events: list[dict] = []
    budget = _Budget()
    failures = [
        ApiError(504, "LLM_TIMEOUT", f"第 {i} 次超时")
        for i in range(1, ENGINE_CALL_MAX_ATTEMPTS + 1)
    ]
    sleeps = _patch_stream(monkeypatch, failures)

    with pytest.raises(ApiError) as excinfo:
        _make_port(events, budget).complete("experiment_code", {})

    assert excinfo.value.code == "LLM_TIMEOUT"
    assert f"第 {ENGINE_CALL_MAX_ATTEMPTS} 次超时" in excinfo.value.message
    kinds = _kinds(events)
    assert kinds.count("llm_call_started") == ENGINE_CALL_MAX_ATTEMPTS
    assert kinds.count("llm_call_failed") == ENGINE_CALL_MAX_ATTEMPTS
    # 最后一次失败后不再退避
    assert sleeps == [2.0, 5.0]
    assert budget.checks == ENGINE_CALL_MAX_ATTEMPTS and budget.charges == 0
