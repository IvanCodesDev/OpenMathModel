import { createRoot } from "react-dom/client";
import App from "./App";
import { preloadKnowledgeLibrary } from "./legacy/openmathmodel-ui";
// 图标随包发布而非走 CDN：桌面端离线与内网部署都要能正常显示。
// 代码里同时使用了 regular 与 fill 两种字重，两份样式都必须引入。
import "@phosphor-icons/web/regular";
import "@phosphor-icons/web/fill";
import "./styles.css";
import "./workflow-refresh.css";

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("Missing #root application mount point.");

/** 只有这些路由会读取赛题/论文数据，其余页面不必为 2MB 的知识库买单。 */
const KNOWLEDGE_ROUTES = new Set([
  "/library/problems",
  "/library/problems/detail",
  "/library/papers",
  "/library/papers/detail",
  "/library/methods",
]);

const path = window.location.pathname.replace(/(.)\/$/, "$1");
if (KNOWLEDGE_ROUTES.has(path)) {
  // 渲染是同步的，数据必须先到位，否则列表会闪空。
  await preloadKnowledgeLibrary();
}

createRoot(rootElement).render(<App />);
