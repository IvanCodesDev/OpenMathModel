/**
 * 设置中心「智能路由」四个任务类型下拉 ↔ llm-config 契约 `task_routes` 的纯映射（ADR-0015）。
 *
 * 表单侧的值与输入框模型选择器同构：`auto` 或 `endpoint-<id>`；契约侧是接口 id 或 null。
 * 这里不碰 DOM、不发请求，llm-settings（保存）、model-modality（携图钉接口）与设置
 * 面板（回填）三处共用同一套换算，避免各写一份前缀拼接。
 */

import type { TaskRouteKind, TaskRoutes } from "../auth/api";

export type { TaskRouteKind, TaskRoutes };

/** 四个下拉的 name / 契约键 / 面板标签，顺序即面板顺序。 */
export const ROUTING_FIELDS: ReadonlyArray<readonly [name: string, kind: TaskRouteKind, label: string]> = [
  ["codingModel", "coding", "编程与 Agent"],
  ["researchModel", "research", "深度研究"],
  ["writingModel", "writing", "长文写作"],
  ["visionModel", "vision", "视觉理解"],
];

export const TASK_ROUTE_KINDS: readonly TaskRouteKind[] = ROUTING_FIELDS.map(([, kind]) => kind);

const ENDPOINT_PREFIX = "endpoint-";

/** 面板下拉值 → 接口 id；`auto`、空值与历史遗留的静态模型名都算自动。 */
export function endpointIdFromRoutingValue(value: unknown): string | null {
  if (typeof value !== "string" || !value.startsWith(ENDPOINT_PREFIX)) return null;
  const id = value.slice(ENDPOINT_PREFIX.length);
  return id ? id : null;
}

/** 接口 id → 面板下拉值。 */
export function routingValueFromEndpointId(id: string | null | undefined): string {
  return id ? `${ENDPOINT_PREFIX}${id}` : "auto";
}

/** 整张设置表（collectSettingsValues 的结果）→ 契约 `task_routes`，四键齐全。 */
export function taskRoutesFromForm(values: Record<string, unknown>): TaskRoutes {
  const routes = {} as TaskRoutes;
  for (const [name, kind] of ROUTING_FIELDS) routes[kind] = endpointIdFromRoutingValue(values[name]);
  return routes;
}

/** 服务端返回的（可能缺键的）task_routes → 四键齐全的规范形。 */
export function normalizeTaskRoutes(raw: Partial<Record<string, unknown>> | null | undefined): TaskRoutes {
  const routes = {} as TaskRoutes;
  for (const kind of TASK_ROUTE_KINDS) {
    const value = raw?.[kind];
    routes[kind] = typeof value === "string" && value ? value : null;
  }
  return routes;
}

interface RoutableConfig {
  endpoints: ReadonlyArray<{ id?: string | null }>;
  smart_routing?: boolean;
  task_routes?: Partial<Record<string, unknown>> | null;
}

/**
 * 某任务类型当前生效的定向接口 id：总开关关闭、未定向、或接口已不在池子里都返回 null。
 * 与服务端 `LlmConfig.route_for` 同一判定，前端据此决定携图时钉住哪条接口。
 */
export function resolveTaskRoute<T extends RoutableConfig>(config: T | null | undefined, kind: TaskRouteKind): string | null {
  if (!config || config.smart_routing === false) return null;
  const id = normalizeTaskRoutes(config.task_routes)[kind];
  if (!id) return null;
  return config.endpoints.some(endpoint => endpoint.id === id) ? id : null;
}
