// @ts-nocheck
import type { ScreenId } from "../types/screens";
import type { KnowledgeLibrary } from "../types/knowledge-library";
import { methodCategories, methodLibrary } from "../data/method-library";
import { RECIPE_LANGUAGES, methodRecipes } from "../data/method-recipes";
import { hydrateAccountUi, initSecurityPane } from "../auth/account-security";

  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
  const icon = (name, extra = "") => `<i class="ph ph-${name} ${extra}" aria-hidden="true"></i>`;
  const projectLogo = (extra = "") =>
    `<img class="project-logo ${extra}" src="/assets/OpenMathModel_IP_Crop.png" alt="" aria-hidden="true">`;
  const providerLogoSources = {
    qwen: "/assets/provider-qwen.svg",
    deepseek: "/assets/provider-deepseek.svg",
    openai: "/assets/provider-openai.webp",
    anthropic: "/assets/provider-anthropic.svg",
    ollama: "/assets/provider-ollama.svg"
  };
  const providerLogo = (provider, label, extra = "") =>
    `<img class="provider-brand-logo provider-brand-${provider} ${extra}" src="${providerLogoSources[provider]}" alt="${escapeHtml(label)}">`;
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
  const normalizeTheme = theme => theme === "dark" ? "dark" : "light";
  const savedTheme = () => {
    try {
      return normalizeTheme(JSON.parse(localStorage.getItem("openmathmodelSettings") || "{}").theme);
    } catch {
      return "light";
    }
  };
  const applyTheme = theme => {
    const next = normalizeTheme(theme);
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    return next;
  };
  applyTheme(savedTheme());

  const sidebarCollapsed = () => {
    try {
      return localStorage.getItem("openmathmodelSidebarCollapsed") === "true";
    } catch {
      return false;
    }
  };

  const composerModelOptions = () => {
    let settings = {};
    try {
      settings = JSON.parse(localStorage.getItem("openmathmodelSettings") || "{}");
    } catch {}
    const customModel = settings.apiModel || "gpt-4.1";
    const customProfile = settings.apiProfileName || "OpenAI 兼容中转站";
    return [
      { id: "auto", label: "Auto", detail: "智能路由 · 自动选择", provider: "agent" },
      { id: "qwen-max", label: "Qwen3.7-Max", detail: "通义千问 · 官方服务", provider: "qwen" },
      { id: "deepseek-v3", label: "DeepSeek V3", detail: "DeepSeek · 官方服务", provider: "deepseek" },
      { id: "gpt-4.1", label: "GPT-4.1", detail: "OpenAI · 官方服务", provider: "openai" },
      { id: "claude-sonnet", label: "Claude Sonnet", detail: "Anthropic · 官方服务", provider: "anthropic" },
      { id: `custom-${customModel}`, label: customModel, detail: `${customProfile} · 自定义 API`, provider: "custom" }
    ];
  };

  const composerModelLogo = option => option.provider === "agent"
    ? projectLogo("composer-logo")
    : option.provider === "custom"
      ? `<span class="custom-api-logo">${icon("plugs-connected")}</span>`
      : providerLogo(option.provider, option.detail, "composer-provider-logo");

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
            <input aria-label="全局搜索" placeholder="搜索任务">
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
    return `<div class="app-shell ${sidebarCollapsed() ? "sidebar-collapsed" : ""}" data-sidebar-shell>
      ${sidebar(active)}
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
      <textarea aria-label="任务描述" placeholder="${placeholder}"></textarea>
      <div class="composer-tools">
        <input class="file-input" type="file" multiple hidden>
        <div class="composer-tool-group">
          <button class="tool-button icon-tool" data-action="attach" title="添加文件">${icon("plus")}</button>
          <button class="tool-button" data-action="reference">${icon("at")}<span class="tool-label">添加上下文</span></button>
          <button class="tool-button" data-action="mode">${icon("circles-three-plus")}<span class="tool-label">自动模式</span>${icon("caret-down")}</button>
        </div>
        <div class="composer-model-picker" data-model-picker>
          <button type="button" class="composer-model" data-action="model-picker" aria-haspopup="listbox" aria-expanded="false" title="选择模型">
            <span class="composer-model-icon" data-model-picker-icon>${composerModelLogo(selected)}</span>
            <span data-model-picker-label>${escapeHtml(selected.label)}</span>${icon("caret-down")}
          </button>
          <div class="agent-model-menu" role="listbox" aria-label="选择模型">
            <div class="agent-model-menu-title">选择模型或 API</div>
            ${options.map(option => `<button type="button" data-action="select-model" data-model-choice="${escapeHtml(option.id)}" role="option" aria-selected="${option.id === selected.id}">
              <span class="model-choice-logo">${composerModelLogo(option)}</span>
              <span class="model-choice-copy"><strong>${escapeHtml(option.label)}</strong><small>${escapeHtml(option.detail)}</small></span>
              ${icon("check")}
            </button>`).join("")}
          </div>
        </div>
        <button class="send-button primary" data-action="send" aria-label="发送" title="发送（Enter）">${icon("arrow-up")}</button>
      </div>
    </div>`;
  }

  function newScreen() {
    return shell(`
      <section class="new-screen">
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
        </div>
        <p class="composer-note">AI 可能会出错，请核查关键结论、代码与引用。</p>
      </section>`, "chat");
  }

  function confirmScreen() {
    return shell(`
      <section class="confirm-wrap">
        <h1>确认任务</h1>
        <p class="muted">检查题目、文件和输出要求，确认后开始执行。</p>
        <div class="headline">2026全国大学生数学建模竞赛A题</div>
        <h3>文件</h3>
        <div class="file-read-list">
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
          <button class="primary" data-go="running">开始任务</button>
        </div>
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
        <strong>${projectName}</strong>
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
      ${composer(active === "complete" ? "继续描述任务，快速问问，@ 添加上下文" : "继续描述任务，快速调用，@ 添加上下文", true)}
    </section>`;
  }

  const focusedStages = new Set(["data", "model", "experiments", "editor", "complete"]);

  function focusedModelingHeader(active) {
    const backRoute = {
      data: routes.running,
      model: routes.data,
      experiments: routes.model,
      editor: routes.experiments,
      complete: routes.editor
    }[active] || routes.running;
    const backLabel = {
      data: "返回任务执行",
      model: "返回数据准备",
      experiments: "返回模型方案",
      editor: "返回实验结果",
      complete: "返回论文编辑"
    }[active] || "返回任务执行";
    return `<header class="focused-modeling-topbar">
      <div class="focused-topbar-context">
        <a class="focused-back" href="${backRoute}" aria-label="${backLabel}" title="${backLabel}">${icon("arrow-left")}</a>
        <a class="focused-task-name" href="${routes.running}"><span>城市共享单车调度优化</span>${icon("caret-down")}</a>
      </div>
    </header>`;
  }

  function focusedAgentPane(active) {
    const stage = {
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
    }[active];
    const steps = [
      ["已读取题目与附件", "00:03"],
      ["已完成问题拆解", "00:06"],
      ["已完成数据结构分析", "00:12"],
      ["已完成候选模型比较", "00:18"]
    ];
    const attachments = active === "data" ? `<section class="focused-attachments">
      <h3>附件</h3>
      <button type="button" class="focused-attachment" data-action="download-data"><span class="attachment-file-icon xls">X.</span><span><strong>历史供需数据_2024Q4.xlsx</strong><small>24.7 MB</small></span>${icon("download-simple")}</button>
      <button type="button" class="focused-attachment" data-action="download-data"><span class="attachment-file-icon csv">csv</span><span><strong>字段说明草稿.csv</strong><small>8.3 KB</small></span>${icon("download-simple")}</button>
    </section>` : "";
    return `<section class="chat-pane focused-agent-chat">
      <div class="focused-agent-head"><div class="assistant-id">${projectLogo("assistant-logo")}<span>Agent</span></div></div>
      <div class="focused-agent-scroll">
        <button type="button" class="activity-summary" data-action="toggle-activity" aria-expanded="true" aria-controls="focused-activity-list-${active}">${icon("eye-slash")} 收起执行步骤 ${icon("caret-up")}</button>
        <div class="focused-activity-list" id="focused-activity-list-${active}">
          ${steps.map(([text, time]) => `<div class="focused-step"><span class="focused-step-dot done">${icon("check-circle")}</span><span>${text}</span><time>${time}</time>${icon("caret-down", "chev")}</div>`).join("")}
          <div class="focused-step current"><span class="focused-step-dot ${active === "complete" ? "done" : ""}">${active === "complete" ? icon("check-circle") : ""}</span><span>${stage.current}</span><span class="focused-loading">${active === "complete" ? "完成" : "·····"}</span>${icon("caret-up", "chev")}</div>
        </div>
        <div class="focused-agent-copy">${stage.copy}</div>
        ${attachments}
        <button class="focused-stage-cta" type="button" data-go="${stage.next}">${stage.button}</button>
      </div>
      ${composer("继续描述任务，快速调用，@ 添加上下文", true)}
    </section>`;
  }

  function workspaceTabs(tabs, activeTab) {
    return `<div class="focused-workspace-tabs" role="tablist">${tabs.map(([key, label, iconName]) => `<button type="button" class="${key === activeTab ? "active" : ""}" data-workspace-tab="${key}" role="tab" aria-selected="${key === activeTab}">${icon(iconName)}<span>${label}</span></button>`).join("")}</div>`;
  }

  function modelingShell(content, active, auxiliary = "") {
    if (focusedStages.has(active)) {
      return `<div class="modeling-shell modeling-clone-shell" data-modeling-shell data-focused-stage="${active}">
        ${focusedModelingHeader(active)}
        <div class="focused-modeling-split" data-modeling-split>
          <aside class="focused-agent-pane">${focusedAgentPane(active)}</aside>
          <div class="modeling-resizer focused-modeling-resizer" data-modeling-resizer role="separator" aria-label="调整 Agent 与建模内容的宽度" aria-orientation="vertical" aria-valuemin="20" aria-valuemax="58" aria-valuenow="27" tabindex="0"></div>
          <main class="focused-stage-pane">${content}</main>
        </div>
      </div>`;
    }
    return `<div class="modeling-shell" data-modeling-shell>
      ${modelingHeader(active)}
      <div class="modeling-split" data-modeling-split>
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
    const prompt = sessionStorage.getItem("openmathmodelPrompt") || "请结合共享单车订单、站点与天气数据，完成需求预测、区域划分和调度优化。";
    const steps = [
      ["已读取题目与附件", "00:03", "已识别题面、订单、站点和天气数据。"],
      ["已完成问题拆解", "00:06", "任务拆解为需求预测、区域划分和调度优化。"],
      ["已完成数据结构分析", "00:12", "已检查字段完整性、时间粒度与异常值。"],
      ["已完成候选模型比较", "00:18", "已比较 XGBoost、Prophet 和 LSTM 的适配度。"]
    ];
    return shell(`
      <section class="running-main">
        <section class="chat-pane running-chat-pane">
          <header class="task-toolbar">
            <a class="back" href="${routes.new}" aria-label="返回首页" title="返回首页">${icon("arrow-left")}</a>
            <div><h2>城市共享单车调度优化</h2><p>2026 国赛 A 题　·　自动模式</p></div>
            <div class="task-toolbar-actions">
              <span class="run-status complete"><b></b> 规划完成</span>
              <button type="button" data-action="files" aria-label="查看 3 个附件">${icon("paperclip")} 3</button>
              <button type="button" data-action="more" aria-label="更多操作">${icon("dots-three")}</button>
            </div>
          </header>
          <div class="chat-scroll">
            <div class="user-message"><div class="user-bubble">${escapeHtml(prompt)}</div></div>
            <div class="assistant-block">
              <div class="assistant-id">${projectLogo("assistant-logo")}<span>Agent</span></div>
              <button class="activity-summary" data-action="toggle-activity">${icon("eye-slash")} 收起执行步骤 ${icon("caret-up")}</button>
              <div class="activity-list">
                ${steps.map(step => progressStep(true, step[0], step[1], step[2], true)).join("")}
              </div>
              <div class="analysis-copy">
                <p>我已经完成题目和附件的初步读取。这个任务可以稳定地拆成三个相互衔接的子问题：</p>
                <ol><li>需求预测</li><li>区域划分</li><li>调度优化</li></ol>
              </div>
              <h4>推荐建模路线</h4>
              <div class="plan-card">
                <div><div class="plan-title"><span class="step-dot done">${icon("check")}</span>XGBoost 需求预测 + 混合整数规划调度</div><p class="plan-reason">理由：能够捕捉短期时空需求变化，并在约束条件下获得全局最优调度方案。</p></div>
                <div class="plan-time"><span class="muted">预计运行时间</span><strong>2.5 ~ 3.5 小时</strong></div>
                <button type="button" class="next-step-link" data-go="data">进入数据准备 ${icon("arrow-right")}</button>
              </div>
              <details class="alternatives"><summary>查看备选路线</summary><p>Prophet + 层次聚类 + 线性规划；LSTM + K-means + 启发式调度。</p></details>
            </div>
          </div>
          ${composer("继续描述任务，/ 快速调用，@ 添加上下文", true)}
        </section>
        <aside class="inspector">
          <div class="inspector-head"><span>任务上下文</span><button type="button" data-action="more" aria-label="更多上下文操作">${icon("dots-three")}</button></div>
          <section class="inspector-section">
            <div class="inspector-title">附件 <span>3</span></div>
            <button class="attachment-chip" data-action="files">${icon("file-pdf")}2026国赛A题题目.pdf</button>
            <button class="attachment-chip" data-action="files">${icon("file-csv")}共享单车数据集.csv</button>
            <button class="attachment-chip" data-action="files">${icon("file-image")}城市区域划分示意图.png</button>
            <button class="text-action" data-action="files">管理附件</button>
          </section>
          <section class="inspector-section">
            <div class="inspector-title">当前假设 <button class="text-action" data-action="edit-assumption">编辑</button></div>
            <ul><li>数据完整且质量可用</li><li>共享单车可跨区域调度</li><li>调度以最小化总缺车惩罚为目标</li><li>满足车辆总量与站点容量约束</li></ul>
          </section>
          <section class="inspector-section">
            <div class="inspector-title">输出要求 <button class="text-action" data-action="edit-output">编辑</button></div>
            <ul><li>给出完整建模流程与假设说明</li><li>提供关键模型公式与变量定义</li><li>输出可复现实验结果与对比分析</li><li>编写模型求解代码与可视化结果</li></ul>
          </section>
        </aside>
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
                <td class="project-name"><div class="table-doc">${icon("file-text")}<div><strong>${r[0]}</strong><span>${r[1]}</span></div></div></td>
                <td>${r[2]}</td><td>${r[3]}</td><td>${r[4]}</td><td>${r[5]}</td><td>${r[6]}</td>
                <td><button style="border:0" data-action="row-menu">${icon("dots-three")}</button></td>
              </tr>`).join("")}
            </tbody>
          </table>
        </div>
        <div class="project-footer"><span>共 7 项</span><div class="pagination"><button class="page-button" disabled>‹</button><button class="page-button active">1</button><button class="page-button">›</button><select class="page-size"><option>20 条/页</option></select></div></div>
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

  function dataScreen() {
    return modelingShell(`
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
      </section>`, "data");
  }

  function modelScreen() {
    return modelingShell(`
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
      </section>`, "model");
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

  function experimentsScreen() {
    return modelingShell(`
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
      </section>`, "experiments");
  }

  function editorScreen() {
    return modelingShell(`
      <section class="focused-workspace paper-editor-workspace paper-only-workspace">
        <div class="focused-workspace-panel active paper-only-panel">
          <section class="editor-main workflow-editor paper-only-editor">
            <div class="editor-layout">
              <aside class="outline"><div class="outline-heading"><h3>论文大纲</h3>${icon("dots-three-vertical")}</div>${["摘要","1 引言","2 相关工作","3 需求预测模型构建","4 实证分析","5 结果与讨论","6 结论与展望"].map((x,i)=>`<a href="#section-${i}" class="${i===3?"active":""}"><span class="outline-status ${i<3?"done":""}">${i<3?icon("check"):""}</span>${x}</a>`).join("")}</aside>
              <article class="paper-editor">
                <div class="editor-toolbar">
                  <div class="editor-format-tools">
                    <button data-command="undo" aria-label="撤销">${icon("arrow-u-up-left")}</button><button data-command="redo" aria-label="重做">${icon("arrow-u-up-right")}</button><span class="toolbar-divider"></span>
                    <button>正文 ${icon("caret-down")}</button><button>宋体 ${icon("caret-down")}</button><button>五号 ${icon("caret-down")}</button><span class="toolbar-divider"></span>
                    <button data-command="bold" aria-label="加粗"><strong>B</strong></button><button data-command="italic" aria-label="斜体"><i>I</i></button><button data-command="underline" aria-label="下划线"><u>U</u></button><button>${icon("text-t")}${icon("caret-down")}</button><span class="toolbar-divider"></span>
                    <button>${icon("text-align-left")}${icon("caret-down")}</button><button>${icon("table")}</button><button data-action="image">${icon("image")}</button><button data-action="formula">ƒx</button><button data-action="cite">${icon("link")}</button><span class="toolbar-divider"></span><button data-action="cite">${icon("quotes")} 引用</button>
                  </div>
                  <div class="paper-editor-inline-actions"><button data-action="editor-check">检查</button><button data-action="export-paper">导出</button><button class="primary" data-action="continue-paper">完成交付</button></div>
                </div>
                <div class="editor-page" contenteditable="true" spellcheck="false">
                  <h1>城市共享单车需求预测与调度优化研究</h1>
                  <h2 id="section-3">3 需求预测模型构建</h2>
                  <h3>3.1 问题定义</h3><p>在给定研究区域与时间范围内，基于历史数据与相关影响因素，预测各区域在未来时段的共享单车需求，并制定车辆调度方案，使得调度总成本最小，同时满足各区域的需求平衡约束。</p>
                  <h3>3.2 特征设计</h3><p>本文从时间、空间、天气和社会活动四个维度构建特征体系。时间维度包括小时、星期、节假日等；<mark>空间维度包括区域类型、POI 密度、周边地铁站距离等；</mark>天气维度包括温度、降水、风速等；社会活动维度包括大型活动、演出、赛事等。</p>
                  <button class="source-chip" contenteditable="false" data-action="source-detail">来源：Run #04 · 结果表 2　${icon("arrow-square-out")}</button>
                  <h3>3.3 模型设定</h3><p>采用基于图卷积网络（GCN）的时空预测模型，结合区域间拓扑关系与动态特征，捕捉需求的时空相关性。</p><p>模型目标函数如下：</p>
                  <div class="editor-formula"><em>min</em>　∑<sub>i=1</sub><sup>N</sup> ∑<sub>t=1</sub><sup>T</sup> (y<sub>it</sub> − ŷ<sub>it</sub>)² + λ‖Θ‖²<sub>2</sub></div>
                  <p>其中，y<sub>it</sub> 表示区域 i 在时段 t 的真实需求，ŷ<sub>it</sub> 表示模型预测值，Θ 为模型参数，λ 为正则化系数。</p>
                </div>
              </article>
            </div>
          </section>
        </div>
      </section>`, "editor");
  }

  // 2MB 的赛题库改由 preloadKnowledgeLibrary 原地填充，使它能拆成独立 chunk 而不进主包。
  // 保持同一个数组引用，下游的 map/find/length 调用无需改动。
  const problems = [];
  const papers = [];
  const problemTabs = () => ["全部赛题", ...new Set(problems.map(problem => problem.category)), "收藏"];
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
    try {
      const url = new URL(source, window.location.origin);
      const decodedPath = decodeURIComponent(url.pathname);
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
  const paperPdfSources = paper => [...new Set([
    localPaperPdfUrl(paper),
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
  const paperEntries = () => papers
    .map((paper, index) => ({
      paper,
      index,
      problem: paperProblem(paper),
      displayTitle: paperDisplayTitle(paper),
      group: paperGroup(paper),
    }))
    .filter(({ paper }) => paper.record_type === "paper" && paper.access_scope === "linked_content" && paperPdfUrl(paper));
  const paperTabs = () => ["全部", ...new Set(paperEntries().map(({ paper }) => paper.category))];
  // 页码按钮由 applyResourceFilters 按“筛选后”的条数现算，所以这里不再预渲染：
  // 搜索或切换分类之后总页数会变，静态渲染出来的 1…N 只会是假的。
  const RESOURCE_PAGE_SIZE = 15;

  function problemsScreen() {
    return shell(`
      <section class="library-main resource-library problems-main">
        <div class="library-heading"><h1>赛题库</h1><p>浏览历年赛题、问题类型与建模方向。</p></div>
        <div class="library-tools resource-tools"><label class="search-box">${icon("magnifying-glass")}<input type="search" name="problem-search" data-problem-search autocomplete="off" aria-label="搜索赛题" placeholder="搜索赛题、领域或关键词"></label>
          <div class="filters">${["比赛","年份","问题类型","建模方向"].map(x=>`<button class="filter-button" data-action="filter">${x}${icon("caret-down")}</button>`).join("")}</div>
        </div>
        <div class="resource-tabs" role="tablist" aria-label="赛题分类">
          ${problemTabs().map((x,i)=>`<button class="${i===0?"active":""}" data-resource-tab="${escapeHtml(x)}" data-resource-kind="problem">${x==="收藏"?icon("star"):""}${escapeHtml(x)}</button>`).join("")}
        </div>
        <div class="resource-table-wrap">
          <table class="resource-table problem-resource-table">
            <thead><tr><th>题目</th><th>比赛</th><th>年份</th><th>问题类型</th><th>关键词</th><th>数据要求</th><th>状态</th></tr></thead>
            <tbody data-problem-list>
              ${problems.map((p,i)=>`<tr class="problem-item ${i===0?"active":""}" data-resource-index="${i}" data-resource-category="${escapeHtml(p.category)}" data-resource-search="${escapeHtml([p.code,p.title,p.competition,p.category,p.year,p.problem_type,...p.keywords,...p.modeling_directions].join(" "))}" data-saved="false" tabindex="0" role="link" aria-label="查看赛题：${escapeHtml(p.code)} ${escapeHtml(p.title)}">
                <td><div class="resource-title-cell"><button class="row-star" data-action="resource-bookmark" aria-label="收藏 ${escapeHtml(p.code)}">${icon("star")}</button><strong>${escapeHtml(p.code)}　${escapeHtml(p.title)}</strong></div></td>
                <td>${escapeHtml(p.category)}</td><td>${p.year}</td><td>${escapeHtml(p.problem_type)}</td><td>${escapeHtml(p.keywords.join("，"))}</td><td>${escapeHtml(p.data_requirement)}</td>
                <td><div class="resource-status-cell"><span>${escapeHtml(p.status)}</span></div></td>
              </tr>`).join("")}
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
    const years = [...new Set(entries.map(({ paper }) => paper.year))].sort((a, b) => b - a);
    const groups = [...new Set(entries.map(({ group }) => group).filter(group => group !== "—"))].sort();
    return shell(`
      <section class="library-main resource-library papers-main">
        <div class="library-heading paper-library-heading"><div><h1>优秀论文</h1><p>按研究主题、年份与题组浏览获奖论文，点击即可阅读完整正文。</p></div><span class="paper-library-total"><strong>${entries.length}</strong> 篇完整论文</span></div>
        <div class="paper-library-controls"><label class="search-box">${icon("magnifying-glass")}<input type="search" name="paper-search" data-paper-search autocomplete="off" aria-label="搜索论文" placeholder="搜索研究主题、论文编号或关键词"></label>
          <label class="paper-year-filter"><span>年份</span><select data-paper-year-filter aria-label="按年份筛选论文"><option value="">全部年份</option>${years.map(year=>`<option value="${year}">${year} 年</option>`).join("")}</select>${icon("caret-down")}</label>
          <button class="paper-filter-reset" type="button" data-paper-filter-reset>${icon("arrow-counter-clockwise")} 重置</button>
        </div>
        <div class="paper-classification-panel" aria-label="论文分类筛选">
          <div class="paper-classification-row"><span class="paper-classification-label">奖项</span><div class="resource-tabs paper-resource-tabs" role="tablist" aria-label="按奖项分类">
            ${paperTabs().map((x,i)=>`<button class="${i===0?"active":""}" data-resource-tab="${escapeHtml(x)}" data-resource-kind="paper">${escapeHtml(x)}</button>`).join("")}
          </div></div>
          <div class="paper-classification-row"><span class="paper-classification-label">题组</span><div class="paper-group-tabs" role="group" aria-label="按题组筛选">
            <button class="active" type="button" data-paper-group-filter="">全部题组</button>${groups.map(group=>`<button type="button" data-paper-group-filter="${escapeHtml(group)}">${escapeHtml(group)} 题</button>`).join("")}
          </div><span class="paper-result-count" data-paper-result-copy>${entries.length} 篇</span></div>
        </div>
        <div class="resource-table-wrap paper-resource-wrap">
          <table class="resource-table paper-resource-table">
            <thead><tr><th>研究主题与论文编号</th><th>题目分类</th><th>奖项</th><th>正文</th><th>收藏</th></tr></thead>
            <tbody data-paper-list>
              ${entries.map(({ paper: p, index: sourceIndex, problem, displayTitle, group })=>`<tr class="paper-item" data-resource-index="${sourceIndex}" data-resource-category="${escapeHtml(p.category)}" data-paper-year="${p.year}" data-paper-group="${escapeHtml(group)}" data-resource-search="${escapeHtml([displayTitle,p.title,p.team_id,p.problem_code,p.competition,p.year,p.award,p.institution,problem?.problem_type,...(problem?.keywords || []),...p.distinctions,...p.models].filter(Boolean).join(" "))}" data-saved="false" tabindex="0" role="link" aria-label="阅读论文：${escapeHtml(displayTitle)}，编号 ${escapeHtml(paperIdentifier(p))}">
                <td><div class="paper-primary-cell"><strong>${escapeHtml(displayTitle)}</strong><span>论文编号 ${escapeHtml(paperIdentifier(p))}　·　${escapeHtml(p.problem_code)}</span></div></td>
                <td><div class="paper-topic-cell"><span class="paper-group-badge">${escapeHtml(group)} 题</span><span>${escapeHtml(problem?.problem_type || "数学建模研究")}</span></div></td>
                <td><span class="paper-award-badge">${escapeHtml(p.award)}</span></td>
                <td><div class="paper-access-cell">${icon("file-pdf")}<span><strong>完整 PDF</strong><small>${escapeHtml(formatFileSize(p.source_file_bytes) || "在线阅读")}</small></span></div></td>
                <td><button class="row-star" data-action="resource-bookmark" aria-label="收藏 ${escapeHtml(displayTitle)}">${icon("star")}</button></td>
              </tr>`).join("")}
              <tr class="paper-empty-row" data-paper-empty hidden><td colspan="5">${icon("magnifying-glass")}<strong>没有符合条件的论文</strong><span>调整年份、题组或搜索词后再试。</span></td></tr>
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
   * KaTeX 体积远大于本页其余代码，因此只在真正出现公式时动态加载，
   * 加载前 data-tex 节点保留 LaTeX 源码作为可读回退。
   */
  let katexLoader = null;
  function renderFormulas(scope = document) {
    const nodes = $$("[data-tex]", scope).filter(node => node.dataset.texDone !== "true");
    if (!nodes.length) return;
    katexLoader = katexLoader || Promise.all([
      import("katex"),
      import("katex/dist/katex.min.css"),
    ]).then(([module]) => module.default ?? module);
    katexLoader
      .then(katex => {
        nodes.forEach(node => {
          try {
            katex.render(node.dataset.tex, node, { throwOnError: false, displayMode: true });
            node.dataset.texDone = "true";
          } catch {
            // 渲染失败时保留 LaTeX 源码文本，不让公式区变空白
          }
        });
      })
      .catch(() => {
        // 离线或加载失败：回退文本已经在 DOM 里，无需额外处理
      });
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
    const collapsed = methodTreeCollapsed();
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
      files: deliverables
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

  function completeScreen() {
    return modelingShell(`
      <section class="focused-workspace final-delivery-workspace">
        ${workspaceTabs([["final-summary","最终成果","check-circle"],["paper-package","论文文件","file-text"],["data-code-package","数据与代码","code"],["delivery-record","交付记录","clipboard-text"]], "final-summary")}
        <div class="focused-workspace-panel active" data-workspace-panel="final-summary">${resultDocument(completeWorkspacePages["final-summary"], "final-summary", "final-delivery-document")}</div>
        <div class="focused-workspace-panel" data-workspace-panel="paper-package">${resultDocument(completeWorkspacePages["paper-package"], "paper-package", "final-delivery-document")}</div>
        <div class="focused-workspace-panel" data-workspace-panel="data-code-package">${resultDocument(completeWorkspacePages["data-code-package"], "data-code-package", "final-delivery-document")}</div>
        <div class="focused-workspace-panel" data-workspace-panel="delivery-record">${resultDocument(completeWorkspacePages["delivery-record"], "delivery-record", "final-delivery-document")}</div>
      </section>`, "complete");
  }

  const renderers = {
    new: newScreen,
    confirm: confirmScreen,
    running: runningScreen,
    projects: projectsScreen,
    data: dataScreen,
    model: modelScreen,
    experiments: experimentsScreen,
    editor: editorScreen,
    problems: problemsScreen,
    papers: papersScreen,
    problemDetail: problemDetailScreen,
    paperDetail: paperDetailScreen,
    methods: methodsScreen,
    complete: completeScreen
  };

  function go(name) {
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

  function openSettingsCenter() {
    $(".settings-backdrop")?.remove();
    const themeBeforeOpen = normalizeTheme(document.documentElement.dataset.theme);
    let settingsSaved = false;
    const backdrop = document.createElement("div");
    backdrop.className = "settings-backdrop";
    backdrop.innerHTML = `
      <div class="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <aside class="settings-sidebar">
          <div class="settings-brand">${projectLogo("settings-logo")}<div><strong>OpenMathModel</strong><span>设置中心</span></div></div>
          <nav class="settings-nav" aria-label="设置分类">
            <button class="active" data-settings-nav="general" data-title="通用设置" data-subtitle="语言、地区和基础任务行为">${icon("sliders-horizontal")}<span>通用</span></button>
            <button data-settings-nav="appearance" data-title="外观与显示" data-subtitle="主题、字号和界面密度">${icon("palette")}<span>外观与显示</span></button>
            <button data-settings-nav="personalization" data-title="个性化" data-subtitle="定制 Agent 的回答习惯与工作方式">${icon("user-focus")}<span>个性化</span></button>
            <button data-settings-nav="usage" data-title="用量监控" data-subtitle="查看 Token、请求量和费用预算">${icon("chart-bar")}<span>用量监控</span></button>
            <button data-settings-nav="security" data-title="账户与安全" data-subtitle="密码、双重验证和登录设备">${icon("shield-check")}<span>账户与安全</span></button>
            <button data-settings-nav="providers" data-title="模型厂商" data-subtitle="管理官方模型服务与默认路由">${icon("circles-four")}<span>模型厂商</span></button>
            <button data-settings-nav="api" data-title="自定义 API" data-subtitle="连接模型厂商或 OpenAI 兼容中转站">${icon("plugs-connected")}<span>自定义 API</span></button>
            <button data-settings-nav="privacy" data-title="数据与隐私" data-subtitle="管理历史记录、数据保留与通知">${icon("lock-key")}<span>数据与隐私</span></button>
            <button data-settings-nav="advanced" data-title="高级设置" data-subtitle="代理、并发、超时与开发选项">${icon("terminal-window")}<span>高级设置</span></button>
          </nav>
          <div class="settings-account-card">
            <span class="avatar">I</span>
            <div><strong>Ivan</strong><span>个人专业版</span></div>
            <span class="settings-plan">PRO</span>
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

            <div class="settings-pane" data-settings-pane="appearance">
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>界面主题</h3><p>选择适合当前工作环境的外观。</p></div></div>
                <div class="theme-options" role="radiogroup" aria-label="界面主题">
                  <button class="theme-option active" type="button" data-theme-choice="light" aria-pressed="true"><span class="theme-preview light"><i></i><b></b></span><strong>浅色</strong><small>清晰明亮</small></button>
                  <button class="theme-option" type="button" data-theme-choice="dark" aria-pressed="false"><span class="theme-preview dark"><i></i><b></b></span><strong>深色</strong><small>减少眩光</small></button>
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>显示密度</h3><p>调整列表、侧栏和正文的视觉密度。</p></div></div>
                <div class="settings-grid two">
                  <label class="settings-field"><span>界面密度</span><select name="interfaceDensity"><option>舒适</option><option>紧凑</option><option>宽松</option></select></label>
                  <label class="settings-field"><span>代码字体</span><select name="codeFont"><option>JetBrains Mono</option><option>Consolas</option><option>系统等宽字体</option></select></label>
                </div>
                <label class="settings-range"><span><b>正文字号</b><output data-font-output>15 px</output></span><input type="range" min="13" max="19" value="15" name="fontSize" data-font-size></label>
                ${settingsToggle("reduceMotion", "减少动态效果", "减少弹窗、页面切换与进度反馈动画", false)}
                ${settingsToggle("highContrast", "增强文字对比度", "使用更深的正文与边界颜色", false)}
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="personalization">
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>默认工作方式</h3><p>让 Agent 更贴合你的表达和交付偏好。</p></div></div>
                <div class="settings-grid two">
                  <label class="settings-field"><span>回答风格</span><select name="responseStyle"><option>专业、简洁</option><option>详细解释</option><option>学术写作</option><option>启发式引导</option></select></label>
                  <label class="settings-field"><span>默认任务模式</span><select name="defaultMode"><option>自动模式</option><option>深度研究</option><option>快速分析</option></select></label>
                  <label class="settings-field"><span>默认模型</span><select name="defaultModel"><option>自动选择</option><option>Qwen3.7-Max</option><option>DeepSeek V3</option><option>GPT-4.1</option></select></label>
                  <label class="settings-field"><span>引用格式</span><select name="citationStyle"><option>GB/T 7714</option><option>APA 7th</option><option>IEEE</option><option>MLA</option></select></label>
                </div>
                <label class="settings-field settings-textarea"><span>自定义指令</span><textarea name="customInstructions" placeholder="例如：回答前先给结论；数学公式使用 LaTeX；论文段落保持学术语气。">优先使用中文回答；建模任务中先说明假设，再给出公式和可复现步骤。</textarea><small>这些指令会应用到所有新任务，你仍可在单个任务中覆盖。</small></label>
              </div>
              <div class="settings-section">
                ${settingsToggle("deepReasoning", "复杂任务自动开启深度思考", "检测到研究、编程或数学建模任务时提升推理强度", true)}
                ${settingsToggle("sendWithEnter", "按 Enter 发送消息", "关闭后使用 Ctrl + Enter 发送，Enter 仅换行", true)}
                ${settingsToggle("rememberPreferences", "记住长期偏好", "允许 Agent 记住稳定的格式、术语和工作习惯", true)}
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="usage">
              <div class="usage-overview">
                <div class="usage-stat"><span>本月 Token</span><strong>2.84M</strong><small>较上月 +12.4%</small></div>
                <div class="usage-stat"><span>Agent 任务</span><strong>186</strong><small>其中 42 个深度任务</small></div>
                <div class="usage-stat"><span>预估费用</span><strong>¥ 96.40</strong><small>预算剩余 ¥ 103.60</small></div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading usage-heading"><div><h3>本月用量</h3><p>2026年7月1日－7月31日</p></div><button type="button" class="secondary-small" data-settings-action="export-usage">${icon("download-simple")} 导出明细</button></div>
                <div class="usage-budget"><div><span>¥ 96.40 / ¥ 200.00</span><b>48%</b></div><progress value="96.4" max="200"></progress></div>
                <div class="usage-chart" aria-label="最近 14 天用量柱状图">
                  ${[34,48,42,64,52,79,68,91,75,83,58,96,70,87].map((x,i)=>`<span style="--usage:${x}%" title="7月${i+14}日 · ${x}k Token"></span>`).join("")}
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>模型用量分布</h3><p>费用为本月预估值。</p></div></div>
                <div class="usage-table">
                  <div class="usage-table-head"><span>模型</span><span>请求</span><span>Token</span><span>费用</span></div>
                  <div><strong>Qwen3.7-Max</strong><span>96</span><span>1.42M</span><span>¥ 31.80</span></div>
                  <div><strong>DeepSeek V3</strong><span>54</span><span>860K</span><span>¥ 18.60</span></div>
                  <div><strong>GPT-4.1</strong><span>24</span><span>410K</span><span>¥ 40.20</span></div>
                  <div><strong>本地模型</strong><span>12</span><span>150K</span><span>¥ 0.00</span></div>
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-grid two">
                  <label class="settings-field"><span>月度预算提醒</span><div class="field-with-unit"><input type="number" name="monthlyBudget" value="200"><b>元 / 月</b></div></label>
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
                <div class="settings-section-heading"><div><h3>已接入模型厂商</h3><p>启用厂商并指定任务的默认模型。</p></div><button type="button" class="primary-small" data-settings-jump="api">${icon("plus")} 添加厂商</button></div>
                <div class="provider-list">
                  <div class="provider-card connected"><div class="provider-logo">${providerLogo("qwen", "通义千问")}</div><div><strong>通义千问</strong><span>Qwen3.7-Max · 官方服务</span></div><span class="provider-status">${icon("check-circle")} 已连接</span><button type="button" data-settings-action="configure-provider" data-provider="通义千问">管理</button></div>
                  <div class="provider-card connected"><div class="provider-logo">${providerLogo("deepseek", "DeepSeek")}</div><div><strong>DeepSeek</strong><span>DeepSeek V3 / R1</span></div><span class="provider-status">${icon("check-circle")} 已连接</span><button type="button" data-settings-action="configure-provider" data-provider="DeepSeek">管理</button></div>
                  <div class="provider-card"><div class="provider-logo">${providerLogo("openai", "OpenAI")}</div><div><strong>OpenAI</strong><span>GPT-4.1 / o3</span></div><span class="provider-status idle">未配置</span><button type="button" data-settings-action="configure-provider" data-provider="OpenAI">配置</button></div>
                  <div class="provider-card"><div class="provider-logo">${providerLogo("anthropic", "Anthropic")}</div><div><strong>Anthropic</strong><span>Claude Sonnet / Opus</span></div><span class="provider-status idle">未配置</span><button type="button" data-settings-action="configure-provider" data-provider="Anthropic">配置</button></div>
                  <div class="provider-card"><div class="provider-logo">${providerLogo("ollama", "Ollama")}</div><div><strong>本地模型</strong><span>Ollama / LM Studio</span></div><span class="provider-status idle">未发现服务</span><button type="button" data-settings-action="configure-provider" data-provider="本地模型">扫描</button></div>
                </div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>智能路由</h3><p>根据任务类型、速度与费用自动选择模型。</p></div></div>
                ${settingsToggle("smartRouting", "启用模型智能路由", "优先满足质量要求，并在同等能力下选择成本更低的模型", true)}
                <div class="settings-grid two">
                  <label class="settings-field"><span>编程与 Agent</span><select name="codingModel"><option>自动选择</option><option>GPT-4.1</option><option>DeepSeek V3</option></select></label>
                  <label class="settings-field"><span>深度研究</span><select name="researchModel"><option>Qwen3.7-Max</option><option>DeepSeek R1</option><option>Claude Opus</option></select></label>
                  <label class="settings-field"><span>长文写作</span><select name="writingModel"><option>自动选择</option><option>Claude Sonnet</option><option>Qwen3.7-Max</option></select></label>
                  <label class="settings-field"><span>视觉理解</span><select name="visionModel"><option>自动选择</option><option>GPT-4.1</option><option>Qwen VL Max</option></select></label>
                </div>
              </div>
            </div>

            <div class="settings-pane" data-settings-pane="api">
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>自定义模型接口</h3><p>支持厂商官方 API、OpenAI 兼容接口和第三方中转站。</p></div><span class="api-security-note">${icon("shield-check")} 密钥仅加密保存在本机</span></div>
                <div class="settings-grid two">
                  <label class="settings-field"><span>配置名称</span><input name="apiProfileName" value="OpenAI 兼容中转站" placeholder="例如：团队模型网关"></label>
                  <label class="settings-field"><span>接口协议</span><select name="apiProtocol"><option>OpenAI Compatible</option><option>Anthropic Messages API</option><option>Google Gemini API</option><option>Ollama</option><option>自定义 REST</option></select></label>
                  <label class="settings-field settings-span-two"><span>Base URL</span><input name="apiBaseUrl" value="https://api.example.com/v1" placeholder="https://api.example.com/v1"></label>
                  <label class="settings-field settings-span-two"><span>API Key</span><div class="secret-field"><input type="password" name="apiKey" value="sk-openmathmodel-demo-key"><button type="button" data-settings-action="toggle-secret" aria-label="显示或隐藏 API Key">${icon("eye")}</button></div></label>
                  <label class="settings-field"><span>默认模型 ID</span><input name="apiModel" value="gpt-4.1" placeholder="gpt-4.1"></label>
                  <label class="settings-field"><span>组织 / 项目标识</span><input name="apiOrganization" placeholder="可选"></label>
                </div>
                <details class="api-advanced"><summary>请求头与高级参数 ${icon("caret-down")}</summary><div class="settings-grid two"><label class="settings-field"><span>自定义请求头</span><input name="customHeader" placeholder="X-API-Source: OpenMathModel"></label><label class="settings-field"><span>路径前缀</span><input name="apiPathPrefix" placeholder="/chat/completions"></label></div></details>
                <div class="api-actions"><button type="button" data-settings-action="test-api">${icon("pulse")} 测试连接</button><button type="button" class="primary-small" data-settings-action="add-endpoint">${icon("plus")} 保存为新接口</button></div>
              </div>
              <div class="settings-section">
                ${settingsToggle("allowProxyApi", "允许使用第三方中转站", "发送请求前显示实际域名，并记录接口用量", true)}
                ${settingsToggle("streamResponse", "流式输出", "支持时逐步显示模型回复，降低首字等待时间", true)}
                ${settingsToggle("fallbackApi", "失败时自动切换备用接口", "仅在主接口超时或达到限流后触发", true)}
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>已保存接口</h3><p>可为不同任务绑定不同接口。</p></div></div>
                <div class="endpoint-list" data-endpoint-list>
                  <div class="endpoint-item"><span class="endpoint-dot online"></span><div><strong>团队模型网关</strong><span>https://gateway.example.com/v1 · 6 个模型</span></div><span>主接口</span><button type="button" data-settings-action="endpoint-menu">${icon("dots-three")}</button></div>
                  <div class="endpoint-item"><span class="endpoint-dot"></span><div><strong>本地 Ollama</strong><span>http://127.0.0.1:11434/v1 · 3 个模型</span></div><span>本地</span><button type="button" data-settings-action="endpoint-menu">${icon("dots-three")}</button></div>
                </div>
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
                  <label class="settings-field"><span>网络代理</span><select name="proxyMode"><option>跟随系统</option><option>不使用代理</option><option>手动配置</option></select></label>
                  <label class="settings-field"><span>代理地址</span><input name="proxyUrl" placeholder="http://127.0.0.1:7890"></label>
                  <label class="settings-field"><span>请求超时</span><div class="field-with-unit"><input type="number" name="requestTimeout" value="120"><b>秒</b></div></label>
                  <label class="settings-field"><span>最大并发任务</span><select name="maxConcurrency"><option>3 个</option><option>1 个</option><option>5 个</option><option>8 个</option></select></label>
                  <label class="settings-field"><span>下载目录</span><input name="downloadDirectory" value="E:\\OpenMathModel\\Downloads"></label>
                  <label class="settings-field"><span>临时文件目录</span><input name="tempDirectory" value="自动管理"></label>
                </div>
              </div>
              <div class="settings-section">
                ${settingsToggle("retryRequest", "自动重试失败请求", "针对网络错误和限流最多重试 3 次", true)}
                ${settingsToggle("parallelTools", "允许并行调用工具", "Agent 可同时运行互不依赖的搜索、分析和文件任务", true)}
                ${settingsToggle("confirmExternal", "外部操作前请求确认", "发送邮件、发布内容或变更远程数据前暂停确认", true)}
                ${settingsToggle("developerMode", "开发者模式", "显示请求 ID、Token 明细、工具调用和调试日志", false)}
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>诊断</h3><p>用于排查模型接口和本地运行问题。</p></div></div>
                <div class="diagnostic-actions"><button type="button" data-settings-action="network-diagnosis">${icon("pulse")} 运行网络诊断</button><button type="button" data-settings-action="open-logs">${icon("file-text")} 打开日志目录</button><button type="button" data-settings-action="copy-system-info">${icon("copy")} 复制系统信息</button></div>
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
      if (!settingsSaved) applyTheme(themeBeforeOpen);
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
    const saveSettings = () => {
      const values = {};
      $$("[name]", backdrop).forEach(control => {
        values[control.name] = control.matches("[data-setting-toggle]") ? control.getAttribute("aria-checked") === "true" : control.value;
      });
      values.theme = $(".theme-option.active", backdrop)?.dataset.themeChoice || "light";
      localStorage.setItem("openmathmodelSettings", JSON.stringify(values));
      $$('[data-model-choice^="custom-"]', document).forEach(option => {
        const picker = option.closest("[data-model-picker]");
        const wasSelected = option.getAttribute("aria-selected") === "true";
        option.dataset.modelChoice = `custom-${values.apiModel || "gpt-4.1"}`;
        $(".model-choice-copy strong", option).textContent = values.apiModel || "gpt-4.1";
        $(".model-choice-copy small", option).textContent = `${values.apiProfileName || "OpenAI 兼容中转站"} · 自定义 API`;
        if (wasSelected && picker) {
          $("[data-model-picker-label]", picker).textContent = values.apiModel || "gpt-4.1";
          localStorage.setItem("openmathmodelSelectedModel", option.dataset.modelChoice);
        }
      });
      settingsSaved = true;
      applyTheme(values.theme);
      $("[data-settings-save-state]", backdrop).textContent = "已保存";
      toast("设置已保存");
      setTimeout(closeSettings, 280);
    };
    const restoreSettings = () => {
      try {
        const saved = JSON.parse(localStorage.getItem("openmathmodelSettings") || "{}");
        Object.entries(saved).forEach(([name, value]) => {
          if (name === "theme") {
            const theme = normalizeTheme(value);
            $$("[data-theme-choice]", backdrop).forEach(option => {
              const active = option.dataset.themeChoice === theme;
              option.classList.toggle("active", active);
              option.setAttribute("aria-pressed", String(active));
            });
            return;
          }
          const control = $(`[name="${name}"]`, backdrop);
          if (!control) return;
          if (control.matches("[data-setting-toggle]")) {
            control.classList.toggle("active", Boolean(value));
            control.setAttribute("aria-checked", String(Boolean(value)));
          } else {
            control.value = value;
          }
        });
        applyTheme(normalizeTheme(saved.theme));
      } catch (error) {
        localStorage.removeItem("openmathmodelSettings");
        applyTheme("light");
      }
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
        return;
      }
      const theme = event.target.closest("[data-theme-choice]");
      if (theme) {
        $$("[data-theme-choice]", backdrop).forEach(option => {
          const active = option === theme;
          option.classList.toggle("active", active);
          option.setAttribute("aria-pressed", String(active));
        });
        applyTheme(theme.dataset.themeChoice);
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
        actionButton.disabled = true;
        actionButton.innerHTML = `${icon("spinner-gap")} 正在连接…`;
        setTimeout(() => {
          actionButton.disabled = false;
          actionButton.classList.add("connection-ok");
          actionButton.innerHTML = `${icon("check-circle")} 连接成功 · 428ms`;
          toast("API 连接成功，已发现 12 个模型");
        }, 780);
      }
      if (action === "add-endpoint") {
        const name = $('[name="apiProfileName"]', backdrop).value.trim() || "未命名接口";
        const url = $('[name="apiBaseUrl"]', backdrop).value.trim() || "尚未填写地址";
        $("[data-endpoint-list]", backdrop).insertAdjacentHTML("afterbegin", `<div class="endpoint-item"><span class="endpoint-dot online"></span><div><strong>${escapeHtml(name)}</strong><span>${escapeHtml(url)} · 待同步模型</span></div><span>新接口</span><button type="button" data-settings-action="endpoint-menu">${icon("dots-three")}</button></div>`);
        toast("自定义接口已添加");
      }
      if (action === "configure-provider") {
        activatePane($('[data-settings-nav="api"]', backdrop));
        $('[name="apiProfileName"]', backdrop).value = `${actionButton.dataset.provider} API`;
        $('[name="apiBaseUrl"]', backdrop).focus();
      }
      if (action === "reset-defaults") {
        localStorage.removeItem("openmathmodelSettings");
        settingsSaved = true;
        applyTheme("light");
        closeSettings();
        openSettingsCenter();
        toast("已恢复默认设置");
      }
      if (action === "export-usage") toast("用量明细 CSV 已生成");
      if (action === "export-data") toast("数据导出申请已提交，完成后会通知你");
      if (action === "clear-cache") toast("本地缓存已清理，共释放 386 MB");
      if (action === "delete-account") toast("演示环境不会执行账户删除");
      if (action === "endpoint-menu") popupMenu(actionButton, ["设为主接口", "编辑", "复制配置", "删除"]);
      if (action === "network-diagnosis") {
        actionButton.disabled = true;
        actionButton.textContent = "诊断中…";
        setTimeout(() => { actionButton.disabled = false; actionButton.innerHTML = `${icon("check-circle")} 网络正常`; }, 680);
      }
      if (action === "open-logs") toast("日志目录已打开");
      if (action === "copy-system-info") {
        navigator.clipboard?.writeText("OpenMathModel v1.0 · Windows · Chromium");
        toast("系统信息已复制");
      }
    });
    $("[data-font-size]", backdrop).addEventListener("input", event => {
      $("[data-font-output]", backdrop).textContent = `${event.target.value} px`;
    });
    document.addEventListener("keydown", onSettingsKeydown);
    restoreSettings();
    enhanceSettingsSelects();
    $(".settings-close", backdrop).focus();
  }

  function popupMenu(anchor, items) {
    $(".menu")?.remove();
    const menu = document.createElement("div");
    menu.className = "menu";
    menu.innerHTML = items.map(i => `<button data-menu-value="${i}">${i}</button>`).join("");
    document.body.appendChild(menu);
    const rect = anchor.getBoundingClientRect();
    menu.style.left = `${Math.min(rect.left, window.innerWidth - 190)}px`;
    menu.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - items.length * 38 - 16)}px`;
    menu.addEventListener("click", e => {
      const button = e.target.closest("button");
      if (!button) return;
      anchor.dataset.value = button.dataset.menuValue;
      toast(`已选择：${button.dataset.menuValue}`);
      menu.remove();
    });
    setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }), 0);
  }

  /**
   * Chart.js 只有数据页和实验页用得到，动态载入避免让其他页面为它买单。
   * 原实现依赖 CDN 注入的 window.Chart，加载失败会静默返回、图表区直接空白；
   * 现在改为本地依赖并显式报错。
   */
  let chartLoader = null;
  function initCharts(screen) {
    if (screen !== "data" && screen !== "experiments") return;
    chartLoader = chartLoader || import("chart.js/auto").then(module => module.default);
    chartLoader
      .then(Chart => renderCharts(Chart, screen))
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

  function appendConversationTurn(text) {
    const scroll = $(".chat-scroll");
    if (!scroll) return;
    const replyId = `reply-${Date.now()}`;
    scroll.insertAdjacentHTML("beforeend", `
      <div class="user-message"><div class="user-bubble">${escapeHtml(text)}</div></div>
      <div class="assistant-block follow-up-reply" id="${replyId}">
        <div class="assistant-id">${projectLogo("assistant-logo")}<span>Agent</span></div>
        <div class="analysis-copy"><p class="muted">正在分析你的补充要求…</p></div>
      </div>`);
    scroll.scrollTo({ top: scroll.scrollHeight, behavior: "smooth" });
    setTimeout(() => {
      const reply = document.getElementById(replyId);
      if (!reply) return;
      reply.querySelector(".analysis-copy").innerHTML = "<p>已收到。我会把这项要求合并到当前建模计划中，并在后续的数据处理、实验评估和论文交付阶段持续遵循。</p>";
      scroll.scrollTo({ top: scroll.scrollHeight, behavior: "smooth" });
    }, 650);
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

  function bindCommon(screen) {
    document.addEventListener("click", event => {
      if (!event.target.closest("[data-model-picker]")) {
        $$("[data-model-picker].open").forEach(picker => {
          picker.classList.remove("open");
          $("[data-action=\"model-picker\"]", picker)?.setAttribute("aria-expanded", "false");
        });
      }
      const codeTab = event.target.closest("[data-code-lang]");
      if (codeTab) { switchMethodLanguage(codeTab.dataset.codeLang); return; }
      const goButton = event.target.closest("[data-go]");
      if (goButton) { go(goButton.dataset.go); return; }
      const action = event.target.closest("[data-action]")?.dataset.action;
      if (!action) return;
      const target = event.target.closest("[data-action]");
      if (action === "new-task") go("new");
      if (action === "toggle-sidebar") {
        const sidebarShell = target.closest("[data-sidebar-shell]");
        if (!sidebarShell) return;
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
      if (action === "sidebar-filter") popupMenu(target, ["全部任务", "进行中", "已完成", "我创建的"]);
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
      if (action === "export-paper") popupMenu(target, ["导出 Word", "导出 PDF", "导出 LaTeX"]);
      if (action === "source-detail") modal("引用来源", "<p>来源：Run #04 · 结果表 2。该结果已通过完整性和一致性校验。</p>");
      if (action === "fake-close") toast("这是演示界面，窗口保持打开");
      if (action === "attach") target.closest(".composer")?.querySelector(".file-input")?.click();
      if (action === "reference") popupMenu(target, ["赛题库", "优秀论文", "方法库"]);
      if (action === "mode") popupMenu(target, ["自动模式", "深度研究", "快速分析"]);
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
        const textarea = target.closest(".composer")?.querySelector("textarea");
        const text = textarea?.value.trim();
        if (!text) { toast("请输入你的问题"); return; }
        if (screen === "new") {
          sessionStorage.setItem("openmathmodelPrompt", text);
          go("running");
        } else {
          appendConversationTurn(text);
          textarea.value = "";
        }
      }
      if (action === "files") modal("附件", '<div class="attachment-chip">2026国赛A题题目.pdf</div><div class="attachment-chip">共享单车数据集.csv</div><div class="attachment-chip">城市区域划分示意图.png</div>');
      if (action === "more" || action === "row-menu") popupMenu(target, ["重命名", "复制", "归档"]);
      if (action === "toggle-activity") {
        const activityHost = target.closest(".focused-agent-chat, .assistant-block");
        const list = activityHost?.querySelector(".focused-activity-list, .activity-list") || $(".focused-activity-list") || $(".activity-list");
        list?.classList.toggle("collapsed");
        const collapsed = list?.classList.contains("collapsed") ?? false;
        target.setAttribute("aria-expanded", String(!collapsed));
        target.innerHTML = collapsed
          ? `${icon("eye")} 查看 4 个执行步骤 ${icon("caret-down")}`
          : `${icon("eye-slash")} 收起执行步骤 ${icon("caret-up")}`;
      }
      if (action === "edit-assumption") modal("编辑假设", '<textarea>数据完整且质量可用\n共享单车可跨区域调度\n调度以最小化总缺车惩罚为目标</textarea>', () => toast("假设已更新"));
      if (action === "edit-output") modal("编辑输出要求", '<textarea>给出完整建模流程与假设说明\n提供关键模型公式与变量定义\n输出可复现实验结果与对比分析</textarea>', () => toast("输出要求已更新"));
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
      if (action === "editor-check") toast("AI 检查完成：章节结构、公式和引用均未发现严重问题");
      if (action === "experiment-filter" || action === "filter") popupMenu(target, ["全部", "已完成", "进行中", "失败"]);
      if (action === "page-size") popupMenu(target, ["15 条/页", "20 条/页", "50 条/页"]);
      if (action === "rerun") {
        target.disabled = true; target.textContent = "运行中 0%";
        let value = 0; const timer = setInterval(() => { value += 20; target.textContent = `运行中 ${value}%`; if (value >= 100) { clearInterval(timer); target.disabled = false; target.innerHTML = `${icon("arrow-clockwise")} 重新运行`; toast("实验重新运行完成"); } }, 240);
      }
      if (action === "compare") { target.classList.toggle("primary"); target.innerHTML = target.classList.contains("primary") ? `${icon("check")} 已加入对比` : `${icon("plus")} 加入对比`; }
      if (action === "formula") { document.execCommand("insertText", false, "  ∑ᵢ xᵢ = b  "); toast("已插入公式"); }
      if (action === "image") toast("图片插入面板已打开");
      if (action === "cite") toast("已打开引用资料列表");
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

    $$("[data-command]").forEach(button => button.addEventListener("click", () => document.execCommand(button.dataset.command, false)));
    $$(".file-input").forEach(input => input.addEventListener("change", () => toast(`已添加 ${input.files.length} 个文件`)));
    $$(".composer textarea").forEach(textarea => textarea.addEventListener("keydown", event => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      textarea.closest(".composer")?.querySelector('[data-action="send"]')?.click();
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
        $(".menu")?.remove();
        $(".modal-backdrop")?.remove();
        $$("[data-model-picker].open").forEach(picker => {
          picker.classList.remove("open");
          $("[data-action=\"model-picker\"]", picker)?.setAttribute("aria-expanded", "false");
        });
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
      const paperYearFilter = kind === "paper" ? $("[data-paper-year-filter]") : null;
      const paperGroupFilters = kind === "paper" ? $$('[data-paper-group-filter]') : [];
      const paperReset = kind === "paper" ? $("[data-paper-filter-reset]") : null;
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
        const selectedYear = paperYearFilter?.value || "";
        const selectedGroup = paperGroupFilters.find(button => button.classList.contains("active"))?.dataset.paperGroupFilter || "";
        const matches = [];
        rows.forEach(row => {
          const matchesSearch = !query || row.dataset.resourceSearch.toLowerCase().includes(query);
          const matchesCategory = selected === "全部赛题" || selected === "全部" || selected === "按赛题" || selected === "按模型"
            || (selected === "收藏" ? row.dataset.saved === "true" : row.dataset.resourceCategory === selected);
          const matchesYear = kind !== "paper" || !selectedYear || row.dataset.paperYear === selectedYear;
          const matchesGroup = kind !== "paper" || !selectedGroup || row.dataset.paperGroup === selectedGroup;
          if (matchesSearch && matchesCategory && matchesYear && matchesGroup) matches.push(row);
          row.hidden = true;
        });
        const pageCount = Math.max(1, Math.ceil(matches.length / pageSize));
        currentPage = Math.min(Math.max(1, currentPage), pageCount);
        matches.slice((currentPage - 1) * pageSize, currentPage * pageSize).forEach(row => { row.hidden = false; });
        const copy = $("[data-resource-page-copy]");
        if (copy) copy.textContent = `共 ${matches.length} ${kind === "problem" ? "题" : "篇"} · 第 ${currentPage}/${pageCount} 页`;
        const resultCopy = kind === "paper" ? $("[data-paper-result-copy]") : null;
        if (resultCopy) resultCopy.textContent = `${matches.length} 篇`;
        const emptyRow = kind === "paper" ? $("[data-paper-empty]") : null;
        if (emptyRow) emptyRow.hidden = matches.length > 0;
        renderPagination(pageCount);
      };

      $(searchSelector)?.addEventListener("input", () => { currentPage = 1; applyResourceFilters(); });
      tabs.forEach(tab => tab.addEventListener("click", () => {
        tabs.forEach(item => item.classList.remove("active"));
        tab.classList.add("active");
        currentPage = 1;
        applyResourceFilters();
      }));
      paperYearFilter?.addEventListener("change", () => { currentPage = 1; applyResourceFilters(); });
      paperGroupFilters.forEach(button => button.addEventListener("click", () => {
        paperGroupFilters.forEach(item => item.classList.toggle("active", item === button));
        currentPage = 1;
        applyResourceFilters();
      }));
      paperReset?.addEventListener("click", () => {
        const search = $(searchSelector);
        if (search) search.value = "";
        if (paperYearFilter) paperYearFilter.value = "";
        tabs.forEach((tab, index) => tab.classList.toggle("active", index === 0));
        paperGroupFilters.forEach((button, index) => button.classList.toggle("active", index === 0));
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
    }
    if (screen === "data") {
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
    if (screen === "model") {
      $$("[data-plan-option]").forEach(button => button.addEventListener("click", () => {
        $$("[data-plan-option]").forEach(item => item.classList.remove("selected"));
        button.classList.add("selected");
        $$("[data-plan-option] > i").forEach(item => item.className = "ph ph-caret-down");
        const caret = $("i", button);
        if (caret) caret.className = "ph ph-caret-up";
      }));
    }
    if (screen === "experiments") {
      $$(".experiment-item").forEach(item => item.addEventListener("click", () => {
        $$(".experiment-item").forEach(i => i.classList.remove("active")); item.classList.add("active");
        $(".experiment-titlebar h2").textContent = experiments[+item.dataset.experiment][0];
      }));
      $$("[data-experiment-tab]").forEach(button => button.addEventListener("click", () => {
        $$("[data-experiment-tab]").forEach(b => b.classList.remove("active")); button.classList.add("active");
        toast(`已切换到${button.dataset.experimentTab}`);
      }));
    }
    if (screen === "editor") {
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
}

