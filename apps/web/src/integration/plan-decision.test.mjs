import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./plan-decision.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  describePlanDecision,
  focusedPlan,
  languageLabel,
  planProposalStamp,
  recommendedPlan,
  LANGUAGE_LABELS,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

/** 契约 fixture plan-proposal.2：三案 A/B/C（推荐 A），台账 adopt:B 带备注，三卡 language=python。 */
const fixture = JSON.parse(
  await readFile(
    new URL("../../../../packages/contracts/fixtures/v1/valid/plan-proposal.2.json", import.meta.url),
    "utf8",
  ),
);
/** 契约 fixture plan-proposal.1：切片 6 之前的形状——没有 decision / language 键。 */
const legacy = JSON.parse(
  await readFile(
    new URL("../../../../packages/contracts/fixtures/v1/valid/plan-proposal.1.json", import.meta.url),
    "utf8",
  ),
);

test("decision: fixture ledger adopt:B → chosen B, not as recommended, comment kept verbatim", () => {
  assert.deepEqual(describePlanDecision(fixture), {
    chosenPlanId: "B",
    asRecommended: false,
    optionId: "adopt:B",
    actor: "user",
    comment: "先跑基线，A 的求解时间等数据补齐再说",
    resolvedAt: "2026-09-05T12:45:10.000000Z",
  });
  assert.equal(focusedPlan(fixture).id, "B");
  assert.equal(recommendedPlan(fixture).id, "A", "推荐案不因用户改选而改写");
});

test("decision: legacy proposal (no key) / explicit null / no plans → null, focus falls back to the recommended plan", () => {
  assert.equal(describePlanDecision(legacy), null);
  assert.equal(focusedPlan(legacy).id, legacy.recommended_plan_id);
  assert.equal(describePlanDecision({ ...fixture, decision: null }), null);
  assert.equal(focusedPlan({ ...fixture, decision: null }).id, "A");
  assert.equal(describePlanDecision({ plans: [], recommended_plan_id: "A", decision: fixture.decision }), null);
});

test("decision: approve → chosen = recommended, blank comment → null", () => {
  const view = describePlanDecision({
    ...fixture,
    decision: { ...fixture.decision, option_id: "approve", chosen_plan_id: "A", comment: "   " },
  });
  assert.equal(view.chosenPlanId, "A");
  assert.equal(view.asRecommended, true);
  assert.equal(view.comment, null);
  // adopt 的正好是推荐案：也算「按推荐」
  const adoptedRecommended = describePlanDecision({
    ...fixture,
    decision: { ...fixture.decision, option_id: "adopt:A", chosen_plan_id: "A", comment: null },
  });
  assert.equal(adoptedRecommended.asRecommended, true);
});

test("decision: dangling chosen_plan_id is re-derived locally (adopt target → recommended → first)", () => {
  const base = { ...fixture.decision, chosen_plan_id: "Z" };
  assert.equal(describePlanDecision({ ...fixture, decision: { ...base, option_id: "adopt:C" } }).chosenPlanId, "C");
  assert.equal(describePlanDecision({ ...fixture, decision: { ...base, option_id: "adopt:Z" } }).chosenPlanId, "A");
  assert.equal(describePlanDecision({ ...fixture, decision: { ...base, option_id: "approve" } }).chosenPlanId, "A");
  // 推荐 id 也悬空：退到首个方案，绝不落空
  const orphan = describePlanDecision({
    plans: fixture.plans, recommended_plan_id: "Q", decision: { ...base, option_id: "approve" },
  });
  assert.equal(orphan.chosenPlanId, "A");
  assert.equal(orphan.asRecommended, false);
});

test("stamp: the panel re-renders when the ledger lands even though updated_at is unchanged", () => {
  const before = planProposalStamp({ ...fixture, decision: null });
  const after = planProposalStamp(fixture);
  assert.equal(before, `${fixture.updated_at}|`);
  assert.equal(after, `${fixture.updated_at}|${fixture.decision.resolved_at}`);
  assert.notEqual(before, after);
  // 旧契约形状（没有 decision 键）与 decision:null 同签名
  assert.equal(planProposalStamp({ updated_at: fixture.updated_at }), before);
});

test("language: known codes map to display names, unknown codes are shown verbatim, missing → null", () => {
  assert.deepEqual(fixture.plans.map(plan => languageLabel(plan.language)), ["Python", "Python", "Python"]);
  assert.equal(languageLabel("R"), "R");
  assert.equal(languageLabel(" matlab "), "MATLAB");
  assert.equal(languageLabel("baltamatica"), LANGUAGE_LABELS.baltamatica);
  assert.equal(languageLabel("fortran"), "fortran");
  assert.equal(languageLabel(null), null);
  assert.equal(languageLabel(undefined), null);
  assert.equal(languageLabel("   "), null);
  assert.deepEqual(legacy.plans.map(plan => languageLabel(plan.language)), legacy.plans.map(() => null));
});
