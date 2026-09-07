"""模型目录（ADR-0017）：公共目录裁剪、亮点挑选、单价索引、缓存文件与接口行为。

目录内容一律用内嵌的 models.dev 同构字典，经注入的 fetcher 喂给 ModelCatalog，
不出网。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from omm_api import model_catalog as mc
from omm_api.config import Settings
from omm_api.errors import ApiError
from omm_api.usage import model_pricing


def _entry(release: str, *, family: str = "", inputs=("text",), outputs=("text",), cost=(1.0, 2.0), status=None, reasoning=True):
    entry = {
        "release_date": release,
        "modalities": {"input": list(inputs), "output": list(outputs)},
        "reasoning": reasoning,
        "limit": {"context": 1_000_000},
    }
    if family:
        entry["family"] = family
    if cost is not None:
        entry["cost"] = {"input": cost[0], "output": cost[1]}
    if status:
        entry["status"] = status
    return entry


RAW = {
    "anthropic": {
        "id": "anthropic",
        "models": {
            "claude-fable-5-1": _entry("2026-09-01", family="claude-fable", inputs=("text", "image", "pdf"), cost=(10, 50)),
            "claude-fable-5": _entry("2026-06-07", family="claude-fable", inputs=("text", "image"), cost=(10, 50)),
            "claude-opus-5": _entry("2026-07-24", family="claude-opus", inputs=("text", "image"), cost=(5, 25)),
            "claude-sonnet-5": _entry("2026-06-29", family="claude-sonnet", inputs=("text", "image"), cost=(2, 10)),
            "claude-opus-4-5-20251101": _entry("2025-11-24", family="claude-opus", inputs=("text", "image"), cost=(5, 25)),
            "claude-haiku-4-5": _entry("2025-10-15", family="claude-haiku", inputs=("text", "image"), cost=(1, 5)),
        },
    },
    "google": {
        "id": "google",
        "models": {
            "gemini-3.8-flash": _entry("2026-09-02", family="gemini-flash", inputs=("text", "image", "video"), cost=(0.75, 3.75)),
            "gemini-3.7-flash": _entry("2026-08-13", family="gemini-flash", inputs=("text", "image"), cost=(0.75, 3.75)),
            "gemini-flash-latest": _entry("2026-08-13", family="gemini-flash", inputs=("text", "image"), cost=(0.75, 3.75)),
            "gemini-3.5-flash-lite": _entry("2026-07-21", family="gemini-flash-lite", inputs=("text", "image"), cost=(0.3, 2.5)),
            "gemini-3.1-flash-image": _entry("2026-05-28", family="gemini-flash", outputs=("text", "image"), cost=(0.5, 60)),
            "gemini-embedding-2": _entry("2026-04-22", family="gemini", cost=(0.2, 0)),
            "gemini-2.0-flash": _entry("2025-02-05", family="gemini-flash", status="deprecated", cost=(0.1, 0.4)),
        },
    },
    "deepseek": {
        "id": "deepseek",
        "models": {
            "deepseek-v4-flash-vision-exp": _entry(
                "2026-08-21", family="deepseek-flash", inputs=("text", "image"), status="beta", cost=(0.14, 0.28)
            ),
            "deepseek-v4-pro": _entry("2026-08-12", family="deepseek-thinking", cost=(0.435, 0.87)),
            "deepseek-v4-flash": _entry("2026-07-31", family="deepseek-flash", cost=(0.14, 0.28)),
        },
    },
    "alibaba": {
        "id": "alibaba",
        "models": {
            "qwen3.8-flash": _entry("2026-08-26", family="qwen", cost=(0.14, 0.43)),
            "qwen3.8-max": _entry("2026-08-03", family="qwen", cost=(0.6, 2.4)),
            "deepseek-v4-flash-0731": _entry("2026-07-31", family="deepseek", cost=(9.9, 9.9)),
            "glm-5.2": _entry("2026-06-13", family="glm", cost=(9.9, 9.9)),
            "qwen3-asr-flash": _entry("2025-09-08", family="qwen", inputs=("audio",), cost=(0.03, 0.03)),
        },
    },
    "zai": {"id": "zai", "models": {"glm-5.3": _entry("2026-08-14", family="glm", cost=(1.4, 4.4))}},
    "some-relay": {
        "id": "some-relay",
        "models": {"claude-opus-5": _entry("2026-07-24", cost=(99, 99)), "relay-only-model": _entry("2026-01-01", cost=(3, 4))},
    },
}


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        model_catalog_enabled=False,
        model_catalog_cache_path=tmp_path / "catalog.json",
        model_catalog_url="https://catalog.test/api.json",
        **overrides,
    )


# ── 裁剪与亮点 ────────────────────────────────────────────────────────────────


def test_reduce_filters_non_chat_deprecated_and_sorts_newest_first():
    snapshot = mc.reduce_catalog(RAW, "https://catalog.test/api.json")
    google = [m.id for m in snapshot.providers["google"]]
    assert google == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-flash-latest", "gemini-3.5-flash-lite"], (
        "图像生成 / 向量 / 已下线型号被过滤，其余按发布日期新在前"
    )
    assert mc.highlights_of(snapshot.providers["google"]) == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite"], (
        "亮点 = 最新三款正式版；-latest 别名不进亮点"
    )
    latest = next(m for m in snapshot.providers["google"] if m.id == "gemini-flash-latest")
    assert latest.alias is True


def test_google_card_only_lists_gemini_family():
    raw = json.loads(json.dumps(RAW))
    raw["google"]["models"]["gemma-4-31b-it"] = _entry("2026-09-03", family="gemma", inputs=("text", "image"))
    raw["google"]["models"]["lyria-3-pro-preview"] = _entry("2026-09-03", family="lyria", outputs=("text", "audio"))
    snapshot = mc.reduce_catalog(raw, "u")
    assert all(m.id.startswith("gemini") for m in snapshot.providers["google"]), "Gemma / Lyria 不进 Gemini 卡片"


def test_highlights_newest_stable_first_then_unstable():
    snapshot = mc.reduce_catalog(RAW, "u")
    assert mc.highlights_of(snapshot.providers["anthropic"]) == ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"]
    dated = next(m for m in snapshot.providers["anthropic"] if m.id == "claude-opus-4-5-20251101")
    assert dated.alias is True, "带日期的快照 ID 仍可手填但不进亮点"
    # deepseek：beta 的视觉实验版最新，但亮点里排最后
    assert mc.highlights_of(snapshot.providers["deepseek"]) == ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"]
    vision = next(m for m in snapshot.providers["deepseek"] if m.id == "deepseek-v4-flash-vision-exp")
    assert vision.status == "beta" and vision.vision is True
    assert next(m for m in snapshot.providers["deepseek"] if m.id == "deepseek-v4-pro").vision is False


def test_dashscope_only_keeps_qwen_family_and_drops_audio_only():
    snapshot = mc.reduce_catalog(RAW, "u")
    assert [m.id for m in snapshot.providers["qwen"]] == ["qwen3.8-flash", "qwen3.8-max"], (
        "DashScope 代售的 deepseek / glm 不算通义千问；纯音频输入的 ASR 不是对话模型"
    )


def test_zhipu_falls_back_to_alternate_catalog_key():
    snapshot = mc.reduce_catalog(RAW, "u")
    assert [m.id for m in snapshot.providers["zhipu"]] == ["glm-5.3"], "zhipuai 缺席时用 zai 键"
    assert "openai" not in snapshot.providers, "目录里没有的厂商不产生条目（视图层回落内置快照）"


def test_price_index_prefers_official_provider_and_fills_gaps_from_others():
    snapshot = mc.reduce_catalog(RAW, "u")
    assert snapshot.prices["claude-opus-5"] == (5.0, 25.0), "官方目录优先于中转平台的同名条目"
    assert snapshot.prices["relay-only-model"] == (3.0, 4.0), "只有别的平台有的型号照样收录"
    assert snapshot.prices["glm-5.2"] == (9.9, 9.9)
    assert "gemini-2.0-flash" in snapshot.prices, "单价索引不过滤已下线型号：历史用量仍要能估价"


def test_reduce_rejects_empty_or_foreign_catalog():
    with pytest.raises(ValueError):
        mc.reduce_catalog({}, "u")
    with pytest.raises(ValueError):
        mc.reduce_catalog({"unknown-provider": {"models": {"x": _entry("2026-01-01")}}}, "u")


# ── 服务：刷新、缓存、视图、定价 ─────────────────────────────────────────────


def test_refresh_writes_cache_and_reloads_on_next_start(tmp_path):
    settings = _settings(tmp_path)
    calls = []

    def fetcher(url, timeout):
        calls.append((url, timeout))
        return RAW

    catalog = mc.ModelCatalog(settings, fetcher=fetcher)
    assert catalog.view()["source"] == "builtin"
    assert catalog.refresh(force=True) is True
    assert calls == [("https://catalog.test/api.json", settings.model_catalog_timeout_seconds)]
    view = catalog.view()
    assert view["source"] == "catalog" and view["stale"] is False and view["error"] is None
    anthropic = next(p for p in view["providers"] if p["id"] == "anthropic")
    assert anthropic["source"] == "catalog"
    assert anthropic["highlights"] == ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"]
    assert anthropic["models"][0]["id"] == "claude-fable-5-1"
    openai = next(p for p in view["providers"] if p["id"] == "openai")
    assert openai["source"] == "builtin" and openai["highlights"] == ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"], (
        "目录里没有的厂商回落内置快照，并如实标 builtin"
    )
    ollama = next(p for p in view["providers"] if p["id"] == "ollama")
    assert ollama["source"] == "none" and ollama["subtitle"]

    cached = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    assert cached["version"] == mc.CACHE_VERSION and cached["synced_at"] == view["synced_at"]

    def failing(url, timeout):
        raise AssertionError("有缓存时启动不该出网")

    reloaded = mc.ModelCatalog(settings, fetcher=failing)
    again = reloaded.view()
    assert again["source"] == "catalog" and again["synced_at"] == view["synced_at"]
    assert reloaded.pricing_cny("claude-opus-5") == (35.0, 175.0)


def test_outdated_or_corrupt_cache_file_is_ignored(tmp_path):
    settings = _settings(tmp_path)
    path = tmp_path / "catalog.json"
    stale_format = mc.CatalogSnapshot("2026-09-01T00:00:00+00:00", "u", providers={"anthropic": [mc.CatalogModel(id="x")]}).to_json()
    stale_format["version"] = mc.CACHE_VERSION - 1
    path.write_text(json.dumps(stale_format), encoding="utf-8")
    assert mc.ModelCatalog(settings, fetcher=lambda u, t: RAW).view()["source"] == "builtin", "旧版本缓存视为无缓存，等待重新同步"
    path.write_text("{not json", encoding="utf-8")
    assert mc.ModelCatalog(settings, fetcher=lambda u, t: RAW).view()["source"] == "builtin"


def test_failed_refresh_keeps_previous_snapshot_and_reports_error(tmp_path):
    settings = _settings(tmp_path)
    state = {"fail": False}

    def fetcher(url, timeout):
        if state["fail"]:
            raise OSError("connection reset")
        return RAW

    catalog = mc.ModelCatalog(settings, fetcher=fetcher)
    catalog.refresh(force=True)
    state["fail"] = True
    catalog._last_attempt = 0.0  # 绕过强制刷新的最小间隔
    with pytest.raises(ApiError) as excinfo:
        catalog.refresh(force=True)
    assert excinfo.value.code == "MODEL_CATALOG_UNREACHABLE"
    view = catalog.view()
    assert view["source"] == "catalog" and "connection reset" in view["error"]
    assert view["providers"][1]["highlights"] == ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"]


def test_refresh_respects_ttl_and_force_interval(tmp_path):
    settings = _settings(tmp_path, model_catalog_ttl_seconds=3600)
    calls = []

    def fetcher(url, timeout):
        calls.append(1)
        return RAW

    catalog = mc.ModelCatalog(settings, fetcher=fetcher)
    assert catalog.refresh() is True, "从未同步过 = 过期，后台刷新要拉"
    assert catalog.refresh() is False, "TTL 内不重复拉"
    assert catalog.refresh(force=True) is False, "强制刷新也有最小间隔"
    catalog._last_attempt = 0.0
    assert catalog.refresh(force=True) is True
    assert len(calls) == 2
    # 把同步时间拨回 2 小时前 → 过期
    catalog._snapshot.synced_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    assert catalog.is_stale() is True
    assert catalog.refresh() is True


def test_pricing_and_vision_lookup(tmp_path):
    settings = _settings(tmp_path, usd_cny_rate=7.2)
    catalog = mc.ModelCatalog(settings, fetcher=lambda url, timeout: RAW)
    assert catalog.pricing_cny("claude-opus-5") is None, "未同步时不猜价"
    catalog.refresh(force=True)
    assert catalog.pricing_cny("Claude-Opus-5") == (36.0, 180.0), "大小写不敏感，美元 × 汇率"
    assert catalog.pricing_cny("claude-opus-5-20260724") is None, "只做精确命中，变体交给手写表"
    assert catalog.vision("gemini-3.8-flash") is True
    assert catalog.vision("deepseek-v4-pro") is False
    assert catalog.vision("not-in-catalog") is None


def test_usage_pricing_prefers_catalog_then_table(tmp_path):
    settings = _settings(tmp_path)
    catalog = mc.ModelCatalog(settings, fetcher=lambda url, timeout: RAW)
    previous = mc.current()
    try:
        mc.set_current(catalog)
        assert model_pricing("claude-opus-5") == (35.0, 175.0), "目录未同步：手写表"
        catalog.refresh(force=True)
        assert model_pricing("deepseek-v4-pro") == (round(0.435 * 7, 4), round(0.87 * 7, 4)), "同步后：目录单价 × 7"
        assert model_pricing("gpt-6-astra") == (70.0, 350.0), "目录未收录的仍走手写表"
        assert model_pricing("some-unknown") == (4.0, 12.0)
    finally:
        mc.set_current(previous)


# ── 接口 ──────────────────────────────────────────────────────────────────────


def test_catalog_endpoints_require_login(second_client):
    assert second_client.get("/api/llm/catalog").status_code == 401
    assert second_client.post("/api/llm/catalog/refresh").status_code == 401


def test_catalog_endpoint_serves_builtin_when_sync_disabled(client):
    response = client.get("/api/llm/catalog")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "builtin" and body["enabled"] is False and body["synced_at"] is None
    ids = [p["id"] for p in body["providers"]]
    assert ids == ["openai", "anthropic", "google", "deepseek", "qwen", "kimi", "zhipu", "xai", "ollama"]
    anthropic = next(p for p in body["providers"] if p["id"] == "anthropic")
    assert anthropic["highlights"] == ["claude-fable-5", "claude-opus-5", "claude-sonnet-5"]
    assert anthropic["protocol"] == "anthropic" and anthropic["base_url"] == "https://api.anthropic.com"
    refused = client.post("/api/llm/catalog/refresh")
    assert refused.status_code == 409 and refused.json()["code"] == "MODEL_CATALOG_DISABLED"


def test_refresh_endpoint_returns_fresh_view(client, app):
    app.state.settings.model_catalog_enabled = True
    catalog = app.state.model_catalog
    original_fetch = catalog._fetch
    catalog._fetch = lambda url, timeout: RAW
    try:
        response = client.post("/api/llm/catalog/refresh")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["source"] == "catalog"
        google = next(p for p in body["providers"] if p["id"] == "google")
        assert google["highlights"][0] == "gemini-3.8-flash"
        assert client.get("/api/llm/catalog").json()["synced_at"] == body["synced_at"]
    finally:
        catalog._fetch = original_fetch
        app.state.settings.model_catalog_enabled = False
