import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./model-catalog-view.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  catalogFreshnessText,
  catalogVision,
  providerHighlights,
  providerModels,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

const model = (id, vision = true) => ({
  id, name: id, release_date: "2026-09-01", reasoning: true, vision, context: 1, status: "", input_usd: 1, output_usd: 2, alias: false,
});

const view = {
  source: "catalog",
  enabled: true,
  catalog_url: "https://models.dev/api.json",
  synced_at: "2026-09-05T12:00:00+00:00",
  stale: false,
  refreshing: false,
  error: null,
  providers: [
    {
      id: "anthropic", label: "Anthropic", logo: "anthropic", protocol: "anthropic", base_url: "https://api.anthropic.com",
      alt_hosts: [], subtitle: "", source: "catalog",
      highlights: ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"],
      models: [model("claude-fable-5-1"), model("claude-opus-5"), model("claude-sonnet-5"), model("claude-haiku-4-5")],
    },
    {
      id: "deepseek", label: "DeepSeek", logo: "deepseek", protocol: "openai", base_url: "https://api.deepseek.com",
      alt_hosts: [], subtitle: "", source: "catalog",
      highlights: ["deepseek-v4-pro"],
      models: [model("deepseek-v4-pro", false)],
    },
    {
      id: "openai", label: "OpenAI", logo: "openai", protocol: "openai", base_url: "https://api.openai.com",
      alt_hosts: [], subtitle: "", source: "builtin",
      highlights: ["gpt-6-astra"],
      models: [model("gpt-6-astra")],
    },
  ],
};

test("按厂商取亮点与全部型号；目录没有的厂商为空", () => {
  assert.deepEqual(providerHighlights(view, "anthropic"), ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"]);
  assert.deepEqual(providerModels(view, "anthropic"), ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]);
  assert.deepEqual(providerModels(view, "kimi"), []);
  assert.deepEqual(providerHighlights(null, "anthropic"), []);
});

test("视觉能力只信目录同步来的条目，内置快照与未收录的都交给命名规则", () => {
  assert.equal(catalogVision(view, "Claude-Opus-5"), true);
  assert.equal(catalogVision(view, "deepseek-v4-pro"), false);
  assert.equal(catalogVision(view, "gpt-6-astra"), undefined, "builtin 快照没有模态信息");
  assert.equal(catalogVision(view, "not-listed"), undefined);
  assert.equal(catalogVision(null, "claude-opus-5"), undefined);
});

test("新鲜度文案：同步时间、来源域名、过期与失败状态", () => {
  const now = Date.parse("2026-09-05T12:03:00+00:00");
  assert.equal(catalogFreshnessText(view, now), "型号目录同步于 3 分钟前，来源 models.dev。");
  assert.equal(
    catalogFreshnessText({ ...view, stale: true }, now + 7 * 3600_000),
    "型号目录同步于 7 小时前，来源 models.dev；已到刷新时间，后台正在重试。",
  );
  assert.equal(
    catalogFreshnessText({ ...view, error: "模型目录同步失败（…）：timeout" }, now + 3 * 86400_000),
    "型号目录同步于 3 天前，来源 models.dev；上次刷新失败，沿用上一份结果。",
  );
  assert.equal(catalogFreshnessText({ ...view, synced_at: "2026-09-05T12:02:50+00:00" }, now), "型号目录刚刚同步，来源 models.dev。");
});

test("未同步 / 关闭 / 不可用时如实说明是内置快照", () => {
  assert.equal(catalogFreshnessText(null), "模型目录暂不可用，型号为内置快照，可能已过期。");
  const builtin = { ...view, source: "builtin", synced_at: null };
  assert.equal(catalogFreshnessText(builtin), "模型目录尚未同步，型号为内置快照；后台正在拉取。");
  assert.equal(catalogFreshnessText({ ...builtin, enabled: false }), "服务端已关闭模型目录同步，型号为内置快照。");
  assert.match(catalogFreshnessText({ ...builtin, error: "x 不可达" }), /尚未同步成功（x 不可达）/);
});
