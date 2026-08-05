# ADR-0003：Web 路由库与服务端数据层选型

- 状态：Accepted
- 日期：2026-08-05

## 背景

`apps/web` 此前用手写 `Map<path, Component>` 做整页路由，页面数据全部来自打包进前端的本地模拟数据。T3 交付了真实控制面 API（REST + SSE，契约 `schemas/v1`），Web 需要真实路由与服务端状态管理，且必须与 155KB 遗留字符串 UI（14 个原型页面）长期共存、不回退既有页面。

## 决策

### 路由：React Router v8（library 模式）

- `BrowserRouter + Routes + Route` 平移现有 14 条路由，遗留页面组件原样挂载；新真实页面获得 `useSearchParams / useNavigate / Link` 等能力。
- 不选 TanStack Router：其类型安全路由树与代码生成在深层嵌套/复杂参数场景收益最大，当前 14 条扁平路由 + 遗留整页跳转（`<a href>` 全刷新）用不到；引入成本（路由树定义、构建集成）高于收益。
- 复评条件：Phase 5+ 工作区页面出现深层嵌套路由与复杂参数契约时，重评 TanStack Router。
- 遗留共存策略：遗留页面继续用全页跳转（router 在新加载时照常匹配），新页面间用客户端导航；两者互不干扰。

### 服务端数据层：TanStack Query v5 + 手写契约客户端

- 查询/失效/重试/缓存由 TanStack Query 承担；API 客户端为薄 fetch 封装（`src/api/http.ts`），沿用账户模块的错误约定（统一错误信封 `{code, message, request_id}` → `ApiError`），并支持 `Idempotency-Key` 请求头。
- 类型直接消费 `@openmathmodel/contracts` 生成的 TS 类型（`src/ts/v1`），不手写第二份接口类型；契约变更经 codegen 传导，CI 的 contracts-ts 作业防生成物过期。
- 暂不引入 OpenAPI 生成式客户端：控制面端点尚少且稳定性高，等 OpenAPI 基线随 API 属主落地后再评估替换（届时本薄客户端可整体退役）。

### 实时事件：原生 EventSource

- SSE 端点 `GET /api/v1/task-runs/{id}/events` 由浏览器原生 `EventSource` 消费：自动重连并自动携带 `Last-Event-ID`，与后端断线补拉契约（事件 id = sequence）天然咬合，不引入第三方 SSE 库。
- 终态由服务端 `stream.end` 事件显式收尾，前端据此关闭连接。

## 结果

优点：新页面具备真实路由与服务端状态；类型与契约单一真源；SSE 断线恢复零代码。代价：遗留页面与新页面并存期存在两种导航习惯（全刷新 vs 客户端导航），待逐页真实化后收敛。

## 后续约束

- 新真实页面一律走 `src/api/` 数据层与契约类型，禁止再造第二套 fetch 封装（账户模块 `src/auth/api.ts` 为既有先例，合并时机由账户属主定）。
- 遗留页面真实化时同步把该路由的入口链接换成 `<Link>`。
