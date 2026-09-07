"""模型目录（ADR-0017）：厂商在售型号与单价不再写死在前端，由服务端定时同步。

数据源是公共模型目录 models.dev（``/api.json``，无需密钥，收录各厂商模型的发布
日期、模态、上下文与美元单价）。服务端按 ``model_catalog_ttl_seconds`` 定时拉取，
裁剪成本项目关心的 8 家厂商视图并落一份文件缓存（``data/model-catalog.json``），
进程重启先读缓存、再在后台刷新；目录不可达时保留上一份数据，从未成功过则回落
到本文件内置的快照——快照只是"离线也有东西可显示"的兜底，会过期是预期内的。

三个消费方：
- 设置中心「模型厂商」卡片副标题与「配置」一键填入的默认模型（``GET /api/llm/catalog``）；
- 「默认模型 ID」输入框的补全列表（同上，前端再与接口自报的清单合并）；
- 「用量监控」的费用估算：``pricing_cny`` 按模型 ID 精确命中目录单价（美元 ×
  汇率），命中不了才回落 ``usage.PRICING`` 的手写表。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from .config import Settings
from .errors import ApiError

logger = logging.getLogger("omm.model_catalog")

#: 缓存文件格式版本；字段或裁剪规则变化时递增，旧文件按无缓存处理（重新同步）。
CACHE_VERSION = 2
#: 两次强制刷新的最小间隔：目录 JSON 有 4–5 MB，别让「立即同步」按钮变成压测器。
FORCE_REFRESH_MIN_INTERVAL_S = 60.0
#: 后台线程检查是否过期的节拍。
_TICK_S = 60.0
#: 卡片副标题展示的型号数。
HIGHLIGHT_COUNT = 3


@dataclass(frozen=True)
class ProviderPreset:
    """一家厂商的品牌与接入事实（变化极慢），以及在公共目录里的对应关系。"""

    id: str
    label: str
    logo: str
    protocol: str
    base_url: str
    #: 公共目录里的 provider 键，按序取第一个存在的（国内外双入口时两个键型号一致）。
    catalog_ids: tuple[str, ...] = ()
    #: 只收这些前缀的模型（DashScope 这类托管平台还代售别家模型）。空 = 不过滤。
    id_prefixes: tuple[str, ...] = ()
    alt_hosts: tuple[str, ...] = ()
    #: 目录从未同步成功时的离线兜底（旗舰在前）。
    fallback_models: tuple[str, ...] = ()
    #: 副标题固定文案（本地模型这类没有目录概念的厂商）。
    subtitle: str = ""


#: 内置快照采集自各厂商官方文档（2026-09-05）；运行时以目录同步结果为准。
PROVIDERS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="openai", label="OpenAI", logo="openai", protocol="openai",
        base_url="https://api.openai.com", catalog_ids=("openai",),
        fallback_models=("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
    ),
    ProviderPreset(
        id="anthropic", label="Anthropic", logo="anthropic", protocol="anthropic",
        base_url="https://api.anthropic.com", catalog_ids=("anthropic",),
        fallback_models=("claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
    ),
    ProviderPreset(
        id="google", label="Google Gemini", logo="google", protocol="gemini",
        base_url="https://generativelanguage.googleapis.com", catalog_ids=("google",),
        # 同一 API 下还挂着 Gemma 开源权重、Lyria 音乐、Veo 视频、Deep Research 等
        id_prefixes=("gemini",),
        fallback_models=("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"),
    ),
    ProviderPreset(
        id="deepseek", label="DeepSeek", logo="deepseek", protocol="openai",
        base_url="https://api.deepseek.com", catalog_ids=("deepseek",),
        fallback_models=("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"),
    ),
    ProviderPreset(
        id="qwen", label="通义千问", logo="qwen", protocol="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", catalog_ids=("alibaba",),
        id_prefixes=("qwen", "qwq", "qvq"),
        fallback_models=("qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus"),
    ),
    ProviderPreset(
        id="kimi", label="Kimi", logo="kimi", protocol="openai",
        base_url="https://api.moonshot.cn/v1", catalog_ids=("moonshotai",),
        alt_hosts=("api.moonshot.ai",),
        fallback_models=("kimi-k3", "kimi-k2.7-code", "kimi-k2.6"),
    ),
    ProviderPreset(
        id="zhipu", label="智谱 GLM", logo="zhipu", protocol="openai",
        base_url="https://open.bigmodel.cn/api/paas/v4", catalog_ids=("zhipuai", "zai"),
        alt_hosts=("api.z.ai",),
        fallback_models=("glm-5.3", "glm-5.3-flash", "glm-5.2"),
    ),
    ProviderPreset(
        id="xai", label="xAI Grok", logo="xai", protocol="openai",
        base_url="https://api.x.ai/v1", catalog_ids=("xai",),
        fallback_models=("grok-4.6", "grok-4.5"),
    ),
    ProviderPreset(
        id="ollama", label="本地模型", logo="ollama", protocol="ollama",
        base_url="http://127.0.0.1:11434/v1", subtitle="Ollama · 本地已安装模型",
    ),
)


@dataclass(frozen=True)
class CatalogModel:
    """目录里一个可对话模型的裁剪视图。"""

    id: str
    name: str = ""
    release_date: str = ""
    reasoning: bool = False
    vision: bool = False
    context: int = 0
    #: "" 稳定 / "beta" 测试 / "preview" 预览
    status: str = ""
    input_usd: Optional[float] = None
    output_usd: Optional[float] = None
    #: 别名（*-latest）或带日期的快照 ID：仍可手填，不进卡片亮点。
    alias: bool = False


#: 不属于对话补全的型号：向量、语音、实时、图像/视频生成、审核、翻译专用等。
_NON_CHAT_ID = re.compile(
    r"(embed|tts|whisper|transcri|\basr\b|-asr|realtime|moderation|imagine|image|video|audio|"
    r"\bocr\b|-ocr|rerank|translate|-live|-mt-|character|guard|search)",
    re.IGNORECASE,
)
_ALIAS_ID = re.compile(r"(-latest$|chat-latest|-\d{8}$|-\d{4}-\d{2}-\d{2}$)", re.IGNORECASE)
_PREVIEW_ID = re.compile(r"(preview|-exp\b|-exp-|experimental)", re.IGNORECASE)


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_from_entry(model_id: str, entry: dict[str, Any]) -> Optional[CatalogModel]:
    """目录条目 → 裁剪视图；不是对话模型或已下线的返回 None。"""
    status = str(entry.get("status") or "").lower()
    if status in {"deprecated", "retired", "removed"}:
        return None
    if _NON_CHAT_ID.search(model_id):
        return None
    modalities = entry.get("modalities") if isinstance(entry.get("modalities"), dict) else {}
    inputs = [str(x).lower() for x in (modalities.get("input") or [])]
    outputs = [str(x).lower() for x in (modalities.get("output") or [])]
    if inputs and "text" not in inputs:
        return None
    if outputs and "text" not in outputs:
        return None
    cost = entry.get("cost") if isinstance(entry.get("cost"), dict) else {}
    limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
    if status not in {"", "beta", "preview"}:
        status = ""
    if not status and _PREVIEW_ID.search(model_id):
        status = "preview"
    return CatalogModel(
        id=model_id,
        name=str(entry.get("name") or model_id),
        release_date=str(entry.get("release_date") or ""),
        reasoning=bool(entry.get("reasoning")),
        vision="image" in inputs,
        context=int(_as_float(limit.get("context")) or 0),
        status=status,
        input_usd=_as_float(cost.get("input")),
        output_usd=_as_float(cost.get("output")),
        alias=bool(_ALIAS_ID.search(model_id)),
    )


def _sort_models(models: list[CatalogModel]) -> None:
    """新的在前；同日发布时正式 ID 排在别名前，其余按 ID 字典序（稳定排序保证）。"""
    models.sort(key=lambda m: m.id)
    models.sort(key=lambda m: (m.release_date, 0 if m.alias else 1), reverse=True)


def reduce_provider(preset: ProviderPreset, raw_models: dict[str, Any]) -> list[CatalogModel]:
    """一家厂商的原始目录 → 可对话型号（新在前）。"""
    models: list[CatalogModel] = []
    for model_id, entry in raw_models.items():
        if not isinstance(entry, dict):
            continue
        if preset.id_prefixes and not str(model_id).lower().startswith(preset.id_prefixes):
            continue
        model = _model_from_entry(str(model_id), entry)
        if model is not None:
            models.append(model)
    _sort_models(models)
    return models


def highlights_of(models: list[CatalogModel]) -> list[str]:
    """卡片副标题的型号：最新的几款正式版；测试版 / 预览版只在正式版不够时垫后，
    别名与带日期的快照不进亮点。

    曾试过「每个家族各取最新一款」以保证旗舰 / 主力 / 轻量档各占一席，但目录的
    family 标注并不一致（GLM-5.3-Flash 标 glm、GLM-4.7-Flash 标 glm-flash），真实
    数据上反而把三代前的轻量档顶进卡片；按时间取最新更可预期。读取时现算而不
    随缓存落盘，规则调整不必等下一次同步。
    """
    stable = [m.id for m in models if not m.alias and not m.status]
    unstable = [m.id for m in models if not m.alias and m.status]
    return (stable + unstable)[:HIGHLIGHT_COUNT]


#: 定价索引的 provider 优先级：先本项目的 8 家官方目录，再其余平台补缺。
_PRICE_PRIORITY = tuple(cid for preset in PROVIDERS for cid in preset.catalog_ids)


def build_price_index(raw: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """全目录的「模型 ID → (美元输入价, 美元输出价)」；官方目录优先，其余平台只补缺。"""
    prices: dict[str, tuple[float, float]] = {}
    ordered = [pid for pid in _PRICE_PRIORITY if pid in raw] + [pid for pid in raw if pid not in _PRICE_PRIORITY]
    for provider_id in ordered:
        provider = raw.get(provider_id)
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, dict):
            continue
        for model_id, entry in models.items():
            key = str(model_id).lower()
            if key in prices or not isinstance(entry, dict):
                continue
            cost = entry.get("cost") if isinstance(entry.get("cost"), dict) else {}
            input_usd, output_usd = _as_float(cost.get("input")), _as_float(cost.get("output"))
            if input_usd is None or output_usd is None:
                continue
            prices[key] = (input_usd, output_usd)
    return prices


@dataclass
class CatalogSnapshot:
    """一次同步（或缓存文件）的全部结果。"""

    synced_at: str
    catalog_url: str
    providers: dict[str, list[CatalogModel]] = field(default_factory=dict)
    prices: dict[str, tuple[float, float]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "synced_at": self.synced_at,
            "catalog_url": self.catalog_url,
            "providers": {pid: [asdict(m) for m in models] for pid, models in self.providers.items()},
            "prices": {k: list(v) for k, v in self.prices.items()},
        }

    @classmethod
    def from_json(cls, data: Any) -> Optional["CatalogSnapshot"]:
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return None
        try:
            providers = {
                str(pid): [CatalogModel(**m) for m in models]
                for pid, models in (data.get("providers") or {}).items()
            }
            prices = {str(k): (float(v[0]), float(v[1])) for k, v in (data.get("prices") or {}).items()}
            return cls(
                synced_at=str(data.get("synced_at") or ""),
                catalog_url=str(data.get("catalog_url") or ""),
                providers=providers,
                prices=prices,
            )
        except (TypeError, ValueError, KeyError):
            return None


def reduce_catalog(raw: dict[str, Any], catalog_url: str, now: Optional[datetime] = None) -> CatalogSnapshot:
    """原始目录 JSON → 本项目的裁剪快照。"""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("目录内容为空或不是 JSON 对象")
    snapshot = CatalogSnapshot(
        synced_at=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        catalog_url=catalog_url,
    )
    for preset in PROVIDERS:
        source = next((raw[cid] for cid in preset.catalog_ids if isinstance(raw.get(cid), dict)), None)
        if source is None:
            continue
        raw_models = source.get("models")
        if not isinstance(raw_models, dict):
            continue
        models = reduce_provider(preset, raw_models)
        if models:
            snapshot.providers[preset.id] = models
    if not snapshot.providers:
        raise ValueError("目录里没有本项目收录的任何厂商")
    snapshot.prices = build_price_index(raw)
    return snapshot


Fetcher = Callable[[str, float], dict[str, Any]]


def _http_fetch(url: str, timeout: float) -> dict[str, Any]:
    # 目录站在境外：走系统代理（trust_env 默认）即可，与访问官方厂商一致。
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers={"accept": "application/json"})
    response.raise_for_status()
    return response.json()


class ModelCatalog:
    """进程内的目录服务：缓存文件 + 后台定时刷新 + 只读视图 / 定价查询。"""

    def __init__(self, settings: Settings, fetcher: Optional[Fetcher] = None) -> None:
        self._settings = settings
        self._fetch: Fetcher = fetcher or _http_fetch
        # 可重入：refresh 持锁期间要调 is_stale → synced_at
        self._lock = threading.RLock()
        self._snapshot: Optional[CatalogSnapshot] = None
        self._last_error: str = ""
        self._last_attempt: float = 0.0
        self._refreshing = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load_cache()

    # ── 缓存文件 ────────────────────────────────────────────────────────

    def _cache_path(self) -> Path:
        return Path(self._settings.model_catalog_cache_path)

    def _load_cache(self) -> None:
        path = self._cache_path()
        try:
            if not path.exists():
                return
            snapshot = CatalogSnapshot.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            logger.warning("model catalog: 缓存文件不可读，忽略（%s）", error)
            return
        if snapshot is not None and snapshot.providers:
            self._snapshot = snapshot

    def _save_cache(self, snapshot: CatalogSnapshot) -> None:
        path = self._cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(snapshot.to_json(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as error:
            logger.warning("model catalog: 缓存文件写入失败（%s）", error)

    # ── 刷新 ────────────────────────────────────────────────────────────

    def synced_at(self) -> Optional[datetime]:
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None or not snapshot.synced_at:
            return None
        try:
            return datetime.fromisoformat(snapshot.synced_at)
        except ValueError:
            return None

    def is_stale(self) -> bool:
        synced = self.synced_at()
        if synced is None:
            return True
        age = (datetime.now(timezone.utc) - synced).total_seconds()
        return age >= self._settings.model_catalog_ttl_seconds

    def refresh(self, *, force: bool = False) -> bool:
        """拉一次目录。返回是否真的拉取了；失败抛 ApiError（旧数据保留）。

        force=False 时只在过期才拉（后台线程用）；force=True 是「立即同步」，
        但两次强制刷新至少间隔 FORCE_REFRESH_MIN_INTERVAL_S。
        """
        with self._lock:
            if self._refreshing:
                return False
            if not force and not self.is_stale():
                return False
            if force and time.monotonic() - self._last_attempt < FORCE_REFRESH_MIN_INTERVAL_S and self._snapshot is not None:
                return False
            self._refreshing = True
            self._last_attempt = time.monotonic()
        url = self._settings.model_catalog_url
        try:
            raw = self._fetch(url, self._settings.model_catalog_timeout_seconds)
            snapshot = reduce_catalog(raw, url)
        except Exception as error:  # noqa: BLE001 - 网络/解析失败都只记录，旧数据照用
            message = f"模型目录同步失败（{url}）：{error}"
            logger.warning("model catalog: %s", message)
            with self._lock:
                self._last_error = message
                self._refreshing = False
            raise ApiError(502, "MODEL_CATALOG_UNREACHABLE", message) from error
        with self._lock:
            self._snapshot = snapshot
            self._last_error = ""
            self._refreshing = False
        self._save_cache(snapshot)
        logger.info(
            "model catalog: 同步完成，%d 家厂商 / %d 条单价",
            len(snapshot.providers),
            len(snapshot.prices),
        )
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="omm-model-catalog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh(force=False)
            except ApiError:
                pass  # 已记录 last_error；下一拍再试
            except Exception:  # noqa: BLE001 - 后台线程不允许被任何异常杀死
                logger.exception("model catalog: 后台刷新异常")
            # 失败后按节拍重试；成功后节拍检查也很便宜（只比时间）
            self._stop.wait(_TICK_S)

    # ── 只读视图 ────────────────────────────────────────────────────────

    def view(self) -> dict[str, Any]:
        """``GET /api/llm/catalog`` 的响应体。"""
        with self._lock:
            snapshot = self._snapshot
            error = self._last_error
            refreshing = self._refreshing
        providers = []
        for preset in PROVIDERS:
            models = snapshot.providers.get(preset.id, []) if snapshot else []
            if models:
                highlights = highlights_of(models)
                source = "catalog"
            else:
                highlights = list(preset.fallback_models[:HIGHLIGHT_COUNT])
                models = [CatalogModel(id=m) for m in preset.fallback_models]
                source = "builtin"
            providers.append(
                {
                    "id": preset.id,
                    "label": preset.label,
                    "logo": preset.logo,
                    "protocol": preset.protocol,
                    "base_url": preset.base_url,
                    "alt_hosts": list(preset.alt_hosts),
                    "subtitle": preset.subtitle,
                    "source": source if preset.catalog_ids else "none",
                    "highlights": highlights,
                    "models": [asdict(m) for m in models],
                }
            )
        return {
            "source": "catalog" if snapshot else "builtin",
            "enabled": bool(self._settings.model_catalog_enabled),
            "catalog_url": self._settings.model_catalog_url,
            "synced_at": snapshot.synced_at if snapshot else None,
            "stale": self.is_stale(),
            "refreshing": refreshing,
            "error": error or None,
            "providers": providers,
        }

    def pricing_cny(self, model: str) -> Optional[tuple[float, float]]:
        """模型 ID 精确命中目录单价 → (输入, 输出) 元 / 百万 token；未收录返回 None。"""
        key = (model or "").strip().lower()
        if not key:
            return None
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return None
        priced = snapshot.prices.get(key)
        if priced is None:
            return None
        rate = float(self._settings.usd_cny_rate)
        return round(priced[0] * rate, 4), round(priced[1] * rate, 4)

    def vision(self, model: str) -> Optional[bool]:
        """目录是否明确知道该模型收图；未收录返回 None（交给命名规则判断）。"""
        key = (model or "").strip().lower()
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None or not key:
            return None
        for models in snapshot.providers.values():
            for item in models:
                if item.id.lower() == key:
                    return item.vision
        return None


# ── 进程级访问点：usage.model_pricing 等无 app 上下文的调用方使用 ──────────

_current: Optional[ModelCatalog] = None


def set_current(catalog: Optional[ModelCatalog]) -> None:
    global _current
    _current = catalog


def current() -> Optional[ModelCatalog]:
    return _current
