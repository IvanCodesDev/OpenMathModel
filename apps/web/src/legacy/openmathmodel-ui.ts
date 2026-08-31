// @ts-nocheck
import type { ScreenId } from "../types/screens";
import type { KnowledgeLibrary } from "../types/knowledge-library";
import { methodCategories, methodLibrary } from "../data/method-library";
import {
  buildDiagnosticReport,
  copyTextToClipboard,
  downloadDiagnosticReport,
  runNetworkDiagnostics,
} from "../diagnostics/system-diagnostics";
import { RECIPE_LANGUAGES, methodRecipes } from "../data/method-recipes";
import { hydrateAccountUi, initSecurityPane } from "../auth/account-security";
import { applyLocale, currentLocale, t } from "../i18n/locale";
import { hydrateMaxConcurrency, persistMaxConcurrency } from "../preferences/account-preferences";
import { ApiError, authApi } from "../auth/api";
import { attachmentsOf } from "../attachments/composer-attachments";
import { collectConversationAttachments } from "../attachments/conversation-context";
import { encodePassthroughImages, planImagePassthrough } from "../attachments/image-passthrough";
import { resolveSelectedModality } from "../integration/model-modality";
import { OPENING_ANALYSIS_PROMPT, conversationSnapshot, sendConversationTurn } from "../integration/agent-chat";
import { CHAT_MODES, currentChatMode, saveChatMode } from "../integration/chat-mode";
import {
  addComposerReference,
  clearComposerReferences,
  composerReferenceBlock,
  listComposerReferences,
  methodReference,
  mountReferenceChips,
  paperReference,
  problemReference,
  resetComposerReferences,
  restorePendingTaskReferences,
} from "../integration/composer-references";
import { attachTraceToLastReply, loadConversationLog } from "../tasks/conversation-log";
import {
  endpointHost,
  presetMatchesHost,
  PROVIDER_PRESETS,
  providerPreset,
} from "../integration/llm-providers";
import {
  endpointFromForm,
  fetchEndpointModels,
  fetchLlmConfig,
  labelFromProtocol,
  persistLlmSettings,
  removeEndpoint,
  saveEndpointAsNew,
  setEndpointWeight,
  setPrimaryEndpoint,
  updateEndpoint,
} from "../integration/llm-settings";
import {
  clearLlmUsage,
  listLlmUsage,
  proxyTransparencyEnabled,
  recordLlmUsage,
} from "../integration/llm-usage";
import {
  exportUsageCsv,
  hydrateUsagePane,
  maybeNotifyBudgetAlert,
  persistUsageSettings,
} from "../integration/usage-monitor";
import {
  hydratePrivacyPane,
  persistPrivacySettings,
  saveHistoryEnabled,
  syncPrivacyGatesOnce,
} from "../preferences/privacy-preferences";
import { mountModelingWorkspace } from "../integration/modeling-workspace-controller";
import { mountSidebarSearch } from "../integration/sidebar-search";
import { hydrateRecentTasks } from "../integration/recent-tasks";
import { hydrateProjectsPage } from "../integration/projects-page";
import { renderMarkdown } from "../text/markdown";
import { typesetMath } from "../text/math-typeset";
import { createStreamingMarkdownRenderer, createThrottledTextSink } from "../text/stream-render";
import {
  notificationsSupported,
  requestNotificationPermission,
  sendNotificationPreview,
} from "../notifications/desktop-notifications";
import {
  applyDisplayPreferences,
  currentDisplayPreferences,
  TEXT_BASE_PX,
} from "../preferences/display-preferences";
import { mountTaskAutosave } from "../tasks/task-autosave";

  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
  const icon = (name, extra = "") => `<i class="ph ph-${name} ${extra}" aria-hidden="true"></i>`;
  const projectLogo = (extra = "") =>
    `<img class="project-logo ${extra}" src="/assets/OpenMathModel_IP_Crop.png" alt="" aria-hidden="true">`;
  const providerLogoSources = {
    qwen: "/assets/provider-qwen.svg",
    deepseek: "/assets/provider-deepseek.svg",
    openai: "/assets/provider-openai.svg",
    anthropic: "/assets/provider-anthropic.svg",
    google: "/assets/provider-google.svg",
    kimi: "/assets/provider-kimi.svg",
    zhipu: "/assets/provider-zhipu.svg",
    xai: "/assets/provider-xai.svg",
    ollama: "/assets/provider-ollama.svg"
  };
  // 自定义中转站等没有品牌资源的来源回落为首字母标，不放错误的图
  const providerLogo = (provider, label, extra = "") =>
    providerLogoSources[provider]
      ? `<img class="provider-brand-logo provider-brand-${provider} ${extra}" src="${providerLogoSources[provider]}" alt="${escapeHtml(label)}">`
      : `<span class="provider-brand-logo provider-letter-logo ${extra}" aria-hidden="true">${escapeHtml(String(label || provider).trim().charAt(0).toUpperCase())}</span>`;
  const escapeHtml = value => String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[character]);
  const formatFileSize = value => {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  };
  // 只有赛事主办方自己的域名算“来源”。社区仓库（zhanwen/MathModel 等）是我们
  // 采集题面的中间站，不是可以引用的出处，也不该把用户送到第三方账号下，
  // 所以这些链接一律不渲染，而不是渲染成灰色按钮。
  const OFFICIAL_SOURCE_HOSTS = [
    "comap.org", "mcm.edu.cn", "apmcm.org", "cmathc.org.cn", "acge.org.cn", "mathorcup.org", "saikr.com",
    "tipdm.org", "tjjmds.ai-learning.net",
  ];
  const isOfficialSourceUrl = value => {
    const raw = String(value || "").trim();
    if (!raw) return false;
    let host = "";
    try {
      const parsed = new URL(raw, window.location.origin);
      if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return false;
      host = parsed.hostname.toLowerCase();
    } catch {
      return false;
    }
    return OFFICIAL_SOURCE_HOSTS.some(allowed => host === allowed || host.endsWith(`.${allowed}`));
  };
  /**
   * 侧栏的三种形态按视口切换：宽屏完整、821~1180 自动收成图标栏、820 以下改抽屉。
   * 自动收起只作用于当前视口，不写回 openmathmodelSidebarCollapsed——那是用户手动设的偏好。
   */
  const RAIL_VIEWPORT = "(min-width: 821px) and (max-width: 1180px)";
  const DRAWER_VIEWPORT = "(max-width: 820px)";
  const matchesViewport = query => {
    try {
      return window.matchMedia(query).matches;
    } catch {
      return false;
    }
  };
  const sidebarCollapsed = () => {
    try {
      return localStorage.getItem("openmathmodelSidebarCollapsed") === "true";
    } catch {
      return false;
    }
  };

  const AUTO_MODEL_OPTION = { id: "auto", label: "Auto", detail: "智能路由 · 按问题难度自动选择", provider: "agent" };

  /** 接口域名 → 厂商标（有品牌资源的用品牌图，其余按自定义 API 处理）。 */
  const providerForEndpointHost = host =>
    PROVIDER_PRESETS.find(preset => presetMatchesHost(preset, host))?.logo || "custom";

  /** 一条已保存接口 → 模型选择器选项；权重展示给 Auto 路由做参照。 */
  const endpointModelOption = (endpoint, isPrimary) => {
    const host = endpointHost(endpoint.base_url);
    const weightText = endpoint.weight ? `权重 ${endpoint.weight}` : "权重自动";
    return {
      id: `endpoint-${endpoint.id}`,
      label: endpoint.model || endpoint.name,
      detail: `${endpoint.name} · ${isPrimary ? "主接口" : "备用"} · ${weightText}`,
      provider: providerForEndpointHost(host),
    };
  };

  /**
   * 模型选择器的选项列表。已保存接口即模型池：Auto 在首位（难度判定 +
   * 权重路由），其后是每条接口。未登录/未配置时保持演示期的静态选项。
   */
  const composerModelOptions = (config = null) => {
    if (config && config.endpoints.length) {
      return [
        AUTO_MODEL_OPTION,
        ...config.endpoints.map(endpoint =>
          endpointModelOption(endpoint, endpoint.id === config.active_endpoint_id)),
      ];
    }
    let settings = {};
    try {
      settings = JSON.parse(localStorage.getItem("openmathmodelSettings") || "{}");
    } catch {}
    const customModel = settings.apiModel || "gpt-5.6-sol";
    const customProfile = settings.apiProfileName || "OpenAI 兼容中转站";
    return [
      AUTO_MODEL_OPTION,
      { id: "qwen3.8-max", label: "Qwen3.8-Max", detail: "通义千问 · 官方服务", provider: "qwen" },
      { id: "deepseek-v4-pro", label: "DeepSeek-V4-Pro", detail: "DeepSeek · 官方服务", provider: "deepseek" },
      { id: "gpt-5.6-sol", label: "GPT-5.6 Sol", detail: "OpenAI · 官方服务", provider: "openai" },
      { id: "claude-sonnet-5", label: "Claude Sonnet 5", detail: "Anthropic · 官方服务", provider: "anthropic" },
      { id: `custom-${customModel}`, label: customModel, detail: `${customProfile} · 自定义 API`, provider: "custom" }
    ];
  };

  const composerModelLogo = option => option.provider === "agent"
    ? projectLogo("composer-logo")
    : option.provider === "custom"
      ? `<span class="custom-api-logo">${icon("plugs-connected")}</span>`
      : providerLogo(option.provider, option.detail, "composer-provider-logo");

  const modelMenuMarkup = (options, selectedId) => `
    <div class="agent-model-menu-title">选择模型或 API</div>
    ${options.map(option => `<button type="button" data-action="select-model" data-model-choice="${escapeHtml(option.id)}" role="option" aria-selected="${option.id === selectedId}">
      <span class="model-choice-logo">${composerModelLogo(option)}</span>
      <span class="model-choice-copy"><strong>${escapeHtml(option.label)}</strong><small>${escapeHtml(option.detail)}</small></span>
      ${icon("check")}
    </button>`).join("")}`;

  /**
   * 页面渲染后把模型选择器换成真实接口池：Auto + 已保存接口。
   * 旧选择指向已删除接口时重置为 Auto，避免请求携带失效的 endpoint_id。
   */
  async function hydrateModelPickers() {
    if (!$$("[data-model-picker]").length) return;
    const config = await fetchLlmConfig();
    if (!config || !config.endpoints.length) return; // 保持演示选项
    const options = composerModelOptions(config);
    let saved = "auto";
    try { saved = localStorage.getItem("openmathmodelSelectedModel") || "auto"; } catch {}
    const selected = options.find(option => option.id === saved) || options[0];
    if (selected.id !== saved) {
      try { localStorage.setItem("openmathmodelSelectedModel", selected.id); } catch {}
    }
    $$("[data-model-picker]").forEach(picker => {
      const menu = $(".agent-model-menu", picker);
      if (menu) menu.innerHTML = modelMenuMarkup(options, selected.id);
      const label = $("[data-model-picker-label]", picker);
      if (label) label.textContent = selected.label;
      const logo = $("[data-model-picker-icon]", picker);
      if (logo) logo.innerHTML = composerModelLogo(selected);
    });
  }

  const routes = {
    new: "/",
    confirm: "/confirm",
    running: "/task/running",
    projects: "/projects",
    data: "/workspace/data",
    model: "/workspace/model-plan",
    experiments: "/workspace/experiments",
    editor: "/workspace/paper-editor",
    problems: "/library/problems",
    papers: "/library/papers",
    methods: "/library/methods",
    complete: "/task/complete",
    problemDetail: "/library/problems/detail",
    paperDetail: "/library/papers/detail"
  };

  const navItems = [
    ["projects", "folders", "我的项目", "projects"]
  ];

  const resourceItems = [
    ["problems", "trophy", "赛题库", "problems"],
    ["papers", "file-dashed", "优秀论文", "papers"],
    ["methods", "book-open", "方法库", "methods"]
  ];

  const recentTasks = [
    "2024国赛A题：健康数据建模分析",
    "光伏功率预测与不确定性分析",
    "城市出租车需求建模与优化",
    "基于多指标的供应链风险评估"
  ];

  function navSection(items, active) {
    return items.map(([id, ico, label, route]) => `
      <a class="nav-item ${active === id ? "active" : ""}" href="${routes[route]}" title="${label}">
        ${icon(ico)}<span>${label}</span>
      </a>`).join("");
  }

  function sidebar(active = "chat") {
    return `<aside class="sidebar" aria-label="主导航">${sidebarInner(active)}</aside>`;
  }

  function sidebarInner(active = "chat") {
    return `
        <div class="brand-row">
          <a class="brand" href="${routes.new}"><img src="/assets/OpenMathModel_IP_Crop.png" alt="OpenMathModel"><span>OpenMathModel</span></a>
          <button class="sidebar-collapse" type="button" data-action="toggle-sidebar" aria-expanded="${!sidebarCollapsed()}" title="${sidebarCollapsed() ? "展开侧栏" : "收起侧栏"}">${icon("sidebar-simple")}</button>
        </div>
        <div class="sidebar-search-row">
          <label class="global-search">
            ${icon("magnifying-glass")}
            <input type="search" name="sidebar-task-search" data-sidebar-search autocomplete="off" aria-label="全局搜索" placeholder="搜索任务">
          </label>
          <button class="search-filter" data-action="sidebar-filter" title="筛选">${icon("sliders-horizontal")}</button>
        </div>
        <button class="new-task-button ${active === "chat" ? "active" : ""}" data-action="new-task">${icon("plus")}<span>新建任务</span></button>
        <nav class="sidebar-nav primary-nav">${navSection(navItems, active)}</nav>
        <div class="nav-section-label resource-label">知识资源</div>
        <nav class="sidebar-nav">${navSection(resourceItems, active)}</nav>
        <div class="recent">
          <div class="recent-title">最近任务</div>
          ${recentTasks.slice(0, 3).map((task, i) => `<a class="recent-link" href="${routes.running}">${icon(i === 0 ? "circle-half" : "check")}<span>${task}</span>${i === 0 ? '<b class="unread-dot"></b>' : ""}</a>`).join("")}
        </div>
        <div class="profile-row">
          <span class="avatar">I</span>
          <div><strong>Ivan</strong><small>个人工作区</small></div>
          <button class="settings" data-action="settings" style="border:0;background:transparent">${icon("gear")}<span>设置</span></button>
        </div>`;
  }

  function windowControls() {
    return "";
  }

  function shell(content, active, options = {}) {
    // 手机档侧栏是抽屉，抽屉里要展示完整侧栏，图标栏状态不能同时挂着。
    const railed = !matchesViewport(DRAWER_VIEWPORT) && (sidebarCollapsed() || matchesViewport(RAIL_VIEWPORT));
    return `<div class="app-shell ${railed ? "sidebar-collapsed" : ""}" data-sidebar-shell>
      <button class="sidebar-drawer-toggle" type="button" data-action="toggle-sidebar-drawer" aria-label="打开导航" aria-expanded="false">${icon("list")}</button>
      ${sidebar(active)}
      <div class="sidebar-backdrop" data-action="close-sidebar-drawer"></div>
      <main class="main">${content}</main>
      ${options.window === false ? "" : windowControls()}
    </div>`;
  }

  function composer(placeholder, compact = false) {
    const options = composerModelOptions();
    let savedModel = "auto";
    try { savedModel = localStorage.getItem("openmathmodelSelectedModel") || "auto"; } catch {}
    const selected = options.find(option => option.id === savedModel) || options[0];
    return `<div class="composer ${compact ? "chat-composer" : ""}">
      <div class="composer-input-row"><textarea aria-label="任务描述" placeholder="${placeholder}"></textarea></div>
      <div class="composer-tools">
        <input class="file-input" type="file" multiple hidden>
        <div class="composer-tool-group">
          <button class="tool-button icon-tool" data-action="attach" title="添加文件（也可直接拖入或粘贴）">${icon("plus")}</button>
          <button class="tool-button" data-action="reference">${icon("at")}<span class="tool-label">添加上下文</span></button>
          <button class="tool-button" data-action="mode">${icon("circles-three-plus")}<span class="tool-label">${t(currentChatMode().label)}</span>${icon("caret-down")}</button>
        </div>
        <div class="composer-model-picker" data-model-picker>
          <button type="button" class="composer-model" data-action="model-picker" aria-haspopup="listbox" aria-expanded="false" title="选择模型">
            <span class="composer-model-icon" data-model-picker-icon>${composerModelLogo(selected)}</span>
            <span data-model-picker-label>${escapeHtml(selected.label)}</span>${icon("caret-down")}
          </button>
          <div class="agent-model-menu" role="listbox" aria-label="选择模型">
            ${modelMenuMarkup(options, selected.id)}
          </div>
        </div>
        <button class="send-button primary" data-action="send" aria-label="发送" title="发送（Enter）">${icon("arrow-up")}</button>
      </div>
    </div>`;
  }

  function newScreen() {
    return shell(`
      <section class="new-screen" data-task-start-root data-task-start-screen="new">
        ${projectLogo("hero-logo")}
        <h1>让 Agent 完成整套建模工作</h1>
        <p class="lead">从赛题解析、数据处理到实验评估与论文交付，一个任务持续推进。</p>
        <div class="composer-area">
          <div class="composer-shortcuts">
            <button data-task-type="竞赛建模">${icon("trophy")} 竞赛建模</button>
            <button data-task-type="数据分析">${icon("chart-bar")} 数据分析</button>
            <button data-task-type="论文优化">${icon("file-text")} 论文优化</button>
            <button data-task-type="模型比较">${icon("arrows-left-right")} 模型比较</button>
          </div>
          ${composer("描述任务，/ 快速调用，@ 添加上下文")}
          <p class="muted" data-task-start-status role="status" hidden></p>
        </div>
        <p class="composer-note">AI 可能会出错，请核查关键结论、代码与引用。</p>
      </section>`, "chat");
  }

  function confirmScreen() {
    return shell(`
      <section class="confirm-wrap" data-task-start-root data-task-start-screen="confirm">
        <h1>确认任务</h1>
        <p class="muted">检查题目、文件和输出要求，确认后开始执行。</p>
        <div class="headline" data-task-project-name>2026全国大学生数学建模竞赛A题</div>
        <p class="muted" data-task-description-preview>请完成题目解析、建模、实验与论文交付。</p>
        <h3>文件</h3>
        <div class="file-read-list" data-task-file-list>
          ${[
            ["file-pdf", "A题.pdf", "1.28 MB"],
            ["file-xls", "附件一.xlsx", "86.7 KB"],
            ["file-csv", "站点数据.csv", "512.4 KB"],
            ["file-csv", "天气数据.csv", "248.9 KB"]
          ].map(([ico, name, size]) => `<div class="file-read-row">
            <span class="file-name">${icon(ico)}${name}</span><span class="size">${size}</span><span class="read">已读取</span>
          </div>`).join("")}
        </div>
        <h3>任务目标</h3>
        <ul class="compact-list"><li>分析题目并拆解子问题</li><li>建立候选模型并运行实验</li><li>生成完整论文与代码</li></ul>
        <h3>输出要求</h3>
        <ul class="compact-list"><li>中文论文</li><li>Python代码</li><li>Word与PDF</li><li>包含敏感性分析</li></ul>
        <h3>执行方式</h3>
        <ul class="compact-list"><li>关键步骤需要确认</li><li>自动运行实验</li><li>保留失败实验记录</li></ul>
        <div class="confirm-actions">
          <button data-go="new">返回修改</button>
          <button class="primary" data-go="running" data-task-start-submit>开始任务</button>
        </div>
        <p class="muted" data-task-start-status role="status" hidden></p>
      </section>`, "chat");
  }

  function progressStep(done, text, time, details, expanded = false) {
    return `<div class="progress-step ${expanded ? "open" : ""}" tabindex="0">
      <span class="step-dot ${done ? "done" : ""}">${done ? icon("check") : ""}</span>
      <span>${text}</span><span class="step-time">${time}</span>${icon("caret-down", "chev")}
    </div><div class="step-details">${details}</div>`;
  }

  const stageAgentCopy = {
    running: ["正在分析题目", "正在基于题目与附件梳理研究边界、子问题、已知条件与建模难点。"],
    data: ["正在准备数据", "正在检查字段质量、时间粒度、缺失记录，并生成可复现的数据清洗方案。"],
    model: ["正在优化模型方案", "已完成候选路线比较，正在输出优化后的模型方案，并给出建议。"],
    experiments: ["正在评估实验", "正在输出优化实验结果，已完成验证检查并生成结论。"],
    editor: ["正在协助论文撰写", "正在检查章节结构、公式编号、图表引用与结论一致性。"],
    complete: ["建模任务已完成", "论文、数据、代码与实验记录已经整理完成，可继续优化或下载全部文件。"]
  };

  function modelingHeader(active) {
    const completed = active === "complete";
    if (active === "editor") {
      return `<header class="modeling-topbar editor-topbar">
        <a class="modeling-home-link" href="${routes.experiments}" aria-label="返回实验结果" title="返回实验结果">${icon("arrow-left")}</a>
        <a class="modeling-project-title" href="${routes.editor}">
          <strong>论文撰写</strong>
          <span>第 3 章　·　需求预测模型构建　 <b class="saved-state">${icon("check-circle")} 已保存</b>　v4 ${icon("caret-down")}</span>
        </a>
        <div class="modeling-topbar-actions editor-topbar-actions">
          <button class="header-action-button" type="button" data-action="editor-check">${icon("shield-check")} 检查</button>
          <button class="header-action-button primary" type="button" data-action="continue-paper">继续生成</button>
          <button class="header-text-action" type="button" data-action="export-paper">导出 ${icon("caret-down")}</button>
          <span class="modeling-toolbar-divider"></span>
          <button type="button" data-action="history" aria-label="任务历史" title="任务历史">${icon("clock-counter-clockwise")}</button>
          <button type="button" data-action="task-doc" aria-label="任务文档" title="任务文档">${icon("file-text")}</button>
          <button type="button" data-action="settings" aria-label="设置" title="设置">${icon("gear")}</button>
        </div>
      </header>`;
    }
    const projectName = active === "data" ? "OpenMathModel" : "城市共享单车调度优化";
    return `<header class="modeling-topbar">
      <a class="modeling-home-link" href="${routes.new}" aria-label="返回首页" title="返回首页">${icon("arrow-left")}</a>
      <a class="modeling-project-title" href="${routes.running}">
        <strong data-bind="project-name">${projectName}</strong>
        <span>2026 国赛 A 题　·　自动模式</span>
      </a>
      <div class="modeling-topbar-actions">
        <span class="run-status ${completed ? "complete" : ""}"><b></b> ${completed ? "已完成" : "进行中"}</span>
        <span class="modeling-toolbar-divider"></span>
        <button type="button" data-action="history" aria-label="任务历史" title="任务历史">${icon("clock-counter-clockwise")}</button>
        <button type="button" data-action="task-doc" aria-label="任务文档" title="任务文档">${icon("file-text")}</button>
        <button type="button" data-action="settings" aria-label="设置" title="设置">${icon("gear")}</button>
      </div>
    </header>`;
  }

  function modelingAgentPane(active) {
    const copy = stageAgentCopy[active] || stageAgentCopy.running;
    const activeIndex = ["running", "data", "model", "experiments", "editor", "complete"].indexOf(active);
    const steps = [
      ["已读取题目与附件", "00:03", "已识别题面、订单、站点与天气数据。"],
      ["已完成问题拆解", "00:06", "任务拆解为需求预测、区域划分与调度优化。"],
      ["已完成数据结构分析", "00:12", "已检查字段完整性、时间粒度与异常记录。"],
      ["已完成候选模型比较", "00:18", "已比较 XGBoost、LightGBM 与 LSTM 的适配度。"]
    ];
    return `<section class="chat-pane modeling-chat-pane">
      <div class="modeling-agent-head">
        <div class="assistant-id">${projectLogo("assistant-logo")}<span>Agent</span></div>
      </div>
      <div class="chat-scroll">
        <div class="assistant-block modeling-assistant-block">
          <button class="activity-summary" data-action="toggle-activity">${icon("eye-slash")} 收起执行步骤 ${icon("caret-up")}</button>
          <div class="activity-list">
            ${steps.map(step => progressStep(true, step[0], step[1], step[2])).join("")}
            ${progressStep(active === "complete", active === "complete" ? "全部成果已交付" : copy[0], active === "complete" ? "完成" : "··:··", copy[1])}
          </div>
          <div class="analysis-copy modeling-agent-copy"><p>${copy[1]}</p></div>
        </div>
      </div>
      ${composer(active === "complete" ? "继续描述任务，快速问问，@ 添加上下文" : "继续描述任务，/ 快速调用，@ 添加上下文", true)}
    </section>`;
  }

  const focusedStages = new Set(["data", "model", "experiments", "editor", "complete"]);

  function focusedModelingHeader(active) {
    // 统一导航模型：顶部返回箭头一律回“任务执行”总览页（hub）；
    // 阶段之间的横向跳转由左栏六阶段时间线承担，不再用返回键模拟线性浏览历史。
    // 例外：任务已交付的“最终成果”页，返回键即一键回首页。
    const backRoute = active === "complete" ? routes.new : routes.running;
    const backLabel = active === "complete" ? "返回首页" : "返回任务执行";
    return `<header class="focused-modeling-topbar">
      <div class="focused-topbar-context">
        <a class="focused-back" href="${backRoute}" data-back-from="${active}" aria-label="${backLabel}" title="${backLabel}">${icon("arrow-left")}</a>
        <a class="focused-task-name" href="${routes.running}"><span data-bind="project-name">城市共享单车调度优化</span>${icon("caret-down")}</a>
      </div>
    </header>`;
  }

  // 五个阶段的演示态左栏文案：合并工作台的 showWorkspaceStage 也要用，抽到函数外共享。
  const FOCUSED_STAGE_DEMO = {
    data: {
      copy: "数据准备已完成初步检查，存在以下问题：时间粒度不一致、可用车辆数存在缺失、平均等待时间单位不明确。建议按右侧清洗方案处理后进入建模阶段。",
      button: "确认清洗方案",
      next: "model",
      current: "正在准备建模数据"
    },
    model: {
      copy: "已完成候选路线比较，推荐采用方案 A 作为主方案，因为其综合收益更高且风险可控。<br><br>当前已选择结果：方案 A（推荐主方案）。<br><br>如需了解更多细节，您可以继续提问方案假设、数据敏感性或潜在风险。",
      button: "采用方案 A",
      next: "experiments",
      current: "正在优化模型方案"
    },
    experiments: {
      copy: "已完成候选路线比较，正在输出优化后的模型方案，实验结果显示模型效果稳定，已有显著改进。<br><br><strong>风险与建议</strong><br>• 关注早晚高峰时段的区域供需错配风险。<br>• 若节假日或异常天气，模型需进行参数自适应调整。<br>• 建议结合实时数据，进一步缩短响应延迟。",
      button: `${icon("check-circle")} 采用该结果`,
      next: "editor",
      current: "正在评估实验结果"
    },
    editor: {
      copy: "论文正文已与实验结果同步，正在检查章节结构、公式编号、图表引用和结论一致性。<br><br><strong>当前进度</strong><br>• 第 3 章正在编辑，其余章节已生成内容骨架。",
      button: `${icon("check-circle")} 完成并交付`,
      next: "complete",
      current: "正在协助论文写作"
    },
    complete: {
      copy: "论文、图表、数据、代码与复现记录已经整理完成，所有交付文件均可在右侧查看。<br><br><strong>交付状态</strong><br>• 文件完整、关键数字一致，代码可复现。",
      button: "继续优化论文",
      next: "editor",
      current: "全部成果已交付"
    }
  };

  function focusedAgentPane(active) {
    const stage = FOCUSED_STAGE_DEMO[active];
    // 与 runningScreen 同一判定：真实运行不预渲染演示步骤/摘要/附件，
    // 步骤区以 boot 思考态占位等控制器接管，避免假内容闪现后被清换。
    const isRealRun = /^run_[0-9a-f]{32}$/.test(new URL(window.location.href).searchParams.get("run_id") ?? "");
    const steps = [
      ["已读取题目与附件", "00:03"],
      ["已完成问题拆解", "00:06"],
      ["已完成数据结构分析", "00:12"],
      ["已完成候选模型比较", "00:18"]
    ];
    const attachments = `<section class="focused-attachments" data-demo-only${active === "data" ? "" : " hidden"}>
      <h3>附件</h3>
      <button type="button" class="focused-attachment" data-action="download-data"><span class="attachment-file-icon xls">X.</span><span><strong>历史供需数据_2024Q4.xlsx</strong><small>24.7 MB</small></span>${icon("download-simple")}</button>
      <button type="button" class="focused-attachment" data-action="download-data"><span class="attachment-file-icon csv">csv</span><span><strong>字段说明草稿.csv</strong><small>8.3 KB</small></span>${icon("download-simple")}</button>
    </section>`;
    const demoSteps = `${steps.map(([text, time]) => `<div class="focused-step"><span class="focused-step-dot done">${icon("check-circle")}</span><span>${text}</span><time>${time}</time>${icon("caret-down", "chev")}</div>`).join("")}
          <div class="focused-step current"><span class="focused-step-dot ${active === "complete" ? "done" : ""}">${active === "complete" ? icon("check-circle") : ""}</span><span>${stage.current}</span><span class="focused-loading">${active === "complete" ? "完成" : "·····"}</span>${icon("caret-up", "chev")}</div>`;
    // 真实运行：步骤时间线与演示附件不再渲染（阶段计划归输入框上方的执行计划
    // 面板），只保留摘要与 CTA 槽位给控制器填充。
    const demoTimeline = `<button type="button" class="activity-summary" data-action="toggle-activity" aria-expanded="true" aria-controls="focused-activity-list-${active}">${icon("eye-slash")} 收起执行步骤 <span class="steps-count" data-steps-count hidden></span>${icon("caret-up")}</button>
        <div class="focused-activity-list" id="focused-activity-list-${active}" data-agent-steps>
          ${demoSteps}
        </div>`;
    return `<section class="chat-pane focused-agent-chat">
      <div class="focused-agent-head"><div class="assistant-id">${projectLogo("assistant-logo")}<span>Agent</span></div></div>
      <div class="focused-agent-scroll">
        ${isRealRun ? "" : demoTimeline}
        <div class="focused-agent-copy" data-agent-summary>${isRealRun ? "" : stage.copy}</div>
        ${isRealRun ? "" : attachments}
        <button class="focused-stage-cta" type="button" data-go="${stage.next}" data-agent-cta${isRealRun ? " hidden" : ""}>${isRealRun ? "" : stage.button}</button>
      </div>
      ${composer("继续描述任务，/ 快速调用，@ 添加上下文", true)}
    </section>`;
  }

  function workspaceTabs(tabs, activeTab) {
    return `<div class="focused-workspace-tabs" role="tablist">${tabs.map(([key, label, iconName]) => `<button type="button" class="${key === activeTab ? "active" : ""}" data-workspace-tab="${key}" role="tab" aria-selected="${key === activeTab}">${icon(iconName)}<span>${label}</span></button>`).join("")}</div>`;
  }

  /**
   * 手机档两栏放不下，改成一次只显示一侧。控件常驻 DOM，靠 CSS 在桌面端隐藏，
   * 这样切换视口宽度时不需要重新渲染页面。
   */
  function modelingPaneSwitch() {
    return `<div class="modeling-pane-switch" role="tablist" aria-label="切换 Agent 对话与工作区">
      <button type="button" data-modeling-pane="agent" role="tab" aria-selected="false">Agent 对话</button>
      <button type="button" class="active" data-modeling-pane="stage" role="tab" aria-selected="true">工作区</button>
    </div>`;
  }

  function modelingShell(content, active, auxiliary = "") {
    if (focusedStages.has(active)) {
      return `<div class="modeling-shell modeling-clone-shell" data-modeling-shell data-focused-stage="${active}" data-workspace-page="${active}">
        ${focusedModelingHeader(active)}
        <div class="focused-modeling-split" data-modeling-split data-mobile-pane="stage">
          ${modelingPaneSwitch()}
          <aside class="focused-agent-pane">${focusedAgentPane(active)}</aside>
          <div class="modeling-resizer focused-modeling-resizer" data-modeling-resizer role="separator" aria-label="调整 Agent 与建模内容的宽度" aria-orientation="vertical" aria-valuemin="20" aria-valuemax="58" aria-valuenow="27" tabindex="0"></div>
          <main class="focused-stage-pane" data-stage-view>${content}</main>
        </div>
      </div>`;
    }
    return `<div class="modeling-shell" data-modeling-shell>
      ${modelingHeader(active)}
      <div class="modeling-split" data-modeling-split data-mobile-pane="stage">
        ${modelingPaneSwitch()}
        <aside class="modeling-agent-pane">${modelingAgentPane(active)}</aside>
        <div class="modeling-resizer" data-modeling-resizer role="separator" aria-label="调整 Agent 对话与建模流程的显示比例" aria-orientation="vertical" aria-valuemin="24" aria-valuemax="62" aria-valuenow="32" tabindex="0"></div>
        <main class="modeling-stage-pane">
          <div class="modeling-stage-scroll">${content}</div>
        </main>
      </div>
      ${auxiliary}
    </div>`;
  }

  function runningScreen() {
    // 首屏气泡按运行隔离：优先读本运行的题面（发送链路按 run_id 写入）；真实
    // 运行没有记录时留空，等控制器用工作台快照的 goal 回填——绝不回落到同标签
    // 页里别的任务写下的全局 openmathmodelPrompt（数据隔离）。演示态维持原状。
    const runIdParam = new URL(window.location.href).searchParams.get("run_id") ?? "";
    const isRealRun = /^run_[0-9a-f]{32}$/.test(runIdParam);
    const prompt = isRealRun
      ? sessionStorage.getItem(`openmathmodel.taskGoal.${runIdParam}`) ?? ""
      : sessionStorage.getItem("openmathmodelPrompt") || "请结合共享单车订单、站点与天气数据，完成需求预测、区域划分和调度优化。";
    const steps = [
      ["已读取题目与附件", "00:03", "已识别题面、订单、站点和天气数据。"],
      ["已完成问题拆解", "00:06", "任务拆解为需求预测、区域划分和调度优化。"],
      ["已完成数据结构分析", "00:12", "已检查字段完整性、时间粒度与异常值。"],
      ["已完成候选模型比较", "00:18", "已比较 XGBoost、Prophet 和 LSTM 的适配度。"]
    ];
    // 真实运行不预渲染任何演示内容：阶段计划由输入框上方的「执行计划」面板
    // （task-todo-panel）承载，气泡里只保留摘要槽位，由控制器填充真实数据。
    const demoAssistantBlock = `
              <button class="activity-summary" data-action="toggle-activity">${icon("eye-slash")} 收起执行步骤 <span class="steps-count" data-steps-count hidden></span>${icon("caret-up")}</button>
              <div class="activity-list" data-agent-steps>
                ${steps.map(step => progressStep(true, step[0], step[1], step[2], true)).join("")}
              </div>
              <div class="analysis-copy modeling-agent-copy" data-agent-summary>
                <p>我已经完成题目和附件的初步读取。这个任务可以稳定地拆成三个相互衔接的子问题：</p>
                <ol><li>需求预测</li><li>区域划分</li><li>调度优化</li></ol>
              </div>
              <h4 data-demo-only>推荐建模路线</h4>
              <div class="plan-card" data-demo-only>
                <div><div class="plan-title"><span class="step-dot done">${icon("check")}</span>XGBoost 需求预测 + 混合整数规划调度</div><p class="plan-reason">理由：能够捕捉短期时空需求变化，并在约束条件下获得全局最优调度方案。</p></div>
                <div class="plan-time"><span class="muted">预计运行时间</span><strong>2.5 ~ 3.5 小时</strong></div>
                <button type="button" class="next-step-link" data-go="data" data-agent-cta>进入数据准备 ${icon("arrow-right")}</button>
              </div>
              <details class="alternatives" data-demo-only><summary>查看备选路线</summary><p>Prophet + 层次聚类 + 线性规划；LSTM + K-means + 启发式调度。</p></details>`;
    const liveAssistantBlock = `
              <div class="analysis-copy modeling-agent-copy" data-agent-summary></div>`;
    return shell(`
      <section class="running-main" data-modeling-shell data-workspace-page="running">
        <section class="chat-pane running-chat-pane">
          <header class="task-toolbar">
            <a class="back" href="${routes.new}" aria-label="返回首页" title="返回首页">${icon("arrow-left")}</a>
            <div><h2 data-bind="project-name">${isRealRun ? "" : "城市共享单车调度优化"}</h2><p${isRealRun ? " hidden" : ""}>2026 国赛 A 题　·　自动模式</p></div>
            <div class="task-toolbar-actions">
              <span class="run-status${isRealRun ? "" : " complete"}"><b></b> ${isRealRun ? "加载中…" : "规划完成"}</span>
              <button type="button" data-action="files" aria-label="查看 3 个附件"${isRealRun ? ' style="display:none"' : ""}>${icon("paperclip")} 3</button>
              <button type="button" data-action="more" aria-label="更多操作">${icon("dots-three")}</button>
            </div>
          </header>
          <div class="chat-scroll">
            <div class="user-message"><div class="user-bubble">${escapeHtml(prompt)}</div></div>
            <div class="assistant-block">
              <div class="assistant-id">${projectLogo("assistant-logo")}<span>Agent</span></div>
              ${isRealRun ? liveAssistantBlock : demoAssistantBlock}
              <button type="button" class="running-live-cta" data-agent-cta data-live-only>Agent 正在执行</button>
            </div>
          </div>
          ${composer("继续描述任务，/ 快速调用，@ 添加上下文", true)}
        </section>
      </section>`, "chat");
  }

  const projectRows = [
    ["2026国赛A题", "城市共享单车调度优化", "实验验证", "2025-05-26 14:32", 28, 15, 2],
    ["SIR传播分析", "基于社交网络的传染病模型", "论文撰写", "2025-05-24 10:18", 16, 9, 1],
    ["电动汽车充电站选址", "多目标优化与空间分析", "数据处理", "2025-05-22 16:05", 23, 12, 0],
    ["校园餐饮需求预测", "时间序列与回归分析", "数据处理", "2025-05-21 09:41", 18, 6, 0],
    ["物流路径优化", "改进遗传算法求解VRP", "实验验证", "2025-05-19 18:27", 21, 14, 1],
    ["水质评价模型", "综合指数法与模糊评价", "论文撰写", "2025-05-18 11:03", 15, 7, 1],
    ["股票趋势预测", "LSTM深度学习模型", "数据处理", "2025-05-16 21:14", 17, 8, 0]
  ];

  function projectsScreen() {
    return shell(`
      <section class="main-pad">
        <h1 class="page-title">项目</h1>
        <button class="top-action primary" data-action="new-project">${icon("plus")} 新建项目</button>
        <div class="project-tabs tabs-line">
          ${["全部", "进行中", "已完成", "已归档"].map((t, i) => `<button class="tab-button ${i === 0 ? "active" : ""}" data-project-tab="${t}">${t}</button>`).join("")}
        </div>
        <div class="project-card">
          <div class="project-search-row"><label class="search-box">${icon("magnifying-glass")}<input type="search" name="project-search" data-table-search autocomplete="off" aria-label="搜索项目" placeholder="搜索项目名称……"></label></div>
          <table class="project-table">
            <thead><tr><th>项目名称</th><th>当前阶段</th><th>最近更新　⌃</th><th>文件</th><th>实验</th><th>论文</th><th>操作</th></tr></thead>
            <tbody>
              ${projectRows.map(r => `<tr data-project="${r[0]}">
                <td class="project-name"><strong>${r[0]}</strong><span>${r[1]}</span></td>
                <td><span class="stage-pill" data-stage="${r[2]}">${r[2]}</span></td><td>${r[3]}</td><td>${r[4]}</td><td>${r[5]}</td><td>${r[6]}</td>
                <td><button type="button" class="row-menu-button" data-action="row-menu" aria-label="更多操作">${icon("dots-three")}</button></td>
              </tr>`).join("")}
            </tbody>
          </table>
        </div>
        <div class="project-footer"><span>共 7 项</span><div class="pagination"><button class="page-button" disabled>‹</button><button class="page-button active">1</button><button class="page-button">›</button>
          <div class="settings-custom-select page-size-select" data-page-size-select data-select-menu>
            <button type="button" class="settings-select-trigger" data-select-trigger aria-haspopup="listbox" aria-expanded="false" aria-label="每页条数"><span data-select-label>20 条/页</span>${icon("caret-down")}</button>
            <div class="settings-select-menu" role="listbox" aria-label="每页条数">
              ${["10", "20", "50"].map(size=>`<button type="button" role="option" data-select-option="${size}" aria-selected="${size === "20"}"><span>${size} 条/页</span>${icon("check")}</button>`).join("")}
            </div>
          </div>
        </div></div>
      </section>`, "projects");
  }

  function taskDetailsDrawer() {
    return `<aside class="task-detail-drawer" data-task-detail-drawer aria-label="查看详情" aria-hidden="true">
      <div class="drawer-heading"><h2>查看详情</h2><button type="button" data-action="close-details" aria-label="关闭详情">${icon("x")}</button></div>
      <div class="drawer-tabs" role="tablist">
        ${["当前信息", "历史版本", "运行记录", "相关产物"].map((tab, index) => `<button class="${index === 0 ? "active" : ""}" data-drawer-tab="${tab}" role="tab">${tab}</button>`).join("")}
      </div>
      <div class="drawer-content">
        <section class="drawer-card">
          <div class="drawer-section-title">${icon("flow-arrow")}<strong>任务图（概览）</strong></div>
          <p>数据准备 → 模型设定 → 求解与验证 → 结果输出</p>
          <p>当前步骤：数据准备（第 2 轮 · 输入检查完成）</p>
        </section>
        <section class="drawer-card">
          <div class="drawer-section-title">${icon("clock")}<strong>循环记录</strong></div>
          <ul class="drawer-timeline"><li><span>2025-05-10 10:21</span>第 2 轮　输入检查完成（输入 v2）</li><li><span>2025-05-10 09:47</span>第 1 轮　输入初检完成（输入 v1）</li><li><span>2025-05-10 09:12</span>第 0 轮　任务创建</li></ul>
        </section>
        <section class="drawer-card">
          <div class="drawer-section-title">${icon("table")}<strong>输入来源</strong></div>
          <ul><li>需求表：历史供需数据_2024Q4.xlsx（Sheet: demand）</li><li>参数：用户提供（12 项）</li><li>附件：无</li></ul>
        </section>
        <section class="drawer-card">
          <div class="drawer-section-title">${icon("seal")}<strong>模型假设</strong></div>
          <ul><li>需求在 15 分钟粒度内近似平稳</li><li>车辆服务时间服从正态分布</li><li>所有区域可独立调度</li></ul>
        </section>
        <button class="drawer-card drawer-validation" type="button" data-action="validation-details">
          <span class="drawer-section-title">${icon("check-circle")}<strong>完整验证结果</strong></span>${icon("caret-right")}
          <small>通过（6/6）<br>数据完整性、范围校验、逻辑一致性、异常检测、单位一致性、可解性</small>
        </button>
        <section class="drawer-card">
          <div class="drawer-section-title">${icon("record")}<strong>运行环境</strong></div>
          <p>求解器：Gurobi 11.0.1<br>运行时长：00:01:48<br>机器：8 vCPU / 32 GB RAM<br>时间：2025-05-10 10:21<br>运行者：Agent</p>
        </section>
      </div>
    </aside>`;
  }

  function dataStageContent() {
    return `
      <section class="focused-workspace data-report-workspace">
        ${workspaceTabs([["data-report","数据报告","file-text"],["raw-data","原始数据","table"],["clean-data","清洗数据","sliders-horizontal"],["field-guide","字段说明","files"]], "data-report")}
        <div class="focused-workspace-panel active" data-workspace-panel="data-report">
          <header class="focused-document-heading"><div><h1>数据报告</h1><p>数据质量检查与处理建议</p></div><div><button type="button" data-action="refresh-report" aria-label="刷新报告">${icon("arrow-clockwise")}</button><button type="button" data-action="download-data" aria-label="下载报告">${icon("download-simple")}</button></div></header>
          <div class="focused-conclusion-strip">${icon("check-circle")}<strong>核心结论：</strong><span>完成以下 3 项清洗后，数据可进入建模阶段。</span></div>
          <section class="focused-metrics three"><article><span>记录数</span><strong>12,480</strong></article><article><span>字段数</span><strong>18</strong></article><article><span>缺失比例</span><strong>2.7%</strong></article></section>
          <section class="focused-section compact"><h2>数据问题与处理建议</h2><div class="focused-table-wrap"><table class="focused-table issue-table"><thead><tr><th></th><th>问题描述</th><th>处理方法</th><th>应用</th></tr></thead><tbody>
            <tr><td>1</td><td>时间粒度不一致（5min / 15min / 30min 混杂）</td><td>统一重采样为 15 分钟粒度，采用均值/求和汇总</td><td><button class="focused-toggle is-on" data-action="suggestion-toggle" aria-pressed="true"><span></span></button></td></tr>
            <tr><td>2</td><td>可用车辆数存在缺失</td><td>按区域 × 时间填充前向填充，并标记缺失来源</td><td><button class="focused-toggle is-on" data-action="suggestion-toggle" aria-pressed="true"><span></span></button></td></tr>
            <tr><td>3</td><td>平均等待时间单位不明确</td><td>统一转换为“分钟”，并在字段说明中注明单位</td><td><button class="focused-toggle is-on" data-action="suggestion-toggle" aria-pressed="true"><span></span></button></td></tr>
          </tbody></table></div></section>
          <section class="focused-section compact raw-preview-section"><h2>原始数据预览 <span>（前 5 行）</span></h2><div class="focused-table-wrap"><table class="focused-table preview-data-table"><thead><tr><th>时间</th><th>区域</th><th>投放点数</th><th>可用车辆数</th><th>平均等待时间（单位待定）</th><th>···</th></tr></thead><tbody>
            <tr><td>2024-10-01 00:00</td><td>中心城区</td><td>128</td><td>356</td><td>7.2</td><td>···</td></tr>
            <tr><td>2024-10-01 00:05</td><td>中心城区</td><td>128</td><td>–</td><td>6.8</td><td>···</td></tr>
            <tr><td>2024-10-01 00:15</td><td>中心城区</td><td>128</td><td>312</td><td>–</td><td>···</td></tr>
            <tr><td>2024-10-01 00:30</td><td>中心城区</td><td>128</td><td>298</td><td>8.1</td><td>···</td></tr>
            <tr><td>2024-10-01 00:45</td><td>中心城区</td><td>128</td><td>410</td><td>7.5</td><td>···</td></tr>
          </tbody></table></div><footer class="focused-table-footer"><span>显示前 5 行，共 12,480 条记录</span><nav aria-label="数据分页"><button disabled>${icon("caret-left")}</button><button class="active">1</button><button>2</button><button>3</button><span>···</span><button>104</button><button>${icon("caret-right")}</button></nav></footer></section>
        </div>
        <div class="focused-workspace-panel" data-workspace-panel="raw-data"><section class="focused-template">
          <header class="focused-template-heading"><div><h1>原始数据</h1><p>历史供需数据 · 只读预览</p></div><button type="button" data-action="download-data">${icon("download-simple")} 导出</button></header>
          <section class="focused-metrics three focused-template-metrics"><article><span>记录数</span><strong>12,480</strong><small>2024 Q4</small></article><article><span>数据表</span><strong>2</strong><small>订单 / 站点</small></article><article><span>更新时间</span><strong>10:32</strong><small>今天</small></article></section>
          <section class="focused-template-section"><div class="focused-template-section-title"><h2>历史供需数据</h2><span>前 8 行</span></div><div class="focused-table-wrap"><table class="focused-table focused-template-table"><thead><tr><th>时间</th><th>区域</th><th>投放点数</th><th>可用车辆</th><th>平均等待</th><th>订单量</th></tr></thead><tbody>
            <tr><td>2024-10-01 00:00</td><td>中心城区</td><td>128</td><td>356</td><td>7.2 min</td><td>442</td></tr><tr><td>2024-10-01 00:05</td><td>中心城区</td><td>128</td><td>—</td><td>6.8 min</td><td>419</td></tr><tr><td>2024-10-01 00:15</td><td>中心城区</td><td>128</td><td>312</td><td>—</td><td>461</td></tr><tr><td>2024-10-01 00:30</td><td>中心城区</td><td>128</td><td>298</td><td>8.1 min</td><td>506</td></tr><tr><td>2024-10-01 00:45</td><td>中心城区</td><td>128</td><td>410</td><td>7.5 min</td><td>473</td></tr><tr><td>2024-10-01 01:00</td><td>滨江新区</td><td>96</td><td>274</td><td>6.4 min</td><td>388</td></tr><tr><td>2024-10-01 01:15</td><td>滨江新区</td><td>96</td><td>266</td><td>6.7 min</td><td>401</td></tr><tr><td>2024-10-01 01:30</td><td>大学城</td><td>84</td><td>221</td><td>5.9 min</td><td>357</td></tr>
          </tbody></table></div><footer class="focused-template-footer"><span>共 12,480 条记录</span><span>数据版本 v1</span></footer></section>
        </section></div>
        <div class="focused-workspace-panel" data-workspace-panel="clean-data"><section class="focused-template">
          <header class="focused-template-heading"><div><h1>清洗数据</h1><p>规则执行结果与清洗后预览</p></div><span class="focused-template-status">${icon("check-circle")} 已完成</span></header>
          <section class="focused-metrics three focused-template-metrics"><article><span>保留记录</span><strong>12,436</strong><small>99.65%</small></article><article><span>缺失比例</span><strong>0.4%</strong><small>清洗前 2.7%</small></article><article><span>处理规则</span><strong>3</strong><small>全部通过</small></article></section>
          <section class="focused-template-section"><div class="focused-template-section-title"><h2>处理规则</h2><span>按顺序执行</span></div><div class="focused-rule-list">
            <article><b>01</b><div><strong>统一时间粒度</strong><span>重采样为 15 分钟</span></div><em>已应用</em></article><article><b>02</b><div><strong>补全车辆缺失</strong><span>区域内前向填充</span></div><em>已应用</em></article><article><b>03</b><div><strong>校正等待单位</strong><span>统一转换为分钟</span></div><em>已应用</em></article>
          </div></section>
          <section class="focused-template-section"><div class="focused-template-section-title"><h2>清洗后预览</h2><span>数据版本 v2</span></div><div class="focused-table-wrap"><table class="focused-table focused-template-table"><thead><tr><th>时间</th><th>区域</th><th>可用车辆</th><th>平均等待</th><th>质量标记</th></tr></thead><tbody><tr><td>2024-10-01 00:00</td><td>中心城区</td><td>356</td><td>7.2 min</td><td>原始</td></tr><tr><td>2024-10-01 00:15</td><td>中心城区</td><td>312</td><td>6.9 min</td><td>汇总</td></tr><tr><td>2024-10-01 00:30</td><td>中心城区</td><td>298</td><td>8.1 min</td><td>原始</td></tr><tr><td>2024-10-01 00:45</td><td>中心城区</td><td>410</td><td>7.5 min</td><td>原始</td></tr></tbody></table></div></section>
        </section></div>
        <div class="focused-workspace-panel" data-workspace-panel="field-guide"><section class="focused-template">
          <header class="focused-template-heading"><div><h1>字段说明</h1><p>字段、类型、单位与质量状态</p></div><span class="focused-template-status neutral">18 个字段</span></header>
          <div class="focused-conclusion-strip focused-template-notice">${icon("check-circle")}<span>核心建模字段已完成类型和单位校验。</span></div>
          <section class="focused-template-section"><div class="focused-table-wrap"><table class="focused-table focused-template-table field-table"><thead><tr><th>字段</th><th>含义</th><th>类型</th><th>单位</th><th>来源</th><th>状态</th></tr></thead><tbody><tr><td><strong>timestamp</strong></td><td>统计时刻</td><td>datetime</td><td>—</td><td>订单表</td><td>已校验</td></tr><tr><td><strong>region_id</strong></td><td>运营区域</td><td>string</td><td>—</td><td>站点表</td><td>已校验</td></tr><tr><td><strong>dock_count</strong></td><td>投放点数</td><td>integer</td><td>个</td><td>站点表</td><td>已校验</td></tr><tr><td><strong>available_bikes</strong></td><td>可用车辆数</td><td>integer</td><td>辆</td><td>状态表</td><td>已清洗</td></tr><tr><td><strong>avg_wait</strong></td><td>平均等待时间</td><td>float</td><td>分钟</td><td>订单表</td><td>已校正</td></tr><tr><td><strong>order_count</strong></td><td>订单数量</td><td>integer</td><td>单</td><td>订单表</td><td>已校验</td></tr><tr><td><strong>temperature</strong></td><td>气温</td><td>float</td><td>℃</td><td>天气表</td><td>已校验</td></tr><tr><td><strong>is_holiday</strong></td><td>节假日标记</td><td>boolean</td><td>—</td><td>日历表</td><td>已校验</td></tr></tbody></table></div><footer class="focused-template-footer"><span>显示核心字段 8 / 18</span><span>最后校验 10:36</span></footer></section>
        </section></div>
      </section>`;
  }

  function modelStageContent() {
    return `
      <section class="focused-workspace model-plan-workspace">
        ${workspaceTabs([["model-plan","模型方案","file-text"],["assumptions","模型假设","table"],["symbols","符号表","list-dashes"],["implementation","实现计划","chart-line"]], "model-plan")}
        <div class="focused-workspace-panel active" data-workspace-panel="model-plan">
          <div class="focused-conclusion-strip model-recommendation">${icon("check-circle")}<span>建议采用方案 A 作为主方案，方案 B 作为可运行基线，方案 C 作为条件备用方案。</span></div>
          <div class="focused-plan-list">
            <button class="focused-plan-row selected" data-plan-option="0" type="button"><span class="plan-radio"></span><strong>方案 A <small>（推荐主方案）</small></strong><span>核心方法：需求预测 + 混合整数优化</span><span>计划角色：主方案</span><span>主要风险：需求预测不确定性</span>${icon("caret-up")}</button>
            <button class="focused-plan-row" data-plan-option="1" type="button"><span class="plan-radio"></span><strong>方案 B <small>（可运行基线）</small></strong><span>核心方法：分区聚类 + 分阶段调度</span><span>计划角色：基线</span><span>主要风险：边界效应可能影响结果</span>${icon("caret-down")}</button>
            <button class="focused-plan-row" data-plan-option="2" type="button"><span class="plan-radio"></span><strong>方案 C <small>（条件备用方案）</small></strong><span>核心方法：分层聚合 + 线性规划</span><span>计划角色：备用</span><span>主要风险：线性假设较强</span>${icon("caret-down")}</button>
          </div>
          <section class="focused-plan-detail selected-plan-overview">
            <div><h2>建模思路</h2><p>先基于历史数据进行需求预测，得到各区域在各时间段的净需求；<br>再构建以调度成本最小化为目标的混合整数优化模型，决定车辆的跨区调度与分时投放计划。</p></div>
            <div><h2>主要输入</h2><ul><li>共享单车历史订单数据、站点分布与容量信息</li><li>时间区间、车辆总量与调度成本参数</li><li>运营约束：车辆调拨、站点容量、调度策略规则等</li></ul></div>
            <div><h2>预期输出</h2><ul><li>各时间段各区域车辆投放与回收数量</li><li>跨区域调度路径与车辆流转计划</li><li>目标函数值（总调度成本）与关键指标（满足率、平衡度等）</li></ul></div>
            <div><h2>验证方式</h2><ul><li>在历史数据上进行回测，比较方案指标（满足率、成本、平衡度）</li><li>与基线方案对比分析提升幅度</li><li>鲁棒性测试：参数扰动与需求波动下的稳定性评估</li></ul></div>
          </section>
        </div>
        <div class="focused-workspace-panel" data-workspace-panel="assumptions"><section class="focused-template">
          <header class="focused-template-heading"><div><h1>模型假设</h1><p>全局假设与方案 A 特定假设</p></div><span class="focused-template-status neutral">6 项</span></header>
          <div class="focused-conclusion-strip focused-template-notice">${icon("check-circle")}<span>当前假设均可由数据或运营规则支撑。</span></div>
          <section class="focused-template-section"><div class="focused-table-wrap"><table class="focused-table focused-template-table assumption-table"><thead><tr><th>#</th><th>假设</th><th>适用范围</th><th>依据</th><th>影响</th><th>状态</th></tr></thead><tbody><tr><td>A1</td><td>15 分钟内需求保持平稳</td><td>全局</td><td>时间粒度</td><td>低</td><td>已确认</td></tr><tr><td>A2</td><td>站点容量短期内固定</td><td>全局</td><td>运营规则</td><td>中</td><td>已确认</td></tr><tr><td>A3</td><td>调度车辆速度近似恒定</td><td>全局</td><td>历史均值</td><td>中</td><td>待检验</td></tr><tr><td>A4</td><td>天气信息可提前获得</td><td>全局</td><td>数据接口</td><td>低</td><td>已确认</td></tr><tr><td>M1</td><td>区域需求可独立预测</td><td>方案 A</td><td>分区结果</td><td>中</td><td>待检验</td></tr><tr><td>M2</td><td>调度成本近似线性</td><td>方案 A</td><td>成本规则</td><td>高</td><td>重点验证</td></tr></tbody></table></div></section>
          <footer class="focused-template-callout"><strong>验证重点</strong><span>围绕 A3、M1、M2 进行敏感性和鲁棒性测试。</span></footer>
        </section></div>
        <div class="focused-workspace-panel" data-workspace-panel="symbols"><section class="focused-template">
          <header class="focused-template-heading"><div><h1>符号表</h1><p>集合、参数与决策变量</p></div><span class="focused-template-status neutral">统一单位</span></header>
          <section class="focused-template-section"><div class="focused-table-wrap"><table class="focused-table focused-template-table symbol-table"><thead><tr><th>符号</th><th>类型</th><th>定义</th><th>单位</th><th>范围</th></tr></thead><tbody><tr><td><strong>i ∈ I</strong></td><td>集合</td><td>运营区域索引</td><td>—</td><td>1…R</td></tr><tr><td><strong>t ∈ T</strong></td><td>集合</td><td>15 分钟时间段</td><td>—</td><td>1…96</td></tr><tr><td><strong>dᵢₜ</strong></td><td>参数</td><td>区域 i 在时段 t 的预测需求</td><td>单</td><td>≥ 0</td></tr><tr><td><strong>cᵢⱼ</strong></td><td>参数</td><td>区域 i 到 j 的单位调度成本</td><td>元/辆</td><td>≥ 0</td></tr><tr><td><strong>Kᵢ</strong></td><td>参数</td><td>区域 i 的容量上限</td><td>辆</td><td>正整数</td></tr><tr><td><strong>xᵢⱼₜ</strong></td><td>变量</td><td>时段 t 从 i 调往 j 的车辆数</td><td>辆</td><td>非负整数</td></tr><tr><td><strong>sᵢₜ</strong></td><td>变量</td><td>时段 t 区域 i 的可用车辆</td><td>辆</td><td>0…Kᵢ</td></tr><tr><td><strong>z</strong></td><td>目标</td><td>总调度成本</td><td>元</td><td>最小化</td></tr></tbody></table></div><footer class="focused-template-footer"><span>8 个核心符号</span><span>符号版本 v1</span></footer></section>
        </section></div>
        <div class="focused-workspace-panel" data-workspace-panel="implementation"><section class="focused-template">
          <header class="focused-template-heading"><div><h1>实现计划</h1><p>数据、算法、求解与验证</p></div><span class="focused-template-status">准备就绪</span></header>
          <section class="focused-template-steps"><article><b>01</b><div><strong>特征构建</strong><span>时间、空间、天气</span></div><em>输入 v2</em></article><article><b>02</b><div><strong>需求预测</strong><span>滚动窗口回测</span></div><em>Python</em></article><article><b>03</b><div><strong>调度求解</strong><span>混合整数规划</span></div><em>求解器</em></article><article><b>04</b><div><strong>结果验证</strong><span>基线与鲁棒性</span></div><em>5 组实验</em></article></section>
          <section class="focused-template-grid"><article class="focused-template-card"><h2>运行环境</h2><dl><div><dt>语言</dt><dd>Python 3.11</dd></div><div><dt>求解器</dt><dd>HiGHS 1.7</dd></div><div><dt>随机种子</dt><dd>42</dd></div></dl></article><article class="focused-template-card"><h2>输出产物</h2><ul><li>需求预测结果</li><li>调度决策表</li><li>实验对比报告</li></ul></article></section>
          <footer class="focused-template-callout"><strong>预计耗时</strong><span>约 12–18 分钟，可在沙盒中复现。</span></footer>
        </section></div>
      </section>`;
  }

  const experiments = [
    ["#12 最优参数探索", "已完成", "MAE 2.31 / RMSE 3.12"],
    ["#11 学习率调整", "已完成", "MAE 2.58 / RMSE 3.47"],
    ["#10 特征增强", "已完成", "MAE 2.85 / RMSE 3.91"],
    ["#9 基线模型", "已完成", "MAE 3.42 / RMSE 4.78"],
    ["#8 特征选择", "运行中 45%", ""],
    ["#7 数据预处理", "失败", ""]
  ];

  const experimentResultPages = {
    charts: {
      title: "结果图表",
      kicker: "图表成果已生成",
      project: "城市共享单车模型效果可视化",
      summary: [
        ["图表范围", "核心指标对比 / 随机种子稳定性 / 分时段表现"],
        ["关键指标", "总行程时间下降 9.38% / 需求满足率提升 6.4pp / 区域平衡度提升 8.32%"],
        ["主要结论", "<ul><li>当前方案在成本、满足率与区域平衡度上均优于基线。</li><li>五组随机种子下目标值波动较小，最大标准差为 1.52%。</li><li>高峰期与平峰期的指标方向一致，未出现效果反转。</li></ul>"],
        ["使用说明", "<ul><li>核心指标对比图可直接用于论文结果章节。</li><li>稳定性图建议放入附录，并保留随机种子与数据版本。</li></ul>"]
      ],
      section: "图表文件",
      files: [
        ["file-image", "核心指标对比图.png", "PNG", "1.21 MB"],
        ["file-image", "随机种子稳定性图.png", "PNG", "846 KB"],
        ["file-pdf", "图表说明与统计口径.pdf", "PDF", "328 KB"]
      ]
    },
    "results-table": {
      title: "结果表",
      kicker: "指标成果已汇总",
      project: "模型关键指标验收汇总",
      summary: [
        ["数据版本", "清洗数据 v2 / Run #04 / seed 42"],
        ["验收结果", "6 项指标全部通过 / 通过率 100% / 指标单位已统一"],
        ["关键变化", "<ul><li>总行程时间由 2,033,414 秒下降至 1,842,596 秒。</li><li>需求满足率由 86.4% 提升至 92.8%。</li><li>平均等待时间由 7.46 分钟下降至 6.71 分钟。</li></ul>"],
        ["口径说明", "<ul><li>所有指标使用相同数据切分、约束条件与运行环境。</li><li>阈值来自模型方案阶段确认的成功标准。</li></ul>"]
      ],
      section: "结果文件",
      files: [
        ["file-xls", "模型关键指标汇总.xlsx", "Excel", "512 KB"],
        ["file-text", "验收阈值与结论.csv", "CSV", "86 KB"],
        ["file-code", "run_04_metrics.json", "JSON", "42 KB"]
      ]
    },
    "run-log": {
      title: "运行日志",
      kicker: "模型运行已完成",
      project: "run_20261001_104233",
      summary: [
        ["运行状态", "退出状态 0 / 总耗时 15 分 28 秒 / 未发现阻断错误"],
        ["运行环境", "Python 3.11 / HiGHS 1.7 / 清洗数据 v2 / seed 42"],
        ["关键节点", "<ul><li>10:27 完成数据加载与 32 个时空特征构建。</li><li>10:39 求解器达到 0.18% 最优间隙。</li><li>10:42 完成稳定性测试、结果表与实验报告生成。</li></ul>"],
        ["复现结论", "<ul><li>数据版本、求解参数、随机种子和输出路径均已记录。</li><li>可使用当前运行 ID 定位全部产物与中间结果。</li></ul>"]
      ],
      section: "日志文件",
      files: [
        ["file-text", "run_20261001_104233.log", "LOG", "184 KB"],
        ["file-code", "solver_trace.json", "JSON", "296 KB"],
        ["file-text", "runtime_environment.txt", "TXT", "12 KB"]
      ]
    },
    "model-code": {
      title: "模型代码",
      kicker: "复现材料已整理",
      project: "需求预测与调度优化代码包",
      summary: [
        ["采用技术", "Python 3.11 / XGBoost / K-means / HiGHS 1.7"],
        ["复现命令", "python solve.py --seed 42 --data data_v2"],
        ["代码组成", "<ul><li>demand.py 负责需求预测与回测。</li><li>solve.py 负责调度模型构建、求解与结果保存。</li><li>validate.py 负责基线、阈值和稳定性检查。</li></ul>"],
        ["复现说明", "<ul><li>依赖版本已锁定，输入与输出目录采用相对路径。</li><li>固定随机种子后，关键指标允许误差不超过 0.5%。</li></ul>"]
      ],
      section: "代码文件",
      files: [
        ["file-py", "solve.py", "Python", "18 KB"],
        ["file-py", "demand.py", "Python", "24 KB"],
        ["file-py", "validate.py", "Python", "11 KB"],
        ["file-text", "requirements.txt", "TXT", "3 KB"],
        ["file-zip", "模型代码与复现数据.zip", "ZIP", "18.63 MB"]
      ]
    }
  };

  const defaultResultActions = [
    ["继续优化", 'data-go="editor"', false],
    ["复制为新任务", 'data-action="copy-task"', false],
    ["下载全部", 'data-action="download-all"', true]
  ];

  function resultDocument(page, key, extraClass = "") {
    const actions = page.actions || defaultResultActions;
    return `<section class="stage-document complete-wrap result-detail-document ${extraClass}" data-result-document="${key}">
      <h1>${page.title}</h1>
      <div class="complete-kicker">${icon("check-circle")} ${page.kicker}</div>
      <h2 class="complete-project-name">${page.project}</h2>
      <div class="result-summary">
        ${page.summary.map(([label, content]) => `<div class="summary-row"><span>${label}</span>${content.startsWith("<") ? content : `<span>${content}</span>`}</div>`).join("")}
      </div>
      <section class="deliverable-section result-detail-deliverables"><h2>${page.section}</h2>
        <div class="deliverables"><div class="deliverable-head"><span>文件名称</span><span>类型</span><span>大小</span><span>操作</span></div>
          ${page.files.map(item => `<div class="deliverable"><span class="deliverable-name">${icon(item[0])}${item[1]}</span><span>${item[2]}</span><span>${item[3]}</span><button class="open-file" data-file="${item[1]}" aria-label="下载 ${item[1]}">${icon("download-simple")}</button></div>`).join("")}
        </div>
      </section>
      <div class="stage-actions complete-actions result-detail-actions">${actions.map(([label, attributes, primary]) => `<button ${primary ? 'class="primary"' : ""} ${attributes}>${label}</button>`).join("")}</div>
    </section>`;
  }

  function experimentResultDocument(key) {
    return resultDocument(experimentResultPages[key], key);
  }

  function experimentsStageContent() {
    return `
      <section class="focused-workspace experiment-report-workspace">
        ${workspaceTabs([["experiment-report","实验报告","file-text"],["charts","结果图表","chart-bar"],["results-table","结果表","clipboard-text"],["run-log","运行日志","terminal-window"],["model-code","模型代码","code"]], "experiment-report")}
        <div class="focused-workspace-panel active" data-workspace-panel="experiment-report">
          <section class="focused-report-card">
            <h1>实验结论</h1>
            <div class="focused-report-conclusion">${icon("check-circle")}<strong>结果通过：</strong><span>模型在关键指标上较基线有所提升，满足题目要求。</span></div>
            <section class="focused-metrics three experiment-metrics"><article><span>当前结果</span><strong>1,842,596</strong><small>总行程时间（秒）</small></article><article><span>基线结果</span><strong>2,033,414</strong><small>总行程时间（秒）</small></article><article><span>改进幅度</span><strong>-9.38%</strong><small>越低越好</small></article></section>
            <section class="focused-experiment-chart"><div class="focused-chart-heading"><h2>核心对比：总行程时间（秒）</h2><div><span><b class="legend-dot baseline"></b>基线结果</span><span><b class="legend-dot current"></b>当前结果</span></div></div><div class="cost-chart-wrap"><canvas id="costChart" aria-label="基线结果与当前结果总行程时间对比柱状图"></canvas></div></section>
            <section class="focused-experiment-notes"><article><h2>稳健性与风险结论</h2><ul><li>${icon("check-circle")} 在 5 个不同随机种子下波动较小，最大标准差 1.52%。</li><li>${icon("check-circle")} 跨时段与区域验证均优于基线，整体性能稳定。</li><li>${icon("check-circle")} 高峰极端工况下模型仍具有良好鲁棒性，建议上线试运行观察。</li></ul></article><article><h2>Agent 最终采用建议</h2><p>建议采用该模型作为当前候选方案，进入提交准备阶段。</p><p>后续可在业务场景验证基础上，持续监控并优化。</p></article></section>
            <footer class="focused-run-meta">运行 ID: run_20261001_104233　|　完成时间：2026-10-01 10:42:33　|　耗时：15 分 28 秒</footer>
          </section>
        </div>
        <div class="focused-workspace-panel" data-workspace-panel="charts">${experimentResultDocument("charts")}</div>
        <div class="focused-workspace-panel" data-workspace-panel="results-table">${experimentResultDocument("results-table")}</div>
        <div class="focused-workspace-panel" data-workspace-panel="run-log">${experimentResultDocument("run-log")}</div>
        <div class="focused-workspace-panel" data-workspace-panel="model-code">${experimentResultDocument("model-code")}</div>
      </section>`;
  }

  function editorStageContent() {
    return `
      <section class="focused-workspace paper-editor-workspace paper-only-workspace">
        <div class="focused-workspace-panel active paper-only-panel">
          <section class="editor-main workflow-editor paper-only-editor">
            <div class="editor-layout">
              <aside class="outline"><div class="outline-heading"><h3>论文大纲</h3>${icon("dots-three-vertical")}</div>${["摘要","1 引言","2 相关工作","3 需求预测模型构建","4 实证分析","5 结果与讨论","6 结论与展望"].map((x,i)=>`<a href="#section-${i}" class="${i===3?"active":""}"><span class="outline-status ${i<3?"done":""}">${i<3?icon("check"):""}</span>${x}</a>`).join("")}</aside>
              <article class="paper-editor">
                <div class="editor-toolbar">
                  <div class="editor-format-tools">
                    <button data-command="undo" aria-label="撤销" title="撤销 (Ctrl+Z)">${icon("arrow-u-up-left")}</button><button data-command="redo" aria-label="重做" title="重做 (Ctrl+Y)">${icon("arrow-u-up-right")}</button><span class="toolbar-divider"></span>
                    <button data-editor-menu="block" aria-label="段落样式" title="段落样式"><span data-editor-label="block">正文</span> ${icon("caret-down")}</button><button data-editor-menu="font" aria-label="字体" title="字体"><span data-editor-label="font">宋体</span> ${icon("caret-down")}</button><button data-editor-menu="size" aria-label="字号" title="字号"><span data-editor-label="size">五号</span> ${icon("caret-down")}</button><span class="toolbar-divider"></span>
                    <button data-command="bold" aria-label="加粗" title="加粗 (Ctrl+B)"><strong>B</strong></button><button data-command="italic" aria-label="斜体" title="斜体 (Ctrl+I)"><i>I</i></button><button data-command="underline" aria-label="下划线" title="下划线 (Ctrl+U)"><u>U</u></button><button data-editor-menu="color" aria-label="文字颜色" title="文字颜色">${icon("text-t")}${icon("caret-down")}</button><span class="toolbar-divider"></span>
                    <button data-editor-menu="align" aria-label="对齐方式" title="对齐方式">${icon("text-align-left")}${icon("caret-down")}</button><button data-action="insert-table" aria-label="插入表格" title="插入 3×3 表格">${icon("table")}</button><button data-action="image" aria-label="插入图片" title="插入本地图片">${icon("image")}</button><button data-action="formula" aria-label="插入公式" title="插入 LaTeX 公式">ƒx</button><button data-action="cite" aria-label="插入引用链接" title="插入来源引用">${icon("link")}</button><span class="toolbar-divider"></span><button data-action="cite" title="插入来源引用">${icon("quotes")} 引用</button>
                  </div>
                  <div class="paper-editor-inline-actions"><button data-action="editor-check">检查</button><button data-action="export-paper">导出</button><button class="primary" data-action="continue-paper">完成交付</button></div>
                </div>
                <div class="editor-page" contenteditable="true" spellcheck="false">
                  <h1>城市共享单车需求预测与调度优化研究</h1>
                  <h2 id="section-3">3 需求预测模型构建</h2>
                  <h3>3.1 问题定义</h3><p>在给定研究区域与时间范围内，基于历史数据与相关影响因素，预测各区域在未来时段的共享单车需求，并制定车辆调度方案，使得调度总成本最小，同时满足各区域的需求平衡约束。</p>
                  <h3>3.2 特征设计</h3><p>本文从时间、空间、天气和社会活动四个维度构建特征体系。时间维度包括小时、星期、节假日等；<mark>空间维度包括区域类型、POI 密度、周边地铁站距离等；</mark>天气维度包括温度、降水、风速等；社会活动维度包括大型活动、演出、赛事等。</p>
                  <button class="source-chip" contenteditable="false" data-action="source-detail" title="点击引用到左侧对话，直接提问或要求修改">来源：Run #04 · 结果表 2　${icon("arrow-square-out")}</button>
                  <h3>3.3 模型设定</h3><p>采用基于图卷积网络（GCN）的时空预测模型，结合区域间拓扑关系与动态特征，捕捉需求的时空相关性。</p><p>模型目标函数如下：</p>
                  <div class="editor-formula" data-tex="\\min\\;\\sum_{i=1}^{N}\\sum_{t=1}^{T}\\left(y_{it}-\\hat{y}_{it}\\right)^{2}+\\lambda\\lVert\\Theta\\rVert_{2}^{2}" contenteditable="false" title="点击编辑公式"><em>min</em>　∑<sub>i=1</sub><sup>N</sup> ∑<sub>t=1</sub><sup>T</sup> (y<sub>it</sub> − ŷ<sub>it</sub>)² + λ‖Θ‖²<sub>2</sub></div>
                  <p>其中，y<sub>it</sub> 表示区域 i 在时段 t 的真实需求，ŷ<sub>it</sub> 表示模型预测值，Θ 为模型参数，λ 为正则化系数。</p>
                </div>
                <input type="file" accept="image/*" hidden data-editor-image-input>
                <footer class="editor-statusbar">
                  <span data-editor-wordcount>0 字</span>
                  <div class="editor-statusbar-actions">
                    <span data-editor-savestate>编辑内容自动保存到本机</span>
                    <button type="button" data-action="paper-save-now" title="立即保存本机草稿 (Ctrl+S)">立即保存</button>
                    <button type="button" data-action="paper-reset-draft" title="丢弃本机草稿，恢复打开时的正文">恢复初始正文</button>
                  </div>
                </footer>
              </article>
            </div>
          </section>
        </div>
      </section>`;
  }

  // 2MB 的赛题库改由 preloadKnowledgeLibrary 原地填充，使它能拆成独立 chunk 而不进主包。
  // 保持同一个数组引用，下游的 map/find/length 调用无需改动。
  const problems = [];
  const papers = [];
  // 页签顺序：国赛、美赛固定排最前（产品要求），其余分类按数据出现顺序跟在后面。
  const problemTabs = () => {
    const pinned = ["国赛", "美赛"];
    const seen = [...new Set(problems.map(problem => problem.category))];
    return [
      "全部赛题",
      ...pinned.filter(category => seen.includes(category)),
      ...seen.filter(category => !pinned.includes(category)),
      "收藏",
    ];
  };
  // 右上角四个筛选不再是装饰按钮：选项由已加载的赛题数据现算。
  // 比赛/问题类型/建模方向按数据出现顺序，年份新在前；value 为空代表「全部」不过滤。
  const problemFilterGroups = () => [
    { field: "competition", label: "比赛", allLabel: "全部比赛", options: [...new Set(problems.map(problem => problem.competition))].map(value => [value, value]) },
    { field: "year", label: "年份", allLabel: "全部年份", options: [...new Set(problems.map(problem => String(problem.year)))].sort((a, b) => Number(b) - Number(a)).map(value => [value, `${value} 年`]) },
    { field: "type", label: "问题类型", allLabel: "全部类型", options: [...new Set(problems.map(problem => problem.problem_type))].map(value => [value, value]) },
    { field: "direction", label: "建模方向", allLabel: "全部方向", options: [...new Set(problems.flatMap(problem => problem.modeling_directions || []))].map(value => [value, value]) },
  ];
  // 列表里放不下"含本地原题 PDF及 353 个随题附件"这种全句，压缩成"原题 PDF · 353 附件"；
  // 完整描述仍在详情页展示。
  const problemDataSummary = problem => {
    const requirement = String(problem.data_requirement || "");
    if (!requirement.includes("PDF")) return requirement || "—";
    const attachmentCount = /(\d+)\s*个/.exec(requirement)?.[1];
    return attachmentCount ? `原题 PDF · ${attachmentCount} 附件` : "原题 PDF";
  };
  const remotePaperPdfUrl = paper => {
    const source = String(paper?.full_text_url || "").trim();
    if (!source || !/\.pdf(?:$|[?#])/i.test(source)) return "";
    if (source.startsWith("/")) return source;
    try {
      const url = new URL(source);
      if (url.hostname.toLowerCase() !== "github.com") return source;
      const parts = url.pathname.split("/").filter(Boolean);
      if (parts.length < 6 || parts[2] !== "blob") return "";
      const [owner, repository, , revision, ...pathParts] = parts;
      return `https://raw.githubusercontent.com/${owner}/${repository}/${revision}/${pathParts.join("/")}`;
    } catch {
      return "";
    }
  };
  const localPaperPdfUrl = paper => {
    const source = String(paper?.full_text_url || "").trim();
    if (!source || !/\.pdf(?:$|[?#])/i.test(source)) return "";
    if (source.startsWith("/")) return source;
    try {
      const url = new URL(source, window.location.origin);
      const decodedPath = decodeURIComponent(url.pathname);
      // 美赛论文（Jackksonns 快照）：开发中转路由 /paper-files/mcm/<年>/<题组|X>/<控制号>.pdf；
      // 2013–2015 源路径没有题组目录，用 X 占位。
      const mcmMatch = /^\/Jackksonns\/MCM-ICM-Outstanding-Papers\/blob\/[0-9a-f]{40}\/(\d{4})\/(?:([A-F])\/)?(\d{4,8}\.pdf)$/i.exec(decodedPath);
      if (url.hostname.toLowerCase() === "github.com" && mcmMatch) {
        return `/paper-files/mcm/${mcmMatch[1]}/${(mcmMatch[2] || "X").toUpperCase()}/${encodeURIComponent(mcmMatch[3])}`;
      }
      const segments = decodedPath.split("/").filter(Boolean);
      const yearIndex = segments.findIndex(segment => /^\d{4}年优秀论文$/.test(segment));
      const year = yearIndex >= 0 ? segments[yearIndex].slice(0, 4) : "";
      const filename = segments.at(-1) || "";
      const sourceDirectory = segments[yearIndex + 1] || "";
      const problemGroup = /^[A-F]$/i.test(sourceDirectory)
        ? sourceDirectory.toUpperCase()
        : (/^[A-F]/i.exec(filename)?.[0].toUpperCase() || "");
      if (!year || !problemGroup || !/\.pdf$/i.test(filename)) return "";
      return `/paper-files/${year}/${problemGroup}/${encodeURIComponent(filename)}`;
    } catch {
      return "";
    }
  };
  // raw.githubusercontent.com 在部分网络/代理下会超时或被劫持，pdf.js 拿到残缺字节流
  // 会把论文渲染成乱码页；jsDelivr 提供同一提交的字节级镜像，作为直连 raw 前的兜底源。
  // 各镜像域名的 DNS 解析在部分网络下会间歇失效，官方备用域一并列入候选。
  const jsDelivrPaperPdfUrls = paper => {
    const source = String(paper?.full_text_url || "").trim();
    if (!source || !/\.pdf(?:$|[?#])/i.test(source)) return [];
    try {
      const url = new URL(source);
      if (url.hostname.toLowerCase() !== "github.com") return [];
      const parts = url.pathname.split("/").filter(Boolean);
      if (parts.length < 6 || parts[2] !== "blob") return [];
      const [owner, repository, , revision, ...pathParts] = parts;
      return ["cdn.jsdelivr.net", "gcore.jsdelivr.net", "testingcf.jsdelivr.net"].map(
        host => `https://${host}/gh/${owner}/${repository}@${revision}/${pathParts.join("/")}`,
      );
    } catch {
      return [];
    }
  };
  const paperPdfSources = paper => [...new Set([
    localPaperPdfUrl(paper),
    ...jsDelivrPaperPdfUrls(paper),
    remotePaperPdfUrl(paper),
  ].filter(Boolean))];
  const paperPdfUrl = paper => paperPdfSources(paper)[0] || "";
  const archivedPaperTopics = {
    "2021-A": "相关矩阵组的低复杂度计算和存储",
    "2021-B": "空气质量二次预报",
    "2021-C": "帕金森病的脑深部电刺激治疗",
    "2021-D": "抗乳腺癌候选药物优化",
    "2021-E": "信号干扰下的 UWB 精确定位",
    "2021-F": "航空公司机组优化排班",
    "2022-A": "移动场景超分辨定位",
    "2022-B": "方形件排样优化与订单组批",
    "2022-C": "汽车制造涂装-总装缓存区调度",
    "2022-D": "PISA 架构芯片资源排布",
    "2022-E": "草原放牧策略",
    "2022-F": "疫情期间生活物资科学管理",
  };
  const paperIdentifier = paper => String(paper.team_id || paper.title || "论文编号待补充");
  const paperGroup = paper => {
    const codeMatch = /(?:^|\s)([A-F])$/i.exec(String(paper.problem_code || ""));
    return (codeMatch?.[1] || /^[A-F]/i.exec(paperIdentifier(paper))?.[0] || "—").toUpperCase();
  };
  const paperProblem = paper => problems.find(problem => problem.id === paper.problem_id);
  const paperDisplayTitle = paper => {
    const rawTitle = String(paper.title || "").trim();
    const looksLikeIdentifier = !rawTitle || rawTitle === paperIdentifier(paper) || /^[A-F]?\d{8,}$/i.test(rawTitle);
    const archivedTopic = archivedPaperTopics[`${paper.year}-${paperGroup(paper)}`];
    return looksLikeIdentifier ? (paperProblem(paper)?.title || archivedTopic || `${paper.year} 年 ${paperGroup(paper)} 题获奖论文`) : rawTitle;
  };
  // 奖项名本身看不出比赛（"优秀论文" 是研究生赛的、"Outstanding Winner" 是美赛的），
  // 所以比赛必须作为独立的第一维度显式给出，而不是让读者从奖项去反推。
  const paperCompetitionKey = paper => {
    const competition = String(paper.competition || "").trim();
    if (competition === "COMAP MCM/ICM") return "美赛";
    if (competition === "中国研究生数学建模竞赛") return "研究生赛";
    return competition || "其他";
  };
  const PAPER_COMPETITION_LABELS = {
    "研究生赛": "研究生赛（华为杯）",
    "美赛": "美赛（MCM/ICM）",
  };
  const paperCompetitionLabel = key => PAPER_COMPETITION_LABELS[key] || key;
  // 美赛的 A/B/C 属 MCM、D/E/F 属 ICM，选中美赛后按真实赛别命名题组；
  // 2013–2015 的美赛快照源目录没有题组层级，这批论文的题组只能留空。
  const paperGroupLabel = (competition, group) => {
    if (!group || group === "—") return "未标注题组";
    if (competition === "美赛") return `${"ABC".includes(group) ? "MCM" : "ICM"} ${group}`;
    return `${group} 题`;
  };
  const paperAwardLabel = paper => String(paper.award || paper.category || "").trim();
  const paperEntries = () => papers
    .map((paper, index) => ({
      paper,
      index,
      problem: paperProblem(paper),
      displayTitle: paperDisplayTitle(paper),
      group: paperGroup(paper),
      competition: paperCompetitionKey(paper),
      award: paperAwardLabel(paper),
    }))
    .filter(({ paper }) => paper.record_type === "paper" && paper.access_scope === "linked_content" && paperPdfUrl(paper));
  const paperCompetitionTabs = () => {
    const pinned = ["研究生赛", "美赛"];
    const seen = [...new Set(paperEntries().map(({ competition }) => competition))];
    return [...pinned.filter(key => seen.includes(key)), ...seen.filter(key => !pinned.includes(key))];
  };
  const paperAwardTabs = () => {
    const awards = [...new Set(paperEntries().map(({ award }) => award))].filter(Boolean);
    const pinned = ["优秀论文", "Outstanding Winner", "数模之星提名奖"];
    return ["全部", ...pinned.filter(award => awards.includes(award)), ...awards.filter(award => !pinned.includes(award))];
  };
  // 奖项/题组/年份都只存在于某一个比赛下，渲染时把各选项的归属比赛写进 data-paper-scope，
  // 切换比赛后由绑定层据此隐藏无关选项，避免出现必定为空的组合。
  const paperOptionScopes = pick => {
    const scopes = new Map();
    paperEntries().forEach(entry => {
      const key = pick(entry);
      if (!scopes.has(key)) scopes.set(key, new Set());
      scopes.get(key).add(entry.competition);
    });
    return scopes;
  };
  // 页码按钮由 applyResourceFilters 按“筛选后”的条数现算，所以这里不再预渲染：
  // 搜索或切换分类之后总页数会变，静态渲染出来的 1…N 只会是假的。
  const RESOURCE_PAGE_SIZE = 15;

  function problemsScreen() {
    return shell(`
      <section class="library-main resource-library problems-main">
        <div class="library-heading"><h1>赛题库</h1><p>浏览历年赛题、问题类型与建模方向。</p></div>
        <div class="library-tools resource-tools"><label class="search-box">${icon("magnifying-glass")}<input type="search" name="problem-search" data-problem-search autocomplete="off" aria-label="搜索赛题" placeholder="搜索赛题、领域或关键词"></label>
          <div class="filters">${problemFilterGroups().map(({ field, label, allLabel, options }) => `<div class="problem-filter-select" data-problem-filter="${field}" data-select-menu>
            <button type="button" class="filter-button" data-select-trigger aria-haspopup="listbox" aria-expanded="false" aria-label="按${label}筛选赛题"><span data-select-label>${label}</span>${icon("caret-down")}</button>
            <div class="settings-select-menu" role="listbox" aria-label="按${label}筛选赛题">
              ${[["", allLabel], ...options].map(([value, text], index) => `<button type="button" role="option" data-select-option="${escapeHtml(value)}" aria-selected="${index === 0}"><span>${escapeHtml(text)}</span>${icon("check")}</button>`).join("")}
            </div>
          </div>`).join("")}</div>
        </div>
        <div class="resource-tabs" role="tablist" aria-label="赛题分类">
          ${problemTabs().map((x,i)=>`<button class="${i===0?"active":""}" data-resource-tab="${escapeHtml(x)}" data-resource-kind="problem">${x==="收藏"?icon("star"):""}${escapeHtml(x)}</button>`).join("")}
        </div>
        <div class="resource-table-wrap">
          <table class="resource-table problem-resource-table">
            <thead><tr><th>题目</th><th>年份</th><th>问题类型</th><th>数据附件</th><th>收藏</th></tr></thead>
            <tbody data-problem-list>
              ${problems.map((p,i)=>`<tr class="problem-item" data-resource-index="${i}" data-resource-category="${escapeHtml(p.category)}" data-problem-competition="${escapeHtml(p.competition)}" data-problem-year="${p.year}" data-problem-type="${escapeHtml(p.problem_type)}" data-problem-directions="${escapeHtml((p.modeling_directions || []).join("|"))}" data-resource-search="${escapeHtml([p.code,p.title,p.competition,p.category,p.year,p.problem_type,...p.keywords,...p.modeling_directions].join(" "))}" data-saved="false" tabindex="0" role="link" aria-label="查看赛题：${escapeHtml(p.code)} ${escapeHtml(p.title)}">
                <td><div class="resource-title-cell"><div class="problem-title-copy"><strong>${escapeHtml(p.title)}</strong><span>${escapeHtml(p.code)} · ${escapeHtml(p.competition)}</span></div></div></td>
                <td class="problem-year-cell">${p.year}</td>
                <td><span class="problem-type-badge">${escapeHtml(p.problem_type)}</span></td>
                <td class="problem-data-cell">${escapeHtml(problemDataSummary(p))}</td>
                <td><button class="row-star" data-action="resource-bookmark" aria-label="收藏 ${escapeHtml(p.code)}">${icon("star")}</button></td>
              </tr>`).join("")}
              <tr class="paper-empty-row" data-problem-empty hidden><td colspan="5">${icon("magnifying-glass")}<strong>没有符合条件的赛题</strong><span>调整筛选、分类或搜索词后再试。</span></td></tr>
            </tbody>
          </table>
        </div>
        <div class="resource-footer">
          <span data-resource-page-copy>共 ${problems.length} 题 · 第 1 页</span>
          <div class="resource-pagination" data-resource-pagination>
            <button data-resource-page="prev" aria-label="上一页">${icon("caret-left")}</button>
            <span data-resource-page-numbers></span>
            <button data-resource-page="next" aria-label="下一页">${icon("caret-right")}</button>
          </div>
          <button class="page-size-button" data-action="page-size">15 条/页 ${icon("caret-down")}</button>
        </div>
      </section>`, "problems");
  }

  function papersScreen() {
    const entries = paperEntries();
    const competitions = paperCompetitionTabs();
    const yearScopes = paperOptionScopes(({ paper }) => String(paper.year));
    const awardScopes = paperOptionScopes(({ award }) => award);
    const groupScopes = paperOptionScopes(({ group }) => group);
    const years = [...yearScopes.keys()].sort((a, b) => Number(b) - Number(a));
    const groups = [...groupScopes.keys()].filter(group => group !== "—").sort();
    const scopeAttr = (scopes, key) => escapeHtml([...(scopes.get(key) || [])].join(" "));
    return shell(`
      <section class="library-main resource-library papers-main">
        <div class="library-heading paper-library-heading"><div><h1>优秀论文</h1><p>先选比赛，再按年份与题组浏览获奖论文，点击即可阅读完整正文。</p></div><span class="paper-library-total"><strong>${entries.length}</strong> 篇完整论文</span></div>
        <div class="paper-library-controls"><label class="search-box">${icon("magnifying-glass")}<input type="search" name="paper-search" data-paper-search autocomplete="off" aria-label="搜索论文" placeholder="搜索研究主题、论文编号或关键词"></label>
          <div class="paper-year-filter" data-paper-year-select data-select-menu><span>年份</span><button type="button" class="paper-year-trigger" data-select-trigger aria-haspopup="listbox" aria-expanded="false" aria-label="按年份筛选论文"><span data-select-label>全部年份</span></button>${icon("caret-down")}
            <div class="settings-select-menu" role="listbox" aria-label="按年份筛选论文">
              <button type="button" role="option" data-select-option="" aria-selected="true"><span>全部年份</span>${icon("check")}</button>
              ${years.map(year=>`<button type="button" role="option" data-select-option="${escapeHtml(year)}" data-paper-scope="${scopeAttr(yearScopes, year)}" aria-selected="false"><span>${escapeHtml(year)} 年</span>${icon("check")}</button>`).join("")}
            </div>
          </div>
          <button class="paper-filter-reset" type="button" data-paper-filter-reset>${icon("arrow-counter-clockwise")} 重置</button>
        </div>
        <div class="paper-classification-panel" aria-label="论文分类筛选">
          <div class="paper-classification-row"><span class="paper-classification-label">比赛</span><div class="paper-competition-tabs" role="group" aria-label="按比赛筛选">
            <button class="active" type="button" data-paper-competition-filter="">全部比赛</button>${competitions.map(key=>`<button type="button" data-paper-competition-filter="${escapeHtml(key)}">${escapeHtml(paperCompetitionLabel(key))}</button>`).join("")}
          </div></div>
          <div class="paper-classification-row"><span class="paper-classification-label">奖项</span><div class="resource-tabs paper-resource-tabs" role="tablist" aria-label="按奖项分类">
            ${paperAwardTabs().map((x,i)=>`<button class="${i===0?"active":""}" data-resource-tab="${escapeHtml(x)}" data-resource-kind="paper"${i===0?"":` data-paper-scope="${scopeAttr(awardScopes, x)}"`}>${escapeHtml(x)}</button>`).join("")}
          </div></div>
          <div class="paper-classification-row"><span class="paper-classification-label">题组</span><div class="paper-group-tabs" role="group" aria-label="按题组筛选">
            <button class="active" type="button" data-paper-group-filter="">全部题组</button>${groups.map(group=>`<button type="button" data-paper-group-filter="${escapeHtml(group)}" data-paper-scope="${scopeAttr(groupScopes, group)}">${escapeHtml(paperGroupLabel("", group))}</button>`).join("")}${groupScopes.has("—")?`<button type="button" data-paper-group-filter="—" data-paper-scope="${scopeAttr(groupScopes, "—")}">未标注题组</button>`:""}
          </div><span class="paper-result-count" data-paper-result-copy>${entries.length} 篇</span></div>
        </div>
        <div class="resource-table-wrap paper-resource-wrap">
          <table class="resource-table paper-resource-table">
            <thead><tr><th>研究主题与论文编号</th><th>比赛与题组</th><th>奖项</th><th>正文</th><th>收藏</th></tr></thead>
            <tbody data-paper-list>
              ${entries.map(({ paper: p, index: sourceIndex, problem, displayTitle, group, competition, award })=>`<tr class="paper-item" data-resource-index="${sourceIndex}" data-resource-category="${escapeHtml(award)}" data-paper-competition="${escapeHtml(competition)}" data-paper-year="${p.year}" data-paper-group="${escapeHtml(group)}" data-resource-search="${escapeHtml([displayTitle,p.title,p.team_id,p.problem_code,competition,p.competition,p.year,award,p.institution,problem?.problem_type,...(problem?.keywords || []),...p.distinctions,...p.models].filter(Boolean).join(" "))}" data-saved="false" tabindex="0" role="link" aria-label="阅读论文：${escapeHtml(displayTitle)}，编号 ${escapeHtml(paperIdentifier(p))}">
                <td><div class="paper-primary-cell"><strong>${escapeHtml(displayTitle)}</strong><span>论文编号 ${escapeHtml(paperIdentifier(p))}　·　${escapeHtml(p.problem_code)}</span></div></td>
                <td><div class="paper-topic-cell"><span class="paper-competition-badge">${escapeHtml(competition)}</span>${group === "—" ? "" : `<span class="paper-group-badge">${escapeHtml(paperGroupLabel(competition, group))}</span>`}<span>${escapeHtml(problem?.problem_type || "")}</span></div></td>
                <td><span class="paper-award-badge">${escapeHtml(award)}</span></td>
                <td><div class="paper-access-cell">${icon("file-pdf")}<span><strong>完整 PDF</strong><small>${escapeHtml(formatFileSize(p.source_file_bytes) || "在线阅读")}</small></span></div></td>
                <td><button class="row-star" data-action="resource-bookmark" aria-label="收藏 ${escapeHtml(displayTitle)}">${icon("star")}</button></td>
              </tr>`).join("")}
              <tr class="paper-empty-row" data-paper-empty hidden><td colspan="5">${icon("magnifying-glass")}<strong>没有符合条件的论文</strong><span>调整比赛、年份、题组或搜索词后再试。</span></td></tr>
            </tbody>
          </table>
        </div>
        <div class="resource-footer paper-resource-footer">
          <span data-resource-page-copy>共 ${entries.length} 篇 · 第 1 页</span>
          <div class="resource-pagination" data-resource-pagination>
            <button data-resource-page="prev" aria-label="上一页">${icon("caret-left")}</button>
            <span data-resource-page-numbers></span>
            <button data-resource-page="next" aria-label="下一页">${icon("caret-right")}</button>
          </div>
          <span class="paper-sort-note">按年份从新到旧排列</span>
        </div>
      </section>`, "papers");
  }

  function selectedResource(items) {
    const value = Number(new URLSearchParams(window.location.search).get("index"));
    const index = Number.isInteger(value) && value >= 0 && value < items.length ? value : 0;
    return { item: items[index], index };
  }

  function renderProblemContentBlock(block, index) {
    if (block.type === "heading") {
      const level = Math.min(4, Math.max(2, Number(block.level) + 1));
      return `<h${level} class="problem-content-heading">${escapeHtml(block.text)}</h${level}>`;
    }
    if (block.type === "paragraph") {
      const lead = block.lead ? `<strong>${escapeHtml(block.lead)}</strong> ` : "";
      return `<p class="problem-content-paragraph">${lead}${escapeHtml(block.text).replace(/\n/g, "<br>")}</p>`;
    }
    if (block.type === "list_item") {
      // 采集层把原文的序号（"1."、"a)"、"①"）单独存进 marker，正文里已经不含它。
      // 有序号就照原样显示，序号本身有意义；只有原文用圆点时才由这里补一个。
      const marker = String(block.marker || "").trim() || "•";
      return `<div class="problem-content-list-item"><span class="problem-list-marker" aria-hidden="true">${escapeHtml(marker)}</span><p>${escapeHtml(block.text).replace(/\n/g, "<br>")}</p></div>`;
    }
    if (block.type === "code") {
      // 原题把附件的文件格式示例排在一个方框里，各列是靠空格对齐的，
      // 拆成普通段落就读不出对应关系，所以整段按原样保留换行和空格。
      return `<pre class="problem-content-code">${escapeHtml(block.text)}</pre>`;
    }
    if (block.type === "image") {
      return `<figure class="problem-content-figure"><img src="${escapeHtml(block.src)}" alt="${escapeHtml(block.alt)}" loading="lazy" data-problem-asset="${index}"></figure>`;
    }
    if (block.type === "table") {
      // 竞赛题面常要求"将结果填入表 1"，所以原题里的答题模板本就是空表体。
      // 空单元格标记出来，靠 CSS 保住行高，读者才不会当成渲染失败。
      const cells = (row, rowIndex) => row.map(cell => {
        const tag = rowIndex === 0 ? "th" : "td";
        const text = String(cell ?? "").trim();
        const attr = text ? "" : ' class="is-blank"';
        return `<${tag}${attr}>${escapeHtml(text).replace(/\n/g, "<br>")}</${tag}>`;
      }).join("");
      return `<div class="problem-content-table-wrap"><table class="problem-content-table"><tbody>${block.rows.map((row, rowIndex) => `<tr>${cells(row, rowIndex)}</tr>`).join("")}</tbody></table></div>`;
    }
    if (block.type === "document_break") {
      return `<div class="problem-document-break"><span>题面附录</span><h2>${escapeHtml(block.title)}</h2></div>`;
    }
    return "";
  }

  function completeProblemMarkup(problem) {
    const blocks = Array.isArray(problem.content_blocks) ? problem.content_blocks : [];
    if (!blocks.length) return "";
    return `
      <section class="problem-full-content" aria-label="完整赛题正文">
        ${blocks.map(renderProblemContentBlock).join("")}
      </section>`;
  }

  function problemDetailScreen() {
    const { item: problem } = selectedResource(problems);
    const completeMarkup = completeProblemMarkup(problem);
    // 同 isOfficialSourceUrl 的理由：附件只有两种可以渲染成链接——我们自己镜像到
    // /problem-files 下的本地副本，和主办方域名上的原件。社区仓库里的 .docx 既不是
    // 官方发布物，也会把用户送到第三方账号下，所以整条不渲染。
    const linkableAttachments = problem.attachments.filter(item => {
      const url = String(item.url || "");
      return url.startsWith("/") || isOfficialSourceUrl(url);
    });
    const attachmentMarkup = linkableAttachments.length
      ? `<p class="problem-downloads">${linkableAttachments.map(item => {
          const local = String(item.url).startsWith("/");
          const size = formatFileSize(item.bytes);
          return `<a class="problem-download-link" href="${escapeHtml(item.url)}" ${local ? "download" : 'target="_blank" rel="noreferrer"'}>${escapeHtml(item.title)}</a>${size ? `<span class="problem-download-size">${escapeHtml(size)}</span>` : ""}`;
        }).join('<span class="problem-download-sep">、</span>')}</p>`
      : "";
    return shell(`
      <section class="resource-detail-page problem-detail-page">
        <div class="resource-detail-breadcrumb"><a href="${routes.problems}">赛题库</a><span>/</span><strong>查看赛题</strong></div>
        <article class="resource-detail-article">
          <header class="resource-detail-title">
            <h1>${problem.year} · ${escapeHtml(problem.competition)} · ${escapeHtml(problem.code)}</h1>
            <h2>题目：${escapeHtml(problem.title)}</h2>
          </header>
          <div class="resource-detail-rule"></div>
          ${completeMarkup || `<section class="detail-copy-section problem-metadata-fallback">
            <h3>题面采集记录</h3>
            <p>${escapeHtml(problem.summary)}</p>
            <p><strong>问题类型：</strong>${escapeHtml(problem.problem_type)}　<strong>建模方向：</strong>${escapeHtml(problem.modeling_directions.join("、"))}</p>
            <p><strong>关键词：</strong>${escapeHtml(problem.keywords.join("、"))}</p>
            <p><strong>数据要求：</strong>${escapeHtml(problem.data_requirement)}</p>
          </section>`}
          ${attachmentMarkup}
        </article>
        <footer class="resource-detail-actions problem-detail-actions">
          <div class="detail-action-buttons">
            <button type="button" data-action="detail-bookmark">${icon("star")} 收藏</button>
            ${isOfficialSourceUrl(problem.source_url)
              ? `<button type="button" data-action="open-source" data-source-url="${escapeHtml(problem.source_url)}">${icon("arrow-square-out")} 查看来源</button>`
              : ""}
            <button class="primary" type="button" data-action="use-problem" data-resource-title="${escapeHtml(problem.title)}">用于当前任务</button>
          </div>
        </footer>
      </section>`, "problems");
  }

  function selectedPaperResource() {
    const requested = Number(new URLSearchParams(window.location.search).get("index"));
    if (Number.isInteger(requested) && requested >= 0 && requested < papers.length && paperPdfUrl(papers[requested])) {
      return { item: papers[requested], index: requested };
    }
    const fallback = paperEntries()[0];
    return fallback ? { item: fallback.paper, index: fallback.index } : selectedResource(papers);
  }

  function paperDetailScreen() {
    const { item: paper } = selectedPaperResource();
    const pdfSources = paperPdfSources(paper);
    const pdfUrl = pdfSources[0] || "";
    const displayTitle = paperDisplayTitle(paper);
    const sizeCopy = paper.source_file_bytes ? ` · ${formatFileSize(paper.source_file_bytes)}` : "";
    return shell(`
      <section class="resource-detail-page paper-detail-page">
        <div class="resource-detail-breadcrumb"><a href="${routes.papers}">优秀论文</a><span>/</span><strong>查看论文</strong></div>
        <article class="resource-detail-article paper-reading-article">
          <header class="resource-detail-title paper-reading-title">
            <h1>${escapeHtml(displayTitle)}</h1>
            <h2>论文编号 ${escapeHtml(paperIdentifier(paper))}　·　${escapeHtml(paper.problem_code)}　·　${escapeHtml(paper.award)}${paper.institution ? `　·　${escapeHtml(paper.institution)}` : ""}</h2>
          </header>
          <div class="resource-detail-rule"></div>
          <section class="paper-fulltext-section" aria-label="完整论文正文">
            <div class="paper-fulltext-toolbar">
              <div><strong>${icon("file-pdf")} 完整论文正文</strong><span data-paper-page-copy>${paper.page_count ? `${paper.page_count} 页` : "正在读取页数"}${escapeHtml(sizeCopy)}</span></div>
              <a href="${escapeHtml(pdfUrl)}" target="_blank" rel="noreferrer">在新窗口打开 ${icon("arrow-square-out")}</a>
            </div>
            <div class="paper-pdf-reader" data-paper-pdf-reader data-paper-pdf-sources="${escapeHtml(JSON.stringify(pdfSources))}" aria-live="polite" aria-busy="true">
              <div class="paper-pdf-loading"><span></span><strong>正在加载完整论文正文</strong><small>读取 PDF 页面与图表…</small></div>
            </div>
            <p class="paper-pdf-fallback">若浏览器未显示内嵌正文，可<a href="${escapeHtml(pdfUrl)}" target="_blank" rel="noreferrer">直接打开完整 PDF</a>。</p>
          </section>
        </article>
        <footer class="resource-detail-actions">
          <div class="detail-action-buttons detail-left-actions">
            <button type="button" data-action="detail-bookmark">${icon("star")} 收藏</button>
            <button type="button" data-action="cite-detail" data-resource-title="${escapeHtml(paper.title)}" data-source-url="${isOfficialSourceUrl(paper.source_url) ? escapeHtml(paper.source_url) : ""}">${icon("quotes")} 引用</button>
            <button type="button" data-action="open-source" data-source-url="${escapeHtml(pdfUrl)}">${icon("book-open")} 打开完整 PDF</button>
          </div>
            <button class="primary" type="button" data-action="use-paper" data-resource-title="${escapeHtml(displayTitle)}">${icon("git-branch")} 参考该论文</button>
        </footer>
      </section>`, "papers");
  }

  const methodListMarkup = items => `<ul class="method-copy-list">${items.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;

  const METHOD_FAVORITES_KEY = "openmathmodelMethodFavorites";
  const METHOD_COMPARE_LIMIT = 3;
  let methodCompareIds = [];
  // 由 bindScreen("methods") 注入，使收藏动作能同时刷新侧栏筛选结果。
  let refreshMethodFavoriteUi = () => {};

  const readMethodFavorites = () => {
    try {
      const raw = JSON.parse(localStorage.getItem(METHOD_FAVORITES_KEY) || "[]");
      return Array.isArray(raw) ? raw.filter(id => typeof id === "string") : [];
    } catch {
      return [];
    }
  };
  const writeMethodFavorites = ids => {
    try {
      localStorage.setItem(METHOD_FAVORITES_KEY, JSON.stringify([...new Set(ids)]));
    } catch {
      // 隐私模式或存储配额耗尽时降级为仅本次会话生效，不阻断交互。
    }
  };
  const isMethodFavorite = id => readMethodFavorites().includes(id);
  const toggleMethodFavorite = id => {
    const favorites = readMethodFavorites();
    const next = favorites.includes(id) ? favorites.filter(item => item !== id) : [...favorites, id];
    writeMethodFavorites(next);
    return next.includes(id);
  };

  const METHOD_LANGUAGE_KEY = "openmathmodelMethodLanguage";
  const readMethodLanguage = () => {
    try {
      const saved = localStorage.getItem(METHOD_LANGUAGE_KEY);
      return RECIPE_LANGUAGES.some(lang => lang.id === saved) ? saved : "python";
    } catch {
      return "python";
    }
  };

  /**
   * KaTeX 排版统一走 text/math-typeset：懒加载 + 结果缓存 + 就绪后同步渲染，
   * 流式对话的实时排版与方法库、论文编辑器共享同一份模块与缓存。
   */
  function renderFormulas(scope = document) {
    typesetMath(scope);
  }

  const codeBlockMarkup = (entryId, recipe) => {
    const active = readMethodLanguage();
    return `<div class="code-block" data-code-block="${escapeHtml(entryId)}">
      <div class="code-tabs" role="tablist">
        ${RECIPE_LANGUAGES.map(lang => `<button type="button" role="tab" data-code-lang="${lang.id}" aria-selected="${lang.id === active}" class="${lang.id === active ? "active" : ""}">${escapeHtml(lang.label)}</button>`).join("")}
      </div>
      ${RECIPE_LANGUAGES.map(lang => `<pre class="code-snippet" data-code-panel="${lang.id}"${lang.id === active ? "" : " hidden"}><code>${escapeHtml(recipe.code[lang.id])}</code></pre>`).join("")}
    </div>`;
  };

  const methodPitfallMarkup = items => `<ul class="method-copy-list method-pitfall-list">${items.map(item => {
    const [symptom, fix] = item.split("；修正：");
    return fix
      ? `<li><span class="pitfall-symptom">${escapeHtml(symptom)}</span><span class="pitfall-fix">${icon("wrench")}${escapeHtml(fix)}</span></li>`
      : `<li>${escapeHtml(item)}</li>`;
  }).join("")}</ul>`;

  function methodDetailMarkup(entry) {
    const relatedProblems = entry.relatedProblemIds
      .map(id => ({ item: problems.find(problem => problem.id === id), index: problems.findIndex(problem => problem.id === id) }))
      .filter(({ item, index }) => item && index >= 0);
    const relatedPapers = relatedProblems
      .map(({ item: problem }) => ({ item: papers.find(paper => paper.problem_id === problem.id), index: papers.findIndex(paper => paper.problem_id === problem.id) }))
      .filter(({ item, index }, position, collection) => item && index >= 0 && collection.findIndex(candidate => candidate.index === index) === position);
    const resourceLinks = (resources, route, label) => resources.length
      ? `<div class="related-resource-list">${resources.map(({ item, index }) => `<a class="related-resource-link" href="${route}?index=${index}"><strong>${escapeHtml(label(item))}</strong>　${escapeHtml(item.title)}　›</a>`).join("")}</div>`
      : `<span class="method-empty">资源采集队列中</span>`;

    const favorite = isMethodFavorite(entry.id);
    const comparing = methodCompareIds.includes(entry.id);
    const recipe = methodRecipes[entry.id];

    return `
      <div class="method-heading">
        <div>
          <div class="method-category-tag">${escapeHtml(entry.category)}</div>
          <h1 data-method-title>${escapeHtml(entry.name)}</h1>
          <div class="method-sub">${escapeHtml(entry.subtitle)}</div>
        </div>
        <div class="method-actions">
          <button data-action="method-bookmark" data-method-id="${escapeHtml(entry.id)}" class="${favorite ? "saved" : ""}" aria-pressed="${favorite}">${favorite ? '<i class="ph-fill ph-star" aria-hidden="true"></i> 已收藏' : `${icon("star")} 收藏`}</button>
          <button data-action="method-compare-toggle" data-method-id="${escapeHtml(entry.id)}" class="${comparing ? "saved" : ""}" aria-pressed="${comparing}">${icon(comparing ? "check" : "columns")} ${comparing ? "已加入对比" : "加入对比"}</button>
          <button class="primary" data-action="use-method" data-method-id="${escapeHtml(entry.id)}" data-method-name="${escapeHtml(entry.name)}">用于当前任务</button>
        </div>
      </div>
      <table class="method-table"><tbody>
        <tr><th>方法简介</th><td>${escapeHtml(entry.introduction)}</td></tr>
        <tr><th>适用场景</th><td>${methodListMarkup(entry.scenarios)}</td></tr>
        <tr class="method-row-warn"><th>${icon("warning-octagon")}何时不要用</th><td>${methodListMarkup(entry.antipatterns)}</td></tr>
        <tr><th>标准流程</th><td><ol class="method-workflow">${entry.workflow.map(step => `<li>${escapeHtml(step)}</li>`).join("")}</ol></td></tr>
        <tr><th>输入与输出</th><td><strong>输入：</strong>${escapeHtml(entry.input)}<br><strong>输出：</strong>${escapeHtml(entry.output)}</td></tr>
        <tr><th>核心假设</th><td>${methodListMarkup(entry.assumptions)}</td></tr>
        <tr><th>优点</th><td>${methodListMarkup(entry.advantages)}</td></tr>
        <tr><th>限制</th><td>${methodListMarkup(entry.limitations)}</td></tr>
        <tr class="method-row-pitfall"><th>${icon("first-aid-kit")}常见失败与修正</th><td>${methodPitfallMarkup(entry.pitfalls)}</td></tr>
        <tr class="method-row-check"><th>${icon("shield-check")}稳健性检查</th><td>${methodListMarkup(entry.robustness)}</td></tr>
        <tr><th>评价指标</th><td>${escapeHtml(entry.metrics.join("、"))}</td></tr>
        <tr><th>核心公式</th><td><div class="formula" data-tex="${escapeHtml(recipe.formula)}">${escapeHtml(recipe.formula)}</div></td></tr>
        <tr><th>代码示例</th><td>${codeBlockMarkup(entry.id, recipe)}</td></tr>
        <tr><th>相关赛题</th><td>${resourceLinks(relatedProblems, routes.problemDetail, item => item.code)}</td></tr>
        <tr><th>相关优秀论文</th><td>${resourceLinks(relatedPapers, routes.paperDetail, item => `${item.award} · ${item.team_id || item.problem_code}`)}</td></tr>
      </tbody></table>`;
  }

  const COMPARE_ROWS = [
    ["适用场景", entry => methodListMarkup(entry.scenarios)],
    ["何时不要用", entry => methodListMarkup(entry.antipatterns)],
    ["核心假设", entry => methodListMarkup(entry.assumptions)],
    ["输入", entry => escapeHtml(entry.input)],
    ["输出", entry => escapeHtml(entry.output)],
    ["优点", entry => methodListMarkup(entry.advantages)],
    ["限制", entry => methodListMarkup(entry.limitations)],
    ["稳健性检查", entry => methodListMarkup(entry.robustness)],
    ["评价指标", entry => escapeHtml(entry.metrics.join("、"))],
    ["核心公式", entry => {
      const tex = methodRecipes[entry.id]?.formula ?? "";
      return `<div class="formula" data-tex="${escapeHtml(tex)}">${escapeHtml(tex)}</div>`;
    }],
  ];

  function methodCompareMarkup(entries) {
    return `
      <div class="method-compare-head">
        <div>
          <h1>方法对比</h1>
          <div class="method-sub">并排比较 ${entries.length} 个方法的适用边界与代价</div>
        </div>
        <button data-action="method-compare-exit">${icon("x")} 退出对比</button>
      </div>
      <div class="method-compare-scroll">
        <table class="method-table method-compare-table">
          <thead><tr><th>对比项</th>${entries.map(entry => `<th><div class="compare-name">${escapeHtml(entry.name)}</div><div class="compare-sub">${escapeHtml(entry.category)} · ${escapeHtml(entry.subtitle)}</div><button class="compare-remove" data-action="method-compare-toggle" data-method-id="${escapeHtml(entry.id)}">${icon("minus-circle")} 移出</button></th>`).join("")}</tr></thead>
          <tbody>
            ${COMPARE_ROWS.map(([label, render]) => `<tr><th>${escapeHtml(label)}</th>${entries.map(entry => `<td>${render(entry)}</td>`).join("")}</tr>`).join("")}
          </tbody>
        </table>
      </div>`;
  }

  const setMethodGroupExpanded = (group, expanded) => {
    if (!group) return;
    const children = $(".tree-children", group);
    const trigger = $("[data-tree-group]", group);
    if (children) children.hidden = !expanded;
    trigger?.setAttribute("aria-expanded", String(expanded));
    const groupIcon = $("i", trigger);
    if (groupIcon) groupIcon.className = `ph ph-caret-${expanded ? "down" : "right"}`;
  };

  const methodLinkById = id => $$("[data-method]").find(link => link.dataset.method === id);

  /** 语言选择是全局偏好：切一次，页面上所有代码块和后续打开的方法都跟随。 */
  function switchMethodLanguage(language) {
    if (!RECIPE_LANGUAGES.some(item => item.id === language)) return;
    try {
      localStorage.setItem(METHOD_LANGUAGE_KEY, language);
    } catch {
      // 存储不可用时仅本次会话生效
    }
    $$("[data-code-block]").forEach(block => {
      $$("[data-code-lang]", block).forEach(tab => {
        const active = tab.dataset.codeLang === language;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
      });
      $$("[data-code-panel]", block).forEach(panel => {
        panel.hidden = panel.dataset.codePanel !== language;
      });
    });
  }

  function showMethodById(id, { notify = true } = {}) {
    const entry = methodLibrary.find(candidate => candidate.id === id);
    const detail = $("[data-method-detail]");
    if (!entry || !detail) return null;
    const item = methodLinkById(id);
    $$("[data-method]").forEach(link => {
      const active = link.dataset.method === id;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    if (item) setMethodGroupExpanded(item.closest("[data-method-group]"), true);
    if (detail.dataset.selectedMethod !== entry.id || detail.dataset.mode === "compare") {
      detail.dataset.selectedMethod = entry.id;
      detail.dataset.mode = "detail";
      detail.innerHTML = methodDetailMarkup(entry);
    }
    detail.scrollTop = 0;
    renderFormulas(detail);
    item?.scrollIntoView({ block: "nearest" });
    window.history.replaceState({}, "", `${routes.methods}?method=${encodeURIComponent(entry.id)}`);
    if (notify) toast(`已切换到 ${entry.name}`);
    return entry;
  }

  function syncMethodCompareBar() {
    const bar = $("[data-method-compare-bar]");
    if (!bar) return;
    bar.hidden = methodCompareIds.length === 0;
    $("[data-method-detail]")?.classList.toggle("has-compare-bar", methodCompareIds.length > 0);
    const count = $("[data-compare-count]", bar);
    if (count) count.textContent = String(methodCompareIds.length);
    const chips = $("[data-compare-chips]", bar);
    if (chips) {
      chips.innerHTML = methodCompareIds.map(id => {
        const entry = methodLibrary.find(candidate => candidate.id === id);
        return entry ? `<button class="compare-chip" data-action="method-compare-toggle" data-method-id="${escapeHtml(id)}">${escapeHtml(entry.name)}${icon("x")}</button>` : "";
      }).join("");
    }
    $$("[data-action='method-compare-toggle'][data-method-id]").forEach(button => {
      if (button.classList.contains("compare-chip") || button.classList.contains("compare-remove")) return;
      const active = methodCompareIds.includes(button.dataset.methodId);
      button.classList.toggle("saved", active);
      button.setAttribute("aria-pressed", String(active));
      button.innerHTML = `${icon(active ? "check" : "columns")} ${active ? "已加入对比" : "加入对比"}`;
    });
  }

  function openMethodCompare() {
    const detail = $("[data-method-detail]");
    if (!detail) return;
    const entries = methodCompareIds
      .map(id => methodLibrary.find(candidate => candidate.id === id))
      .filter(Boolean);
    if (entries.length < 2) {
      toast("请至少选择两个方法再对比");
      return;
    }
    detail.dataset.mode = "compare";
    detail.innerHTML = methodCompareMarkup(entries);
    detail.scrollTop = 0;
    renderFormulas(detail);
  }

  function exitMethodCompare() {
    const detail = $("[data-method-detail]");
    if (!detail || detail.dataset.mode !== "compare") return;
    const entry = methodLibrary.find(candidate => candidate.id === detail.dataset.selectedMethod) || methodLibrary[0];
    detail.dataset.mode = "detail";
    detail.innerHTML = methodDetailMarkup(entry);
    detail.scrollTop = 0;
    renderFormulas(detail);
  }

  const METHOD_TREE_COLLAPSED_KEY = "openmathmodelMethodTreeCollapsed";
  const methodTreeCollapsed = () => {
    try {
      return localStorage.getItem(METHOD_TREE_COLLAPSED_KEY) === "true";
    } catch {
      return false;
    }
  };

  function methodsScreen() {
    const requestedMethod = new URLSearchParams(window.location.search).get("method");
    const selected = methodLibrary.find(entry => entry.id === requestedMethod) || methodLibrary[0];
    const favorites = readMethodFavorites();
    // 手机档左侧方法树是浮层，默认必须收起，否则一进页面就盖住正文。
    const collapsed = methodTreeCollapsed() || matchesViewport(DRAWER_VIEWPORT);
    methodCompareIds = [];
    return shell(`
      <div class="method-layout ${collapsed ? "tree-collapsed" : ""}" data-method-layout>
        <button type="button" class="method-tree-toggle" data-action="toggle-method-tree"
                aria-expanded="${!collapsed}" aria-controls="method-tree-panel"
                title="${collapsed ? "展开方法列表" : "收起方法列表"}">${icon(collapsed ? "caret-right" : "caret-left")}</button>
        <aside class="method-tree" id="method-tree-panel">
          <label class="search-box">${icon("magnifying-glass")}<input type="search" name="method-search" data-method-search aria-label="搜索方法论" autocomplete="off" placeholder="搜索方法、场景或关键词……"></label>
          <div class="method-tree-toolbar">
            <div class="method-search-status" data-method-search-status aria-live="polite">${methodLibrary.length} 种方法</div>
            <button type="button" class="method-favorite-filter" data-method-favorite-filter aria-pressed="false">${icon("star")}<span data-favorite-count>${favorites.length}</span></button>
          </div>
          <div data-method-no-results class="method-no-results" hidden>没有匹配的方法</div>
          ${methodCategories.map(category => {
            const children = methodLibrary.filter(entry => entry.category === category);
            const open = children.some(entry => entry.id === selected.id);
            return `<div class="tree-group" data-method-group="${escapeHtml(category)}"><button type="button" class="tree-group-title" data-tree-group="${escapeHtml(category)}" aria-expanded="${open}">${icon(open ? "caret-down" : "caret-right")}<span>${escapeHtml(category)}</span><span class="tree-group-count">${children.length}</span></button><div class="tree-children" ${open ? "" : "hidden"}>${children.map(entry => `<a class="tree-child ${entry.id === selected.id ? "active" : ""}" href="${routes.methods}?method=${encodeURIComponent(entry.id)}" data-method="${escapeHtml(entry.id)}" data-method-favorite="${favorites.includes(entry.id)}" data-method-search-copy="${escapeHtml([entry.name, entry.subtitle, entry.category, entry.introduction, entry.input, entry.output, ...entry.scenarios, ...entry.antipatterns, ...entry.workflow, ...entry.assumptions, ...entry.metrics].join(" "))}">${escapeHtml(entry.name)}</a>`).join("")}</div></div>`;
          }).join("")}
        </aside>
        <section class="method-content" data-method-detail data-selected-method="${escapeHtml(selected.id)}">
          ${methodDetailMarkup(selected)}
        </section>
      </div>
      <div class="method-compare-bar" data-method-compare-bar hidden>
        <span class="compare-bar-label">${icon("columns")} 已选 <strong data-compare-count>0</strong>/${METHOD_COMPARE_LIMIT}</span>
        <div class="compare-bar-chips" data-compare-chips></div>
        <button data-action="method-compare-clear">清空</button>
        <button class="primary" data-action="method-compare-open">开始对比</button>
      </div>`, "methods");
  }

  const deliverables = [
    ["file-pdf", "最终报告_城市共享单车需求预测与调度优化.pdf", "PDF", "2.48 MB"],
    ["file-xls", "需求预测结果汇总.xlsx", "Excel", "512 KB"],
    ["file-image", "调度方案可视化.png", "PNG", "1.21 MB"],
    ["file-doc", "模型实现与参数说明.docx", "Word", "842 KB"],
    ["file-ppt", "答辩PPT_最终成果.pptx", "PowerPoint", "3.17 MB"],
    ["file-zip", "全部代码与数据包.zip", "ZIP", "18.63 MB"]
  ];

  const completeWorkspacePages = {
    "final-summary": {
      title: "最终成果",
      kicker: "建模任务已完成",
      project: "城市共享单车需求预测与调度优化",
      summary: [
        ["采用模型", "XGBoost + K-means + 混合整数规划"],
        ["关键指标", "R² 0.913 / 缺车率下降 18.7% / 平均调度距离下降 11.4%"],
        ["主要结论", "<ul><li>需求预测模型具备较高精度，能够有效捕捉时空需求波动规律。</li><li>基于聚类的分区调度策略显著降低缺车率并提升资源利用效率。</li><li>混合整数规划优化了车辆调度路径与数量配置，减少总体调度成本。</li></ul>"],
        ["模型限制", "<ul><li>数据来源与时间范围有限，可能影响模型泛化能力。</li><li>极端天气与突发事件尚未充分建模，需结合实时机制增强鲁棒性。</li><li>实际运营中仍需考虑更多约束与成本项。</li></ul>"]
      ],
      section: "交付文件",
      files: deliverables,
      actions: [
        ["返回首页", 'data-go="new"', false],
        ["继续优化", 'data-go="editor"', false],
        ["复制为新任务", 'data-action="copy-task"', false],
        ["下载全部", 'data-action="download-all"', true]
      ]
    },
    "paper-package": {
      title: "论文文件",
      kicker: "论文交付包已生成",
      project: "城市共享单车需求预测与调度优化研究",
      summary: [
        ["正文版本", "v4 / 7 个章节 / 18,642 字"],
        ["检查结果", "结构、公式、图表、引用与核心数字全部通过"],
        ["包含内容", "<ul><li>摘要、问题重述与模型假设。</li><li>需求预测、区域划分和调度优化方法。</li><li>实验结果、稳健性分析与模型限制。</li></ul>"],
        ["导出说明", "<ul><li>PDF 用于最终提交。</li><li>Word 保留可编辑正文。</li><li>PPT 用于答辩展示。</li></ul>"]
      ],
      section: "论文交付文件",
      files: [deliverables[0], deliverables[3], deliverables[4]]
    },
    "data-code-package": {
      title: "数据与代码",
      kicker: "复现材料已归档",
      project: "数据、代码与运行环境交付包",
      summary: [
        ["数据版本", "清洗数据 v2 / Run #04 / seed 42"],
        ["运行环境", "Python 3.11 / XGBoost / HiGHS 1.7"],
        ["复现命令", "python solve.py --seed 42 --data data_v2"],
        ["归档说明", "<ul><li>输入、输出和中间结果目录使用相对路径。</li><li>依赖版本、随机种子与求解参数均已锁定。</li></ul>"]
      ],
      section: "数据与代码文件",
      files: [
        deliverables[1],
        ["file-text", "requirements.txt", "TXT", "3 KB"],
        deliverables[5]
      ]
    },
    "delivery-record": {
      title: "交付记录",
      kicker: "交付检查已通过",
      project: "最终成果完整性与一致性记录",
      summary: [
        ["交付状态", "6 个正式文件全部生成 / 0 个缺失"],
        ["一致性", "论文、结果表、图表与最终摘要中的关键数字一致"],
        ["验证记录", "<ul><li>生产构建、静态检查和浏览器交互均通过。</li><li>代码、数据版本和运行 ID 可追溯。</li></ul>"],
        ["后续使用", "<ul><li>可继续返回论文页优化正文。</li><li>可复制为新任务并保留当前成果。</li></ul>"]
      ],
      section: "交付记录文件",
      files: [
        ["file-pdf", "最终交付检查报告.pdf", "PDF", "438 KB"],
        ["file-text", "成果文件清单.txt", "TXT", "12 KB"],
        ["file-code", "run_20261001_104233.json", "JSON", "42 KB"]
      ]
    }
  };

  function completeStageContent() {
    return `
      <section class="focused-workspace final-delivery-workspace">
        ${workspaceTabs([["final-summary","最终成果","check-circle"],["paper-package","论文文件","file-text"],["data-code-package","数据与代码","code"],["delivery-record","交付记录","clipboard-text"]], "final-summary")}
        <div class="focused-workspace-panel active" data-workspace-panel="final-summary">${resultDocument(completeWorkspacePages["final-summary"], "final-summary", "final-delivery-document")}</div>
        <div class="focused-workspace-panel" data-workspace-panel="paper-package">${resultDocument(completeWorkspacePages["paper-package"], "paper-package", "final-delivery-document")}</div>
        <div class="focused-workspace-panel" data-workspace-panel="data-code-package">${resultDocument(completeWorkspacePages["data-code-package"], "data-code-package", "final-delivery-document")}</div>
        <div class="focused-workspace-panel" data-workspace-panel="delivery-record">${resultDocument(completeWorkspacePages["delivery-record"], "delivery-record", "final-delivery-document")}</div>
      </section>`;
  }

  // ── B 方案合并工作台：五个阶段面板同存于一个页面，六条路由保留为面板直达别名 ──
  const WORKSPACE_STAGES = ["data", "model", "experiments", "editor", "complete"];
  const workspaceStageContent = {
    data: dataStageContent,
    model: modelStageContent,
    experiments: experimentsStageContent,
    editor: editorStageContent,
    complete: completeStageContent
  };
  const WORKSPACE_STAGE_TITLES = {
    data: "数据准备",
    model: "建模方案",
    experiments: "实验结果",
    editor: "论文编辑",
    complete: "任务完成"
  };

  function workspaceScreen(initialStage) {
    const panes = WORKSPACE_STAGES.map(stage => (
      `<div class="workspace-stage" data-stage-pane="${stage}"${stage === initialStage ? "" : " hidden"}>${workspaceStageContent[stage]()}</div>`
    )).join("");
    return modelingShell(`<div class="workspace-stage-host" data-workspace-stage-host>${panes}</div>`, initialStage);
  }

  function updateFocusedDemoRail(stageKey, shell) {
    const demo = FOCUSED_STAGE_DEMO[stageKey];
    const rail = $(".focused-agent-pane", shell);
    if (!demo || !rail) return;
    const copy = $(".focused-agent-copy", rail);
    if (copy && !copy.dataset.agentState) copy.innerHTML = demo.copy;
    const current = $(".focused-step.current > span:nth-child(2)", rail);
    if (current) current.textContent = demo.current;
    const cta = $("[data-agent-cta]", rail);
    if (cta && !cta.dataset.agentAction) {
      cta.dataset.go = demo.next;
      cta.innerHTML = demo.button;
    }
    const attachments = $(".focused-attachments", rail);
    if (attachments) attachments.hidden = stageKey !== "data";
  }

  function showWorkspaceStage(stageKey, options = {}) {
    const host = $("[data-workspace-stage-host]");
    if (!host || !workspaceStageContent[stageKey]) return false;
    $$(".workspace-stage", host).forEach(pane => { pane.hidden = pane.dataset.stagePane !== stageKey; });
    const shell = host.closest("[data-modeling-shell]");
    if (shell) {
      shell.dataset.focusedStage = stageKey;
      shell.dataset.workspacePage = stageKey;
      const stagePane = $(".focused-stage-pane", shell);
      if (stagePane) stagePane.dataset.workspacePage = stageKey;
      const back = $(".focused-back", shell);
      if (back) {
        const backLabel = stageKey === "complete" ? "返回首页" : "返回任务执行";
        back.setAttribute("href", stageKey === "complete" ? routes.new : routes.running);
        back.setAttribute("aria-label", backLabel);
        back.setAttribute("title", backLabel);
        back.dataset.backFrom = stageKey;
      }
      // 演示态由模板更新左栏文案；真实运行的左栏由工作台控制器渲染，这里不碰。
      if (shell.dataset.workspaceSource !== "api") updateFocusedDemoRail(stageKey, shell);
    }
    document.body.dataset.screen = stageKey;
    document.title = `OpenMathModel · ${t(WORKSPACE_STAGE_TITLES[stageKey])}`;
    if (options.pushUrl) history.pushState({ ommStage: stageKey }, "", options.url || routes[stageKey]);
    // 隐藏面板里的 Chart.js 画布尺寸为 0，切换可见后靠 resize 事件自适应恢复。
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    document.dispatchEvent(new CustomEvent("omm:stage-shown", { detail: { stage: stageKey } }));
    return true;
  }

  let workspaceNavBound = false;
  function bindWorkspaceStageNav() {
    if (workspaceNavBound) return;
    workspaceNavBound = true;
    window.addEventListener("popstate", () => {
      if (!$("[data-workspace-stage-host]")) return;
      const raw = window.location.pathname;
      const path = raw.length > 1 && raw.endsWith("/") ? raw.slice(0, -1) : raw;
      const stage = WORKSPACE_STAGES.find(key => routes[key] === path);
      if (stage) showWorkspaceStage(stage, { pushUrl: false });
      else window.location.reload();
    });
    // 工作台控制器（真实运行）经此事件请求软切换，避免模块循环依赖。
    document.addEventListener("omm:show-stage", event => {
      const detail = (event as CustomEvent<{ stage?: string; url?: string }>).detail || {};
      if (detail.stage) showWorkspaceStage(detail.stage, { pushUrl: true, url: detail.url });
    });
  }

  const renderers = {
    new: newScreen,
    confirm: confirmScreen,
    running: runningScreen,
    projects: projectsScreen,
    data: () => workspaceScreen("data"),
    model: () => workspaceScreen("model"),
    experiments: () => workspaceScreen("experiments"),
    editor: () => workspaceScreen("editor"),
    problems: problemsScreen,
    papers: papersScreen,
    problemDetail: problemDetailScreen,
    paperDetail: paperDetailScreen,
    methods: methodsScreen,
    complete: () => workspaceScreen("complete")
  };

  function go(name) {
    // 合并工作台内的阶段跳转走软切换（无整页重载）；其余目标保持整页导航。
    if (workspaceStageContent[name] && $("[data-workspace-stage-host]") && showWorkspaceStage(name, { pushUrl: true })) return;
    if (routes[name]) window.location.href = routes[name];
  }

  function toast(message, duration = 1900) {
    $(".toast")?.remove();
    const node = document.createElement("div");
    node.className = "toast";
    node.textContent = message;
    document.body.appendChild(node);
    setTimeout(() => node.remove(), duration);
  }

  function modal(title, body, onConfirm) {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML = `<div class="modal" role="dialog" aria-modal="true"><h2>${title}</h2>${body}<div class="modal-actions"><button data-modal-cancel>取消</button><button class="primary" data-modal-confirm>确认</button></div></div>`;
    document.body.appendChild(backdrop);
    $("[data-modal-cancel]", backdrop).onclick = () => backdrop.remove();
    backdrop.onclick = event => { if (event.target === backdrop) backdrop.remove(); };
    $("[data-modal-confirm]", backdrop).onclick = () => { onConfirm?.(backdrop); backdrop.remove(); };
    $("input,textarea", backdrop)?.focus();
  }

  function settingsToggle(name, label, description, checked = true) {
    return `<div class="settings-row">
      <div class="settings-row-copy"><strong>${label}</strong><span>${description}</span></div>
      <button type="button" class="settings-switch ${checked ? "active" : ""}" role="switch" aria-checked="${checked}" data-setting-toggle name="${name}">
        <span></span>
      </button>
    </div>`;
  }

  /**
   * 打开开关时立即申请系统权限：被拒绝就把开关拨回去，不留下“开着却不会响”的假状态。
   */
  function requestDesktopNotifications(toggle) {
    if (!notificationsSupported()) {
      toggle.classList.remove("active");
      toggle.setAttribute("aria-checked", "false");
      toast(t("当前浏览器不支持桌面通知"));
      return;
    }
    void requestNotificationPermission().then(permission => {
      if (permission === "granted") {
        // 开关状态要先落盘，预览通知才会通过 desktopNotificationsEnabled 检查。
        try {
          const settings = JSON.parse(localStorage.getItem("openmathmodelSettings") || "{}");
          settings.desktopNotifications = true;
          localStorage.setItem("openmathmodelSettings", JSON.stringify(settings));
        } catch {
          // 存储不可用时仍可在本次会话内发通知
        }
        sendNotificationPreview();
        return;
      }
      toggle.classList.remove("active");
      toggle.setAttribute("aria-checked", "false");
      toast(t("浏览器已阻止通知，请在地址栏的站点设置中允许通知"));
    });
  }

  /** 诊断按钮的忙碌态包装：执行期间禁用并替换文案，结束后还原原始内容。 */
  async function withBusyButton(button, busyLabel, work) {
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = busyLabel;
    try {
      return await work();
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  function renderDiagnosticReport(host, checks) {
    if (!host) return;
    const iconFor = { ok: "check-circle", warn: "warning-circle", fail: "x-circle" };
    host.replaceChildren(...checks.map(check => {
      const row = document.createElement("div");
      row.className = "diagnostic-report-row";
      row.dataset.status = check.status;
      const statusIcon = document.createElement("i");
      statusIcon.className = `ph ph-${iconFor[check.status]}`;
      statusIcon.setAttribute("aria-hidden", "true");
      const name = document.createElement("strong");
      name.textContent = t(check.name);
      const detail = document.createElement("span");
      detail.textContent = check.detail;
      row.append(statusIcon, name, detail);
      return row;
    }));
    host.hidden = false;
  }

  async function runDiagnosticsInto(button, host) {
    const outcome = await withBusyButton(button, t("诊断中…"), runNetworkDiagnostics);
    renderDiagnosticReport(host, outcome.checks);
    const failed = outcome.checks.some(check => check.status === "fail");
    toast(t(failed ? "网络诊断完成，存在异常项" : "网络诊断完成，未发现异常"));
  }

  async function exportDiagnostics(button) {
    const outcome = await withBusyButton(button, t("正在生成报告…"), runNetworkDiagnostics);
    downloadDiagnosticReport(buildDiagnosticReport(outcome));
    toast(t("诊断报告已导出"));
  }

  async function copySystemInfo(button) {
    const outcome = await withBusyButton(button, t("正在收集…"), runNetworkDiagnostics);
    const copied = await copyTextToClipboard(buildDiagnosticReport(outcome));
    toast(t(copied ? "系统信息已复制" : "复制失败，请改用导出诊断报告"));
  }

  function openSettingsCenter(initialPane) {
    $(".settings-backdrop")?.remove();
    const localeBeforeOpen = currentLocale();
    const displayBeforeOpen = currentDisplayPreferences();
    let settingsSaved = false;
    const backdrop = document.createElement("div");
    backdrop.className = "settings-backdrop";
    backdrop.innerHTML = `
      <div class="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <aside class="settings-sidebar">
          <div class="settings-brand">${projectLogo("settings-logo")}<div><strong>OpenMathModel</strong><span>设置中心</span></div></div>
          <nav class="settings-nav" aria-label="设置分类">
            <button class="active" data-settings-nav="general" data-title="通用设置" data-subtitle="语言、地区和基础任务行为">${icon("sliders-horizontal")}<span>通用</span></button>
            <button data-settings-nav="personalization" data-title="个性化" data-subtitle="正文字号、可读性与使用习惯">${icon("user-focus")}<span>个性化</span></button>
            <button data-settings-nav="usage" data-title="用量监控" data-subtitle="查看 Token、请求量和费用预算">${icon("chart-bar")}<span>用量监控</span></button>
            <button data-settings-nav="security" data-title="账户与安全" data-subtitle="密码、双重验证和登录设备">${icon("shield-check")}<span>账户与安全</span></button>
            <button data-settings-nav="providers" data-title="模型厂商" data-subtitle="管理官方模型服务与默认路由">${icon("circles-four")}<span>模型厂商</span></button>
            <button data-settings-nav="api" data-title="自定义 API" data-subtitle="连接模型厂商或 OpenAI 兼容中转站">${icon("plugs-connected")}<span>自定义 API</span></button>
            <button data-settings-nav="privacy" data-title="数据与隐私" data-subtitle="管理历史记录、数据保留与通知">${icon("lock-key")}<span>数据与隐私</span></button>
            <button data-settings-nav="advanced" data-title="高级设置" data-subtitle="代理、并发、超时与开发选项">${icon("terminal-window")}<span>高级设置</span></button>
          </nav>
          <div class="settings-account-card">
            <span class="avatar">I</span>
            <div><strong>Ivan</strong><span>个人工作区</span></div>
          </div>
        </aside>

        <section class="settings-content">
          <header class="settings-header">
            <div><h2 id="settings-title">通用设置</h2><p data-settings-subtitle>语言、地区和基础任务行为</p></div>
            <button class="settings-close" type="button" data-settings-close aria-label="关闭设置">${icon("x")}</button>
          </header>

          <div class="settings-scroll">
            <div class="settings-pane active" data-settings-pane="general">
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>语言与地区</h3><p>选择界面语言、输出语言和本地格式。</p></div></div>
                <div class="settings-grid two">
                  <label class="settings-field"><span>界面语言</span><select name="interfaceLanguage"><option value="zh-CN">简体中文</option><option value="en-US">English</option></select></label>
                  <label class="settings-field"><span>Agent 默认回复语言</span><select name="replyLanguage"><option value="auto">跟随提问语言</option><option value="zh-CN">简体中文</option><option value="en-US">English</option></select></label>
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>任务与文件</h3><p>控制新任务、自动保存和文件处理方式。</p></div></div>
                ${settingsToggle("autoSave", "自动保存任务", "编辑内容和对话每 30 秒自动保存", true)}
                ${settingsToggle("restoreSession", "启动时恢复上次任务", "重新打开 OpenMathModel 时返回最近使用的任务", true)}
                ${settingsToggle("autoOpenFiles", "自动解析上传文件", "上传 PDF、表格或图片后立即生成内容摘要", true)}
                ${settingsToggle("desktopNotifications", "桌面通知", "长任务完成或需要确认时发送系统通知", false)}
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="personalization">
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>正文与可读性</h3><p>调整正文字号、动态效果与对比度。</p></div></div>
                <label class="settings-range"><span><b>正文字号</b><output data-font-output>${TEXT_BASE_PX} px</output></span><input type="range" min="13" max="19" value="${TEXT_BASE_PX}" name="fontSize" data-font-size></label>
                ${settingsToggle("reduceMotion", "减少动态效果", "减少弹窗、页面切换与进度反馈动画", false)}
                ${settingsToggle("highContrast", "增强文字对比度", "使用更深的正文与边界颜色", false)}
              </div>
              <div class="settings-section">
                ${settingsToggle("deepReasoning", "复杂任务自动开启深度思考", "检测到研究、编程或数学建模任务时提升推理强度", true)}
                ${settingsToggle("sendWithEnter", "按 Enter 发送消息", "关闭后使用 Ctrl + Enter 发送，Enter 仅换行", true)}
                ${settingsToggle("rememberPreferences", "记住长期偏好", "允许 Agent 记住稳定的格式、术语和工作习惯", true)}
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="usage">
              <div class="usage-overview">
                <div class="usage-stat"><span>本月 Token</span><strong data-usage-stat="tokens">–</strong><small data-usage-stat-note="tokens">正在加载用量…</small></div>
                <div class="usage-stat"><span>Agent 任务</span><strong data-usage-stat="runs">–</strong><small data-usage-stat-note="runs"></small></div>
                <div class="usage-stat"><span>预估费用</span><strong data-usage-stat="cost">–</strong><small data-usage-stat-note="cost"></small></div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading usage-heading"><div><h3>本月用量</h3><p data-usage-range>——</p></div><button type="button" class="secondary-small" data-settings-action="export-usage">${icon("download-simple")} 导出明细</button></div>
                <div class="usage-budget" data-usage-budget hidden><div><span data-usage-budget-label></span><b data-usage-budget-percent></b></div><progress data-usage-budget-progress value="0" max="100"></progress></div>
                <div class="usage-chart" data-usage-chart aria-label="最近 14 天用量柱状图"></div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>模型用量分布</h3><p>费用为本月预估值。</p></div></div>
                <div class="usage-table" data-usage-models>
                  <div class="usage-table-head"><span>模型</span><span>请求</span><span>Token</span><span>费用</span></div>
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-grid two">
                  <label class="settings-field"><span>月度预算提醒</span><div class="field-with-unit"><input type="number" name="monthlyBudget" value="" min="0" placeholder="未设置"><b>元 / 月</b></div></label>
                  <label class="settings-field"><span>提醒阈值</span><select name="budgetThreshold"><option>达到 80% 时提醒</option><option>达到 60% 时提醒</option><option>达到 100% 时提醒</option></select></label>
                </div>
                ${settingsToggle("hardBudgetLimit", "达到预算后暂停付费模型", "保留本地模型与免费额度，避免产生额外费用", false)}
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="security">
              <div data-security-root>
                <div class="settings-section security-loading">正在加载账户信息…</div>
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="providers">
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>模型厂商</h3><p>点「配置」自动填入官方接口参数，连接状态来自已保存的接口。</p></div><button type="button" class="primary-small" data-settings-jump="api">${icon("plus")} 添加厂商</button></div>
                <div class="provider-list">
                  ${PROVIDER_PRESETS.map(preset => `<div class="provider-card" data-provider-card="${escapeHtml(preset.id)}">
                    <div class="provider-logo">${providerLogo(preset.logo, preset.label)}</div>
                    <div><strong>${escapeHtml(preset.label)}</strong><span>${escapeHtml(preset.subtitle || preset.models.slice(0, 3).join(" / "))}</span></div>
                    <span class="provider-status idle" data-provider-status>未配置</span>
                    <button type="button" data-settings-action="configure-provider" data-provider-id="${escapeHtml(preset.id)}">配置</button>
                  </div>`).join("")}
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>智能路由</h3><p>根据任务类型、速度与费用自动选择模型。</p></div></div>
                ${settingsToggle("smartRouting", "启用模型智能路由", "优先满足质量要求，并在同等能力下选择成本更低的模型", true)}
                <div class="settings-grid two">
                  <label class="settings-field"><span>编程与 Agent</span><select name="codingModel"><option>自动选择</option><option>GPT-5.6 Sol</option><option>Claude Opus 5</option><option>GLM-5.3</option><option>DeepSeek-V4-Pro</option></select></label>
                  <label class="settings-field"><span>深度研究</span><select name="researchModel"><option>自动选择</option><option>Qwen3.8-Max</option><option>DeepSeek-V4-Pro</option><option>Claude Fable 5</option><option>GPT-5.6 Sol</option></select></label>
                  <label class="settings-field"><span>长文写作</span><select name="writingModel"><option>自动选择</option><option>Claude Sonnet 5</option><option>Qwen3.8-Max</option><option>Kimi K3</option></select></label>
                  <label class="settings-field"><span>视觉理解</span><select name="visionModel"><option>自动选择</option><option>Gemini 3.6 Flash</option><option>GPT-5.6 Sol</option><option>Qwen3.8-Max</option></select></label>
                </div>
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="api">
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>自定义模型接口</h3><p>支持厂商官方 API、OpenAI 兼容接口和第三方中转站。</p></div><span class="api-security-note">${icon("shield-check")} 密钥仅保存在本机后端</span></div>
                <div class="settings-grid two">
                  <label class="settings-field"><span>配置名称</span><input name="apiProfileName" value="OpenAI 兼容中转站" placeholder="例如：团队模型网关"></label>
                  <label class="settings-field"><span>接口协议</span><select name="apiProtocol"><option>OpenAI Compatible</option><option>Anthropic Messages API</option><option>Google Gemini API</option><option>Ollama</option><option>自定义 REST</option></select></label>
                  <label class="settings-field settings-span-two"><span>Base URL</span><input name="apiBaseUrl" value="https://api.example.com/v1" placeholder="https://api.example.com/v1"></label>
                  <label class="settings-field settings-span-two"><span>API Key</span><div class="secret-field"><input type="password" name="apiKey" value="sk-openmathmodel-demo-key"><button type="button" data-settings-action="toggle-secret" aria-label="显示或隐藏 API Key">${icon("eye")}</button></div></label>
                  <label class="settings-field"><span>默认模型 ID</span><input name="apiModel" value="gpt-5.6-sol" placeholder="gpt-5.6-sol" list="apiModelOptions" autocomplete="off"><datalist id="apiModelOptions"></datalist></label>
                  <label class="settings-field"><span>组织 / 项目标识</span><input name="apiOrganization" placeholder="可选"></label>
                </div>
                <details class="api-advanced"><summary>请求头与高级参数 ${icon("caret-down")}</summary><div class="settings-grid two"><label class="settings-field"><span>自定义请求头</span><input name="customHeader" placeholder="X-API-Source: OpenMathModel"></label><label class="settings-field"><span>路径前缀</span><input name="apiPathPrefix" placeholder="/chat/completions"></label><label class="settings-field"><span>模型能力权重</span><div class="field-with-unit"><input type="number" name="apiWeight" min="0" max="10" step="1" placeholder="自动"><b>1-10</b></div><small>Auto 模式按权重路由：难题给高权重接口。留空按模型名自动推断</small></label></div></details>
                <input type="hidden" name="apiEditingEndpointId" value="">
                <div class="api-actions"><button type="button" data-settings-action="test-api">${icon("pulse")} 测试连接</button><button type="button" data-settings-action="cancel-endpoint-edit" data-endpoint-edit-cancel hidden>取消编辑</button><button type="button" class="primary-small" data-settings-action="add-endpoint"><span data-endpoint-save-label>${icon("plus")} 保存为新接口</span></button></div>
              </div>
              <div class="settings-section">
                ${settingsToggle("allowProxyApi", "允许使用第三方中转站", "发送请求前显示实际域名，并记录接口用量", true)}
                ${settingsToggle("streamResponse", "流式输出", "支持时逐步显示模型回复，降低首字等待时间", true)}
                ${settingsToggle("fallbackApi", "失败时自动切换备用接口", "主接口超时、限流或余额不足时触发", true)}
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>已保存接口</h3><p>主接口负责默认调用；Auto 模式按能力权重在这些接口间路由，其余接口在超时、限流或余额不足时作为备用。</p></div></div>
                <div class="endpoint-list" data-endpoint-list>
                  <div class="endpoint-item"><span class="endpoint-dot"></span><div><strong>正在读取已保存接口…</strong><span>接口配置保存在本机后端，随账户生效</span></div></div>
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>接口用量记录</h3><p>「允许使用第三方中转站」开启时记录每次调用的实际域名、模型与 token 用量。</p></div><button type="button" class="secondary-small" data-settings-action="clear-llm-usage">${icon("trash")} 清空记录</button></div>
                <div class="endpoint-list" data-llm-usage-list></div>
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="privacy">
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>对话与文件</h3><p>决定任务内容在本机和云端的保留方式。</p></div></div>
                ${settingsToggle("saveHistory", "保存任务历史", "在侧栏保留任务、对话和生成文件", true)}
                ${settingsToggle("localFirst", "敏感文件优先本地处理", "可本地完成的解析和索引不会上传到模型厂商", true)}
                ${settingsToggle("modelTraining", "允许用于改进产品", "发送匿名使用统计，不包含对话、文件或 API Key", false)}
                <div class="settings-grid two">
                  <label class="settings-field"><span>任务保留时间</span><select name="retention"><option>永久保留</option><option>90 天</option><option>30 天</option><option>任务完成后删除</option></select></label>
                  <label class="settings-field"><span>文件缓存</span><select name="fileCache"><option>30 天后清理</option><option>7 天后清理</option><option>关闭任务时清理</option></select></label>
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>通知方式</h3><p>选择需要接收的任务和账户提醒。</p></div></div>
                ${settingsToggle("notifyTaskDone", "任务完成通知", "Agent 完成运行、实验或文件导出时通知", true)}
                ${settingsToggle("notifyBudget", "预算与限额提醒", "费用达到阈值或接口额度不足时通知", true)}
                ${settingsToggle("notifySecurity", "账户安全提醒", "新设备登录、密码或 API 配置发生变化时通知", true)}
                ${settingsToggle("emailDigest", "每周使用摘要", "每周一发送用量、费用和任务完成情况", false)}
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="advanced">
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>网络与运行</h3><p>针对企业网络和大型 Agent 任务调整连接参数。</p></div></div>
                <div class="settings-grid two">
                  <label class="settings-field"><span>网络代理</span><select name="proxyMode"><option>跟随系统</option><option>不使用代理</option><option>手动配置</option></select><small>将随模型服务接入后生效，当前仅保存配置</small></label>
                  <label class="settings-field"><span>代理地址</span><input name="proxyUrl" placeholder="http://127.0.0.1:7890"><small>供服务端出站请求使用，网页自身不经过此代理</small></label>
                  <label class="settings-field"><span>请求超时</span><div class="field-with-unit"><input type="number" name="requestTimeout" value="120" min="5" max="600" step="5"><b>秒</b></div><small>作用于网页与服务端之间的接口请求（5–600 秒）</small></label>
                  <label class="settings-field"><span>最大并发任务</span><select name="maxConcurrency"><option>3 个</option><option>1 个</option><option>5 个</option><option>8 个</option></select><small>同时排队或执行中的任务数上限，保存后立即生效</small></label>
                  <label class="settings-field"><span>下载目录</span><input name="downloadDirectory" value="E:\\OpenMathModel\\Downloads" disabled><small>桌面端功能：网页版的下载位置由浏览器决定</small></label>
                  <label class="settings-field"><span>临时文件目录</span><input name="tempDirectory" value="自动管理" disabled><small>桌面端功能：服务端临时目录由部署配置管理</small></label>
                </div>
              </div>
              <div class="settings-section">
                ${settingsToggle("retryRequest", "自动重试失败请求", "针对网络错误和限流最多重试 3 次", true)}
                ${settingsToggle("confirmExternal", "外部操作前请求确认", "发送邮件、发布内容或变更远程数据前暂停确认", false)}
                ${settingsToggle("developerMode", "开发者模式", "显示请求 ID、Token 明细、工具调用和调试日志", false)}
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>诊断</h3><p>用于排查模型接口和本地运行问题。</p></div></div>
                <div class="diagnostic-actions"><button type="button" data-settings-action="network-diagnosis">${icon("pulse")} 运行网络诊断</button><button type="button" data-settings-action="export-diagnostics">${icon("download-simple")} 导出诊断报告</button><button type="button" data-settings-action="copy-system-info">${icon("copy")} 复制系统信息</button></div>
                <div class="diagnostic-report" data-diagnostic-report hidden></div>
              </div>
            </div>
          </div>

          <footer class="settings-footer">
            <button type="button" class="settings-reset" data-settings-action="reset-defaults">恢复默认设置</button>
            <div><span data-settings-save-state>所有更改将在本机保存</span><button type="button" data-settings-close>取消</button><button type="button" class="primary" data-settings-save>保存更改</button></div>
          </footer>
        </section>
      </div>`;
    document.body.appendChild(backdrop);
    initSecurityPane(backdrop);

    const closeSettings = () => {
      document.removeEventListener("keydown", onSettingsKeydown);
      // 语言与可读性偏好都是即时预览，未保存就关闭要还原成打开前的样子。
      if (!settingsSaved) {
        applyLocale(localeBeforeOpen);
        applyDisplayPreferences(displayBeforeOpen);
      }
      backdrop.remove();
    };
    const activatePane = button => {
      const paneName = button.dataset.settingsNav || button.dataset.settingsJump;
      const navButton = $(`[data-settings-nav="${paneName}"]`, backdrop);
      if (!navButton) return;
      $$("[data-settings-nav]", backdrop).forEach(item => item.classList.toggle("active", item === navButton));
      $$("[data-settings-pane]", backdrop).forEach(pane => pane.classList.toggle("active", pane.dataset.settingsPane === paneName));
      $("#settings-title", backdrop).textContent = navButton.dataset.title;
      $("[data-settings-subtitle]", backdrop).textContent = navButton.dataset.subtitle;
      $(".settings-scroll", backdrop).scrollTop = 0;
      $$(".settings-custom-select.open", backdrop).forEach(select => {
        select.classList.remove("open");
        $("[data-custom-select-trigger]", select)?.setAttribute("aria-expanded", "false");
      });
    };
    // 可读性三项即时预览：直接读当前控件状态，未保存时由 closeSettings 还原
    const previewDisplayPreferences = () => applyDisplayPreferences({
      fontSize: $('[name="fontSize"]', backdrop)?.value,
      reduceMotion: $('[name="reduceMotion"]', backdrop)?.getAttribute("aria-checked") === "true",
      highContrast: $('[name="highContrast"]', backdrop)?.getAttribute("aria-checked") === "true",
    });
    const collectSettingsValues = () => {
      const values = {};
      $$("[name]", backdrop).forEach(control => {
        values[control.name] = control.matches("[data-setting-toggle]") ? control.getAttribute("aria-checked") === "true" : control.value;
      });
      return values;
    };
    const saveSettings = () => {
      const values = collectSettingsValues();
      localStorage.setItem("openmathmodelSettings", JSON.stringify(values));
      $$('[data-model-choice^="custom-"]', document).forEach(option => {
        const picker = option.closest("[data-model-picker]");
        const wasSelected = option.getAttribute("aria-selected") === "true";
        option.dataset.modelChoice = `custom-${values.apiModel || "gpt-5.6-sol"}`;
        $(".model-choice-copy strong", option).textContent = values.apiModel || "gpt-5.6-sol";
        $(".model-choice-copy small", option).textContent = `${values.apiProfileName || "OpenAI 兼容中转站"} · 自定义 API`;
        if (wasSelected && picker) {
          $("[data-model-picker-label]", picker).textContent = values.apiModel || "gpt-5.6-sol";
          localStorage.setItem("openmathmodelSelectedModel", option.dataset.modelChoice);
        }
      });
      settingsSaved = true;
      applyLocale(values.interfaceLanguage);
      applyDisplayPreferences(values);
      // 并发上限的闸门在服务端，异步推送；失败不拦保存，只提示同步结果。
      void persistMaxConcurrency(values.maxConcurrency).then(message => {
        if (message) toast(t(message));
      });
      // 自定义 API 同样存服务端：对话与任务执行按它出网调用模型。
      void persistLlmSettings(values).then(message => {
        if (message) toast(t(message));
        // 接口池或开关可能变化，任务页的模型选择器同步刷新
        void hydrateModelPickers();
      });
      // 预算三项存服务端：暂停付费模型的闸门在服务端调用路径上校验。
      void persistUsageSettings(values).then(message => {
        if (message) toast(t(message));
      });
      // 数据与隐私九项同样存服务端：任务保留与文件缓存清理由服务端清扫执行。
      void persistPrivacySettings(values).then(message => {
        if (message) toast(t(message));
      });
      $("[data-settings-save-state]", backdrop).textContent = t("已保存");
      toast(t("设置已保存"));
      setTimeout(closeSettings, 280);
    };
    const restoreSettings = () => {
      try {
        const saved = JSON.parse(localStorage.getItem("openmathmodelSettings") || "{}");
        // 历史存档里可能残留 theme 等已下线的键，找不到对应控件时静默跳过。
        Object.entries(saved).forEach(([name, value]) => {
          const control = $(`[name="${name}"]`, backdrop);
          if (!control) return;
          if (control.matches("[data-setting-toggle]")) {
            control.classList.toggle("active", Boolean(value));
            control.setAttribute("aria-checked", String(Boolean(value)));
          } else {
            control.value = value;
          }
        });
      } catch (error) {
        localStorage.removeItem("openmathmodelSettings");
      }
      // 滑块回填后同步右侧读数，否则会出现"滑块在 18、标签写 14"的错位
      const fontControl = $('[name="fontSize"]', backdrop);
      const fontOutput = $("[data-font-output]", backdrop);
      if (fontControl && fontOutput) fontOutput.textContent = `${fontControl.value} px`;
    };
    const enhanceSettingsSelects = () => {
      $$("select", backdrop).forEach((select, selectIndex) => {
        const options = [...select.options];
        const selected = options.find(option => option.selected) || options[0];
        const fieldLabel = select.closest(".settings-field")?.querySelector(":scope > span")?.textContent?.trim() || "选择选项";
        select.classList.add("settings-native-select");
        select.hidden = true;
        select.tabIndex = -1;
        select.setAttribute("aria-hidden", "true");
        const custom = document.createElement("div");
        custom.className = "settings-custom-select";
        custom.dataset.customSelect = select.name || String(selectIndex);
        custom.innerHTML = `
          <button type="button" class="settings-select-trigger" data-custom-select-trigger aria-haspopup="listbox" aria-expanded="false" aria-label="${escapeHtml(fieldLabel)}">
            <span>${escapeHtml(selected?.textContent || "")}</span>${icon("caret-down")}
          </button>
          <div class="settings-select-menu" role="listbox" aria-label="${escapeHtml(fieldLabel)}">
            ${options.map(option => `<button type="button" role="option" data-custom-select-option="${escapeHtml(option.value)}" aria-selected="${option.selected}">
              <span>${escapeHtml(option.textContent)}</span>${icon("check")}
            </button>`).join("")}
          </div>`;
        select.insertAdjacentElement("afterend", custom);
      });
    };
    const onSettingsKeydown = event => {
      if (event.key !== "Escape") return;
      const openSelect = $(".settings-custom-select.open", backdrop);
      if (openSelect) {
        openSelect.classList.remove("open");
        $("[data-custom-select-trigger]", openSelect)?.setAttribute("aria-expanded", "false");
        return;
      }
      closeSettings();
    };

    backdrop.addEventListener("click", event => {
      if (event.target === backdrop || event.target.closest("[data-settings-close]")) {
        closeSettings();
        return;
      }
      const customOption = event.target.closest("[data-custom-select-option]");
      if (customOption) {
        const custom = customOption.closest(".settings-custom-select");
        const select = custom.previousElementSibling;
        select.value = customOption.dataset.customSelectOption;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        $("[data-custom-select-trigger] span", custom).textContent = customOption.textContent.trim();
        $$("[data-custom-select-option]", custom).forEach(option => option.setAttribute("aria-selected", String(option === customOption)));
        custom.classList.remove("open");
        $("[data-custom-select-trigger]", custom).setAttribute("aria-expanded", "false");
        return;
      }
      const customTrigger = event.target.closest("[data-custom-select-trigger]");
      if (customTrigger) {
        const custom = customTrigger.closest(".settings-custom-select");
        const willOpen = !custom.classList.contains("open");
        $$(".settings-custom-select.open", backdrop).forEach(select => {
          select.classList.remove("open");
          $("[data-custom-select-trigger]", select)?.setAttribute("aria-expanded", "false");
        });
        custom.classList.toggle("open", willOpen);
        customTrigger.setAttribute("aria-expanded", String(willOpen));
        return;
      }
      $$(".settings-custom-select.open", backdrop).forEach(select => {
        select.classList.remove("open");
        $("[data-custom-select-trigger]", select)?.setAttribute("aria-expanded", "false");
      });
      const nav = event.target.closest("[data-settings-nav], [data-settings-jump]");
      if (nav) {
        activatePane(nav);
        return;
      }
      const toggle = event.target.closest("[data-setting-toggle]");
      if (toggle) {
        const next = toggle.getAttribute("aria-checked") !== "true";
        toggle.classList.toggle("active", next);
        toggle.setAttribute("aria-checked", String(next));
        // 通知权限只能在用户手势里申请，所以就着这次点击问，而不是等保存时。
        if (toggle.name === "desktopNotifications" && next) requestDesktopNotifications(toggle);
        if (toggle.name === "reduceMotion" || toggle.name === "highContrast") previewDisplayPreferences();
        return;
      }
      if (event.target.closest("[data-settings-save]")) {
        saveSettings();
        return;
      }
      const actionButton = event.target.closest("[data-settings-action]");
      if (!actionButton) return;
      const action = actionButton.dataset.settingsAction;
      if (action === "toggle-secret") {
        const input = $("input", actionButton.closest(".secret-field"));
        const visible = input.type === "text";
        input.type = visible ? "password" : "text";
        actionButton.innerHTML = icon(visible ? "eye" : "eye-slash");
      }
      if (action === "test-api") {
        const values = collectSettingsValues();
        const endpoint = endpointFromForm(values);
        if (!endpoint) { toast(t("请先填写有效的 Base URL")); return; }
        void withBusyButton(actionButton, t("正在连接…"), async () => {
          try {
            const result = await authApi.testLlmEndpoint({ ...endpoint, allow_proxy: values.allowProxyApi !== false });
            actionButton.classList.add("connection-ok");
            toast(`${t("连接正常")} · ${result.latency_ms}ms · ${result.model}`);
          } catch (error) {
            actionButton.classList.remove("connection-ok");
            // 404 只会来自我们自己的后端：说明运行中的进程还没加载新路由
            if (error instanceof ApiError && error.code === "NOT_FOUND") {
              toast(t("后端尚未加载新接口，请重启后端服务（npm run dev）"));
              return;
            }
            toast(error instanceof Error && error.message ? error.message : t("连接失败，请检查地址与密钥"));
          }
        });
      }
      if (action === "add-endpoint") {
        const values = collectSettingsValues();
        const editingId = String(values.apiEditingEndpointId || "").trim();
        void withBusyButton(actionButton, t("正在保存…"), async () => {
          if (editingId) {
            const { config, message } = await updateEndpoint(editingId, values);
            toast(t(message));
            if (config) {
              renderEndpointItems(backdrop, config);
              renderProviderStatus(backdrop, config);
            }
            return Boolean(config);
          }
          const message = await saveEndpointAsNew(values);
          toast(t(message));
          await renderEndpointList(backdrop);
          return true;
        }).then(saved => {
          // withBusyButton 会还原按钮内容，编辑态的按钮文案要等它结束后再复位
          if (saved && editingId) exitEndpointEditing(backdrop);
          void hydrateModelPickers();
        });
      }
      if (action === "cancel-endpoint-edit") {
        exitEndpointEditing(backdrop);
        void hydrateLlmPanel(backdrop);
        toast(t("已取消编辑"));
      }
      if (action === "clear-llm-usage") {
        clearLlmUsage();
        renderLlmUsageList(backdrop);
        toast(t("用量记录已清空"));
      }
      if (action === "configure-provider") {
        activatePane($('[data-settings-nav="api"]', backdrop));
        const preset = providerPreset(actionButton.dataset.providerId);
        if (!preset) return;
        const assign = (name, value) => { const control = $(`[name="${name}"]`, backdrop); if (control) control.value = value; };
        assign("apiProfileName", `${preset.label} 官方 API`);
        assign("apiBaseUrl", preset.baseUrl);
        assign("apiModel", preset.models[0] || "");
        assign("apiOrganization", "");
        assign("apiPathPrefix", "");
        assign("apiKey", "");
        setProtocolSelect(backdrop, preset.protocol);
        renderModelOptions(backdrop, preset.models);
        $('[name="apiKey"]', backdrop)?.focus();
        toast(t(preset.id === "ollama"
          ? "已填入本地 Ollama 参数，无需密钥，模型 ID 填你已安装的模型"
          : "已填入官方接口参数，补上 API Key 后点「测试连接」"));
      }
      if (action === "reset-defaults") {
        localStorage.removeItem("openmathmodelSettings");
        settingsSaved = true;
        applyLocale("zh-CN");
        applyDisplayPreferences({});
        closeSettings();
        openSettingsCenter();
        toast(t("已恢复默认设置"));
      }
      if (action === "export-usage") {
        void withBusyButton(actionButton, t("正在导出…"), async () => {
          toast(t(await exportUsageCsv()));
        });
      }
      if (action === "export-data") toast("数据导出申请已提交，完成后会通知你");
      if (action === "clear-cache") toast("本地缓存已清理，共释放 386 MB");
      if (action === "delete-account") toast("演示环境不会执行账户删除");
      if (action === "endpoint-menu") {
        const endpointId = actionButton.dataset.endpointId;
        if (!endpointId) return;
        popupMenu(actionButton, ["设为主接口", "编辑", "调整权重", "删除"], choice => {
          if (choice === "编辑") {
            void (async () => {
              const config = await fetchLlmConfig();
              const endpoint = config?.endpoints.find(item => item.id === endpointId);
              if (!endpoint) { toast(t("操作失败，请确认已登录")); return; }
              enterEndpointEditing(backdrop, endpoint);
              toast(t("正在编辑该接口，改完点「保存修改」"));
            })();
            return;
          }
          if (choice === "调整权重") {
            const weightItems = [
              "自动推断",
              ...Array.from({ length: 10 }, (_, index) => {
                const value = index + 1;
                return `权重 ${value}${value === 1 ? "（最弱）" : value === 10 ? "（最强）" : ""}`;
              }),
            ];
            popupMenu(actionButton, weightItems, weightChoice => {
              const weight = weightChoice === "自动推断" ? 0 : Number(weightChoice.match(/\d+/)?.[0] || 0);
              void (async () => {
                const config = await setEndpointWeight(endpointId, weight);
                if (!config) { toast(t("操作失败，请确认已登录")); return; }
                toast(t(weight ? "权重已更新" : "已恢复自动推断"));
                renderEndpointItems(backdrop, config);
                void hydrateModelPickers();
              })();
            });
            return;
          }
          void (async () => {
            const config = choice === "设为主接口"
              ? await setPrimaryEndpoint(endpointId)
              : await removeEndpoint(endpointId);
            if (!config) { toast(t("操作失败，请确认已登录")); return; }
            toast(t(choice === "设为主接口" ? "已设为主接口" : "接口已删除"));
            renderEndpointItems(backdrop, config);
            renderProviderStatus(backdrop, config);
            void hydrateModelPickers();
          })();
        });
      }
      if (action === "network-diagnosis") void runDiagnosticsInto(actionButton, $("[data-diagnostic-report]", backdrop));
      if (action === "export-diagnostics") void exportDiagnostics(actionButton);
      if (action === "copy-system-info") void copySystemInfo(actionButton);
    });
    $("[data-font-size]", backdrop).addEventListener("input", event => {
      $("[data-font-output]", backdrop).textContent = `${event.target.value} px`;
      previewDisplayPreferences();
    });
    // 语言与主题一样即时预览：自定义下拉在选中后会向原生 select 派发 change。
    backdrop.addEventListener("change", event => {
      if (event.target?.name === "interfaceLanguage") applyLocale(event.target.value);
    });
    // 聚焦模型 ID 时才去要模型列表：地址或密钥还没填完就出网没有意义，
    // 且 fetchEndpointModels 自带按接口缓存，反复聚焦不会反复请求。
    backdrop.addEventListener("focusin", event => {
      if (event.target?.name === "apiModel") void refreshModelOptions(backdrop, collectSettingsValues());
    });
    document.addEventListener("keydown", onSettingsKeydown);
    restoreSettings();
    enhanceSettingsSelects();
    // 并发上限以服务端为准，异步回填显示（覆盖本机残留值）。
    void hydrateMaxConcurrency(backdrop);
    // 自定义 API 同理：表单、开关与已保存接口列表都以服务端为准回填。
    void hydrateLlmPanel(backdrop);
    // 用量监控：统计卡、柱状图、模型分布与预算表单都来自服务端记录。
    void hydrateUsagePane(backdrop);
    // 数据与隐私：开关与保留策略以服务端为准回填（未登录保持本机显示）。
    void hydratePrivacyPane(backdrop);
    if (initialPane) {
      const nav = $(`[data-settings-nav="${initialPane}"]`, backdrop);
      if (nav) activatePane(nav);
    }
    $(".settings-close", backdrop).focus();
  }

  function popupMenu(anchor, items, onPick, decorateItem) {
    $(".menu")?.remove();
    const menu = document.createElement("div");
    menu.className = "menu";
    menu.innerHTML = items.map(i => `<button data-menu-value="${i}">${i}</button>`).join("");
    // 字体菜单等场景给每个选项做所见即所得装饰（例如用对应字体渲染选项本身）
    if (decorateItem) $$("button", menu).forEach(button => decorateItem(button, button.dataset.menuValue));
    document.body.appendChild(menu);
    const rect = anchor.getBoundingClientRect();
    menu.style.left = `${Math.min(rect.left, window.innerWidth - 190)}px`;
    menu.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - items.length * 38 - 16)}px`;
    menu.addEventListener("click", e => {
      const button = e.target.closest("button");
      if (!button) return;
      anchor.dataset.value = button.dataset.menuValue;
      if (onPick) onPick(button.dataset.menuValue);
      else toast(`已选择：${button.dataset.menuValue}`);
      menu.remove();
    });
    setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }), 0);
  }

  /**
   * 「添加上下文」二级选择器：从赛题库 / 优秀论文 / 方法库挑一份资料。
   * 任务页 → 变成 composer 引用 chip，随下一条消息进入模型上下文；
   * 首页新任务 → 没有对话链路，改为把一句参考描述插入任务描述文本。
   */
  async function openReferencePicker(libraryLabel, composer) {
    let entries = [];
    if (libraryLabel === "方法库") {
      entries = methodLibrary.map(method => ({
        reference: methodReference(method),
        meta: `${method.category} · ${method.subtitle}`,
        haystack: `${method.name} ${method.subtitle} ${method.category}`.toLowerCase(),
      }));
    } else {
      if (!problems.length) {
        toast(t("正在加载知识库…"));
        try {
          await preloadKnowledgeLibrary();
        } catch {
          toast(t("知识库加载失败，请稍后重试"));
          return;
        }
      }
      entries = libraryLabel === "赛题库"
        ? problems.map(problem => ({
          reference: problemReference(problem),
          meta: `${problem.competition} · ${problem.year} · ${problem.problem_type}`,
          haystack: `${problem.code} ${problem.title} ${problem.competition} ${(problem.keywords || []).join(" ")}`.toLowerCase(),
        }))
        // 与优秀论文页同口径：只提供有完整正文的论文，标题用解析后的主题名。
        // 美赛结果页的名单元数据（metadata_only、无全文）不进入引用，避免引用一份读不到的论文。
        : paperEntries().map(({ paper, displayTitle }) => ({
          reference: paperReference({ ...paper, title: displayTitle }),
          meta: [`${paper.competition} ${paper.year}`, paper.award, paper.institution].filter(Boolean).join(" · "),
          haystack: `${displayTitle} ${paper.title} ${paper.problem_code} ${(paper.models || []).join(" ")} ${paper.institution || ""}`.toLowerCase(),
        }));
    }

    $(".reference-picker")?.remove();
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop reference-picker";
    backdrop.innerHTML = `<div class="modal" role="dialog" aria-modal="true">
      <h2>${t("添加上下文引用")} · ${t(libraryLabel)}</h2>
      <div class="reference-search">${icon("magnifying-glass")}<input type="search" placeholder="${t("搜索标题、关键词…")}" aria-label="${t("搜索引用条目")}"></div>
      <div class="reference-picker-list" data-reference-list></div>
    </div>`;
    document.body.appendChild(backdrop);
    const list = $("[data-reference-list]", backdrop);
    const input = $("input", backdrop);

    const renderList = keyword => {
      const matches = keyword ? entries.filter(entry => entry.haystack.includes(keyword)) : entries;
      const rows = matches.slice(0, 30).map(entry => `
        <button type="button" data-reference-index="${entries.indexOf(entry)}">
          <strong>${escapeHtml(entry.reference.title)}</strong>
          <span>${escapeHtml(entry.meta)}</span>
        </button>`);
      list.innerHTML = rows.length
        ? rows.join("") + (matches.length > 30 ? `<div class="reference-picker-empty">${t("仅显示前 30 条，继续输入关键词缩小范围")}</div>` : "")
        : `<div class="reference-picker-empty">${t("没有匹配的条目")}</div>`;
    };
    renderList("");
    input.addEventListener("input", () => renderList(input.value.trim().toLowerCase()));
    input.focus();

    const onKeydown = event => {
      if (!document.body.contains(backdrop)) {
        document.removeEventListener("keydown", onKeydown, true);
        return;
      }
      if (event.key === "Escape") {
        backdrop.remove();
        document.removeEventListener("keydown", onKeydown, true);
      }
    };
    document.addEventListener("keydown", onKeydown, true);

    backdrop.addEventListener("click", event => {
      if (event.target === backdrop) {
        backdrop.remove();
        return;
      }
      const row = event.target.closest("[data-reference-index]");
      if (!row) return;
      const entry = entries[Number(row.dataset.referenceIndex)];
      if (!entry) return;
      backdrop.remove();
      // 统一交互：文本框上方出现引用 chip。首页的 chips 随任务创建交接到运行页
      //（task-start-controller 按 run 写入，运行页对话挂载时取回）。
      if (composer) mountReferenceChips(composer);
      const result = addComposerReference(entry.reference);
      if (result === "duplicate") toast(t("该资料已在引用列表中"));
      else if (result === "full") toast(t("最多引用 4 份资料，先移除一份再添加"));
      else toast(t("已添加引用"));
    });
  }

  /** 已保存接口列表：从服务端配置渲染（null = 未登录/后端不可用）。 */
  function renderEndpointItems(backdrop, config) {
    const host = $("[data-endpoint-list]", backdrop);
    if (!host) return;
    if (!config) {
      host.innerHTML = `<div class="endpoint-item"><span class="endpoint-dot"></span><div><strong>登录后可管理已保存接口</strong><span>接口配置保存在本机后端，随账户生效</span></div></div>`;
      return;
    }
    if (!config.endpoints.length) {
      host.innerHTML = `<div class="endpoint-item"><span class="endpoint-dot"></span><div><strong>尚未保存任何接口</strong><span>填写上方表单后点击「保存为新接口」</span></div></div>`;
      return;
    }
    host.innerHTML = config.endpoints.map(endpoint => {
      const active = endpoint.id === config.active_endpoint_id;
      const weightText = endpoint.weight ? `权重 ${endpoint.weight}` : "权重自动";
      return `<div class="endpoint-item"><span class="endpoint-dot ${active ? "online" : ""}"></span><div><strong>${escapeHtml(endpoint.name)}</strong><span>${escapeHtml(endpoint.base_url)} · ${escapeHtml(endpoint.model || "未填模型")}</span></div><span>${active ? "主接口" : "备用"} · ${weightText}</span><button type="button" data-settings-action="endpoint-menu" data-endpoint-id="${escapeHtml(endpoint.id || "")}" aria-label="接口操作">${icon("dots-three")}</button></div>`;
    }).join("");
  }

  async function renderEndpointList(backdrop) {
    renderEndpointItems(backdrop, await fetchLlmConfig());
  }

  /** 接口用量记录列表：随「允许使用第三方中转站」开关变化（关闭即停记）。 */
  function renderLlmUsageList(backdrop) {
    const host = $("[data-llm-usage-list]", backdrop);
    if (!host) return;
    if (!proxyTransparencyEnabled()) {
      host.innerHTML = `<div class="endpoint-item"><span class="endpoint-dot"></span><div><strong>用量记录已停用</strong><span>开启上方「允许使用第三方中转站」并保存后恢复记录</span></div></div>`;
      return;
    }
    const records = listLlmUsage();
    if (!records.length) {
      host.innerHTML = `<div class="endpoint-item"><span class="endpoint-dot"></span><div><strong>暂无调用记录</strong><span>在任务页与 Agent 对话后，这里会列出每次调用的实际接口</span></div></div>`;
      return;
    }
    host.innerHTML = records.slice(0, 8).map(record => {
      const when = new Date(record.ts).toLocaleString("zh-CN", { hour12: false });
      const tags = [
        record.third_party ? "第三方中转站" : "官方/本机",
        record.difficulty ? `Auto 难度 ${record.difficulty}/5` : "",
        record.fallback_used ? "已用备用" : "",
      ].filter(Boolean).join(" · ");
      const tokens = Number.isFinite(record.prompt_tokens) || Number.isFinite(record.completion_tokens)
        ? `${record.prompt_tokens ?? 0}+${record.completion_tokens ?? 0} tok`
        : (Number.isFinite(record.elapsed_ms) ? `${(record.elapsed_ms / 1000).toFixed(1)}s` : "");
      return `<div class="endpoint-item"><span class="endpoint-dot ${record.third_party ? "" : "online"}"></span><div><strong>${escapeHtml(record.host || record.endpoint || "未知接口")} · ${escapeHtml(record.model || "-")}</strong><span>${escapeHtml(when)}${tags ? ` · ${escapeHtml(tags)}` : ""}</span></div><span>${escapeHtml(tokens)}</span></div>`;
    }).join("");
  }

  /** 「编辑」某条已保存接口：回填表单并进入编辑态，「保存」写回原接口。 */
  function enterEndpointEditing(backdrop, endpoint) {
    const assign = (name, value) => {
      const control = $(`[name="${name}"]`, backdrop);
      if (control) control.value = value ?? "";
    };
    assign("apiProfileName", endpoint.name);
    assign("apiBaseUrl", endpoint.base_url);
    assign("apiKey", endpoint.api_key);
    assign("apiModel", endpoint.model);
    assign("apiOrganization", endpoint.organization);
    assign("customHeader", endpoint.headers);
    assign("apiPathPrefix", endpoint.path_prefix);
    assign("apiWeight", endpoint.weight || "");
    assign("apiEditingEndpointId", endpoint.id || "");
    setProtocolSelect(backdrop, endpoint.protocol);
    const saveLabel = $("[data-endpoint-save-label]", backdrop);
    if (saveLabel) saveLabel.innerHTML = `${icon("check")} 保存修改`;
    const cancel = $("[data-endpoint-edit-cancel]", backdrop);
    if (cancel) cancel.hidden = false;
    $('[data-settings-pane="api"]', backdrop)?.scrollIntoView?.({ block: "start" });
    $('[name="apiProfileName"]', backdrop)?.focus();
  }

  function exitEndpointEditing(backdrop) {
    const marker = $('[name="apiEditingEndpointId"]', backdrop);
    if (marker) marker.value = "";
    const saveLabel = $("[data-endpoint-save-label]", backdrop);
    if (saveLabel) saveLabel.innerHTML = `${icon("plus")} 保存为新接口`;
    const cancel = $("[data-endpoint-edit-cancel]", backdrop);
    if (cancel) cancel.hidden = true;
  }

  /** 「协议」下拉：原生 select 与增强的自定义下拉都要同步，否则显示会分叉。 */
  function setProtocolSelect(backdrop, protocol) {
    const select = $('[name="apiProtocol"]', backdrop);
    if (!select) return;
    const label = labelFromProtocol(protocol);
    const option = [...select.options].find(item => (item.textContent || "").trim() === label);
    if (!option) return;
    select.value = option.value;
    const custom = select.nextElementSibling;
    if (custom instanceof HTMLElement && custom.classList.contains("settings-custom-select")) {
      const trigger = $("[data-custom-select-trigger] span", custom);
      if (trigger) trigger.textContent = label;
      $$("[data-custom-select-option]", custom).forEach(button => {
        button.setAttribute("aria-selected", String(button.getAttribute("data-custom-select-option") === option.value));
      });
    }
  }

  /**
   * 「默认模型 ID」的补全列表。两级来源：先按 Base URL 域名秒填厂商预设里的
   * 型号（离线、立即可用），再向接口本身要一次真实清单合并进来——预设表是
   * 快照会过期，接口自报的才跟得上厂商上新。拉不到就只留预设，不打断填写。
   */
  function renderModelOptions(backdrop, names) {
    const list = $("#apiModelOptions", backdrop);
    if (!list) return;
    list.innerHTML = names.map(name => `<option value="${escapeHtml(name)}"></option>`).join("");
  }

  function seedModelOptions(backdrop, baseUrl) {
    const host = endpointHost(String(baseUrl || "").trim());
    const preset = PROVIDER_PRESETS.find(item => presetMatchesHost(item, host));
    renderModelOptions(backdrop, preset?.models || []);
    return preset;
  }

  async function refreshModelOptions(backdrop, values) {
    const preset = seedModelOptions(backdrop, values.apiBaseUrl);
    const live = await fetchEndpointModels(values);
    if (!live.length) return;
    // 接口自报的排前面（通常新型号在前），预设里有而接口没报的仍然保留
    renderModelOptions(backdrop, [...new Set([...live, ...(preset?.models || [])])]);
  }

  /** 厂商卡片状态：已保存接口中存在同域名的即视为已连接。 */
  function renderProviderStatus(backdrop, config) {
    $$("[data-provider-card]", backdrop).forEach(card => {
      const preset = providerPreset(card.dataset.providerCard);
      const connected = Boolean(
        preset && config?.endpoints.some(item => presetMatchesHost(preset, endpointHost(item.base_url))),
      );
      card.classList.toggle("connected", connected);
      const status = $("[data-provider-status]", card);
      if (status) {
        status.classList.toggle("idle", !connected);
        status.innerHTML = connected ? `${icon("check-circle")} 已连接` : "未配置";
      }
      const button = $('[data-settings-action="configure-provider"]', card);
      if (button) button.textContent = connected ? "管理" : "配置";
    });
  }

  /** 打开设置时回填「自定义 API」：以服务端配置为准覆盖本机残留显示。 */
  async function hydrateLlmPanel(backdrop) {
    // restoreSettings 可能把上次会话的编辑标记复原回来，打开面板一律回到非编辑态
    exitEndpointEditing(backdrop);
    renderLlmUsageList(backdrop);
    const config = await fetchLlmConfig();
    renderEndpointItems(backdrop, config);
    renderProviderStatus(backdrop, config);
    if (!config) return;
    const setToggle = (name, on) => {
      const toggle = $(`[name="${name}"]`, backdrop);
      if (!toggle) return;
      toggle.classList.toggle("active", Boolean(on));
      toggle.setAttribute("aria-checked", String(Boolean(on)));
    };
    setToggle("allowProxyApi", config.allow_proxy);
    setToggle("streamResponse", config.stream);
    setToggle("fallbackApi", config.fallback);
    const active = config.endpoints.find(item => item.id === config.active_endpoint_id) || config.endpoints[0];
    if (!active) return;
    const assign = (name, value) => {
      const control = $(`[name="${name}"]`, backdrop);
      if (control) control.value = value ?? "";
    };
    assign("apiProfileName", active.name);
    assign("apiBaseUrl", active.base_url);
    assign("apiKey", active.api_key);
    assign("apiModel", active.model);
    assign("apiOrganization", active.organization);
    assign("customHeader", active.headers);
    assign("apiPathPrefix", active.path_prefix);
    assign("apiWeight", active.weight || "");
    setProtocolSelect(backdrop, active.protocol);
    // 只按预设铺底，真实清单等用户聚焦模型 ID 时再拉：开设置面板不该顺带出网。
    seedModelOptions(backdrop, active.base_url);
  }

  /**
   * Chart.js 只有数据页和实验页用得到，动态载入避免让其他页面为它买单。
   * 原实现依赖 CDN 注入的 window.Chart，加载失败会静默返回、图表区直接空白；
   * 现在改为本地依赖并显式报错。
   */
  let chartLoader = null;
  function initCharts(screen) {
    const mergedWorkspace = Boolean(workspaceStageContent[screen]);
    if (!mergedWorkspace && screen !== "data" && screen !== "experiments") return;
    chartLoader = chartLoader || import("chart.js/auto").then(module => module.default);
    chartLoader
      .then(Chart => {
        // 合并工作台里五个面板同存，一次把数据页与实验页的图都建好；
        // 隐藏面板的画布在首次显示时靠 resize 自适应到正确尺寸。
        if (mergedWorkspace) {
          renderCharts(Chart, "data");
          renderCharts(Chart, "experiments");
        } else {
          renderCharts(Chart, screen);
        }
      })
      .catch(error => console.error("图表库加载失败，数据可视化不可用", error));
  }

  function renderCharts(Chart, screen) {
    const dark = document.documentElement.dataset.theme === "dark";
    const chartInk = dark ? "#ecece8" : "#171717";
    const chartMuted = dark ? "#9f9f99" : "#8a8a86";
    const chartSurface = dark ? "#20201f" : "#fff";
    Chart.defaults.color = dark ? "#b8b7b1" : "#5f5f5f";
    Chart.defaults.borderColor = dark ? "#3b3b38" : "rgba(0,0,0,.1)";
    Chart.defaults.font.family = 'Inter, "Noto Sans SC", "Microsoft YaHei", sans-serif';
    Chart.defaults.animation = false;
    if (screen === "data") {
      const dailyCanvas = $("#dailyChart");
      const hourCanvas = $("#hourChart");
      if (!dailyCanvas || !hourCanvas) return;
      const daily = Array.from({ length: 90 }, (_, i) => Math.max(350, 420 + i * 28 + Math.sin(i * .82) * 360 + (i % 7 === 0 ? 850 : 0)));
      new Chart(dailyCanvas, {
        type: "line",
        data: { labels: daily.map((_, i) => i % 15 === 0 ? ["01-01","01-31","03-02","04-01","05-01","05-31"][i/15] || "06-30" : ""), datasets: [{ data: daily, borderColor: chartInk, borderWidth: 1.6, pointRadius: 0, tension: .08 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false } }, y: { min: 0, max: 5000, ticks: { stepSize: 1000 } } } }
      });
      const hour = [350,260,160,90,80,140,410,890,1450,1760,1580,1170,850,790,760,810,950,1210,1530,1880,2080,2020,1680,1250];
      new Chart(hourCanvas, {
        type: "line",
        data: { labels: hour.map((_, i) => i), datasets: [{ data: hour, borderColor: chartInk, borderWidth: 1.6, pointBackgroundColor: chartSurface, pointBorderColor: chartInk, pointRadius: 2.3, tension: .26 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, title: { display: true, text: "小时" } }, y: { min: 0, max: 2200 } } }
      });
    }
    if (screen === "experiments") {
      const costCanvas = $("#costChart");
      if (costCanvas) new Chart(costCanvas, {
        type: "bar",
        data: {
          labels: ["基线结果", "当前结果"],
          datasets: [{
            data: [2033414, 1842596],
            backgroundColor: [dark ? "#6d6d69" : "#c7c7c7", dark ? "#ecece8" : "#171717"],
            borderRadius: 1,
            barThickness: 132,
            maxBarThickness: 132
          }]
        },
        plugins: [{
          id: "costLabels",
          afterDatasetsDraw(chart) {
            const { ctx } = chart;
            ctx.save();
            ctx.textAlign = "center";
            ctx.font = '500 13px Inter, "Microsoft YaHei", sans-serif';
            ctx.fillStyle = chartInk;
            chart.getDatasetMeta(0).data.forEach((bar, index) => ctx.fillText(["2,033,414", "1,842,596"][index], bar.x, bar.y - 10));
            ctx.restore();
          }
        }],
        options: {
          responsive: true,
          maintainAspectRatio: false,
          layout: { padding: { top: 24, right: 12, left: 4 } },
          plugins: { legend: { display: false }, tooltip: { enabled: true } },
          scales: {
            x: { grid: { display: false }, ticks: { font: { size: 12 } } },
            y: { min: 0, max: 3000000, ticks: { stepSize: 500000, callback: value => value === 0 ? "0" : `${value / 1000}k` } }
          }
        }
      });
      const resultMetricCanvas = $("#resultMetricChart");
      if (resultMetricCanvas) new Chart(resultMetricCanvas, {
        type: "bar",
        data: {
          labels: ["总行程时间", "满足率", "平衡度"],
          datasets: [
            { label: "基线", data: [100, 86.4, 78.1], backgroundColor: dark ? "#62625e" : "#d0d0cc", borderRadius: 3 },
            { label: "当前", data: [90.62, 92.8, 84.6], backgroundColor: chartInk, borderRadius: 3 }
          ]
        },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10 } } }, scales: { x: { grid: { display: false } }, y: { min: 0, max: 110, ticks: { callback: value => `${value}%` } } } }
      });
      const stabilityCanvas = $("#stabilityChart");
      if (stabilityCanvas) new Chart(stabilityCanvas, {
        type: "line",
        data: { labels: ["7", "21", "42", "73", "99"], datasets: [{ data: [1854208, 1839675, 1842596, 1861034, 1838147], borderColor: chartInk, backgroundColor: chartSurface, borderWidth: 1.8, pointRadius: 3, pointBackgroundColor: chartSurface, pointBorderColor: chartInk, tension: .25 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, title: { display: true, text: "随机种子" } }, y: { min: 1800000, max: 1900000, ticks: { callback: value => `${Math.round(value / 1000)}k` } } } }
      });
    }
  }

  /**
   * 任务开场分析：控制器广播规划阶段（omm:run-planning）时，Agent 像回复
   * 聊天消息一样先「思考」再给出对任务的开场分析——真实模型调用，思考块
   * 与流式 Markdown 均与手动对话同构；未配置接口/未登录时整块静默消失，
   * 每个运行只发起一次（sessionStorage 防重，刷新页面不重复扣费）。
   */
  document.addEventListener("omm:run-planning", event => {
    const runId = event.detail?.runId;
    const scroll = $(".chat-scroll");
    if (!runId || !scroll) return;
    // 防重标记只在拿到回复后才落（见下方 then）：一次失败——比如接口报错——
    // 不该把这个任务永久钉成「没有开场分析」，重新进来还在规划阶段就再试一次。
    const guard = `openmathmodelOpeningReply.${runId}`;
    try {
      if (sessionStorage.getItem(guard)) return;
    } catch {
      return;
    }
    const replyId = `reply-opening-${Date.now()}`;
    // 开场分析与执行步骤同属一条 Agent 消息：注入步骤块头部之后，
    // 思考完成 → 分析正文 → 计划在同一气泡内接续展开，不拆成两条对话。
    const stepsBlock = $(".chat-scroll .assistant-block:not(.follow-up-reply)");
    const host = document.createElement("div");
    host.className = "opening-analysis opening-reply";
    host.id = replyId;
    host.innerHTML = `<div class="analysis-copy"><p class="thinking-plain"><span class="thinking-label thinking-shimmer">${t("思考中…")}</span></p></div>`;
    const anchor = stepsBlock?.querySelector(".assistant-id");
    if (anchor) anchor.insertAdjacentElement("afterend", host);
    else if (stepsBlock) stepsBlock.prepend(host);
    else scroll.append(host);
    // 开场分析结束前，计划部分（折叠开关/步骤/摘要/CTA）一律不出现
    if (stepsBlock) stepsBlock.dataset.openingState = "pending";
    scroll.scrollTo({ top: scroll.scrollHeight, behavior: "smooth" });
    void streamAssistantReply(
      OPENING_ANALYSIS_PROMPT,
      replyId,
      scroll,
      { removeOnUnavailable: true, opening: true },
    ).then(delivered => {
      if (delivered) {
        try { sessionStorage.setItem(guard, "1"); } catch {}
      }
    }).finally(() => {
      // 回复彻底结束（成功、失败或静默移除）后才放行计划部分，并立即通知
      // 控制器重渲染，不必等下一次 SSE 刷新。
      if (stepsBlock) stepsBlock.dataset.openingState = "done";
      document.dispatchEvent(new CustomEvent("omm:opening-analysis-done"));
    });
  });

  /**
   * 重新进入任务（点最近任务、刷新、换标签页打开）：控制器拿到工作台快照后
   * 广播运行身份与题面。这里做两件事，都只针对当前 run，不碰别的任务数据：
   * 1. 首条用户气泡换成该运行的真实题面——模板兜底文案与同标签页上一个任务
   *    留下的 openmathmodelPrompt 都不再出现（数据隔离）；
   * 2. 按本机对话记录（「保存任务历史」管辖，按 run_id 隔离）重建开场分析与
   *    此前的对话气泡，恢复离开前的对话现场。
   */
  document.addEventListener("omm:conversation-restore", event => {
    const { runId, goal } = event.detail ?? {};
    const scroll = $(".chat-scroll");
    if (!runId || !scroll) return;
    const firstBubble = $(".user-message .user-bubble", scroll);
    if (firstBubble && goal) firstBubble.textContent = goal;
    if (scroll.dataset.conversationRestored === runId) return;
    scroll.dataset.conversationRestored = runId;
    // 首页随任务创建交接过来的引用：挂回输入框上方的 chips，随首条消息进入上下文。
    const composerHost = $(".chat-pane .composer");
    if (composerHost) restorePendingTaskReferences(runId, composerHost);
    const entries = loadConversationLog(runId);
    if (entries.length === 0) return;
    const stepsBlock = $(".assistant-block:not(.follow-up-reply)", scroll);
    for (const entry of entries) {
      if (entry.role === "assistant" && entry.opening) {
        if (!stepsBlock || stepsBlock.querySelector(".opening-reply")) continue;
        const host = document.createElement("div");
        host.className = "opening-analysis opening-reply";
        host.innerHTML = `<div class="analysis-copy">${renderMarkdown(entry.text)}</div>`;
        const anchor = stepsBlock.querySelector(".assistant-id");
        if (anchor) anchor.insertAdjacentElement("afterend", host);
        else stepsBlock.prepend(host);
        renderFormulas(host);
        appendReplyActions(host, entry.text);
        stepsBlock.dataset.openingState = "done";
        // 已恢复的开场分析不再自动重发（规划阶段重进页面时防重复扣费）。
        try { sessionStorage.setItem(`openmathmodelOpeningReply.${runId}`, "1"); } catch {}
        continue;
      }
      if (entry.role === "user") {
        const chips = entry.attachments?.length
          ? `<div class="user-attachment-chips">${entry.attachments.map(name =>
            `<span class="user-attachment-chip">${icon("paperclip")}${escapeHtml(name)}</span>`).join("")}</div>`
          : "";
        scroll.insertAdjacentHTML("beforeend", `<div class="user-message"><div class="user-bubble">${escapeHtml(entry.text)}${chips}</div></div>`);
        continue;
      }
      const replyBlock = document.createElement("div");
      replyBlock.className = "assistant-block follow-up-reply";
      // 有轨迹的历史回复按原样重建折叠头与过程区（本次改造前的旧记录没有
      // 轨迹字段，保持纯文本形态）；行内耗时用落盘的最终值，不再走秒。
      const traceMarkup = entry.trace?.length
        ? `<button type="button" class="activity-summary" data-action="toggle-activity" aria-expanded="true">${icon("eye-slash")} 收起执行步骤 ${icon("caret-up")}</button>
        <div class="agent-stream reply-trace"></div>`
        : "";
      replyBlock.innerHTML = `
        <div class="assistant-id">${projectLogo("assistant-logo")}<span>Agent</span></div>
        ${traceMarkup}
        <div class="analysis-copy">${renderMarkdown(entry.text)}</div>`;
      entry.trace?.forEach(row => appendReplyTraceRow(replyBlock, {
        icon: row.icon,
        title: row.title,
        suffix: row.suffix ?? "",
        detail: row.detail ?? "",
        elapsed: row.elapsed ?? "",
      }));
      scroll.append(replyBlock);
      renderFormulas($(".analysis-copy", replyBlock));
      appendReplyActions(replyBlock, entry.text);
    }
    scroll.scrollTop = scroll.scrollHeight;
  });

  function appendConversationTurn(text, composer) {
    const scroll = $(".chat-scroll");
    if (!scroll) return;
    // 随消息发送的附件（ADR-0010 批次三）与「添加上下文」引用：
    // 气泡下方以纸夹/@ 徽标如实展示。
    const store = composer ? attachmentsOf(composer) : undefined;
    const attachmentNames = (store?.list() ?? []).map(item => item.file.name);
    const referenceNames = listComposerReferences().map(reference => `@${reference.title}`);
    const chipNames = [...attachmentNames, ...referenceNames];
    const chips = chipNames.length
      ? `<div class="user-attachment-chips">${chipNames.map(name =>
        `<span class="user-attachment-chip">${icon("paperclip")}${escapeHtml(name)}</span>`).join("")}</div>`
      : "";
    const replyId = `reply-${Date.now()}`;
    // 每条回复与首条 Agent 消息同构：「收起/查看执行步骤」折叠头 + 执行过程区
    // （.reply-trace，本轮真实发生的上下文读取/附件解析/难度路由/生成计时）
    // → 思考块 → 正文。过程行由 streamAssistantReply 按实际发生顺序写入。
    scroll.insertAdjacentHTML("beforeend", `
      <div class="user-message"><div class="user-bubble">${escapeHtml(text)}${chips}</div></div>
      <div class="assistant-block follow-up-reply" id="${replyId}">
        <div class="assistant-id">${projectLogo("assistant-logo")}<span>Agent</span></div>
        <button type="button" class="activity-summary" data-action="toggle-activity" aria-expanded="true">${icon("eye-slash")} 收起执行步骤 ${icon("caret-up")}</button>
        <div class="agent-stream reply-trace"></div>
        <div class="analysis-copy"><p class="thinking-plain"><span class="thinking-label thinking-shimmer">${t("思考中…")}</span></p></div>
      </div>`);
    scroll.scrollTo({ top: scroll.scrollHeight, behavior: "smooth" });
    void streamAssistantReply(text, replyId, scroll, { attachments: store });
  }

  /**
   * 思考过程块：推理型模型输出 reasoning 时插在回答上方。流式期间展开、
   * 自动跟随最新内容；回答开始后折叠为「已思考 N 秒」，可点击展开回看。
   */
  function createThinkingBlock(replyBlock) {
    const host = document.createElement("div");
    host.className = "reply-thinking";
    host.innerHTML = `
      <button type="button" class="thinking-header" aria-expanded="true" aria-label="展开或收起思考过程">
        <span class="thinking-label thinking-shimmer">${t("思考中…")}</span>
        ${icon("caret-up", "thinking-chevron")}
      </button>
      <div class="thinking-collapsible">
        <div class="thinking-inner">
          <div class="thinking-viewport"><div class="thinking-stream"></div></div>
        </div>
      </div>`;
    replyBlock.insertBefore(host, replyBlock.querySelector(".analysis-copy"));
    const header = $(".thinking-header", host);
    const label = $(".thinking-label", host);
    const collapsible = $(".thinking-collapsible", host);
    const viewport = $(".thinking-viewport", host);
    const stream = $(".thinking-stream", host);
    const startedAt = Date.now();
    let done = false;
    let open = true;
    const applyOpen = () => {
      header.setAttribute("aria-expanded", String(open));
      collapsible.classList.toggle("is-collapsed", !open);
    };
    header.addEventListener("click", () => {
      if (!done) return;
      open = !open;
      applyOpen();
      if (open) viewport.scrollTop = 0;
    });
    // 文本赋值与布局读写按节流节奏合并：高频 reasoning 增量不再逐条触发重排
    const sink = createThrottledTextSink(fullText => {
      stream.textContent = fullText;
      viewport.classList.toggle("is-capped", viewport.scrollHeight > viewport.clientHeight + 1);
      viewport.scrollTop = viewport.scrollHeight;
    });
    return {
      append(fullText) {
        sink.update(fullText);
      },
      finish() {
        if (done) return;
        done = true;
        sink.flush();
        const seconds = Math.max(1, Math.round((Date.now() - startedAt) / 1000));
        label.classList.remove("thinking-shimmer");
        label.innerHTML = `<span class="thinking-verb">${t("已思考")}</span> ${seconds} ${t("秒")}`;
        header.classList.add("is-clickable");
        open = false;
        applyOpen();
      },
    };
  }

  /** 回复轨迹的耗时文本：与工作台时间线同一节奏（<10s 一位小数，<60s 整秒，更长分+秒）。 */
  function formatTraceElapsed(ms) {
    const clamped = Math.max(0, ms);
    if (clamped < 10_000) return `${(clamped / 1000).toFixed(1)}s`;
    if (clamped < 60_000) return `${Math.round(clamped / 1000)}s`;
    const minutes = Math.floor(clamped / 60_000);
    const seconds = Math.round((clamped % 60_000) / 1000);
    return `${minutes}m ${seconds}s`;
  }

  /**
   * 回复内的执行过程行：与工作台执行轨迹同构（stream-item 结构与交互），
   * 承载本轮真实发生的过程（上下文读取、附件解析、Auto 难度路由、生成计时），
   * 出现在思考块与正文之前。标签与后缀分节点写入，便于语言切换逐段翻译。
   * waiting=true 时本地走秒，settle() 落定图标与最终耗时；before 指定插入
   * 位置以保持与服务端实际发生顺序一致。返回 {element, settle}。
   */
  function appendReplyTraceRow(replyBlock, { icon: iconName, title, suffix = "", detail = "", elapsed = "", waiting = false, before = null }) {
    const trace = $(".reply-trace", replyBlock);
    if (!trace) return null;
    const item = document.createElement("div");
    item.className = `stream-item stream-in${waiting ? " is-waiting" : ""}`;
    item.innerHTML = `
      <div class="stream-row">
        ${icon(iconName)}
        <span class="stream-title"><span>${escapeHtml(title)}</span>${suffix ? `<span>${escapeHtml(suffix)}</span>` : ""}</span>
        <time class="stream-elapsed">${escapeHtml(elapsed)}</time>
      </div>`;
    if (detail) {
      const row = $(".stream-row", item);
      row.classList.add("is-expandable");
      row.setAttribute("role", "button");
      row.tabIndex = 0;
      row.setAttribute("aria-expanded", "false");
      row.insertAdjacentHTML("beforeend", icon("caret-down", "stream-chevron"));
      const detailHost = document.createElement("div");
      detailHost.className = "stream-detail";
      detailHost.hidden = true;
      const pre = document.createElement("pre");
      pre.textContent = detail;
      detailHost.append(pre);
      item.append(detailHost);
      const toggleDetail = () => {
        detailHost.hidden = !detailHost.hidden;
        row.setAttribute("aria-expanded", String(!detailHost.hidden));
      };
      row.addEventListener("click", toggleDetail);
      row.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggleDetail();
        }
      });
    }
    if (before && before.parentElement === trace) trace.insertBefore(item, before);
    else trace.append(item);
    const timeCell = $(".stream-elapsed", item);
    const titleLabel = $(".stream-title > span", item);
    let ticker = null;
    const startedAt = Date.now();
    if (waiting) {
      timeCell.textContent = formatTraceElapsed(0);
      ticker = window.setInterval(() => {
        timeCell.textContent = formatTraceElapsed(Date.now() - startedAt);
      }, 1000);
    }
    return {
      element: item,
      settle({ title: settledTitle = "", elapsedMs = null, failed = false } = {}) {
        if (ticker !== null) {
          window.clearInterval(ticker);
          ticker = null;
        }
        item.classList.remove("is-waiting");
        if (settledTitle) titleLabel.textContent = settledTitle;
        timeCell.textContent = formatTraceElapsed(elapsedMs ?? Date.now() - startedAt);
        const iconEl = $(".stream-row > i", item);
        if (iconEl) iconEl.className = failed ? "ph-fill ph-x-circle" : "ph-fill ph-check-circle";
      },
    };
  }

  /** 回复右下角操作区：复制原始回复文本（Markdown 源码，便于粘贴到论文与笔记）。 */
  function appendReplyActions(replyBlock, replyText) {
    const actions = document.createElement("div");
    actions.className = "reply-actions";
    actions.innerHTML = `<button type="button" class="reply-action-button" data-reply-copy title="复制回复" aria-label="复制回复">${icon("copy")}</button>`;
    replyBlock.appendChild(actions);
    const button = $("[data-reply-copy]", actions);
    let resetTimer = null;
    button.addEventListener("click", async () => {
      const copied = await copyTextToClipboard(replyText);
      button.innerHTML = icon(copied ? "check" : "copy");
      toast(t(copied ? "回复已复制" : "复制失败，请手动选择文本"));
      clearTimeout(resetTimer);
      resetTimer = setTimeout(() => { button.innerHTML = icon("copy"); }, 1400);
    });
  }

  /** 「发送请求前显示实际域名」：按当前模型选择解析本次对话将请求的接口域名。
   *  指定接口/默认主接口时目标确定；Auto 多接口时由服务端难度判定，
   *  实际域名以 meta 事件为准（返回 auto 标记先显示路由中）。 */
  async function expectedChatTarget() {
    let raw = "auto";
    try {
      raw = localStorage.getItem("openmathmodelSelectedModel") || "auto";
    } catch {
      // 存储不可用时按 Auto 处理，与发送通道的路由取值保持一致
    }
    const config = await fetchLlmConfig();
    if (!config || config.endpoints.length === 0) return null;
    if (raw.startsWith("endpoint-")) {
      const endpoint = config.endpoints.find(item => item.id === raw.slice("endpoint-".length));
      return endpoint ? { auto: false, host: endpointHost(endpoint.base_url) } : null;
    }
    if (raw === "auto" && config.endpoints.length > 1) return { auto: true, host: "" };
    const active = config.endpoints.find(item => item.id === config.active_endpoint_id) || config.endpoints[0];
    return active ? { auto: false, host: endpointHost(active.base_url) } : null;
  }

  // ── 暂停生成：回复流式期间发送键变为暂停键，点击中止当前这轮生成 ──────────
  let activeChatAbort = null;

  function setComposerGenerating(controller) {
    activeChatAbort = controller;
    $$('.composer [data-action="send"]').forEach(button => {
      if (controller) {
        button.dataset.mode = "stop";
        button.innerHTML = '<i class="ph-fill ph-stop" aria-hidden="true"></i>';
        button.title = t("暂停生成");
        button.setAttribute("aria-label", t("暂停生成"));
      } else {
        delete button.dataset.mode;
        button.innerHTML = icon("arrow-up");
        button.title = t("发送（Enter）");
        button.setAttribute("aria-label", t("发送"));
      }
    });
  }

  /** 真实模型回复：思考块 + Markdown 正文流式渲染到回复气泡。
   *  对话页不暴露模型名；接口域名随「允许使用第三方中转站」开关：开启时在
   *  发送前显示本次请求的实际域名（含中转/备用标记）并把用量写入本机记录
   *  （设置中心可查），关闭时域名与记录都不留。
   *  options.removeOnUnavailable：未配置接口/未登录时整块静默移除
   *  （用于系统自动发起的开场分析，不该向用户弹配置提示）。
   *  返回是否真的拿到了回复：调用方据此决定要不要落防重标记。 */
  async function streamAssistantReply(text, replyId, scroll, options = {}) {
    const replyBlock = document.getElementById(replyId);
    const copy = replyBlock?.querySelector(".analysis-copy");
    if (!replyBlock || !copy) return false;
    const nearBottom = () => scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120;
    // 流式正文渲染：节流 + 块级增量上屏（stream-render），公式排版随之削峰。
    // 旧写法逐增量整段重建 innerHTML + 全量排版，长回复（尤其多公式）明显卡顿。
    const renderer = createStreamingMarkdownRenderer(copy, { stickTo: scroll });
    const transparency = proxyTransparencyEnabled();
    // 「发送请求前显示实际域名」：开关开启时先显示预期目标，meta 到达后以实际为准
    let transparencySettled = false;
    const renderTransparency = html => {
      let line = replyBlock.querySelector(".chat-transparency");
      if (!line) {
        line = document.createElement("p");
        line.className = "chat-transparency";
        copy.insertAdjacentElement("beforebegin", line);
      }
      line.innerHTML = `${icon("globe")}<span>${html}</span>`;
    };
    if (transparency) {
      void expectedChatTarget().then(target => {
        if (transparencySettled || !target) return;
        renderTransparency(
          target.auto
            ? t("自动路由选择接口中…")
            : `${t("请求发送至")} ${escapeHtml(target.host)}`,
        );
      });
    }
    let thinking = null;
    let answerStarted = false;
    // 生成计时行提升到 try 外：失败路径也要把它落定为中断态
    let generatingRow = null;
    let startedGeneratingAt = Date.now();
    // 暂停生成：本轮的中止句柄挂到发送键（生成期间它就是暂停键）
    const abortController = new AbortController();
    setComposerGenerating(abortController);
    try {
      // 附件先解析再发消息：浏览器没抽到文字的（图片/扫描件）现场走服务端
      // 即席解析（含可选 VL），结果如实回写附件卡片；解析期间沿用「思考中…」占位。
      const store = options.attachments;
      const attachmentNames = store ? store.list().map(item => item.file.name) : [];
      // 「添加上下文」引用与附件上下文并列进入请求正文。开场分析同样携带：
      // 首页随任务交接的引用往往就是题面本身（@ 赛题 +「这道题」的发送方式），
      // 排除引用会让开场分析对着三个字的 goal 说「没收到题目」。
      const references = [...listComposerReferences()];
      const referenceBlock = references.length ? composerReferenceBlock() : "";
      let attachmentContext = "";
      // 视觉直通（ADR-0010）：生效模型具备视觉能力时，托盘位图以原图随消息直发，
      // 跳过分钟级 OCR 并钉住该接口；其余附件照走文本解析通道。
      let passthroughImages = [];
      let passthroughNames = [];
      let pinEndpointId;
      if (store && store.list().length > 0) {
        let passthroughIds;
        const plan = planImagePassthrough(store.list());
        if (plan.send.length > 0) {
          const effective = await resolveSelectedModality(
            localStorage.getItem("openmathmodelSelectedModel") || "auto",
          );
          if (effective.modality === "vision") {
            passthroughImages = await encodePassthroughImages(plan.send);
            passthroughNames = plan.send.map(item => item.file.name);
            pinEndpointId = effective.endpointId;
            passthroughIds = new Set(plan.send.map(item => item.id));
          }
        }
        attachmentContext = (await collectConversationAttachments(store, passthroughIds)).block;
      }
      const contextBlocks = [referenceBlock, attachmentContext].filter(Boolean).join("\n\n");
      // ── 执行轨迹：每条回复都由真实过程行组成（与首条 Agent 消息同构）。
      //    traceLog 收集落定后的行，回复完成后随对话记录落盘，恢复时原样重建。
      const traceLog = [];
      // 行①：读取任务与对话上下文（题面、对话模式、历史轮数都是本轮真实输入）
      if (!options.opening) {
        const snapshot = conversationSnapshot();
        const mode = currentChatMode();
        const contextDetail = [
          snapshot.goal ? `任务：${snapshot.goal.slice(0, 160)}` : "任务：未绑定运行",
          `模式：${mode.label}`,
          `历史：${Math.floor(snapshot.turns / 2)} 轮对话`,
        ].join("\n");
        appendReplyTraceRow(replyBlock, {
          icon: "book-open-text",
          title: "已读取任务与对话上下文",
          detail: contextDetail,
        });
        traceLog.push({ icon: "book-open-text", title: "已读取任务与对话上下文", detail: contextDetail });
      }
      // 行②：附件/引用解析完成并注入上下文（真实动作，解析在上面刚发生）
      if (attachmentNames.length || references.length) {
        const contextNames = [...attachmentNames, ...references.map(reference => `@${reference.title}`)];
        const row = {
          icon: "paperclip",
          title: attachmentNames.length ? "已解析并注入附件" : "已添加上下文引用",
          suffix: ` ×${contextNames.length}`,
          detail: contextNames.join("\n"),
        };
        appendReplyTraceRow(replyBlock, row);
        traceLog.push(row);
      }
      // 行②'：图片原图直通视觉模型（真实动作：这些图跳过 OCR 随消息直发）
      if (passthroughNames.length) {
        const row = {
          icon: "image",
          title: "原图已直通视觉模型",
          suffix: ` ×${passthroughNames.length}`,
          detail: passthroughNames.join("\n"),
        };
        appendReplyTraceRow(replyBlock, row);
        traceLog.push(row);
      }
      // 生成回复行实时走秒；难度判定行到达时插到它前面（服务端先判定后生成）
      generatingRow = options.opening
        ? null
        : appendReplyTraceRow(replyBlock, { icon: "circle-notch", title: "正在生成回复", waiting: true });
      startedGeneratingAt = Date.now();
      const { text: reply, meta } = await sendConversationTurn(text, {
        onMeta: current => {
          // 行③：Auto 路由真实发生的难度判定（详情 = 判定理由；继承/短路轮
          // judged=false 不出现，不制造噪音）。服务端先判定后生成，插在生成行之前。
          if (current.route?.judged && typeof current.route.difficulty === "number") {
            const row = {
              icon: "gauge",
              title: "已判定问题难度",
              suffix: ` ${current.route.difficulty}/5`,
              detail: current.route.reason || "",
            };
            appendReplyTraceRow(replyBlock, { ...row, before: generatingRow?.element ?? null });
            traceLog.push(row);
          }
          // 实际域名以服务端 meta 为准：Auto 路由结果、备用切换都在这里如实反映
          if (!transparency || !current.host) return;
          transparencySettled = true;
          const badges = [
            current.third_party ? t("第三方中转站") : "",
            current.fallback_used ? t("已切换备用接口") : "",
          ].filter(Boolean).map(tag => ` · ${escapeHtml(tag)}`).join("");
          renderTransparency(`${t("请求发送至")} ${escapeHtml(current.host)}${badges}`);
        },
        onReasoning: (_piece, full) => {
          const stick = nearBottom();
          if (!thinking) {
            thinking = createThinkingBlock(replyBlock);
            // 思考块自带「思考中…」标签，回答区占位不必重复
            if (!answerStarted) copy.innerHTML = "";
          }
          thinking.append(full);
          if (stick) scroll.scrollTo({ top: scroll.scrollHeight });
        },
        onDelta: (_piece, full) => {
          answerStarted = true;
          thinking?.finish();
          renderer.update(full);
        },
      }, {
        attachmentContext: contextBlocks,
        openingAnalysis: options.opening === true,
        attachmentNames: [...attachmentNames, ...references.map(reference => `@${reference.title}`)],
        ...(passthroughImages.length ? { images: passthroughImages, pinEndpointId } : {}),
        signal: abortController.signal,
      });
      // 附件与引用内容已随本条消息进入上下文；成功后清空托盘与引用 chips，
      // 失败路径保留以便重试。
      store?.clear();
      if (references.length) clearComposerReferences();
      thinking?.finish();
      // 生成行落定：优先用服务端整程耗时（meta.elapsed_ms），本地走秒兜底
      if (generatingRow) {
        const generateElapsedMs = typeof meta.elapsed_ms === "number"
          ? meta.elapsed_ms
          : Date.now() - startedGeneratingAt;
        const settledTitle = meta.stopped ? "已暂停（保留部分回复）" : "已生成回复";
        generatingRow.settle({ title: settledTitle, elapsedMs: generateElapsedMs });
        traceLog.push({ icon: "check-circle", title: settledTitle, elapsed: formatTraceElapsed(generateElapsedMs) });
      }
      renderer.finish(reply);
      // 对话页不显示模型名；「记录接口用量」随开关开启才落本机记录
      if (transparency && (meta.host || meta.endpoint)) {
        recordLlmUsage({
          ts: Date.now(),
          endpoint: meta.endpoint ?? "",
          host: meta.host ?? "",
          model: meta.model ?? "",
          third_party: Boolean(meta.third_party),
          fallback_used: Boolean(meta.fallback_used),
          prompt_tokens: meta.usage?.prompt_tokens,
          completion_tokens: meta.usage?.completion_tokens,
          elapsed_ms: meta.elapsed_ms,
          difficulty: meta.route?.difficulty,
        });
      }
      appendReplyActions(replyBlock, reply);
      // 轨迹随本机对话记录落盘（「保存任务历史」开启且绑定真实运行）：恢复对话时原样重建
      const traceScope = conversationSnapshot();
      if (!options.opening && traceScope.runId && saveHistoryEnabled() && traceLog.length) {
        attachTraceToLastReply(traceScope.runId, reply, traceLog);
      }
      scroll.scrollTo({ top: scroll.scrollHeight, behavior: "smooth" });
      return true;
    } catch (error) {
      renderer.cancel();
      // 用户主动暂停且一字未收：安静收尾，不按错误渲染
      if (error?.code === "GENERATION_STOPPED") {
        thinking?.finish();
        generatingRow?.settle({ title: "已暂停生成" });
        copy.innerHTML = `<p class="muted">${t("已暂停生成。")}</p>`;
        return false;
      }
      // 失败也要把生成行落定为中断态，不留走秒残影
      generatingRow?.settle({ title: "回复生成中断", failed: true });
      const unavailable = error?.code === "LLM_NOT_CONFIGURED" || error?.code === "AUTH_REQUIRED" || error?.code === "NETWORK_ERROR";
      if (options.removeOnUnavailable && unavailable) {
        replyBlock.remove();
        return false;
      }
      const message = error instanceof Error ? error.message : "对话请求失败，请稍后再试";
      copy.innerHTML = `<p class="muted">${escapeHtml(message)}</p>`;
      if (error?.code === "LLM_NOT_CONFIGURED" || error?.code === "AUTH_REQUIRED") {
        copy.insertAdjacentHTML(
          "beforeend",
          `<p class="muted"><button type="button" class="reply-configure-link" data-action="open-api-settings">${t("前往设置中心配置模型接口")}</button></p>`,
        );
      }
      scroll.scrollTo({ top: scroll.scrollHeight, behavior: "smooth" });
      return false;
    } finally {
      if (activeChatAbort === abortController) setComposerGenerating(null);
    }
  }

  function initModelingResizer() {
    const split = $("[data-modeling-split]");
    const handle = $("[data-modeling-resizer]");
    if (!split || !handle) return;

    const focusedWorkbench = split.classList.contains("focused-modeling-split");
    const paneStorageKey = focusedWorkbench ? "openmathmodelFocusedAgentPanePercentV2" : "openmathmodelAgentPanePercentV2";
    const defaultPercent = 27;
    const storedPercent = Number(localStorage.getItem(paneStorageKey));
    const clampPercent = value => {
      const rect = split.getBoundingClientRect();
      const minLeft = focusedWorkbench ? (rect.width < 980 ? 270 : 330) : (rect.width < 980 ? 280 : 320);
      const minRight = focusedWorkbench ? (rect.width < 980 ? 390 : 560) : (rect.width < 980 ? 420 : 560);
      const min = minLeft / rect.width * 100;
      const max = (rect.width - minRight - handle.offsetWidth) / rect.width * 100;
      return Math.min(Math.max(value, min), Math.max(min, max));
    };
    const applyPercent = (value, persist = false) => {
      const next = clampPercent(value);
      const modelingShell = split.closest("[data-modeling-shell]");
      split.style.setProperty("--agent-pane-width", `${next}%`);
      modelingShell?.style.setProperty("--agent-pane-width", `${next}%`);
      const stagePane = $(".focused-stage-pane", split);
      if (modelingShell && stagePane) {
        const shellRect = modelingShell.getBoundingClientRect();
        const stageRect = stagePane.getBoundingClientRect();
        modelingShell.style.setProperty("--workspace-tabs-left", `${stageRect.left - shellRect.left}px`);
      }
      handle.setAttribute("aria-valuenow", String(Math.round(next)));
      if (persist) localStorage.setItem(paneStorageKey, String(next));
      return next;
    };

    let currentPercent = applyPercent(Number.isFinite(storedPercent) && storedPercent > 0 ? storedPercent : defaultPercent);
    let dragging = false;

    const move = event => {
      if (!dragging) return;
      const rect = split.getBoundingClientRect();
      currentPercent = applyPercent((event.clientX - rect.left) / rect.width * 100);
    };
    const stop = () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("is-resizing-modeling");
      localStorage.setItem(paneStorageKey, String(currentPercent));
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
    };

    handle.addEventListener("pointerdown", event => {
      dragging = true;
      document.body.classList.add("is-resizing-modeling");
      try { handle.setPointerCapture?.(event.pointerId); } catch {}
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
      window.addEventListener("pointercancel", stop);
    });
    handle.addEventListener("dblclick", () => {
      currentPercent = applyPercent(defaultPercent, true);
      toast("已恢复默认显示比例");
    });
    handle.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? defaultPercent : currentPercent + (event.key === "ArrowLeft" ? -2 : 2);
      currentPercent = applyPercent(next, true);
    });
    window.addEventListener("resize", () => {
      currentPercent = applyPercent(currentPercent);
    });
  }

  function closeSelectMenus() {
    $$("[data-select-menu].open").forEach(wrapper => {
      wrapper.classList.remove("open");
      $("[data-select-trigger]", wrapper)?.setAttribute("aria-expanded", "false");
    });
  }

  /**
   * 自定义下拉的通用行为：触发键开合、选项回填标签与 aria-selected。
   * 点击外部与 Escape 的收起挂在 bindCommon 的全局监听上（识别 data-select-menu）。
   * 返回程序化选中函数，供“重置”等入口复用同一套状态更新。
   */
  function bindSelectMenu(wrapper, onSelect) {
    if (!wrapper) return () => {};
    const trigger = $("[data-select-trigger]", wrapper);
    const labelNode = $("[data-select-label]", wrapper);
    const select = option => {
      $$("[data-select-option]", wrapper).forEach(item => item.setAttribute("aria-selected", String(item === option)));
      if (labelNode) labelNode.textContent = option.textContent.trim();
      wrapper.classList.remove("open");
      trigger?.setAttribute("aria-expanded", "false");
    };
    wrapper.addEventListener("click", event => {
      const option = event.target.closest("[data-select-option]");
      if (option) {
        select(option);
        onSelect?.(option.dataset.selectOption, option);
        return;
      }
      if (!event.target.closest("[data-select-trigger]")) return;
      const willOpen = !wrapper.classList.contains("open");
      wrapper.classList.toggle("open", willOpen);
      trigger?.setAttribute("aria-expanded", String(willOpen));
    });
    return select;
  }

  function closeSidebarDrawer(shellNode) {
    const host = shellNode || $("[data-sidebar-shell]");
    if (!host?.classList.contains("sidebar-open")) return;
    host.classList.remove("sidebar-open");
    $('[data-action="toggle-sidebar-drawer"]', host)?.setAttribute("aria-expanded", "false");
  }

  /**
   * 视口跨过断点时同步侧栏形态：进入 821~1180 自动收成图标栏，回到宽屏还原用户偏好，
   * 离开手机档必须收掉抽屉，否则宽屏会留下一层遮罩挡住页面。
   */
  let responsiveShellBound = false;
  function bindResponsiveShell() {
    if (responsiveShellBound) return;
    let rail;
    let drawer;
    try {
      rail = window.matchMedia(RAIL_VIEWPORT);
      drawer = window.matchMedia(DRAWER_VIEWPORT);
    } catch {
      return;
    }
    responsiveShellBound = true;
    const sync = () => {
      const shellNode = $("[data-sidebar-shell]");
      if (!shellNode) return;
      const collapsed = !drawer.matches && (sidebarCollapsed() || rail.matches);
      shellNode.classList.toggle("sidebar-collapsed", collapsed);
      const toggle = $('[data-action="toggle-sidebar"]', shellNode);
      toggle?.setAttribute("aria-expanded", String(!collapsed));
      if (!drawer.matches) closeSidebarDrawer(shellNode);
    };
    rail.addEventListener("change", sync);
    drawer.addEventListener("change", sync);
  }

  // ── 论文编辑器（业主指定实现）：工具栏真实命令、KaTeX 公式、本机草稿、结构检查与导出 ──
  const PAPER_DRAFT_KEY = "openmathmodelPaperDraft.v1";
  const PAPER_BLOCKS = { "正文": "p", "标题 1": "h1", "标题 2": "h2", "标题 3": "h3" };
  // 前四项映射系统字体（Windows/macOS 自带）；后三项是免费可商用的开源字体
  //（SIL OFL 授权：思源宋体/思源黑体来自 Google Noto，霞鹜文楷来自 lxgw），
  // 由 ensurePaperWebfonts 按需从 CDN 挂载 @font-face，系统缺字时也能渲染成功。
  const PAPER_FONTS = {
    "宋体": '"Songti SC", SimSun, "Noto Serif SC", serif',
    "黑体": '"Heiti SC", SimHei, "Microsoft YaHei", "Noto Sans SC", sans-serif',
    "楷体": '"Kaiti SC", KaiTi, "LXGW WenKai", serif',
    "仿宋": '"Fangsong SC", FangSong, "Noto Serif SC", serif',
    "思源宋体": '"Noto Serif SC", "Songti SC", SimSun, serif',
    "思源黑体": '"Noto Sans SC", "Heiti SC", "Microsoft YaHei", sans-serif',
    "霞鹜文楷": '"LXGW WenKai", "Kaiti SC", KaiTi, serif',
    "Times New Roman": '"Times New Roman", Times, serif',
  };
  // 开源字体的 CSS 按 unicode-range 切片，浏览器只下载正文实际用到的字块，
  // 不会一次拉整套中文字库；加载失败时 font-family 链自动回退系统字体。
  // 思源两款带 700 字重（标题/加粗真加粗），霞鹜文楷只挂 regular+bold，
  // 不引 style.css 全家桶（那会连 Mono/Light 的切片 CSS 一起拉下来）。
  const PAPER_WEBFONT_LINKS = [
    "https://cdn.jsdelivr.net/npm/@fontsource/noto-serif-sc@5/index.css",
    "https://cdn.jsdelivr.net/npm/@fontsource/noto-serif-sc@5/700.css",
    "https://cdn.jsdelivr.net/npm/@fontsource/noto-sans-sc@5/index.css",
    "https://cdn.jsdelivr.net/npm/@fontsource/noto-sans-sc@5/700.css",
    "https://cdn.jsdelivr.net/npm/lxgw-wenkai-webfont@1/lxgwwenkai-regular.css",
    "https://cdn.jsdelivr.net/npm/lxgw-wenkai-webfont@1/lxgwwenkai-bold.css",
  ];
  function ensurePaperWebfonts() {
    PAPER_WEBFONT_LINKS.forEach(href => {
      if (document.head.querySelector(`link[href="${href}"]`)) return;
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = href;
      document.head.appendChild(link);
    });
  }
  // 中文字号按 pt 折算 px（三号 16pt / 四号 14pt / 小四 12pt / 五号 10.5pt / 小五 9pt）
  const PAPER_SIZES = { "三号": "21px", "四号": "19px", "小四": "16px", "五号": "14px", "小五": "12px" };
  const PAPER_COLORS = { "正文黑": "#171717", "深灰": "#525252", "强调红": "#a50c25", "批注绿": "#007004" };
  const PAPER_ALIGNS = { "左对齐": "justifyLeft", "居中": "justifyCenter", "右对齐": "justifyRight", "两端对齐": "justifyFull" };
  const PAPER_SOURCES = ["Run #04 · 结果表 2", "Run #04 · 核心指标对比图", "清洗数据 v2 · 字段说明"];

  const paperPage = () => $(".workflow-editor .editor-page");

  const paperWordCount = page => (page.innerText || "").replace(/\s+/g, "").length;

  function refreshPaperStatus(page, savedText) {
    const counter = $("[data-editor-wordcount]");
    if (counter) counter.textContent = `${paperWordCount(page).toLocaleString()} 字`;
    const state = $("[data-editor-savestate]");
    if (state && savedText) state.textContent = savedText;
  }

  let paperAutosaveTimer = 0;
  let paperDirty = false;
  let paperInitialHtml = "";

  function savePaperDraftNow(label = "草稿已保存到本机") {
    const page = paperPage();
    if (!page) return;
    // 手动保存（Ctrl+S / 公式弹窗确认）同样是用户编辑动作，补上用户草稿标记
    page.dataset.userEdited = "true";
    clearTimeout(paperAutosaveTimer);
    paperDirty = false;
    try { localStorage.setItem(PAPER_DRAFT_KEY, page.innerHTML); } catch {
      // 存储不可用时仅本次会话生效
    }
    const now = new Date();
    const stamp = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
    refreshPaperStatus(page, `${label} ${stamp}`);
  }

  function schedulePaperAutosave() {
    const page = paperPage();
    if (!page) return;
    // 标记「用户亲手编辑过」：task-autosave 只把带此标记的现场当作用户草稿落盘，
    // Agent 填充或演示模板不再伪装成用户草稿挡住后续真实论文正文的渲染。
    page.dataset.userEdited = "true";
    // 逐键只改状态文案，字数与落盘放进防抖回调，避免每击键读一次 innerText
    paperDirty = true;
    const state = $("[data-editor-savestate]");
    if (state) state.textContent = "正在编辑…";
    clearTimeout(paperAutosaveTimer);
    paperAutosaveTimer = window.setTimeout(() => savePaperDraftNow(), 700);
  }

  function resetPaperDraft() {
    const page = paperPage();
    if (!page || !paperInitialHtml) return;
    modal("恢复初始正文", "<p>将丢弃本机草稿，恢复到本次打开时的初始正文。确认继续？</p>", () => {
      page.innerHTML = paperInitialHtml;
      try { localStorage.removeItem(PAPER_DRAFT_KEY); } catch {}
      // 用户草稿标记与已填充版本号一并复位：真实运行的下一次刷新会重新灌入
      // Agent 定稿；不清标记的话，被丢弃的草稿会继续挡住真实正文回来。
      delete page.dataset.userEdited;
      delete page.dataset.stageDraftVersion;
      try {
        const projectParam = new URL(window.location.href).searchParams.get("project_id") ?? "";
        const projectScope = /^proj_[0-9a-f]{32}$/.test(projectParam) ? projectParam : "demo";
        localStorage.removeItem(`openmathmodel.paperDraft.v1.${projectScope}`);
      } catch {}
      paperDirty = false;
      renderFormulas(page);
      refreshPaperStatus(page, "已恢复初始正文");
      toast("已恢复初始正文");
    });
  }

  function execPaperCommand(command, value = null, useCss = false) {
    const page = paperPage();
    if (!page) return;
    page.focus();
    try { document.execCommand("styleWithCSS", false, useCss); } catch {
      // 老引擎不支持时退回默认标签写法
    }
    document.execCommand(command, false, value);
    schedulePaperAutosave();
  }

  /** execCommand 的 fontSize 只有 1–7 档；先打 7 号标记再改成精确 px。 */
  function applyPaperFontSize(px) {
    const page = paperPage();
    if (!page) return;
    page.focus();
    try { document.execCommand("styleWithCSS", false, false); } catch {}
    document.execCommand("fontSize", false, "7");
    $$('font[size="7"]', page).forEach(node => {
      node.removeAttribute("size");
      node.style.fontSize = px;
    });
    schedulePaperAutosave();
  }

  function insertPaperHtml(html) {
    const page = paperPage();
    if (!page) return;
    page.focus();
    // 光标不在正文里时落到文末，避免内容插错位置
    const selection = window.getSelection();
    if (!selection?.rangeCount || !page.contains(selection.anchorNode)) {
      const range = document.createRange();
      range.selectNodeContents(page);
      range.collapse(false);
      selection?.removeAllRanges();
      selection?.addRange(range);
    }
    document.execCommand("insertHTML", false, html);
    schedulePaperAutosave();
  }

  function insertPaperFormula() {
    if (!paperPage()) { toast("请先进入论文编辑页"); return; }
    modal(
      "插入公式（LaTeX）",
      '<label>LaTeX 表达式</label><textarea data-formula-input rows="3" placeholder="例：\\min \\sum_{i=1}^{N} (y_i - \\hat{y}_i)^2 + \\lambda \\|\\Theta\\|_2^2"></textarea>',
      backdrop => {
        const tex = $("[data-formula-input]", backdrop)?.value.trim();
        if (!tex) { toast("未输入公式"); return; }
        insertPaperHtml(`<div class="editor-formula" data-tex="${escapeHtml(tex)}" contenteditable="false">${escapeHtml(tex)}</div><p><br></p>`);
        renderFormulas(paperPage());
        toast("公式已插入");
      }
    );
  }

  function insertPaperTable() {
    if (!paperPage()) { toast("请先进入论文编辑页"); return; }
    const bodyRow = "<tr><td><br></td><td><br></td><td><br></td></tr>";
    insertPaperHtml(`<table class="editor-table"><thead><tr><th>列 1</th><th>列 2</th><th>列 3</th></tr></thead><tbody>${bodyRow}${bodyRow}</tbody></table><p><br></p>`);
    toast("已插入 3×3 表格，可直接在单元格内编辑");
  }

  /** 已插入的公式点击即可改 LaTeX 并重新排版。 */
  function editPaperFormula(node) {
    modal(
      "编辑公式（LaTeX）",
      `<label>LaTeX 表达式</label><textarea data-formula-input rows="3">${escapeHtml(node.dataset.tex || "")}</textarea>`,
      backdrop => {
        const tex = $("[data-formula-input]", backdrop)?.value.trim();
        if (!tex) { toast("公式内容不能为空"); return; }
        node.dataset.tex = tex;
        delete node.dataset.texDone;
        node.textContent = tex;
        renderFormulas(paperPage());
        savePaperDraftNow("公式已更新并保存");
      }
    );
  }

  function insertPaperCitation(anchor) {
    if (!paperPage()) { toast("请先进入论文编辑页"); return; }
    popupMenu(anchor, PAPER_SOURCES, choice => {
      insertPaperHtml(`<button class="source-chip" contenteditable="false" data-action="source-detail" title="点击引用到左侧对话，直接提问或要求修改">来源：${escapeHtml(choice)}　${icon("arrow-square-out")}</button>`);
      toast("已插入来源引用");
    });
  }

  /**
   * 点击正文里的来源引用 → 变成左下角输入框上方的引用 chip（与「添加上下文」
   * 同一通道，随下一条消息进入模型上下文），用户可直接对着它提问或要求修改。
   * 上下文带上所在章节与相邻段落，Agent 能准确定位正文位置。
   */
  function quotePaperSourceToComposer(chip) {
    const composerNode = $(".chat-pane .composer") || $(".composer");
    const textarea = composerNode?.querySelector("textarea");
    const label = chip.textContent.trim();
    if (!composerNode || !textarea) { modal("引用来源", `<p>${escapeHtml(label)}。该结果已通过完整性和一致性校验。</p>`); return; }
    let heading = "";
    let paragraph = "";
    for (let node = chip.previousElementSibling; node; node = node.previousElementSibling) {
      if (!paragraph && node.matches("p")) { paragraph = node.textContent.trim(); continue; }
      if (node.matches("h1, h2, h3")) { heading = node.textContent.trim(); break; }
    }
    const contextText = [
      `论文正文中被点选的结果引用：${label}`,
      heading ? `所在章节：${heading}` : "",
      paragraph ? `相邻段落：${paragraph.slice(0, 600)}` : "",
      "用户可能希望核对该来源、改写相关表述或更新引用的数据结果，请结合论文上下文回应。",
    ].filter(Boolean).join("\n");
    mountReferenceChips(composerNode);
    const result = addComposerReference({ key: `quote:${label}`, kind: "quote", title: label, text: contextText });
    if (result === "duplicate") toast("该来源已在引用列表中");
    else if (result === "full") toast("最多引用 4 份资料，先移除一份再添加");
    else toast("已引用到输入框，直接输入修改要求即可");
    // 手机档此刻可能停在工作区一侧：切回 Agent 对话，让引用与输入框可见
    const agentPaneSwitch = $('[data-modeling-pane="agent"]');
    if (agentPaneSwitch && composerNode.offsetParent === null) agentPaneSwitch.click();
    textarea.focus();
  }

  function runPaperCheck() {
    const page = paperPage();
    if (!page) { toast("当前页面没有可检查的论文正文"); return; }
    const headings = $$("h2, h3", page).length;
    const paragraphs = $$("p", page);
    const emptyParagraphs = paragraphs.filter(node => !node.textContent.trim() && !node.querySelector("img")).length;
    const formulas = $$(".editor-formula, [data-tex]", page).length;
    const citations = $$(".source-chip", page).length;
    const figures = $$("img, table", page).length;
    const words = paperWordCount(page).toLocaleString();
    const row = (ok, label, detail) => `<li class="${ok ? "ok" : "warn"}">${icon(ok ? "check-circle" : "warning-circle")}<div><strong>${label}</strong><span>${detail}</span></div></li>`;
    modal("论文检查（本机结构检查）", `<ul class="editor-check-list">
      ${row(headings > 0, "章节结构", headings > 0 ? `检测到 ${headings} 个章节标题` : "未检测到章节标题")}
      ${row(formulas > 0, "公式", formulas > 0 ? `共 ${formulas} 处公式` : "正文未插入公式")}
      ${row(citations > 0, "数据来源引用", citations > 0 ? `共 ${citations} 处来源标注` : "未标注结果来源")}
      ${row(figures > 0, "图表", figures > 0 ? `共 ${figures} 处图片或表格` : "未检测到图片或表格")}
      ${row(emptyParagraphs === 0, "空段落", emptyParagraphs === 0 ? "没有空段落" : `发现 ${emptyParagraphs} 个空段落`)}
      ${row(true, "字数统计", `正文约 ${words} 字`)}
    </ul><p class="editor-check-note">以上为本机结构检查；语义与表达质量请在左侧对话中让 Agent 审阅。</p>`);
  }

  function exportPaper(choice) {
    const page = paperPage();
    if (!page) { toast("当前页面没有可导出的论文正文"); return; }
    const title = $("h1", page)?.textContent.trim() || "论文草稿";
    // 开源字体 + KaTeX 的 CDN 样式一并带上：HTML/打印导出里选用的思源宋体与
    // 已排版的公式照常渲染（Word 忽略外链样式，正文仍完整）。
    const fontLinks = [
      ...PAPER_WEBFONT_LINKS,
      "https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.css",
    ].map(href => `<link rel="stylesheet" href="${href}">`).join("");
    const documentHtml = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtml(title)}</title>${fontLinks}<style>body{max-width:760px;margin:40px auto;padding:0 24px;font-family:"Songti SC",SimSun,"Noto Serif SC",serif;font-size:16px;line-height:2;color:#171717}h1{text-align:center;font-size:26px}h2,h3,h4{font-family:"Heiti SC",SimHei,"Microsoft YaHei","Noto Sans SC",sans-serif;line-height:1.5}h2{margin:28px 0 14px}h3{margin:22px 0 12px}p{text-indent:2em;text-align:justify;margin:0 0 16px}.paper-abstract-heading{text-align:center;letter-spacing:.5em;text-indent:.5em}.paper-keywords{text-indent:0}ul,ol{margin:0 0 16px;padding-left:2em}table{width:100%;border-collapse:collapse;margin:0 0 18px}td,th{border:1px solid #999;padding:6px 10px;text-indent:0}img{max-width:100%}.editor-formula,.md-math-block{text-align:center;margin:18px 0;overflow-x:auto}pre{padding:10px 12px;border:1px solid #ddd;border-radius:6px;background:#fafafa;overflow-x:auto;font-size:13px}.md-inline-code{padding:1px 5px;border-radius:4px;background:#f0f0ee;font-size:.85em}.source-chip{border:1px solid #ddd;border-radius:6px;padding:4px 10px;background:#fff;font-size:12px}</style></head><body>${page.innerHTML}</body></html>`;
    const download = (blob, filename) => {
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      link.click();
      URL.revokeObjectURL(link.href);
    };
    if (choice === "导出 Word (.doc)") {
      download(new Blob(["\ufeff", documentHtml], { type: "application/msword" }), `${title}.doc`);
      toast("已导出 Word 文档");
    } else if (choice === "导出 LaTeX (.tex)") {
      download(new Blob([paperToLatex(page, title)], { type: "application/x-tex;charset=utf-8" }), `${title}.tex`);
      toast("已导出 LaTeX 源文件，可用 xelatex 直接编译");
    } else if (choice === "导出 HTML") {
      download(new Blob([documentHtml], { type: "text/html;charset=utf-8" }), `${title}.html`);
      toast("已导出 HTML 文件");
    } else {
      const preview = window.open("", "_blank");
      if (!preview) { toast("浏览器拦截了打印窗口，请允许弹出窗口后重试"); return; }
      preview.document.write(documentHtml);
      preview.document.close();
      preview.focus();
      setTimeout(() => preview.print(), 260);
    }
  }

  /** LaTeX 文本转义（公式除外——公式保留原始 LaTeX）。 */
  function latexEscape(text) {
    return String(text)
      .replace(/\\/g, "\\textbackslash{}")
      .replace(/([%$#&_{}])/g, "\\$1")
      .replace(/~/g, "\\textasciitilde{}")
      .replace(/\^/g, "\\textasciicircum{}");
  }

  /** 行内节点 → LaTeX：加粗/斜体/下划线/上下标按语义映射，其余取文本。 */
  function latexInline(node) {
    if (node.nodeType === Node.TEXT_NODE) return latexEscape(node.textContent);
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    // 行内公式节点（markdown 渲染的 .md-math）：KaTeX 排版后 innerHTML 是排版
    // 标记，必须取 data-tex 上的原始 LaTeX，不能往下取文本。
    if (node.dataset?.tex) return `$${node.dataset.tex}$`;
    const inner = [...node.childNodes].map(latexInline).join("");
    switch (node.tagName) {
      case "STRONG": case "B": return `\\textbf{${inner}}`;
      case "EM": case "I": return `\\textit{${inner}}`;
      case "U": return `\\underline{${inner}}`;
      case "SUB": return `\\textsubscript{${inner}}`;
      case "SUP": return `\\textsuperscript{${inner}}`;
      case "BR": return "\\\\ ";
      case "IMG": return "\\textit{（插图）}";
      case "BUTTON": return "";
      default: return inner;
    }
  }

  /** 整篇正文 → 可编译的 .tex：标题、章节、公式、表格、来源注记逐块序列化。 */
  function paperToLatex(page, title) {
    const body = [];
    [...page.children].forEach(node => {
      if (node.matches("h1")) return;
      if (node.matches("h2")) { body.push(`\\section*{${latexInline(node)}}`); return; }
      if (node.matches("h3")) { body.push(`\\subsection*{${latexInline(node)}}`); return; }
      if (node.matches(".editor-formula, .md-math-block")) {
        const tex = node.dataset.tex || latexEscape(node.textContent.trim());
        body.push(`\\begin{equation}\n${tex}\n\\end{equation}`);
        return;
      }
      if (node.matches("pre")) {
        body.push(`\\begin{verbatim}\n${node.textContent.replace(/\n$/, "")}\n\\end{verbatim}`);
        return;
      }
      if (node.matches("button.source-chip")) {
        body.push(`\\noindent{\\small\\emph{${latexEscape(node.textContent.trim())}}}`);
        return;
      }
      if (node.matches("table")) {
        const rows = [...node.querySelectorAll("tr")];
        if (!rows.length) return;
        const columnCount = Math.max(...rows.map(row => row.children.length));
        // 三线表列内容居中，贴近赛事论文习惯；左对齐反而像代码清单
        const spec = Array(columnCount).fill("c").join(" ");
        const lines = rows.map(row => `${[...row.children].map(cell => latexInline(cell)).join(" & ")} \\\\`);
        body.push(`\\begin{table}[htbp]\n\\centering\n\\begin{tabular}{${spec}}\n\\toprule\n${lines[0]}\n\\midrule\n${lines.slice(1).join("\n")}\n\\bottomrule\n\\end{tabular}\n\\end{table}`);
        return;
      }
      if (node.matches("img")) {
        body.push(`% 插图：${latexEscape(node.getAttribute("alt") || "未命名")}（图片另存到 .tex 同目录后替换路径并取消注释）\n% \\includegraphics[width=0.8\\textwidth]{figure}`);
        return;
      }
      const text = latexInline(node).trim();
      if (text) body.push(text);
    });
    return [
      "% !TEX program = xelatex",
      "% 由 OpenMathModel 论文编辑器导出；公式保留原始 LaTeX，可直接用 xelatex 编译",
      "\\documentclass[12pt]{article}",
      "\\usepackage[UTF8]{ctex}",
      "\\usepackage{amsmath, amssymb, graphicx, booktabs}",
      "\\usepackage[margin=2.5cm]{geometry}",
      "% 赛事论文排版习惯：正文 1.5 倍行距；链接可点击但不带彩色边框",
      "\\usepackage{setspace}",
      "\\onehalfspacing",
      "\\usepackage[hidelinks]{hyperref}",
      `\\title{\\textbf{${latexEscape(title)}}}`,
      "\\date{}",
      "\\begin{document}",
      "\\maketitle",
      "",
      body.join("\n\n"),
      "",
      "\\end{document}",
      "",
    ].join("\n");
  }

  function refreshPaperToolbar() {
    const page = paperPage();
    if (!page) return;
    const selection = window.getSelection();
    if (!selection?.anchorNode || !page.contains(selection.anchorNode)) return;
    ["bold", "italic", "underline"].forEach(command => {
      const button = $(`.editor-format-tools [data-command="${command}"]`);
      if (!button) return;
      try { button.classList.toggle("active", document.queryCommandState(command)); } catch {
        // queryCommandState 在个别引擎上会抛错，忽略即可
      }
    });
    const blockLabel = $('[data-editor-label="block"]');
    if (blockLabel) {
      const value = String(document.queryCommandValue("formatBlock") || "").toLowerCase();
      blockLabel.textContent = value === "h1" ? "标题 1" : value === "h2" ? "标题 2" : value === "h3" ? "标题 3" : "正文";
    }
  }

  function bindPaperEditor() {
    const page = paperPage();
    if (!page || page.dataset.editorReady === "true") return;
    page.dataset.editorReady = "true";

    // 开源字体（思源宋体/黑体、霞鹜文楷）随编辑器挂载：草稿或字体菜单用到时已就绪
    ensurePaperWebfonts();

    // 先记住初始正文，「恢复初始正文」以此为准
    paperInitialHtml = page.innerHTML;

    // 本机草稿：刷新或换页后不丢内容
    try {
      const saved = localStorage.getItem(PAPER_DRAFT_KEY);
      if (saved && saved !== page.innerHTML) {
        page.innerHTML = saved;
        refreshPaperStatus(page, "已恢复本机草稿");
      }
    } catch {
      // 存储不可用时使用初始正文
    }
    // 草稿里保存的公式带 data-tex-done 标记，重置后重新排版，确保 KaTeX 样式加载
    $$("[data-tex]", page).forEach(node => { delete node.dataset.texDone; });
    renderFormulas(page);
    refreshPaperStatus(page);

    page.addEventListener("input", schedulePaperAutosave);
    document.addEventListener("selectionchange", () => window.requestAnimationFrame(refreshPaperToolbar));

    // 粘贴净化：外部富文本（Word/网页）按纯文本入文，多段自动成段，杜绝脏样式
    page.addEventListener("paste", event => {
      event.preventDefault();
      const text = event.clipboardData?.getData("text/plain") ?? "";
      if (!text) return;
      const paragraphs = text.replace(/\r\n/g, "\n").split(/\n+/).map(part => part.trim()).filter(Boolean);
      if (paragraphs.length > 1) insertPaperHtml(paragraphs.map(part => `<p>${escapeHtml(part)}</p>`).join(""));
      else document.execCommand("insertText", false, text);
      schedulePaperAutosave();
    });

    // Ctrl/Cmd+S 立即保存本机草稿，拦截浏览器默认保存
    page.addEventListener("keydown", event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        savePaperDraftNow("已手动保存到本机");
      }
    });

    // 公式点击即编辑（contenteditable=false 的节点吃不到 input 事件，单独接管）
    page.addEventListener("click", event => {
      const formula = event.target.closest?.(".editor-formula[data-tex]");
      if (formula && page.contains(formula)) editPaperFormula(formula);
    });

    // 关页/切走前兜底落盘，防止 700ms 防抖窗口内丢稿
    window.addEventListener("beforeunload", () => { if (paperDirty) savePaperDraftNow(); });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden" && paperDirty) savePaperDraftNow();
    });

    $$("[data-editor-menu]").forEach(button => button.addEventListener("click", () => {
      const kind = button.dataset.editorMenu;
      const label = $(`[data-editor-label="${kind}"]`);
      if (kind === "block") {
        popupMenu(button, Object.keys(PAPER_BLOCKS), choice => {
          execPaperCommand("formatBlock", `<${PAPER_BLOCKS[choice]}>`);
          if (label) label.textContent = choice;
        });
      }
      if (kind === "font") {
        popupMenu(button, Object.keys(PAPER_FONTS), choice => {
          execPaperCommand("fontName", PAPER_FONTS[choice], true);
          if (label) label.textContent = choice;
        }, (item, name) => { item.style.fontFamily = PAPER_FONTS[name]; });
      }
      if (kind === "size") {
        popupMenu(button, Object.keys(PAPER_SIZES), choice => {
          applyPaperFontSize(PAPER_SIZES[choice]);
          if (label) label.textContent = choice;
        });
      }
      if (kind === "color") {
        popupMenu(button, [...Object.keys(PAPER_COLORS), "清除格式"], choice => {
          if (choice === "清除格式") execPaperCommand("removeFormat");
          else execPaperCommand("foreColor", PAPER_COLORS[choice], true);
        });
      }
      if (kind === "align") {
        popupMenu(button, Object.keys(PAPER_ALIGNS), choice => execPaperCommand(PAPER_ALIGNS[choice]));
      }
    }));

    // 图片：本地文件转 DataURL 插入，离线可用、不依赖后端
    $("[data-editor-image-input]")?.addEventListener("change", event => {
      const file = event.target.files?.[0];
      event.target.value = "";
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        insertPaperHtml(`<img src="${reader.result}" alt="${escapeHtml(file.name)}"><p><br></p>`);
        toast("图片已插入");
      };
      reader.readAsDataURL(file);
    });

    // 大纲：平滑滚动 + 高亮；未生成章节如实提示
    const outlineLinks = $$(".paper-only-editor .outline a");
    outlineLinks.forEach(link => link.addEventListener("click", event => {
      event.preventDefault();
      const anchor = $(link.getAttribute("href"), page);
      outlineLinks.forEach(item => item.classList.toggle("active", item === link));
      if (anchor) anchor.scrollIntoView({ behavior: "smooth", block: "start" });
      else toast("该章节尚未生成，可在左侧对话中让 Agent 续写");
    }));

    // 滚动同步：正文滚到哪一章，大纲高亮跟到哪一章（rAF 节流）
    if (outlineLinks.length) {
      let outlineSpyPending = false;
      page.addEventListener("scroll", () => {
        if (outlineSpyPending) return;
        outlineSpyPending = true;
        window.requestAnimationFrame(() => {
          outlineSpyPending = false;
          // 真实草稿会再生大纲行（stage-content），因此每帧都取当前 DOM 里的行
          const liveLinks = $$(".paper-only-editor .outline a");
          const anchors = liveLinks
            .map(link => ({ link, node: $(link.getAttribute("href"), page) }))
            .filter(item => item.node);
          if (!anchors.length) return;
          const threshold = page.getBoundingClientRect().top + 96;
          let current = anchors[0];
          anchors.forEach(item => {
            if (item.node.getBoundingClientRect().top <= threshold) current = item;
          });
          liveLinks.forEach(link => link.classList.toggle("active", link === current.link));
        });
      }, { passive: true });
    }
  }

  function bindCommon(screen) {
    bindResponsiveShell();
    document.addEventListener("click", event => {
      if (!event.target.closest("[data-model-picker]")) {
        $$("[data-model-picker].open").forEach(picker => {
          picker.classList.remove("open");
          $("[data-action=\"model-picker\"]", picker)?.setAttribute("aria-expanded", "false");
        });
      }
      if (!event.target.closest("[data-select-menu]")) closeSelectMenus();
      const codeTab = event.target.closest("[data-code-lang]");
      if (codeTab) { switchMethodLanguage(codeTab.dataset.codeLang); return; }
      const goButton = event.target.closest("[data-go]");
      if (goButton) { go(goButton.dataset.go); return; }
      const paneButton = event.target.closest("[data-modeling-pane]");
      if (paneButton) {
        const split = paneButton.closest("[data-modeling-split]");
        if (!split) return;
        split.dataset.mobilePane = paneButton.dataset.modelingPane;
        $$("[data-modeling-pane]", split).forEach(button => {
          const on = button === paneButton;
          button.classList.toggle("active", on);
          button.setAttribute("aria-selected", String(on));
        });
        // 图表按容器宽度绘制，从隐藏态切回来必须重算一次。
        requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
        return;
      }
      const action = event.target.closest("[data-action]")?.dataset.action;
      if (!action) return;
      const target = event.target.closest("[data-action]");
      if (action === "new-task") go("new");
      if (action === "toggle-sidebar-drawer" || action === "close-sidebar-drawer") {
        const sidebarShell = target.closest("[data-sidebar-shell]");
        if (!sidebarShell) return;
        const open = action === "toggle-sidebar-drawer" && !sidebarShell.classList.contains("sidebar-open");
        sidebarShell.classList.toggle("sidebar-open", open);
        $('[data-action="toggle-sidebar-drawer"]', sidebarShell)?.setAttribute("aria-expanded", String(open));
        return;
      }
      if (action === "toggle-sidebar") {
        const sidebarShell = target.closest("[data-sidebar-shell]");
        if (!sidebarShell) return;
        // 手机档侧栏本身就是抽屉，这颗按钮的语义随之变成“收起抽屉”。
        if (matchesViewport(DRAWER_VIEWPORT)) { closeSidebarDrawer(sidebarShell); return; }
        const collapsed = sidebarShell.classList.toggle("sidebar-collapsed");
        target.setAttribute("aria-expanded", String(!collapsed));
        target.title = collapsed ? "展开侧栏" : "收起侧栏";
        try { localStorage.setItem("openmathmodelSidebarCollapsed", String(collapsed)); } catch {}
        return;
      }
      if (action === "toggle-method-tree") {
        const layout = $("[data-method-layout]");
        if (!layout) return;
        const collapsed = layout.classList.toggle("tree-collapsed");
        target.setAttribute("aria-expanded", String(!collapsed));
        target.title = collapsed ? "展开方法列表" : "收起方法列表";
        target.innerHTML = icon(collapsed ? "caret-right" : "caret-left");
        try { localStorage.setItem(METHOD_TREE_COLLAPSED_KEY, String(collapsed)); } catch {
          // 存储不可用时仅本次会话生效
        }
        return;
      }
      // sidebar-filter（搜索框旁的筛选按钮）由 integration/recent-tasks.ts 接管：真实筛选最近任务
      if (action === "settings") openSettingsCenter();
      if (action === "history") modal("任务历史", "<p>当前任务共保存 18 个关键节点，可随时回看题目分析、清洗方案、实验与论文版本。</p>");
      if (action === "task-doc") modal("任务文档", "<p>题目、附件、模型方案、实验记录和论文成果均已汇总到当前项目。</p>");
      if (action === "fullscreen") {
        if (document.fullscreenElement) document.exitFullscreen?.();
        else document.documentElement.requestFullscreen?.();
      }
      if (action === "refresh-report") toast("数据报告已刷新");
      if (action === "open-details") {
        const shell = $("[data-modeling-shell]");
        shell?.classList.add("drawer-open");
        $("[data-task-detail-drawer]")?.setAttribute("aria-hidden", "false");
      }
      if (action === "close-details") {
        const shell = $("[data-modeling-shell]");
        shell?.classList.remove("drawer-open");
        $("[data-task-detail-drawer]")?.setAttribute("aria-hidden", "true");
      }
      if (action === "understanding-details") modal("题目理解", "<p>任务已拆分为需求预测、区域划分和调度优化三个子问题。待确认项为缺失车辆字段的填充策略。</p>");
      if (action === "validation-details") modal("完整验证结果", "<p>6 项检查全部通过：数据完整性、范围校验、逻辑一致性、异常检测、单位一致性和可解性。</p>");
      if (action === "model-details") modal("方案详情", "<p>方案 v2 已完成精度、效率、可解释性和风险四个维度的综合比较。</p>");
      if (action === "experiment-details") modal("Run #04 详情", "<p>本轮运行耗时 87.6 秒，验证状态通过，总调度成本较基线降低 9.38%。</p>");
      if (action === "suggestion-toggle") {
        const next = target.getAttribute("aria-pressed") !== "true";
        target.setAttribute("aria-pressed", String(next));
        target.classList.toggle("is-on", next);
      }
      if (action === "download-data") toast("历史供需数据_2024Q4.xlsx 已加入下载队列");
      if (action === "continue-paper") { toast("正在生成第 4 章实证分析"); setTimeout(() => go("complete"), 520); }
      if (action === "export-paper") popupMenu(target, ["导出 Word (.doc)", "导出 LaTeX (.tex)", "导出 HTML", "打印 / PDF"], exportPaper);
      if (action === "source-detail") quotePaperSourceToComposer(target);
      if (action === "fake-close") toast("这是演示界面，窗口保持打开");
      if (action === "attach") target.closest(".composer")?.querySelector(".file-input")?.click();
      if (action === "reference") {
        popupMenu(target, ["赛题库", "优秀论文", "方法库"], choice => {
          void openReferencePicker(choice, target.closest(".composer"));
        });
      }
      if (action === "mode") {
        popupMenu(target, CHAT_MODES.map(mode => mode.label), label => {
          const mode = CHAT_MODES.find(item => item.label === label);
          if (!mode) return;
          saveChatMode(mode.id);
          $$('.composer [data-action="mode"] .tool-label').forEach(el => { el.textContent = t(mode.label); });
          toast(`${t("已切换对话模式")}：${t(mode.label)}`);
        });
      }
      if (action === "model-picker") {
        const picker = target.closest("[data-model-picker]");
        const willOpen = !picker.classList.contains("open");
        $$("[data-model-picker].open").forEach(item => {
          item.classList.remove("open");
          $("[data-action=\"model-picker\"]", item)?.setAttribute("aria-expanded", "false");
        });
        picker.classList.toggle("open", willOpen);
        target.setAttribute("aria-expanded", String(willOpen));
        if (willOpen) {
          const menu = $(".agent-model-menu", picker);
          const composerBox = target.closest(".composer").getBoundingClientRect();
          const triggerBox = target.getBoundingClientRect();
          const menuWidth = Math.min(336, composerBox.width - 16, window.innerWidth - 16);
          menu.style.width = `${menuWidth}px`;
          const menuHeight = menu.getBoundingClientRect().height;
          const minimumLeft = Math.max(8, composerBox.left + 8);
          const maximumLeft = Math.min(window.innerWidth - menuWidth - 8, composerBox.right - menuWidth - 8);
          const idealLeft = triggerBox.right - menuWidth;
          menu.style.left = `${Math.max(minimumLeft, Math.min(idealLeft, maximumLeft))}px`;
          menu.style.top = `${Math.max(8, triggerBox.top - menuHeight - 10)}px`;
        }
        return;
      }
      if (action === "select-model") {
        const picker = target.closest("[data-model-picker]");
        const trigger = $("[data-action=\"model-picker\"]", picker);
        $("[data-model-picker-label]", picker).textContent = $(".model-choice-copy strong", target).textContent;
        $("[data-model-picker-icon]", picker).innerHTML = $(".model-choice-logo", target).innerHTML;
        $$('[data-action="select-model"]', picker).forEach(option => option.setAttribute("aria-selected", String(option === target)));
        picker.classList.remove("open");
        trigger.setAttribute("aria-expanded", "false");
        try { localStorage.setItem("openmathmodelSelectedModel", target.dataset.modelChoice); } catch {}
        toast(`已切换到 ${$(".model-choice-copy strong", target).textContent}`);
        return;
      }
      if (action === "send") {
        // 生成中发送键就是暂停键：点击中止当前这轮回复
        if (target.dataset.mode === "stop") {
          activeChatAbort?.abort();
          return;
        }
        const composer = target.closest(".composer");
        const textarea = composer?.querySelector("textarea");
        const text = textarea?.value.trim();
        if (!text) { toast("请输入你的问题"); return; }
        if (screen === "new") {
          sessionStorage.setItem("openmathmodelPrompt", text);
          go("running");
        } else {
          // 传入 composer 让对话轮拿到输入框的附件集合（ADR-0010 批次三）
          appendConversationTurn(text, composer);
          textarea.value = "";
        }
      }
      if (action === "open-api-settings") openSettingsCenter("api");
      if (action === "files") modal("附件", '<div class="attachment-chip">2026国赛A题题目.pdf</div><div class="attachment-chip">共享单车数据集.csv</div><div class="attachment-chip">城市区域划分示意图.png</div>');
      if (action === "more" || action === "row-menu") popupMenu(target, ["重命名", "复制", "归档"]);
      if (action === "toggle-activity") {
        const activityHost = target.closest(".focused-agent-chat, .assistant-block");
        // 对话尾部的执行轨迹块没有步骤时间线，折叠对象是块内的活动流（.agent-stream）
        const list = activityHost?.querySelector(".focused-activity-list, .activity-list, .agent-stream") || $(".focused-activity-list") || $(".activity-list");
        list?.classList.toggle("collapsed");
        const collapsed = list?.classList.contains("collapsed") ?? false;
        target.setAttribute("aria-expanded", String(!collapsed));
        // 重建文案时保留步骤计数徽标（n/6 由控制器持续更新），不再写死数字
        const badge = target.querySelector("[data-steps-count]");
        const badgeHtml = badge ? badge.outerHTML : '<span class="steps-count" data-steps-count hidden></span>';
        target.innerHTML = collapsed
          ? `${icon("eye")} 查看执行步骤 ${badgeHtml}${icon("caret-down")}`
          : `${icon("eye-slash")} 收起执行步骤 ${badgeHtml}${icon("caret-up")}`;
      }
      if (action === "new-project") modal("新建项目", '<label>项目名称</label><input placeholder="输入项目名称"><label>项目说明</label><textarea placeholder="简单描述建模目标"></textarea>', () => toast("项目已创建"));
      if (action === "upload-data") $(".file-input")?.click() || toast("可直接拖入 CSV、XLSX 文件");
      if (action === "clean-data") { target.textContent = "清洗中…"; setTimeout(() => { target.textContent = "清洗完成"; toast("已处理缺失值与异常记录"); }, 850); }
      if (action === "reanalyze") toast("已将重新分析要求发送给 Agent");
      if (action === "adjust-cleaning") modal("调整清洗方案", "<label>缺失值策略</label><input value=\"按字段选择中位数或前向填充\"><label>异常值策略</label><input value=\"IQR 检测 + 分位数截断\">", () => toast("清洗方案已更新"));
      if (action === "alternate-plans") modal("备选方案", "<p>方案 B：LSTM + K-means + 混合整数规划</p><p>方案 C：XGBoost + 层次聚类 + 线性规划</p>");
      if (action === "continue-question") {
        $(".modeling-chat-pane textarea")?.focus();
        toast("可以在左侧继续补充调参要求");
      }
      if (action === "editor-check") runPaperCheck();
      if (action === "experiment-filter") popupMenu(target, ["全部", "已完成", "进行中", "失败"]);
      if (action === "page-size") popupMenu(target, ["15 条/页", "20 条/页", "50 条/页"]);
      if (action === "rerun") {
        target.disabled = true; target.textContent = "运行中 0%";
        let value = 0; const timer = setInterval(() => { value += 20; target.textContent = `运行中 ${value}%`; if (value >= 100) { clearInterval(timer); target.disabled = false; target.innerHTML = `${icon("arrow-clockwise")} 重新运行`; toast("实验重新运行完成"); } }, 240);
      }
      if (action === "compare") { target.classList.toggle("primary"); target.innerHTML = target.classList.contains("primary") ? `${icon("check")} 已加入对比` : `${icon("plus")} 加入对比`; }
      if (action === "formula") insertPaperFormula();
      if (action === "image") { if (paperPage()) $("[data-editor-image-input]")?.click(); else toast("请先进入论文编辑页"); }
      if (action === "cite") insertPaperCitation(target);
      if (action === "insert-table") insertPaperTable();
      if (action === "paper-save-now") savePaperDraftNow("已手动保存到本机");
      if (action === "paper-reset-draft") resetPaperDraft();
      if (action === "ai-edit") { target.closest(".ai-prompt").textContent = "Agent 正在检查本章逻辑与表达……"; setTimeout(() => toast("检查完成：未发现严重问题"), 700); }
      if (action === "full-case") modal("完整案例", "<p>案例包含题目解析、变量定义、模型建立、求解流程、敏感性分析和可复现代码。</p>");
      if (action === "bookmark") { target.classList.toggle("blue"); target.innerHTML = target.classList.contains("blue") ? `${icon("star-fill")} 已收藏` : `${icon("star")} 收藏`; }
      if (action === "resource-bookmark") {
        const row = target.closest("tr");
        const saved = row?.dataset.saved !== "true";
        if (row) row.dataset.saved = String(saved);
        row?.querySelectorAll('[data-action="resource-bookmark"]').forEach(button => {
          button.classList.toggle("saved", saved);
          button.setAttribute("aria-pressed", String(saved));
          button.innerHTML = saved ? '<i class="ph-fill ph-star"></i>' : icon("star");
        });
        toast(saved ? "已加入收藏" : "已取消收藏");
      }
      if (action === "detail-bookmark") {
        const saved = target.getAttribute("aria-pressed") !== "true";
        target.setAttribute("aria-pressed", String(saved));
        target.classList.toggle("saved", saved);
        target.innerHTML = saved ? '<i class="ph-fill ph-star" aria-hidden="true"></i> 已收藏' : `${icon("star")} 收藏`;
        toast(saved ? "已加入收藏" : "已取消收藏");
      }
      // 渲染层已经过滤掉非官方来源，这里再挡一次：属性可能来自缓存的旧页面。
      if (action === "open-source") {
        const requested = target.dataset.sourceUrl;
        if (isOfficialSourceUrl(requested)) window.open(requested, "_blank", "noopener,noreferrer");
        else toast("该记录没有可公开跳转的官方来源页");
      }
      if (action === "cite-detail") {
        const title = escapeHtml(target.dataset.resourceTitle || "数学建模论文");
        const raw = target.dataset.sourceUrl || "";
        const sourceUrl = isOfficialSourceUrl(raw) ? escapeHtml(raw) : "";
        const citation = sourceUrl ? `${title}［EB/OL］. ${sourceUrl}` : `${title}［EB/OL］.`;
        modal("引用论文", `<label>引用格式</label><input value="GB/T 7714—2015" readonly><label>引用文本</label><textarea readonly>${citation}</textarea>`, () => toast("引用文本已复制"));
      }
      if (action === "use-problem") {
        sessionStorage.setItem("openmathmodelPrompt", `请围绕“${target.dataset.resourceTitle}”建立完整数学模型，并给出可复现的求解流程。`);
        toast("已添加赛题上下文");
        setTimeout(() => go("new"), 320);
      }
      if (action === "method-bookmark") {
        const id = target.dataset.methodId;
        const saved = toggleMethodFavorite(id);
        target.setAttribute("aria-pressed", String(saved));
        target.classList.toggle("saved", saved);
        target.innerHTML = saved ? '<i class="ph-fill ph-star" aria-hidden="true"></i> 已收藏' : `${icon("star")} 收藏`;
        const link = methodLinkById(id);
        if (link) link.dataset.methodFavorite = String(saved);
        const counter = $("[data-favorite-count]");
        if (counter) counter.textContent = String(readMethodFavorites().length);
        refreshMethodFavoriteUi();
        toast(saved ? "已加入收藏" : "已取消收藏");
      }
      if (action === "method-compare-toggle") {
        const id = target.dataset.methodId;
        if (methodCompareIds.includes(id)) {
          methodCompareIds = methodCompareIds.filter(item => item !== id);
        } else if (methodCompareIds.length >= METHOD_COMPARE_LIMIT) {
          toast(`最多同时对比 ${METHOD_COMPARE_LIMIT} 个方法`);
          return;
        } else {
          methodCompareIds = [...methodCompareIds, id];
        }
        const detail = $("[data-method-detail]");
        if (detail?.dataset.mode === "compare") {
          if (methodCompareIds.length >= 2) openMethodCompare();
          else exitMethodCompare();
        }
        syncMethodCompareBar();
      }
      if (action === "method-compare-clear") {
        methodCompareIds = [];
        exitMethodCompare();
        syncMethodCompareBar();
      }
      if (action === "method-compare-open") openMethodCompare();
      if (action === "method-compare-exit") exitMethodCompare();
      if (action === "use-method") {
        const entry = methodLibrary.find(candidate => candidate.id === target.dataset.methodId);
        const prompt = entry
          ? [
              `请使用“${entry.name}”（${entry.subtitle}）完成当前数学建模任务。`,
              `核心假设需逐条核对：${entry.assumptions.join("；")}。`,
              `标准流程：${entry.workflow.map((step, index) => `${index + 1}) ${step}`).join(" ")}。`,
              `以下情况必须换方法并说明理由：${entry.antipatterns.join("；")}。`,
              `请给出可复现代码，报告 ${entry.metrics.join("、")} 等指标，并完成稳健性检查：${entry.robustness.join("；")}。`,
            ].join("\n")
          : `请使用“${target.dataset.methodName}”完成当前数学建模任务，说明适用假设、完整流程、评价指标与敏感性分析，并给出可复现代码。`;
        sessionStorage.setItem("openmathmodelPrompt", prompt);
        toast("已添加方法论上下文");
        setTimeout(() => go("new"), 320);
      }
      if (action === "use-paper") {
        sessionStorage.setItem("openmathmodelPrompt", `请参考论文“${target.dataset.resourceTitle}”的建模思路，帮助我完成当前任务。`);
        toast("已添加论文作为参考");
        setTimeout(() => go("new"), 320);
      }
      if (action === "read-paper") modal("论文预览", "<p>正文预览已加载。演示版本保留目录、翻页、收藏与引用入口。</p>");
      if (action === "download-all") {
        const blob = new Blob(["OpenMathModel 交付文件清单\n" + deliverables.map(d => d[1]).join("\n")], { type: "text/plain;charset=utf-8" });
        const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = "OpenMathModel-交付文件清单.txt"; link.click(); URL.revokeObjectURL(link.href); toast("已开始下载全部文件");
      }
      if (action === "copy-task") { sessionStorage.setItem("copiedTask", "1"); toast("已复制为新任务"); setTimeout(() => go("new"), 450); }
    });

    $$("[data-command]").forEach(button => button.addEventListener("click", () => {
      document.execCommand(button.dataset.command, false);
      schedulePaperAutosave();
      refreshPaperToolbar();
    }));
    bindPaperEditor();
    $$(".composer textarea").forEach(textarea => textarea.addEventListener("keydown", event => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
      const send = textarea.closest(".composer")?.querySelector('[data-action="send"]');
      // 生成中发送键是暂停键：Enter 不触发它（回车照常换行），避免误停生成
      if (send?.dataset.mode === "stop") return;
      event.preventDefault();
      send?.click();
    }));
    $$("[data-task-type]").forEach(button => button.addEventListener("click", () => {
      $$("[data-task-type]").forEach(b => b.classList.remove("active")); button.classList.add("active");
      const placeholder = button.dataset.taskType === "数据分析" ? "描述你的数据分析目标，或上传数据文件……" : button.dataset.taskType === "论文优化" ? "上传论文，或描述需要优化的章节……" : "描述你的问题，或上传赛题与数据……";
      $(".composer textarea").placeholder = placeholder;
    }));
    $$(".progress-step").forEach(step => {
      const toggle = () => step.classList.toggle("open");
      step.addEventListener("click", toggle);
      step.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") toggle(); });
    });
    $$(".open-file").forEach(button => button.addEventListener("click", () => modal(button.dataset.file, `<p>${button.dataset.file} 已准备好。演示版显示文件预览入口。</p>`)));

    document.addEventListener("keydown", event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        const sidebarShell = $("[data-sidebar-shell]");
        if (sidebarShell?.classList.contains("sidebar-collapsed")) {
          sidebarShell.classList.remove("sidebar-collapsed");
          const toggle = $('[data-action="toggle-sidebar"]', sidebarShell);
          toggle?.setAttribute("aria-expanded", "true");
          if (toggle) toggle.title = "收起侧栏";
          try { localStorage.setItem("openmathmodelSidebarCollapsed", "false"); } catch {}
        }
        $(".global-search input")?.focus();
      }
      if (event.key === "Escape") {
        closeSidebarDrawer();
        $(".menu")?.remove();
        $(".modal-backdrop")?.remove();
        $$("[data-model-picker].open").forEach(picker => {
          picker.classList.remove("open");
          $("[data-action=\"model-picker\"]", picker)?.setAttribute("aria-expanded", "false");
        });
        closeSelectMenus();
      }
    });
  }

  function bindScreen(screen) {
    $$('[data-workspace-tab]').forEach(button => button.addEventListener("click", () => {
      const modelingShell = button.closest("[data-modeling-shell]");
      const workspace = button.closest(".focused-workspace") || $(".focused-workspace", modelingShell);
      const tab = button.dataset.workspaceTab;
      if (!workspace || !tab) return;
      $$('[data-workspace-tab]', modelingShell || workspace).forEach(item => {
        item.classList.toggle("active", item === button);
        item.setAttribute("aria-selected", String(item === button));
      });
      $$('[data-workspace-panel]', workspace).forEach(panel => panel.classList.toggle("active", panel.dataset.workspacePanel === tab));
      requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
    }));

    const bindResourceDirectory = kind => {
      const rowSelector = kind === "problem" ? ".problem-item" : ".paper-item";
      const searchSelector = kind === "problem" ? "[data-problem-search]" : "[data-paper-search]";
      const rows = $$(rowSelector);
      const tabs = $$(`[data-resource-kind="${kind}"]`);
      const pageSize = kind === "paper" ? 10 : RESOURCE_PAGE_SIZE;
      const paperYearSelect = kind === "paper" ? $("[data-paper-year-select]") : null;
      const paperYearOptions = paperYearSelect ? $$("[data-select-option]", paperYearSelect) : [];
      const paperCompetitionFilters = kind === "paper" ? $$('[data-paper-competition-filter]') : [];
      const paperGroupFilters = kind === "paper" ? $$('[data-paper-group-filter]') : [];
      const paperReset = kind === "paper" ? $("[data-paper-filter-reset]") : null;
      const problemFilterSelects = kind === "problem" ? $$("[data-problem-filter]") : [];
      let currentPage = 1;
      // 页码窗口以当前页为中心，最多 5 个；两端各留首页/末页和省略号，
      // 这样 2 页时就只出 "1 2"，20 页时也不会把按钮铺满一行。
      const pageWindow = pageCount => {
        if (pageCount <= 7) return Array.from({ length: pageCount }, (_, index) => index + 1);
        const numbers = new Set([1, pageCount]);
        for (let page = currentPage - 1; page <= currentPage + 1; page += 1) {
          if (page > 1 && page < pageCount) numbers.add(page);
        }
        if (currentPage <= 3) [2, 3, 4].forEach(page => numbers.add(page));
        if (currentPage >= pageCount - 2) [pageCount - 3, pageCount - 2, pageCount - 1].forEach(page => numbers.add(page));
        const sorted = [...numbers].sort((a, b) => a - b);
        const slots = [];
        sorted.forEach((page, index) => {
          if (index && page - sorted[index - 1] > 1) slots.push("…");
          slots.push(page);
        });
        return slots;
      };
      const renderPagination = pageCount => {
        const host = $("[data-resource-page-numbers]");
        if (!host) return;
        host.innerHTML = pageWindow(pageCount).map(slot => slot === "…"
          ? '<span class="page-gap">…</span>'
          : `<button class="${slot === currentPage ? "active" : ""}" data-resource-page="${slot}" aria-current="${slot === currentPage ? "page" : "false"}">${slot}</button>`).join("");
        const prev = $('[data-resource-page="prev"]');
        const next = $('[data-resource-page="next"]');
        if (prev) prev.disabled = currentPage <= 1;
        if (next) next.disabled = currentPage >= pageCount;
      };
      const applyResourceFilters = () => {
        const query = $(searchSelector)?.value.trim().toLowerCase() || "";
        const selected = tabs.find(tab => tab.classList.contains("active"))?.dataset.resourceTab || "";
        const selectedYear = paperYearOptions.find(option => option.getAttribute("aria-selected") === "true")?.dataset.selectOption || "";
        const selectedCompetition = paperCompetitionFilters.find(button => button.classList.contains("active"))?.dataset.paperCompetitionFilter || "";
        const selectedGroup = paperGroupFilters.find(button => button.classList.contains("active"))?.dataset.paperGroupFilter || "";
        // 赛题库四个下拉的当前选中值；空值（「全部」）不参与过滤
        const problemFilters = problemFilterSelects.map(wrapper => ({
          field: wrapper.dataset.problemFilter,
          value: $$("[data-select-option]", wrapper).find(option => option.getAttribute("aria-selected") === "true")?.dataset.selectOption || "",
        })).filter(({ value }) => value);
        const matches = [];
        rows.forEach(row => {
          const matchesSearch = !query || row.dataset.resourceSearch.toLowerCase().includes(query);
          const matchesCategory = selected === "全部赛题" || selected === "全部" || selected === "按赛题" || selected === "按模型"
            || (selected === "收藏" ? row.dataset.saved === "true" : row.dataset.resourceCategory === selected);
          const matchesYear = kind !== "paper" || !selectedYear || row.dataset.paperYear === selectedYear;
          const matchesCompetition = kind !== "paper" || !selectedCompetition || row.dataset.paperCompetition === selectedCompetition;
          const matchesGroup = kind !== "paper" || !selectedGroup || row.dataset.paperGroup === selectedGroup;
          const matchesProblemFilters = problemFilters.every(({ field, value }) => {
            if (field === "competition") return row.dataset.problemCompetition === value;
            if (field === "year") return row.dataset.problemYear === value;
            if (field === "type") return row.dataset.problemType === value;
            return (row.dataset.problemDirections || "").split("|").includes(value);
          });
          if (matchesSearch && matchesCategory && matchesYear && matchesCompetition && matchesGroup && matchesProblemFilters) matches.push(row);
          row.hidden = true;
        });
        const pageCount = Math.max(1, Math.ceil(matches.length / pageSize));
        currentPage = Math.min(Math.max(1, currentPage), pageCount);
        matches.slice((currentPage - 1) * pageSize, currentPage * pageSize).forEach(row => { row.hidden = false; });
        const copy = $("[data-resource-page-copy]");
        if (copy) copy.textContent = `共 ${matches.length} ${kind === "problem" ? "题" : "篇"} · 第 ${currentPage}/${pageCount} 页`;
        const resultCopy = kind === "paper" ? $("[data-paper-result-copy]") : null;
        if (resultCopy) resultCopy.textContent = `${matches.length} 篇`;
        const emptyRow = kind === "paper" ? $("[data-paper-empty]") : $("[data-problem-empty]");
        if (emptyRow) emptyRow.hidden = matches.length > 0;
        renderPagination(pageCount);
      };
      // 奖项、题组、年份都只存在于某一个比赛下（"Outstanding Winner" 只有美赛、2004 年只有研究生赛），
      // 选定比赛后隐藏不属于它的选项；当前选中项若被隐藏就退回「全部」，否则会停在必定空列表的组合上。
      // 题组文案也随比赛改写：美赛的 A/B/C 是 MCM、D/E/F 是 ICM。
      const syncPaperScopes = () => {
        const competition = paperCompetitionFilters.find(button => button.classList.contains("active"))?.dataset.paperCompetitionFilter || "";
        const inScope = node => {
          const scope = node.dataset.paperScope;
          return !scope || !competition || scope.split(" ").includes(competition);
        };
        const resetToFirst = nodes => {
          if (nodes.some(node => node.hidden && node.classList.contains("active"))) {
            nodes.forEach((node, index) => node.classList.toggle("active", index === 0));
          }
        };
        tabs.forEach(tab => { tab.hidden = !inScope(tab); });
        resetToFirst(tabs);
        paperGroupFilters.forEach(button => {
          button.hidden = !inScope(button);
          const group = button.dataset.paperGroupFilter;
          if (group && group !== "—") button.textContent = paperGroupLabel(competition, group);
        });
        resetToFirst(paperGroupFilters);
        paperYearOptions.forEach(option => { option.hidden = !inScope(option); });
        const activeYear = paperYearOptions.find(option => option.getAttribute("aria-selected") === "true");
        if (activeYear?.hidden && paperYearOptions.length) selectPaperYear(paperYearOptions[0]);
      };

      $(searchSelector)?.addEventListener("input", () => { currentPage = 1; applyResourceFilters(); });
      tabs.forEach(tab => tab.addEventListener("click", () => {
        tabs.forEach(item => item.classList.remove("active"));
        tab.classList.add("active");
        currentPage = 1;
        applyResourceFilters();
      }));
      const selectPaperYear = bindSelectMenu(paperYearSelect, () => {
        currentPage = 1;
        applyResourceFilters();
      });
      problemFilterSelects.forEach(wrapper => bindSelectMenu(wrapper, () => {
        currentPage = 1;
        applyResourceFilters();
      }));
      paperCompetitionFilters.forEach(button => button.addEventListener("click", () => {
        paperCompetitionFilters.forEach(item => item.classList.toggle("active", item === button));
        syncPaperScopes();
        currentPage = 1;
        applyResourceFilters();
      }));
      paperGroupFilters.forEach(button => button.addEventListener("click", () => {
        paperGroupFilters.forEach(item => item.classList.toggle("active", item === button));
        currentPage = 1;
        applyResourceFilters();
      }));
      paperReset?.addEventListener("click", () => {
        const search = $(searchSelector);
        if (search) search.value = "";
        if (paperYearOptions.length) selectPaperYear(paperYearOptions[0]);
        tabs.forEach((tab, index) => tab.classList.toggle("active", index === 0));
        paperCompetitionFilters.forEach((button, index) => button.classList.toggle("active", index === 0));
        paperGroupFilters.forEach((button, index) => button.classList.toggle("active", index === 0));
        syncPaperScopes();
        currentPage = 1;
        applyResourceFilters();
      });
      const openResourceDetail = row => {
        const route = kind === "problem" ? routes.problemDetail : routes.paperDetail;
        window.location.href = `${route}?index=${row.dataset.resourceIndex || 0}`;
      };
      rows.forEach(row => {
        row.addEventListener("click", event => {
          if (event.target.closest("button")) return;
          openResourceDetail(row);
        });
        row.addEventListener("keydown", event => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          openResourceDetail(row);
        });
      });
      // 数字按钮每次筛选后重建，所以监听挂在容器上，而不是挂在按钮本身。
      $("[data-resource-pagination]")?.addEventListener("click", event => {
        const button = event.target.closest("[data-resource-page]");
        if (!button || button.disabled) return;
        const requested = button.dataset.resourcePage;
        if (requested === "prev") currentPage -= 1;
        else if (requested === "next") currentPage += 1;
        else currentPage = Number(requested);
        applyResourceFilters();
        toast(`已切换到第 ${currentPage} 页`);
      });
      applyResourceFilters();
    };

    if (screen === "projects") {
      $("[data-table-search]")?.addEventListener("input", e => {
        const q = e.target.value.trim().toLowerCase();
        $$(".project-table tbody tr").forEach(row => row.hidden = !row.innerText.toLowerCase().includes(q));
      });
      $$("[data-project-tab]").forEach(button => button.addEventListener("click", () => {
        $$("[data-project-tab]").forEach(b => b.classList.remove("active")); button.classList.add("active");
        $$(".project-table tbody tr").forEach((row, i) => row.hidden = button.dataset.projectTab !== "全部" && i % 3 !== ["进行中","已完成","已归档"].indexOf(button.dataset.projectTab));
      }));
      $$(".project-table tbody tr").forEach(row => row.addEventListener("click", event => {
        if (!event.target.closest("button")) go("running");
      }));
      bindSelectMenu($("[data-page-size-select]"), value => toast(`已切换为每页 ${value} 条`));
    }
    // 合并工作台：五个阶段面板同存于 DOM，四个有交互的阶段绑定块一起生效。
    const mergedWorkspace = Boolean(workspaceStageContent[screen]);
    if (screen === "data" || mergedWorkspace) {
      $$("[data-data-tab]").forEach(button => button.addEventListener("click", () => {
        $$("[data-data-tab]").forEach(item => item.classList.remove("active"));
        button.classList.add("active");
        toast(`已切换到${button.dataset.dataTab}`);
      }));
      $$("[data-data-file]").forEach((file, i) => file.addEventListener("click", () => {
        $$("[data-data-file]").forEach(f => f.classList.remove("active")); file.classList.add("active");
        const titles = ["共享单车订单数据", "站点基础信息", "天气观测数据", "清洗后建模数据"];
        $("[data-data-title]").textContent = titles[i];
      }));
      $$("[data-drawer-tab]").forEach(button => button.addEventListener("click", () => {
        $$("[data-drawer-tab]").forEach(item => item.classList.remove("active"));
        button.classList.add("active");
        toast(`已切换到${button.dataset.drawerTab}`);
      }));
    }
    if (screen === "model" || mergedWorkspace) {
      $$("[data-plan-option]").forEach(button => button.addEventListener("click", () => {
        $$("[data-plan-option]").forEach(item => item.classList.remove("selected"));
        button.classList.add("selected");
        $$("[data-plan-option] > i").forEach(item => item.className = "ph ph-caret-down");
        const caret = $("i", button);
        if (caret) caret.className = "ph ph-caret-up";
      }));
    }
    if (screen === "experiments" || mergedWorkspace) {
      $$(".experiment-item").forEach(item => item.addEventListener("click", () => {
        $$(".experiment-item").forEach(i => i.classList.remove("active")); item.classList.add("active");
        $(".experiment-titlebar h2").textContent = experiments[+item.dataset.experiment][0];
      }));
      $$("[data-experiment-tab]").forEach(button => button.addEventListener("click", () => {
        $$("[data-experiment-tab]").forEach(b => b.classList.remove("active")); button.classList.add("active");
        toast(`已切换到${button.dataset.experimentTab}`);
      }));
    }
    if (screen === "editor" || mergedWorkspace) {
      $$(".outline a").forEach(link => link.addEventListener("click", () => { $$(".outline a").forEach(x=>x.classList.remove("active")); link.classList.add("active"); }));
    }
    if (screen === "problems") {
      bindResourceDirectory("problem");
    }
    if (screen === "papers") {
      bindResourceDirectory("paper");
    }
    if (screen === "methods") {
      $$("[data-tree-group]").forEach(trigger => trigger.addEventListener("click", () => {
        const group = trigger.closest("[data-method-group]");
        setMethodGroupExpanded(group, trigger.getAttribute("aria-expanded") !== "true");
      }));
      $$("[data-method]").forEach(item => item.addEventListener("click", event => {
        event.preventDefault();
        showMethodById(item.dataset.method);
      }));

      const searchInput = $("[data-method-search]");
      const searchStatus = $("[data-method-search-status]");
      const favoriteFilter = $("[data-method-favorite-filter]");
      let searchActive = false;
      let expansionBeforeSearch = new Map();
      let autoOpenTimer = 0;

      const applyMethodFilters = () => {
        window.clearTimeout(autoOpenTimer);
        const query = searchInput?.value.trim().normalize("NFKC").toLowerCase() || "";
        const favoritesOnly = favoriteFilter?.getAttribute("aria-pressed") === "true";
        const favorites = readMethodFavorites();
        const narrowed = Boolean(query) || favoritesOnly;
        if (narrowed && !searchActive) {
          expansionBeforeSearch = new Map($$("[data-method-group]").map(group => [group.dataset.methodGroup, $("[data-tree-group]", group)?.getAttribute("aria-expanded") === "true"]));
          searchActive = true;
        }
        let visibleCount = 0;
        $$("[data-method-group]").forEach(group => {
          let groupMatches = 0;
          $$("[data-method]", group).forEach(item => {
            const searchCopy = item.dataset.methodSearchCopy.normalize("NFKC").toLowerCase();
            const matches = (!query || searchCopy.includes(query)) && (!favoritesOnly || favorites.includes(item.dataset.method));
            item.hidden = !matches;
            if (matches) groupMatches += 1;
          });
          group.hidden = narrowed && groupMatches === 0;
          if (narrowed && groupMatches > 0) {
            setMethodGroupExpanded(group, true);
          } else if (!narrowed) {
            setMethodGroupExpanded(group, expansionBeforeSearch.get(group.dataset.methodGroup) ?? $("[data-tree-group]", group)?.getAttribute("aria-expanded") === "true");
          }
          visibleCount += groupMatches;
        });
        const empty = $("[data-method-no-results]");
        if (empty) {
          empty.hidden = !narrowed || visibleCount > 0;
          empty.textContent = favoritesOnly && !query ? "还没有收藏任何方法" : "没有匹配的方法";
        }
        if (searchStatus) {
          if (!narrowed) searchStatus.textContent = `${methodLibrary.length} 种方法`;
          else if (!visibleCount) searchStatus.textContent = favoritesOnly && !query ? "收藏为空" : "未找到匹配方法";
          else searchStatus.textContent = favoritesOnly && !query ? `收藏 ${visibleCount} 种方法` : `找到 ${visibleCount} 种方法`;
        }
        if (query && visibleCount > 0) {
          const visibleMatches = $$("[data-method]").filter(item => !item.hidden && !item.closest("[data-method-group]")?.hidden);
          const exactMatch = visibleMatches.find(item => item.textContent.trim().normalize("NFKC").toLowerCase() === query);
          const autoMatch = exactMatch || (visibleMatches.length === 1 ? visibleMatches[0] : null);
          if (autoMatch) {
            autoOpenTimer = window.setTimeout(() => {
              const currentQuery = searchInput?.value.trim().normalize("NFKC").toLowerCase() || "";
              if (currentQuery !== query || autoMatch.hidden || autoMatch.closest("[data-method-group]")?.hidden) return;
              const entry = showMethodById(autoMatch.dataset.method, { notify: false });
              if (entry && searchStatus) searchStatus.textContent = `找到 ${visibleCount} 种方法 · 已打开 ${entry.name}`;
            }, 160);
          }
        }
        if (!narrowed) {
          searchActive = false;
          expansionBeforeSearch = new Map();
        }
      };

      searchInput?.addEventListener("input", applyMethodFilters);
      searchInput?.addEventListener("keydown", event => {
        if (event.key === "Escape" && searchInput.value) {
          event.preventDefault();
          searchInput.value = "";
          applyMethodFilters();
          return;
        }
        if (event.key !== "Enter") return;
        const matches = $$("[data-method]").filter(item => !item.hidden && !item.closest("[data-method-group]")?.hidden);
        if (matches.length === 1) {
          event.preventDefault();
          showMethodById(matches[0].dataset.method);
        }
      });
      favoriteFilter?.addEventListener("click", () => {
        const next = favoriteFilter.getAttribute("aria-pressed") !== "true";
        favoriteFilter.setAttribute("aria-pressed", String(next));
        favoriteFilter.classList.toggle("active", next);
        applyMethodFilters();
      });
      refreshMethodFavoriteUi = applyMethodFilters;
      renderFormulas();
    }
  }

