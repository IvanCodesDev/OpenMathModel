import { createRoot } from "react-dom/client";
import App from "./App";
import { initInterfaceLocale } from "./i18n/locale";
import { preloadKnowledgeLibrary } from "./legacy/openmathmodel-ui";
import { initDisplayPreferences } from "./preferences/display-preferences";
import { restoreLastTaskOnStartup } from "./tasks/restore-last-task";
// 图标随包发布而非走 CDN：桌面端离线与内网部署都要能正常显示。
// 代码里同时使用了 regular 与 fill 两种字重，两份样式都必须引入。
import "@phosphor-icons/web/regular";
import "@phosphor-icons/web/fill";
import "./styles.css";
import "./workflow-refresh.css";
import "./attachments/attachments.css";
// 放在最后：可读性覆盖需要在同等特异性下压过上面两张基线样式表
import "./accessibility.css";

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

// 必须早于首屏渲染：翻译器要在 React 插入节点前接管，否则会先闪一帧中文。
initInterfaceLocale();
initDisplayPreferences();

// 「启动时恢复上次任务」：本会话首次加载落在首页且存在有效记录时直接去工作台，
// 已发起跳转就不再渲染首页，避免闪一帧又被替换。
if (!restoreLastTaskOnStartup()) {
  createRoot(rootElement).render(<App />);
}
