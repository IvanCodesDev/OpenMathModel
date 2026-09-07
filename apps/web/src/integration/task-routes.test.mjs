import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./task-routes.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const {
  ROUTING_FIELDS,
  TASK_ROUTE_KINDS,
  endpointIdFromRoutingValue,
  routingValueFromEndpointId,
  taskRoutesFromForm,
  normalizeTaskRoutes,
  resolveTaskRoute,
} = await import(`data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`);

test("四个任务类型与面板 name 一一对应，顺序即面板顺序", () => {
  assert.deepEqual(TASK_ROUTE_KINDS, ["coding", "research", "writing", "vision"]);
  assert.deepEqual(
    ROUTING_FIELDS.map(([name]) => name),
    ["codingModel", "researchModel", "writingModel", "visionModel"],
  );
});

test("下拉值 ↔ 接口 id：auto / 空 / 历史静态模型名都算自动", () => {
  assert.equal(endpointIdFromRoutingValue("endpoint-ep_abc"), "ep_abc");
  assert.equal(endpointIdFromRoutingValue("auto"), null);
  assert.equal(endpointIdFromRoutingValue(""), null);
  assert.equal(endpointIdFromRoutingValue("GPT-5.6 Sol"), null);
  assert.equal(endpointIdFromRoutingValue("endpoint-"), null);
  assert.equal(endpointIdFromRoutingValue(undefined), null);
  assert.equal(routingValueFromEndpointId("ep_abc"), "endpoint-ep_abc");
  assert.equal(routingValueFromEndpointId(null), "auto");
  assert.equal(routingValueFromEndpointId(undefined), "auto");
});

test("整张设置表 → task_routes：四键齐全，缺项为 null", () => {
  assert.deepEqual(
    taskRoutesFromForm({ codingModel: "endpoint-ep_c", visionModel: "auto", theme: "dark" }),
    { coding: "ep_c", research: null, writing: null, vision: null },
  );
  assert.deepEqual(taskRoutesFromForm({}), { coding: null, research: null, writing: null, vision: null });
});

test("服务端 task_routes 规范化：缺键、非字符串、空串都归 null，未知键忽略", () => {
  assert.deepEqual(normalizeTaskRoutes({ coding: "ep_c", writing: "", vision: 42, poetry: "ep_x" }), {
    coding: "ep_c",
    research: null,
    writing: null,
    vision: null,
  });
  assert.deepEqual(normalizeTaskRoutes(undefined), { coding: null, research: null, writing: null, vision: null });
});

test("resolveTaskRoute：总开关、定向存在性、接口是否仍在池子里三重判定", () => {
  const config = {
    endpoints: [{ id: "ep_main" }, { id: "ep_vision" }],
    smart_routing: true,
    task_routes: { vision: "ep_vision", coding: "ep_gone" },
  };
  assert.equal(resolveTaskRoute(config, "vision"), "ep_vision");
  assert.equal(resolveTaskRoute(config, "coding"), null, "指向已删除接口的定向不生效");
  assert.equal(resolveTaskRoute(config, "writing"), null);
  assert.equal(resolveTaskRoute({ ...config, smart_routing: false }, "vision"), null, "总开关关闭");
  assert.equal(resolveTaskRoute({ endpoints: config.endpoints }, "vision"), null, "旧后端无字段");
  assert.equal(resolveTaskRoute(null, "vision"), null);
});