/**
 * 赛题库约 2MB，静态 import 会把它焊进主 chunk。用 `?url` 让 Vite 把它作为静态资源
 * 输出，再运行时 fetch：JSON 不再参与 JS 打包，由浏览器原生解析并独立缓存。
 * 只有真正用到赛题数据的页面才需要 await，其余页面完全不必下载。
 */
export async function preloadKnowledgeLibrary(): Promise<void> {
  if (problems.length) return;
  const { default: url } = await import("../data/knowledge-library.json?url");
  const response = await fetch(url);
  if (!response.ok) throw new Error(`知识库加载失败：HTTP ${response.status}`);
  const library = (await response.json()) as KnowledgeLibrary;
  problems.push(...library.problems);
  papers.push(...library.papers);
}

async function initPaperPdfReader(): Promise<void> {
  const reader = document.querySelector("[data-paper-pdf-reader]");
  if (!(reader instanceof HTMLElement) || reader.dataset.initialized === "true") return;
  reader.dataset.initialized = "true";
  let pdfSources: string[] = [];
  try {
    const parsed = JSON.parse(reader.dataset.paperPdfSources || "[]");
    if (Array.isArray(parsed)) pdfSources = parsed.filter(source => typeof source === "string" && source);
  } catch {
    pdfSources = [];
  }
  const pageCopy = document.querySelector("[data-paper-page-copy]");
  try {
    const [{ getDocument, GlobalWorkerOptions }, { default: workerUrl }] = await Promise.all([
      import("pdfjs-dist"),
      import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
    ]);
    GlobalWorkerOptions.workerSrc = workerUrl;
    let pdf = null;
    let lastError: unknown;
    for (const source of pdfSources) {
      try {
        pdf = await getDocument({ url: source }).promise;
        break;
      } catch (error) {
        lastError = error;
      }
    }
    if (!pdf) throw lastError ?? new Error("论文 PDF 地址为空");
    if (pageCopy) {
      const sizeSuffix = pageCopy.textContent?.includes(" · ") ? ` · ${pageCopy.textContent.split(" · ").slice(1).join(" · ")}` : "";
      pageCopy.textContent = `${pdf.numPages} 页${sizeSuffix}`;
    }

    const displayWidth = Math.min(980, Math.max(320, reader.clientWidth - 44));
    const firstPage = await pdf.getPage(1);
    const naturalViewport = firstPage.getViewport({ scale: 1 });
    const estimatedHeight = Math.round(displayWidth * naturalViewport.height / naturalViewport.width);
    const pageHosts = Array.from({ length: pdf.numPages }, (_, index) => {
      const host = document.createElement("section");
      host.className = "paper-pdf-page";
      host.dataset.paperPage = String(index + 1);
      host.dataset.state = "pending";
      host.style.minHeight = `${estimatedHeight}px`;
      host.setAttribute("aria-label", `论文第 ${index + 1} 页，共 ${pdf.numPages} 页`);
      host.setAttribute("aria-busy", "true");
      host.innerHTML = `<span class="paper-page-number">${index + 1} / ${pdf.numPages}</span><div class="paper-page-placeholder"><span></span>正在渲染第 ${index + 1} 页</div>`;
      return host;
    });
    reader.replaceChildren(...pageHosts);
    reader.setAttribute("aria-busy", "false");

    const renderPage = async (pageNumber: number) => {
      const host = pageHosts[pageNumber - 1];
      if (!host || host.dataset.state !== "pending") return;
      host.dataset.state = "loading";
      try {
        const page = pageNumber === 1 ? firstPage : await pdf.getPage(pageNumber);
        const baseViewport = page.getViewport({ scale: 1 });
        const viewport = page.getViewport({ scale: displayWidth / baseViewport.width });
        const outputScale = Math.min(window.devicePixelRatio || 1, 1.75);
        const canvas = document.createElement("canvas");
        canvas.width = Math.floor(viewport.width * outputScale);
        canvas.height = Math.floor(viewport.height * outputScale);
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        const canvasContext = canvas.getContext("2d", { alpha: false });
        if (!canvasContext) throw new Error("Canvas 2D context unavailable");
        await page.render({
          canvasContext,
          viewport,
          transform: outputScale === 1 ? undefined : [outputScale, 0, 0, outputScale, 0, 0],
        }).promise;
        host.style.minHeight = "0";
        host.replaceChildren(canvas);
        host.dataset.state = "rendered";
        host.setAttribute("aria-busy", "false");
      } catch (error) {
        host.dataset.state = "error";
        host.setAttribute("aria-busy", "false");
        host.innerHTML = `<div class="paper-page-error">第 ${pageNumber} 页渲染失败，请使用下方完整 PDF 入口查看。</div>`;
        console.error(`论文第 ${pageNumber} 页渲染失败`, error);
      }
    };

    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        const pageNumber = Number((entry.target as HTMLElement).dataset.paperPage);
        observer.unobserve(entry.target);
        void renderPage(pageNumber);
      });
    }, { rootMargin: "1200px 0px" });
    pageHosts.forEach(host => observer.observe(host));
    await renderPage(1);
  } catch (error) {
    reader.setAttribute("aria-busy", "false");
    reader.innerHTML = `<div class="paper-pdf-error">${icon("warning-circle")}<strong>论文正文加载失败</strong><span>请使用下方完整 PDF 入口查看原文。</span></div>`;
    console.error("完整论文 PDF 加载失败", error);
  }
}

