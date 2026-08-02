// @ts-nocheck
import type { ScreenId } from "../types/screens";

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
      { id: "auto", label: "Agent", detail: "智能路由 · 自动选择", provider: "agent" },
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
    return `
      <aside class="sidebar" aria-label="主导航">
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
        </div>
      </aside>`;
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

  function modelingShell(content, active, auxiliary = "") {
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
          <div class="project-search-row"><label class="search-box">${icon("magnifying-glass")}<input data-table-search placeholder="搜索项目名称……"></label></div>
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
      <section class="workflow-screen data-workflow-screen">
        <header class="workflow-screen-header">
          <div><h1>数据准备</h1><p class="workflow-kicker">${icon("check-circle")} 第 2 轮 · 输入检查完成　　输入 v2</p></div>
          <div class="workflow-header-actions"><button class="plain-link" data-action="open-details">查看详情</button><button class="primary" data-go="model">确认并继续</button></div>
        </header>
        <button class="understanding-strip" type="button" data-action="understanding-details"><strong>题目理解</strong><span>已识别 3 个子问题　·　当前输入类型：表格 / 参数 / 文件　·　存在 1 项待确认</span>${icon("caret-down")}</button>
        <div class="data-body-grid">
          <section class="data-input-card">
            <div class="data-card-tabs" role="tablist">${["表格","参数","文件","图片","公式"].map((tab,index)=>`<button class="${index===0?"active":""}" data-data-tab="${tab}">${tab}</button>`).join("")}</div>
            <div class="data-card-content">
              <h3>需求表（预览）</h3>
              <div class="preview-table-wrap"><table class="preview-table"><thead><tr><th>时间</th><th>区域</th><th>投放点数</th><th>可用车辆数</th><th>平均等待时间(分钟)</th></tr></thead><tbody>
                <tr><td>2025-01-01 08:00</td><td>A区</td><td>32</td><td>48</td><td>6.2</td></tr><tr><td>2025-01-01 08:15</td><td>A区</td><td>32</td><td>47</td><td>6.8</td></tr><tr><td>2025-01-01 08:30</td><td>A区</td><td>33</td><td>49</td><td>5.9</td></tr><tr><td>2025-01-01 08:45</td><td>B区</td><td>28</td><td>41</td><td>7.1</td></tr>
              </tbody></table><div class="table-count">共 200 行</div></div>
              <div class="data-lower-grid">
                <article class="data-mini-card"><h3>参数（预览）</h3><p><b>车辆最大载客量（人）</b> = 2</p><p><b>车辆运营成本（元/公里）</b> = 1.8</p><p><b>用户价值系数（元/分钟）</b> = 0.9</p><footer>共 12 个参数</footer></article>
                <article class="data-mini-card upload-preview"><h3>上传文件（1）</h3><div>${icon("file-xls")}<span><b>历史供需数据_2024Q4.xlsx</b><small>1.24 MB</small></span><button data-action="download-data" aria-label="下载文件">${icon("download-simple")}</button></div><footer>共 1 个文件</footer></article>
              </div>
            </div>
          </section>
          <aside class="data-recommendations"><h2>处理建议</h2>${[
            ["clock","统一时间粒度","将时间粒度统一为 15 分钟，便于对齐分析。"],
            ["table","补全缺失字段","对缺失的可用车辆数进行前向填充处理。"],
            ["tag","校正单位标注","统一平均等待时间单位为分钟。"],
            ["waveform","检查异常值","检测并标记异常等待时间记录。"]
          ].map(([ico,title,copy])=>`<div class="recommendation-row">${icon(ico)}<div><h3>${title}</h3><p>${copy}</p></div><button class="recommend-toggle is-on" data-action="suggestion-toggle" aria-pressed="true"><span></span></button></div>`).join("")}</aside>
        </div>
        <div class="workflow-status-strip">${icon("check-circle")}<span>已检查 6 项输入，发现 2 项问题，处理后可以进入模型设计。</span></div>
      </section>`, "data", taskDetailsDrawer());
  }

  function modelScreen() {
    return modelingShell(`
      <section class="workflow-screen model-workflow-screen">
        <header class="workflow-screen-header"><div><h1>模型方案</h1><p class="workflow-kicker">${icon("check-circle")} 第 2 轮 · 候选路线比较完成　　　方案 v2</p></div><div class="workflow-header-actions"><button class="plain-link" data-action="model-details">查看详情</button><button class="primary" data-go="experiments">确认方案</button></div></header>
        <div class="plan-options">
          ${[
            ["方案 A","先需求预测，再进行混合整数优化调度","需求预测与调度优化一体化","精度高，能较好平衡效率与效果","计算规模较大，对参数敏感"],
            ["方案 B","基于 K-means 分区后分别调度","大规模区域分区调度","计算高效，易于扩展","分区边界效应可能影响结果"],
            ["方案 C","分层聚类 + 线性规划求解","多层级资源配置问题","结构清晰，便于解释","线性假设较强，精度受限"]
          ].map((plan,index)=>`<button class="plan-option-card ${index===0?"selected":""}" data-plan-option="${index}" type="button"><span class="plan-check">${icon("check")}</span><h2>${plan[0]}</h2><p><b>核心思路</b>${plan[1]}</p><p><b>适合解决</b>${plan[2]}</p><p><b>主要优势</b>${plan[3]}</p><p><b>关键风险</b>${plan[4]}</p></button>`).join("")}
        </div>
        <section class="selected-plan-overview"><h3>所选方案 A 概览</h3><div class="overview-grid">
          <article>${icon("lightbulb")}<h3>建模思路</h3><p>先预测分时段需求，再构建混合整数规划模型进行车辆调度优化。</p></article>
          <article>${icon("notepad")}<h3>主要输入</h3><p>历史骑行数据、站点与区域信息、车辆状态、时间分段设置等。</p></article>
          <article>${icon("chart-line-up")}<h3>预期输出</h3><p>各时段各区域的调度方案、车辆调拨量与成本指标等。</p></article>
          <article>${icon("shield-check")}<h3>验证方式</h3><p>与历史数据回测对比，评估成本与服务水平改进幅度。</p></article>
        </div></section>
        <div class="workflow-status-strip model-advice-strip">${icon("seal-question")}<span>建议采用方案 A 作为主方案，方案 B 作为基线进行验证。</span></div>
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

  function experimentsScreen() {
    return modelingShell(`
      <section class="workflow-screen experiment-result-screen">
        <header class="workflow-screen-header"><div><h1>实验结果</h1><p class="workflow-kicker">${icon("check-circle")} Run #04　·　第 3 轮 · 验证完成</p></div><div class="workflow-header-actions"><button class="plain-link" data-action="experiment-details">查看详情</button><button class="primary" data-go="editor">采用该结果</button></div></header>
        <div class="result-metrics">
          <article><h3>优化目标（越小越好）</h3><span>总调度成本</span><strong>1,842,596</strong></article>
          <article><h3>较基线提升</h3><span>百分比</span><strong class="positive">-9.38% ↓</strong></article>
          <article><h3>运行时间</h3><span>秒</span><strong>87.6</strong></article>
          <article><h3>验证状态</h3><span>结果</span><strong>通过 ${icon("check-circle")}</strong></article>
        </div>
        <div class="result-tabs" role="tablist">${["核心结果","图表","结果表"].map((tab,index)=>`<button class="${index===0?"active":""}" data-experiment-tab="${tab}">${tab}</button>`).join("")}</div>
        <section class="cost-chart-card"><div class="chart-card-heading"><h2>成本对比（越小越好）</h2><div><span><b class="legend-dot baseline"></b>基线（Run #00）</span><span><b class="legend-dot current"></b>当前结果（Run #04）</span></div></div><div class="cost-chart-wrap"><canvas id="costChart" aria-label="基线与当前总调度成本对比柱状图"></canvas></div></section>
        <section class="experiment-conclusion-grid">
          <article><h3>是否通过验证</h3><p>${icon("check-circle")} 通过</p></article>
          <article><h3>与基线相比的结果</h3><strong class="positive">↓ -9.38%</strong><p>总调度成本降低</p></article>
          <article><h3>是否存在明显风险</h3><p>${icon("check-circle")} 无明显风险</p></article>
          <article><h3>Agent 结论</h3><p>当前结果通过了全部主检验，较基线在总调度成本上取得了 9.38% 的降低，建议采用该结果。</p></article>
        </section>
      </section>`, "experiments");
  }

  function editorScreen() {
    return modelingShell(`
      <section class="editor-main workflow-editor">
        <div class="editor-layout">
          <aside class="outline"><div class="outline-heading"><h3>论文大纲</h3>${icon("dots-three-vertical")}</div>${["摘要","1 引言","2 相关工作","3 需求预测模型构建","4 实证分析","5 结果与讨论","6 结论与展望"].map((x,i)=>`<a href="#section-${i}" class="${i===3?"active":""}"><span class="outline-status ${i<3?"done":""}">${i<3?icon("check"):""}</span>${x}</a>`).join("")}</aside>
          <article class="paper-editor">
            <div class="editor-toolbar">
              <button data-command="undo" aria-label="撤销">${icon("arrow-u-up-left")}</button><button data-command="redo" aria-label="重做">${icon("arrow-u-up-right")}</button><span class="toolbar-divider"></span>
              <button>正文 ${icon("caret-down")}</button><button>宋体 ${icon("caret-down")}</button><button>五号 ${icon("caret-down")}</button><span class="toolbar-divider"></span>
              <button data-command="bold" aria-label="加粗"><strong>B</strong></button><button data-command="italic" aria-label="斜体"><i>I</i></button><button data-command="underline" aria-label="下划线"><u>U</u></button><button>${icon("text-t")}${icon("caret-down")}</button><span class="toolbar-divider"></span>
              <button>${icon("text-align-left")}${icon("caret-down")}</button><button>${icon("table")}</button><button data-action="image">${icon("image")}</button><button data-action="formula">ƒx</button><button data-action="cite">${icon("link")}</button><span class="toolbar-divider"></span><button data-action="cite">${icon("quotes")} 引用</button>
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
      </section>`, "editor");
  }

  const problems = [
    ["2024国赛A题", "城市共享单车调度", "国赛", "2024", "运筹优化", "调度，需求预测，路网，供需平衡", "中（需清洗整合）"],
    ["2024国赛B题", "交通流量预测", "国赛", "2024", "时间序列", "交通流量，时空预测，深度学习", "中（需清洗整合）"],
    ["2023国赛C题", "智慧能源调度", "国赛", "2023", "优化调度", "能源系统，多目标，负荷预测", "高（多源异构）"],
    ["2023国赛A题", "航班延误预测", "国赛", "2023", "分类预测", "航班延误，特征工程，分类模型", "高（多源异构）"],
    ["2022国赛B题", "企业经营决策", "国赛", "2022", "决策分析", "成本收益，风险评估，决策优化", "中（需清洗整合）"],
    ["2022美赛M题", "公共卫生资源配置", "美赛", "2022", "规划分配", "资源配置，公平性，仿真模拟", "高（多源异构）"],
    ["2021国赛D题", "冷链物流路径优化", "国赛", "2021", "路径规划", "冷链配送，车辆路径，温度约束", "高（多源异构）"],
    ["2021美赛C题", "洪灾风险评估", "美赛", "2021", "风险评估", "洪灾模拟，风险预测，空间分析", "高（多源异构）"],
    ["2020国赛A题", "炉温曲线优化", "国赛", "2020", "工艺优化", "热传导，曲线拟合，参数优化", "中（需清洗整合）"],
    ["2020国赛C题", "中小企业信贷决策", "国赛", "2020", "决策分析", "信用评级，违约风险，收益优化", "中（需清洗整合）"],
    ["2019国赛B题", "同心协力策略研究", "国赛", "2019", "动力学", "多人协同，受力分析，运动控制", "中（需清洗整合）"],
    ["2019美赛D题", "航班登机策略优化", "美赛", "2019", "排队优化", "登机效率，排队模型，仿真分析", "中（需清洗整合）"],
    ["2018校赛A题", "校园班车调度", "校赛", "2018", "运筹优化", "班车调度，路径规划，服务水平", "低（结构化数据）"],
    ["2018国赛B题", "高温作业专用服装设计", "国赛", "2018", "热传导", "传热模型，参数反演，数值仿真", "中（需清洗整合）"],
    ["2017美赛B题", "城市交通网络韧性评估", "美赛", "2017", "网络评价", "交通网络，韧性指标，故障模拟", "高（多源异构）"]
  ];

  function problemsScreen() {
    return shell(`
      <section class="library-main resource-library problems-main">
        <div class="library-heading"><h1>赛题库</h1><p>浏览历年赛题、问题类型与建模方向。</p></div>
        <div class="library-tools resource-tools"><label class="search-box">${icon("magnifying-glass")}<input data-problem-search placeholder="搜索赛题、领域或关键词"></label>
          <div class="filters">${["比赛","年份","问题类型","建模方向"].map(x=>`<button class="filter-button" data-action="filter">${x}${icon("caret-down")}</button>`).join("")}</div>
        </div>
        <div class="resource-tabs" role="tablist" aria-label="赛题分类">
          ${["全部赛题","国赛","美赛","校赛","收藏"].map((x,i)=>`<button class="${i===0?"active":""}" data-resource-tab="${x}" data-resource-kind="problem">${x==="收藏"?icon("star"):""}${x}</button>`).join("")}
        </div>
        <div class="resource-table-wrap">
          <table class="resource-table problem-resource-table">
            <thead><tr><th>题目</th><th>比赛</th><th>年份</th><th>问题类型</th><th>关键词</th><th>数据要求</th><th>状态</th></tr></thead>
            <tbody data-problem-list>
              ${problems.map((p,i)=>`<tr class="problem-item ${i===0?"active":""}" data-resource-index="${i}" data-resource-category="${p[2]}" data-resource-search="${p.join(" ")}" data-saved="false" tabindex="0" role="link" aria-label="查看赛题：${p[0]} ${p[1]}">
                <td><div class="resource-title-cell"><button class="row-star" data-action="resource-bookmark" aria-label="收藏 ${p[0]}">${icon("star")}</button><strong>${p[0]}　${p[1]}</strong></div></td>
                <td>${p[2]}</td><td>${p[3]}</td><td>${p[4]}</td><td>${p[5]}</td><td>${p[6]}</td>
                <td><div class="resource-status-cell"><span>已发布</span></div></td>
              </tr>`).join("")}
            </tbody>
          </table>
        </div>
        <div class="resource-footer">
          <span data-resource-page-copy>共 126 题 · 第 1 页</span>
          <div class="resource-pagination">
            <button data-resource-page="prev" aria-label="上一页">${icon("caret-left")}</button>
            ${["1","2","3","4","5"].map((x,i)=>`<button class="${i===0?"active":""}" data-resource-page="${x}">${x}</button>`).join("")}
            <span>…</span><button data-resource-page="21">21</button>
            <button data-resource-page="next" aria-label="下一页">${icon("caret-right")}</button>
          </div>
          <button class="page-size-button" data-action="page-size">15 条/页 ${icon("caret-down")}</button>
        </div>
      </section>`, "problems");
  }

  const papers = [
    ["城市共享单车需求预测与调度优化","2024国赛A题","国家一等奖","时间序列预测、整数规划、需求聚类","需求预测＋调度联动建模"],
    ["城市交通流量短期预测与拥堵评估","2023国赛B题","国家二等奖","LSTM、图卷积网络、贝叶斯网络","多源数据融合＋不确定性量化"],
    ["区域综合能源系统多目标调度优化","2022国赛C题","国家一等奖","多目标优化、强化学习、场景分析","低碳与经济双目标协同优化"],
    ["冷链物流路径规划与温控优化","2021国赛D题","国家二等奖","车辆路径问题、温度预测、遗传算法","温控约束下的路径－温度联动"],
    ["城市交通信号配时优化与仿真评估","2020国赛A题","国家二等奖","排队论、遗传算法、仿真优化","基于仿真的信号配时自适应优化"],
    ["港口集装箱多式联运调度","2019国赛B题","国家二等奖","混合整数规划、网络流、局部搜索","运输网络与时窗联合优化"],
    ["城市垃圾分类收运路线优化","2019国赛C题","国家一等奖","VRP、聚类、模拟退火","分类投放与动态收运协同"],
    ["公共卫生资源应急配置模型","2018国赛A题","国家一等奖","SEIR、鲁棒优化、情景模拟","疫情演化与资源配置联动"],
    ["复杂网络中的信息传播预测","2018国赛B题","国家二等奖","复杂网络、SIR、蒙特卡洛","传播阈值与干预策略量化"],
    ["高速公路交通事故风险评估","2017国赛C题","国家一等奖","随机森林、贝叶斯网络、风险矩阵","多尺度事故风险融合评估"],
    ["农产品供应链库存优化","2017国赛A题","国家二等奖","时间序列、库存模型、动态规划","预测驱动的多级库存协同"],
    ["水质综合评价与污染溯源","2016国赛B题","国家一等奖","主成分分析、模糊评价、聚类","多指标评价与污染源定位"],
    ["光伏发电功率预测与储能调度","2016国赛C题","国家二等奖","XGBoost、储能优化、场景分析","预测误差下的储能稳健调度"],
    ["区域物流枢纽选址与配送优化","2015国赛A题","国家一等奖","设施选址、整数规划、遗传算法","枢纽选址与末端配送联合决策"],
    ["城市内涝风险分区与排水优化","2015国赛B题","国家二等奖","GIS、层次分析、排水网络优化","风险分区驱动的排水能力提升"]
  ];

  function papersScreen() {
    return shell(`
      <section class="library-main resource-library papers-main">
        <div class="library-heading"><h1>优秀论文</h1><p>浏览数学建模竞赛获奖论文与建模路线。</p></div>
        <div class="library-tools resource-tools"><label class="search-box">${icon("magnifying-glass")}<input data-paper-search placeholder="搜索论文、赛题、模型或关键词"></label>
          <div class="filters">${["比赛","年份","奖项","模型方法"].map(x=>`<button class="filter-button" data-action="filter">${x}${icon("caret-down")}</button>`).join("")}</div>
        </div>
        <div class="resource-tabs paper-resource-tabs" role="tablist" aria-label="论文分类">
          ${["全部","国家一等奖","国家二等奖","按赛题","按模型"].map((x,i)=>`<button class="${i===0?"active":""}" data-resource-tab="${x}" data-resource-kind="paper">${x}</button>`).join("")}
        </div>
        <div class="resource-table-wrap paper-resource-wrap">
          <table class="resource-table paper-resource-table">
            <thead><tr><th></th><th>论文标题</th><th>对应赛题</th><th>奖项</th><th>使用模型</th><th>主要创新</th><th>收藏</th></tr></thead>
            <tbody data-paper-list>
              ${papers.map((p,i)=>`<tr class="paper-item ${i===0?"active":""}" data-resource-index="${i}" data-resource-category="${p[2]}" data-resource-search="${p.join(" ")}" data-saved="false" tabindex="0" role="link" aria-label="查看论文：${p[0]}">
                <td>${i+1}</td><td><strong>${p[0]}</strong></td><td>${p[1]}</td><td>${p[2]}</td><td>${p[3]}</td><td>${p[4]}</td>
                <td><button class="row-star" data-action="resource-bookmark" aria-label="收藏 ${p[0]}">${icon("star")}</button></td>
              </tr>`).join("")}
            </tbody>
          </table>
        </div>
        <div class="resource-footer paper-resource-footer">
          <span data-resource-page-copy>共 1,238 篇 · 第 1 页</span>
          <div class="resource-pagination">
            <button data-resource-page="prev" aria-label="上一页">${icon("caret-left")}</button>
            ${["1","2","3","4","5"].map((x,i)=>`<button class="${i===0?"active":""}" data-resource-page="${x}">${x}</button>`).join("")}
            <span>…</span><button data-resource-page="124">124</button>
            <button data-resource-page="next" aria-label="下一页">${icon("caret-right")}</button>
          </div>
          <button class="page-size-button" data-action="page-size">15 条/页 ${icon("caret-down")}</button>
        </div>
      </section>`, "papers");
  }

  function selectedResource(items) {
    const value = Number(new URLSearchParams(window.location.search).get("index"));
    const index = Number.isInteger(value) && value >= 0 && value < items.length ? value : 0;
    return { item: items[index], index };
  }

  function problemDetailScreen() {
    const { item: problem } = selectedResource(problems);
    const [code, title, competition, year, type, keywords, dataRequirement] = problem;
    return shell(`
      <section class="resource-detail-page problem-detail-page">
        <div class="resource-detail-breadcrumb"><a href="${routes.problems}">赛题库</a><span>/</span><strong>查看赛题</strong></div>
        <article class="resource-detail-article">
          <header class="resource-detail-title">
            <h1>${year} 年全国大学生数学建模竞赛 ${code.slice(-2)}</h1>
            <h2>题目：${title}</h2>
          </header>
          <div class="resource-detail-rule"></div>
          <section class="detail-copy-section">
            <h3>一、问题概述</h3>
            <p>${title}是一个典型的${type}问题，需要结合现实业务约束与可获得的数据，对系统状态进行刻画，并给出可执行、可解释的建模方案。参赛者应在明确研究边界的基础上，构建合理的指标体系与数学模型。</p>
            <p>请建立数学模型，分析“${keywords}”之间的关系，并基于模型给出关键结论、优化策略及稳定性说明。</p>
          </section>
          <section class="detail-copy-section">
            <h3>二、问题分析</h3>
            <ol>
              <li><strong>任务识别：</strong>梳理题目目标、决策变量和关键约束，明确需要预测、评价或优化的核心指标。</li>
              <li><strong>模型构建：</strong>建立与${type}相匹配的数学模型，给出变量定义、参数估计方法和求解流程。</li>
              <li><strong>数据验证：</strong>使用提供的数据对模型进行训练、检验与误差分析，说明模型的合理性和适用范围。</li>
              <li><strong>敏感性分析：</strong>改变关键参数和边界条件，分析结论的稳健性，并讨论潜在风险。</li>
            </ol>
          </section>
          <section class="detail-copy-section">
            <h3>三、数据说明</h3>
            <p>附件提供与赛题相关的结构化数据、时间序列和辅助说明。数据要求为“${dataRequirement}”，请仔细阅读字段定义，处理缺失值、异常值和量纲差异，并保留完整的数据清洗记录。</p>
          </section>
          <section class="detail-copy-section">
            <h3>四、提交要求</h3>
            <p>提交内容应包含问题重述、模型假设、符号说明、模型建立与求解、结果分析、模型评价与推广。图表需标注清晰，代码和数据处理过程应可复现。</p>
          </section>
        </article>
        <footer class="resource-detail-actions">
          <div class="detail-stats"><span>浏览量　1,256</span><span>收藏　342</span></div>
          <div class="detail-action-buttons">
            <button type="button" data-action="detail-bookmark">${icon("star")} 收藏</button>
            <button type="button" data-action="download-problem">${icon("download-simple")} 下载题目</button>
            <button class="primary" type="button" data-action="use-problem" data-resource-title="${title}">用于当前任务</button>
          </div>
        </footer>
      </section>`, "problems");
  }

  function paperDetailScreen() {
    const { item: paper } = selectedResource(papers);
    const [title, problemCode, award, models, innovation] = paper;
    const displayTitle = title === "城市共享单车需求预测与调度优化" ? `基于多源数据的${title}` : title;
    return shell(`
      <section class="resource-detail-page paper-detail-page">
        <div class="resource-detail-breadcrumb"><a href="${routes.papers}">优秀论文</a><span>/</span><strong>查看论文</strong></div>
        <article class="resource-detail-article paper-reading-article">
          <header class="resource-detail-title paper-reading-title">
            <h1>${displayTitle}</h1>
            <h2>${problemCode.replace("国赛", " 年全国大学生数学建模竞赛 ")}　·　${award}</h2>
          </header>
          <div class="resource-detail-rule"></div>
          <section class="detail-copy-section paper-abstract">
            <h3>摘要</h3>
            <p>本文针对${title}问题，构建了“数据预处理—特征分析—模型求解—策略优化”的完整建模框架。首先，对多源数据进行清洗、关联与统计分析；其次，采用${models}建立核心模型；最后，通过对比实验和敏感性分析验证模型的有效性与稳定性。</p>
            <p><strong>关键词：</strong>${models.replaceAll("、", "；")}；${innovation}</p>
          </section>
          <section class="detail-copy-section">
            <h3>1　引言</h3>
            <p>随着城市系统规模持续扩大，数据驱动的预测与优化方法已成为提升资源配置效率的重要手段。如何准确识别影响因素、刻画系统变化规律，并形成可落地的决策方案，是该问题的核心。</p>
          </section>
          <section class="detail-copy-section">
            <h3>2　问题分析</h3>
            <h4>2.1　问题描述</h4>
            <p>给定历史运行数据、环境信息与任务约束，需要对未来状态进行预测，并在满足边界条件的情况下制定优化方案，使综合目标达到最优。</p>
            <h4>2.2　建模思路</h4>
            <p>本文首先完成异常值处理和特征构造，随后使用${models}刻画关键变量之间的关系，并围绕“${innovation}”设计求解与验证流程。</p>
          </section>
          <section class="detail-copy-section">
            <h3>3　模型建立与求解</h3>
            <p>根据问题目标定义状态变量、决策变量和约束条件，对各子问题分别建模，再通过统一的评价指标比较不同方案。模型训练采用交叉验证，优化部分结合启发式搜索与精确求解，以兼顾结果质量和计算效率。</p>
            <div class="paper-detail-formula">min　F(x) = α · L<sub>prediction</sub> + β · C<sub>decision</sub> + γ · R<sub>risk</sub></div>
          </section>
          <section class="detail-copy-section">
            <h3>4　结果与讨论</h3>
            <p>实验结果表明，所建模型在预测精度、决策收益和稳健性方面均优于基线方案。敏感性分析进一步说明，核心结论在参数合理变化范围内保持稳定。</p>
          </section>
        </article>
        <footer class="resource-detail-actions">
          <div class="detail-action-buttons detail-left-actions">
            <button type="button" data-action="detail-bookmark">${icon("star")} 收藏</button>
            <button type="button" data-action="cite-detail">${icon("quotes")} 引用</button>
          </div>
          <button class="primary" type="button" data-action="use-paper" data-resource-title="${displayTitle}">${icon("git-branch")} 参考该论文</button>
        </footer>
      </section>`, "papers");
  }

  function methodsScreen() {
    const groups = [["预测",["XGBoost","ARIMA","LSTM","Prophet"]],["优化",[]],["评价",[]],["分类",[]],["聚类",[]],["统计分析",[]],["图论",[]],["微分方程",[]],["仿真",[]]];
    return shell(`
      <div class="method-layout">
        <aside class="method-tree">
          <label class="search-box">${icon("magnifying-glass")}<input data-method-search placeholder="搜索建模方法……"></label>
          ${groups.map((g,i)=>`<div class="tree-group"><div class="tree-group-title" data-tree-group="${g[0]}">${icon(i===0?"caret-down":"caret-right")}<span>${g[0]}</span></div>${i===0?`<div class="tree-children">${g[1].map((c,j)=>`<a class="tree-child ${j===0?"active":""}" href="#" data-method="${c}">${c}</a>`).join("")}</div>`:""}</div>`).join("")}
        </aside>
        <section class="method-content">
          <h1 data-method-title>XGBoost</h1><div class="method-sub">梯度提升树模型</div>
          <div class="method-actions"><button data-action="bookmark">${icon("star")} 收藏</button><button class="primary" data-go="new">用于当前任务</button></div>
          <table class="method-table"><tbody>
            <tr><th>方法简介</th><td>XGBoost（Extreme Gradient Boosting）是一种高效、可扩展的梯度提升决策树模型，通过逐步拟合残差的方式构建一组弱学习器，并结合正则化、列采样、缺失值处理等技术，显著提升了预测精度与泛化能力。</td></tr>
            <tr><th>适用场景</th><td>适用于回归、分类与排序问题。常用于结构化数据的预测，如时间序列预测、用户行为预测、风险评分、销量预测等。</td></tr>
            <tr><th>输入与输出</th><td>输入：特征矩阵 X ∈ Rⁿˣᵖ，目标向量 y ∈ Rⁿ<br>输出：预测值 ŷ ∈ Rⁿ（回归）或类别概率/标签（分类）</td></tr>
            <tr><th>优点</th><td>• 预测精度高，泛化能力强<br>• 支持并行计算，训练效率高<br>• 内置正则化与防过拟合机制<br>• 可处理缺失值与大规模数据</td></tr>
            <tr><th>限制</th><td>• 参数较多，需调参<br>• 模型可解释性相对较弱<br>• 对极其稀疏的高维数据不够友好</td></tr>
            <tr><th>评价指标</th><td>RMSE、MAE、R²、MAPE 等。</td></tr>
            <tr><th>核心公式</th><td>目标函数（以回归为例）：<div class="formula">Obj⁽ᵗ⁾ = ∑ l(yᵢ, ŷᵢ⁽ᵗ⁻¹⁾ + fₜ(xᵢ)) + Ω(fₜ)</div></td></tr>
            <tr><th>代码示例</th><td><div class="code-snippet">import xgboost as xgb
model = xgb.XGBRegressor(
    n_estimators=300, max_depth=6, learning_rate=0.1,
    subsample=0.8, colsample_bytree=0.8, random_state=42
)
model.fit(X_train, y_train)</div></td></tr>
            <tr><th>相关赛题</th><td class="related-row">2023国赛C题：智慧能源调度　　利用历史负荷与气象数据进行短期负荷预测与调度优化　›<br>2022国赛B题：企业经营决策　　基于销售与成本数据的需求预测与利润优化　›</td></tr>
            <tr><th>相关优秀论文</th><td class="related-row">XGBoost: A Scalable Tree Boosting System　　Tianqi Chen, Carlos Guestrin. KDD, 2016　›<br>A Survey on Gradient Boosting Decision Tree　　Ke et al. IEEE TKDE, 2017　›</td></tr>
          </tbody></table>
        </section>
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

  function completeScreen() {
    return modelingShell(`
      <section class="stage-document complete-wrap">
        <h1>最终成果</h1>
        <div class="complete-kicker">${icon("check-circle")} 建模任务已完成</div>
        <h2 class="complete-project-name">城市共享单车需求预测与调度优化</h2>
        <div class="result-summary">
          <div class="summary-row"><span>采用模型</span><span>XGBoost + K-means + 混合整数规划</span></div>
          <div class="summary-row"><span>关键指标</span><span>R² 0.913 / 缺车率下降 18.7% / 平均调度距离下降 11.4%</span></div>
          <div class="summary-row"><span>主要结论</span><ul><li>需求预测模型具备较高精度，能够有效捕捉时空需求波动规律。</li><li>基于聚类的分区调度策略显著降低缺车率并提升资源利用效率。</li><li>混合整数规划优化了车辆调度路径与数量配置，减少总体调度成本。</li></ul></div>
          <div class="summary-row"><span>模型限制</span><ul><li>数据来源与时间范围有限，可能影响模型泛化能力。</li><li>极端天气与突发事件尚未充分建模，需结合实时机制增强鲁棒性。</li><li>实际运营中仍需考虑更多约束与成本项。</li></ul></div>
        </div>
        <section class="deliverable-section"><h2>交付文件</h2>
          <div class="deliverables"><div class="deliverable-head"><span>文件名称</span><span>类型</span><span>大小</span><span>操作</span></div>
            ${deliverables.map(item => `<div class="deliverable"><span class="deliverable-name">${icon(item[0])}${item[1]}</span><span>${item[2]}</span><span>${item[3]}</span><button class="open-file" data-file="${item[1]}" aria-label="下载 ${item[1]}">${icon("download-simple")}</button></div>`).join("")}
          </div>
        </section>
        <div class="stage-actions complete-actions"><button data-go="editor">继续优化</button><button data-action="copy-task">复制为新任务</button><button class="primary" data-action="download-all">下载全部</button></div>
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
              <div class="settings-section account-identity">
                <span class="account-avatar-large">I</span><div><h3>Ivan</h3><p>ivan@example.com · 个人专业版</p></div><button type="button" class="secondary-small" data-settings-action="edit-profile">编辑资料</button>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>登录安全</h3><p>保护你的账户和 API 凭据。</p></div></div>
                <div class="security-item"><span class="security-icon">${icon("password")}</span><div><strong>账户密码</strong><span>上次修改于 42 天前</span></div><button type="button" data-settings-action="change-password">修改密码</button></div>
                <div class="security-item"><span class="security-icon">${icon("device-mobile")}</span><div><strong>双重验证</strong><span class="green">已通过验证器应用启用</span></div><button type="button" data-settings-action="manage-2fa">管理</button></div>
                <div class="security-item"><span class="security-icon">${icon("key")}</span><div><strong>恢复代码</strong><span>还剩 8 个可用恢复代码</span></div><button type="button" data-settings-action="recovery-codes">查看</button></div>
              </div>
              <div class="settings-section">
                <div class="settings-section-heading"><div><h3>登录设备</h3><p>最近 30 天访问过账户的设备。</p></div><button type="button" class="danger-text" data-settings-action="revoke-others">退出其他设备</button></div>
                <div class="device-list">
                  <div class="device-item"><span class="security-icon">${icon("desktop")}</span><div><strong>Windows · Codex Desktop</strong><span>上海 · 当前设备</span></div><span class="device-status">当前</span></div>
                  <div class="device-item" data-device><span class="security-icon">${icon("browser")}</span><div><strong>Chrome on macOS</strong><span>杭州 · 2 天前</span></div><button type="button" data-settings-action="revoke-device">退出</button></div>
                  <div class="device-item" data-device><span class="security-icon">${icon("device-mobile")}</span><div><strong>Safari on iPhone</strong><span>上海 · 6 天前</span></div><button type="button" data-settings-action="revoke-device">退出</button></div>
                </div>
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
      if (action === "revoke-device") {
        actionButton.closest("[data-device]")?.remove();
        toast("该设备已退出登录");
      }
      if (action === "revoke-others") {
        $$("[data-device]", backdrop).forEach(device => device.remove());
        toast("其他设备已全部退出");
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
      if (action === "change-password") toast("密码修改流程已打开");
      if (action === "manage-2fa") toast("双重验证设置已打开");
      if (action === "recovery-codes") toast("恢复代码已通过安全验证");
      if (action === "edit-profile") toast("个人资料编辑已打开");
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

  function initCharts(screen) {
    if (!window.Chart) return;
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
      if (!costCanvas) return;
      new Chart(costCanvas, {
        type: "bar",
        data: {
          labels: ["基线（Run #00）", "当前结果（Run #04）"],
          datasets: [{
            data: [2033414, 1842596],
            backgroundColor: [dark ? "#6d6d69" : "#c7c7c7", dark ? "#ecece8" : "#171717"],
            borderRadius: 1,
            barPercentage: .58,
            categoryPercentage: .78
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
            const first = chart.getDatasetMeta(0).data[0];
            const second = chart.getDatasetMeta(0).data[1];
            ctx.fillStyle = "#20ad63";
            ctx.font = '600 14px Inter, "Microsoft YaHei", sans-serif';
            ctx.fillText("↓ -9.38%", (first.x + second.x) / 2, second.y + 12);
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
            y: { min: 0, max: 2500000, title: { display: true, text: "总调度成本" }, ticks: { stepSize: 500000, callback: value => value === 0 ? "0" : `${value / 1000000}M` } }
          }
        }
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

    const paneStorageKey = "openmathmodelAgentPanePercentV2";
    const defaultPercent = 27;
    const storedPercent = Number(localStorage.getItem(paneStorageKey));
    const clampPercent = value => {
      const rect = split.getBoundingClientRect();
      const minLeft = rect.width < 980 ? 280 : 320;
      const minRight = rect.width < 980 ? 420 : 560;
      const min = minLeft / rect.width * 100;
      const max = (rect.width - minRight - handle.offsetWidth) / rect.width * 100;
      return Math.min(Math.max(value, min), Math.max(min, max));
    };
    const applyPercent = (value, persist = false) => {
      const next = clampPercent(value);
      split.style.setProperty("--agent-pane-width", `${next}%`);
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
      if (action === "sidebar-filter") popupMenu(target, ["全部任务", "进行中", "已完成", "我创建的"]);
      if (action === "settings") openSettingsCenter();
      if (action === "history") modal("任务历史", "<p>当前任务共保存 18 个关键节点，可随时回看题目分析、清洗方案、实验与论文版本。</p>");
      if (action === "task-doc") modal("任务文档", "<p>题目、附件、模型方案、实验记录和论文成果均已汇总到当前项目。</p>");
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
        const list = $(".activity-list");
        list?.classList.toggle("collapsed");
        target.innerHTML = list?.classList.contains("collapsed")
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
      if (action === "download-problem") toast("赛题文件已加入下载队列");
      if (action === "cite-detail") modal("引用论文", '<label>引用格式</label><input value=\"GB/T 7714—2015\" readonly><label>引用文本</label><textarea readonly>城市共享单车需求预测与调度优化［J］. 数学建模优秀论文集, 2024.</textarea>', () => toast("引用文本已复制"));
      if (action === "use-problem") {
        sessionStorage.setItem("openmathmodelPrompt", `请围绕“${target.dataset.resourceTitle}”建立完整数学模型，并给出可复现的求解流程。`);
        toast("已添加赛题上下文");
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
    const bindResourceDirectory = kind => {
      const rowSelector = kind === "problem" ? ".problem-item" : ".paper-item";
      const searchSelector = kind === "problem" ? "[data-problem-search]" : "[data-paper-search]";
      const rows = $$(rowSelector);
      const tabs = $$(`[data-resource-kind="${kind}"]`);
      const applyResourceFilters = () => {
        const query = $(searchSelector)?.value.trim().toLowerCase() || "";
        const selected = tabs.find(tab => tab.classList.contains("active"))?.dataset.resourceTab || "";
        rows.forEach(row => {
          const matchesSearch = !query || row.dataset.resourceSearch.toLowerCase().includes(query);
          const matchesCategory = selected === "全部赛题" || selected === "全部" || selected === "按赛题" || selected === "按模型"
            || (selected === "收藏" ? row.dataset.saved === "true" : row.dataset.resourceCategory === selected);
          row.hidden = !(matchesSearch && matchesCategory);
        });
      };

      $(searchSelector)?.addEventListener("input", applyResourceFilters);
      tabs.forEach(tab => tab.addEventListener("click", () => {
        tabs.forEach(item => item.classList.remove("active"));
        tab.classList.add("active");
        applyResourceFilters();
      }));
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
      $$("[data-resource-page]").forEach(button => button.addEventListener("click", () => {
        const numericButtons = $$('[data-resource-page]:not([data-resource-page="prev"]):not([data-resource-page="next"])');
        const active = numericButtons.find(item => item.classList.contains("active"));
        let page = +(active?.dataset.resourcePage || 1);
        if (button.dataset.resourcePage === "prev") page = Math.max(1, page - 1);
        else if (button.dataset.resourcePage === "next") page += 1;
        else page = +button.dataset.resourcePage;
        numericButtons.forEach(item => item.classList.toggle("active", +item.dataset.resourcePage === page));
        const copy = $("[data-resource-page-copy]");
        if (copy) copy.textContent = `${kind === "problem" ? "共 126 题" : "共 1,238 篇"} · 第 ${page} 页`;
        toast(`已切换到第 ${page} 页`);
      }));
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
        const title = $("h2", button)?.textContent || "方案";
        const overviewTitle = $(".selected-plan-overview > h3");
        if (overviewTitle) overviewTitle.textContent = `所选 ${title} 概览`;
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
      $$("[data-method]").forEach(item => item.addEventListener("click", e => {
        e.preventDefault(); $$("[data-method]").forEach(i => i.classList.remove("active")); item.classList.add("active");
        $("[data-method-title]").textContent = item.dataset.method; toast(`已切换到 ${item.dataset.method}`);
      }));
      $("[data-method-search]")?.addEventListener("input", e => {
        const q = e.target.value.toLowerCase(); $$("[data-method]").forEach(item => item.hidden = !item.innerText.toLowerCase().includes(q));
      });
    }
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
}

