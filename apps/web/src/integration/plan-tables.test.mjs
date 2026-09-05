import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./plan-tables.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { describeAssumptions, describeSymbols, IMPACT_LABELS, STATUS_LABELS, KIND_LABELS } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

/** 契约 fixture plan-proposal.2：三案 A/B/C，6 条假设（3 全局 + 各案 1 条），8 个符号（4 共享）。 */
const fixture = JSON.parse(
  await readFile(
    new URL("../../../../packages/contracts/fixtures/v1/valid/plan-proposal.2.json", import.meta.url),
    "utf8",
  ),
);

test("assumptions: fixture rows keep contract order (global first, then A/B/C), counts and focus", () => {
  const section = describeAssumptions(fixture);
  assert.equal(section.kind, "table");
  assert.deepEqual(
    section.rows.map(row => [row.id, row.planId]),
    [["G1", null], ["G2", null], ["G3", null], ["A1", "A"], ["B1", "B"], ["C1", "C"]],
  );
  assert.equal(section.globalCount, 3);
  assert.equal(section.planCount, 3);
  // 验证重点：critical 在前（A1、C1）、to_verify 在后（G3、B1），已确认的 G1、G2 不进
  assert.deepEqual(section.focus, ["A1", "C1", "G3", "B1"]);
  // 行只带契约字段与归属，不带页面文案（文案由调用方 t() 拼）
  assert.deepEqual(Object.keys(section.rows[0]).sort(), ["basis", "id", "impact", "planId", "status", "text"]);
});

test("assumptions: out-of-order input is regrouped (global → plan order → unknown scope last), stable within group", () => {
  const section = describeAssumptions({
    plans: [{ id: "A" }, { id: "B" }],
    assumptions: [
      { id: "B1", text: "b1", scope: "B", basis: "", impact: "low", status: "confirmed" },
      { id: "Z1", text: "z1", scope: "Z", basis: "", impact: "low", status: "to_verify" },
      { id: "A1", text: "a1", scope: "A", basis: "", impact: "high", status: "critical" },
      { id: "G1", text: "g1", scope: "global", basis: "题面", impact: "medium", status: "to_verify" },
      { id: "A2", text: "a2", scope: "A", basis: "", impact: "low", status: "critical" },
      { id: "G2", text: "g2", scope: "global", basis: "", impact: "low", status: "confirmed" },
    ],
  });
  assert.deepEqual(section.rows.map(row => row.id), ["G1", "G2", "A1", "A2", "B1", "Z1"]);
  assert.equal(section.globalCount, 2);
  assert.equal(section.planCount, 4);
  // 未知归属 Z 保留原 scope 作方案 id（调用方照样显示「方案 Z」，不假装是全局）
  assert.equal(section.rows[5].planId, "Z");
  assert.deepEqual(section.focus, ["A1", "A2", "G1", "Z1"]);
});

test("assumptions: absent / null / empty → absent (tab stays hidden)", () => {
  assert.deepEqual(describeAssumptions({ plans: [], assumptions: null }), { kind: "absent" });
  assert.deepEqual(describeAssumptions({ plans: [] }), { kind: "absent" });
  assert.deepEqual(describeAssumptions({ plans: [{ id: "A" }], assumptions: [] }), { kind: "absent" });
});

test("assumptions: all confirmed → empty focus", () => {
  const section = describeAssumptions({
    plans: [{ id: "A" }],
    assumptions: [{ id: "G1", text: "g1", scope: "global", basis: "题面", impact: "low", status: "confirmed" }],
  });
  assert.deepEqual(section.focus, []);
});

test("symbols: fixture rows shared first then per plan; unit/range/plan_id pass through verbatim", () => {
  const section = describeSymbols(fixture);
  assert.equal(section.kind, "table");
  assert.equal(section.rows.length, fixture.symbols.length);
  assert.equal(section.sharedCount, 4);
  assert.equal(section.planCount, 4);
  const planIds = section.rows.map(row => row.planId);
  assert.deepEqual(planIds.slice(0, 4), [null, null, null, null]);
  assert.deepEqual(planIds.slice(4), ["A", "A", "B", "C"]);
  // LaTeX 原样（不带 $），单位 / 范围 null 原样透出，由调用方渲染「—」
  const first = section.rows[0];
  assert.equal(first.symbol, fixture.symbols[0].symbol);
  assert.ok(!first.symbol.includes("$"));
  assert.deepEqual(Object.keys(first).sort(), ["definition", "kind", "planId", "range", "symbol", "unit"]);
});

test("symbols: regrouped by plan order, unknown plan last; absent / empty → absent", () => {
  const section = describeSymbols({
    plans: [{ id: "A" }, { id: "B" }],
    symbols: [
      { symbol: "y", kind: "variable", definition: "y", unit: null, range: null, plan_id: "B" },
      { symbol: "q", kind: "other", definition: "q", unit: "kg", range: null, plan_id: "Q" },
      { symbol: "x", kind: "variable", definition: "x", unit: null, range: "{0,1}", plan_id: "A" },
      { symbol: "\\mathcal{I}", kind: "set", definition: "I", unit: null, range: null, plan_id: null },
    ],
  });
  assert.deepEqual(section.rows.map(row => row.symbol), ["\\mathcal{I}", "x", "y", "q"]);
  assert.equal(section.sharedCount, 1);
  assert.equal(section.planCount, 3);
  assert.deepEqual(describeSymbols({ plans: [], symbols: null }), { kind: "absent" });
  assert.deepEqual(describeSymbols({ plans: [], symbols: [] }), { kind: "absent" });
});

test("labels cover every contract enum value", () => {
  assert.deepEqual(Object.keys(IMPACT_LABELS).sort(), ["high", "low", "medium"]);
  assert.deepEqual(Object.keys(STATUS_LABELS).sort(), ["confirmed", "critical", "to_verify"]);
  assert.deepEqual(Object.keys(KIND_LABELS).sort(), ["objective", "other", "parameter", "set", "variable"]);
});