/**
 * 供 React 页面复用产品外壳：返回 <aside class="sidebar"> 的内部结构，
 * 由调用方自己提供 aside 容器，避免 app-shell 的栅格里多出一层包装元素。
 */
export function renderSidebarInner(active: string): string {
  return sidebarInner(active);
}

export function isSidebarCollapsed(): boolean {
  return sidebarCollapsed();
}

/**
 * 侧栏里的折叠、设置、筛选等都挂在 document 级事件委托上。React 页面不走
 * activateScreen，需要单独挂一次；重复调用会叠加监听器，因此加锁只执行一次。
 */
let shellChromeBound = false;
export function mountShellChrome(): void {
  if (shellChromeBound) return;
  shellChromeBound = true;
  bindCommon("");
}

export function getScreenMarkup(screen: ScreenId): string {
  return (renderers[screen] || renderers.new)();
}

export function activateScreen(screen: ScreenId): void {
  document.body.dataset.screen = screen;
  bindCommon(screen);
  bindScreen(screen);
  initModelingResizer();
  initCharts(screen);
  if (screen === "paperDetail") void initPaperPdfReader();
  void hydrateAccountUi();
  // 侧栏「最近任务」换成真实任务记录；未登录保持模板演示条目。
  void hydrateRecentTasks();
  // 「我的项目」页换成全量真实项目清单；未登录保持模板演示表格。
  void hydrateProjectsPage();
  // 隐私开关并入本机（每会话一次），换浏览器后通知/历史闸门立即正确。
  void syncPrivacyGatesOnce();
  // 预算达到提醒阈值时提醒一次；正看着页面时以页内提示呈现。
  void maybeNotifyBudgetAlert().then(message => {
    if (message) toast(t(message));
  });
  mountModelingWorkspace(screen);
  // 侧栏「搜索任务」：document 级委托只挂一次，跨页面重渲染仍生效。
  mountSidebarSearch();
  if (workspaceStageContent[screen]) bindWorkspaceStageNav();
  // 「添加上下文」引用随页面重建复位（chips 宿主已随旧 DOM 销毁）。
  resetComposerReferences();
  // 放在工作台挂载之后：恢复对话草稿要等 composer 渲染完成。
  mountTaskAutosave(screen);
  // 模型选择器换成真实接口池（Auto + 已保存接口），未配置时保持演示选项。
  void hydrateModelPickers();
}

