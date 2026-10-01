/**
 * 执行轨迹的行文案（纯函数、无 DOM）：run.log 里的工具调用、子代理会话、审稿结论等事件
 * → 人话标题与详情、哪些工具调用不进轨迹，以及两级折叠里的组标题。
 * 落 DOM、归组与折叠交互在 modeling-workspace-controller。
 *
 * 工具调用事件的参数与输出只有 Python repr 摘要（omm_agent_tools.invoker.summarize，
 * 至多 512 字符、超长截断），这里按 repr 语法取需要的字段，取不到就退回通用文案。
 */

/** 过程行的类别：没有提示词可循的过程组据此定标题，note 是 agent 旁白。 */
export type TraceStep = "think" | "run" | "read" | "list" | "write" | "probe" | "search" | "note";

export interface ToolRowText {
  icon: string;
  title: string;
  detail: string;
  mono: boolean;
  step: TraceStep;
  failed: boolean;
  elapsedMs?: number;
}

export interface SubagentRef {
  kind: string;
  goal: string;
}

export interface ReviewText {
  heading: string;
  summary: string;
}

// ── 提示词 id → 思考行的阶段名 / 过程组在做的事 ─────────────────────────────

const THINKING_STAGE_BY_PROMPT: Record<string, string> = {
  "problem_analysis.default": "题意解析",
  "data_preparation.default": "数据准备",
  // 沙盒会话（H3）：清洗 / 实验 / 检验 / 补图的多轮写码跑码会话，prompt_id 即会话标签
  "data_cleaning.sandbox": "数据清洗",
  "model_planning.default": "建模方案",
  "model_planning.proposer": "方案提议",
  "model_planning.reduce": "方案汇总",
  "model_planning.formalize": "方案定稿",
  "experiment_code.default": "实验代码",
  "experiment_code.sandbox": "实验执行",
  "validating.default": "结果验证",
  "validating.sandbox": "稳健性检验",
  "experiment_review.default": "实验审稿",
  "validating_review.default": "稳健性审稿",
  "data_cleaning_review.default": "清洗审稿",
  "paper_figures.sandbox": "论文补图",
  "paper_figures_review.default": "补图审稿",
  // 整篇回退路径（总编规划失败时的单次生成）
  "paper_writing.default": "论文撰写",
  // 论文分章多轮管线按环节区分：一个阶段里的多次调用各自说清在干什么
  "paper_outline.default": "论文骨架规划",
  "paper_section.default": "论文章节写作",
  "paper_finalize.default": "论文统稿收口",
};

const ACTIVITY_BY_PROMPT: Record<string, string> = {
  "problem_analysis.default": "解析题意",
  "data_preparation.default": "规划数据准备",
  "data_cleaning.sandbox": "编写并运行清洗脚本",
  "model_planning.default": "制定建模方案",
  "model_planning.proposer": "提出候选方案",
  "model_planning.reduce": "比较并汇总方案",
  "model_planning.formalize": "把方案写成可执行规格",
  "experiment_code.default": "编写实验代码",
  "experiment_code.sandbox": "编写并调试实验代码",
  "validating.default": "规划稳健性检验",
  "validating.sandbox": "复跑实验、检验稳健性",
  "experiment_review.default": "审阅实验代码与结果",
  "validating_review.default": "审阅稳健性检验",
  "data_cleaning_review.default": "审阅清洗脚本",
  "paper_figures.sandbox": "编写并运行补图脚本",
  "paper_figures_review.default": "审阅论文补图",
  "paper_writing.default": "撰写论文",
  "paper_outline.default": "规划论文骨架",
  "paper_section.default": "撰写论文章节",
  "paper_finalize.default": "论文统稿收口",
};

/** 多语言任务卡（ADR-0021）的模板 id 带语言后缀（如 experiment_code.sandbox.r），展示口径同主模板。 */
function lookupPrompt(table: Record<string, string>, promptId: string): string {
  return table[promptId] ?? table[promptId.replace(/\.[a-z]+$/, "")] ?? "";
}

export function thinkingStage(promptId: string): string {
  return lookupPrompt(THINKING_STAGE_BY_PROMPT, promptId);
}

export function activityLabel(promptId: string): string {
  return lookupPrompt(ACTIVITY_BY_PROMPT, promptId);
}

// ── Python repr 摘要的字段提取 ──────────────────────────────────────────────

const TRUNCATION_MARK = /\.\.\.\(\+\d+ chars\)$/;

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** 从 start（引号之后）读一个 repr 字符串字面量；返回正文与结束位置（截断时读到末尾）。 */
function readReprString(source: string, start: number, quote: string): { value: string; end: number } {
  let value = "";
  let index = start;
  while (index < source.length) {
    const char = source[index];
    if (char === quote) return { value, end: index + 1 };
    if (char === "\\") {
      const next = source[index + 1];
      if (next === undefined) break;
      if (next === "n") value += "\n";
      else if (next === "t") value += "\t";
      else if (next === "r") value += "\r";
      else if (next === "x" || next === "u") {
        const width = next === "x" ? 2 : 4;
        const hex = source.slice(index + 2, index + 2 + width);
        if (/^[0-9a-fA-F]+$/.test(hex) && hex.length === width) {
          value += String.fromCharCode(parseInt(hex, 16));
          index += 2 + width;
          continue;
        }
        value += next;
      } else {
        value += next;
      }
      index += 2;
      continue;
    }
    value += char;
    index += 1;
  }
  return { value: value.replace(TRUNCATION_MARK, "…"), end: source.length };
}

/** repr 摘要里某个键的字符串值（``{'path': 'data/x.csv'}`` → ``data/x.csv``）；取不到返回 null。 */
export function reprField(summary: string, key: string): string | null {
  const match = new RegExp(`'${escapeRegExp(key)}':\\s*(['"])`).exec(summary);
  if (!match) return null;
  return readReprString(summary, match.index + match[0].length, match[1]).value;
}

/** repr 摘要里某个键的字符串列表；truncated = 摘要在列表中途被截断（条目数不全）。 */
export function reprList(summary: string, key: string): { items: string[]; truncated: boolean } | null {
  const match = new RegExp(`'${escapeRegExp(key)}':\\s*\\[`).exec(summary);
  if (!match) return null;
  const items: string[] = [];
  let index = match.index + match[0].length;
  while (index < summary.length) {
    const char = summary[index];
    if (char === "]") return { items, truncated: false };
    if (char === "'" || char === '"') {
      const { value, end } = readReprString(summary, index + 1, char);
      if (end >= summary.length) return { items, truncated: true };
      items.push(value);
      index = end;
      continue;
    }
    index += 1;
  }
  return { items, truncated: true };
}

/** 整段就是一个 repr 字符串（失败工具的 output_summary 是报错文本的 repr）时去掉引号与转义。 */
function reprText(summary: string): string {
  const trimmed = summary.trim();
  const quote = trimmed[0];
  if (quote !== "'" && quote !== '"') return trimmed;
  return readReprString(trimmed, 1, quote).value;
}

// ── 工具调用行 ──────────────────────────────────────────────────────────────

/** 不进执行轨迹的工具：列目录、查知识库、探测运行环境这类摸底动作对用户没有信息量，成败都不画（事件照常落库）。 */
const QUIET_TOOLS: ReadonlySet<string> = new Set(["ws_list", "knowledge_search", "knowledge_read", "env_probe"]);

export function isQuietTool(tool: string): boolean {
  return QUIET_TOOLS.has(tool);
}

const LANGUAGE_LABELS: Record<string, string> = { python: "Python", r: "R" };

/** 只读小工具（读文件、列目录）不到 1 秒的用时不显示：满屏 0.0s 只是噪音。 */
function visibleElapsed(duration: number, always: boolean): number | undefined {
  if (!Number.isFinite(duration) || duration <= 0) return undefined;
  return always || duration >= 1000 ? duration : undefined;
}

function failureText(payload: Record<string, unknown>): string {
  const detail = typeof payload.failure_detail === "string" ? payload.failure_detail.trim() : "";
  if (detail) return detail;
  return reprText(String(payload.output_summary ?? ""));
}

/** 一次工具调用 → 行文案；不认识的工具返回 null（调用方退回原样展示）。 */
export function describeToolCall(payload: Record<string, unknown>): ToolRowText | null {
  const tool = String(payload.tool ?? "");
  const status = String(payload.status ?? "");
  const failed = status !== "succeeded";
  const input = String(payload.input_summary ?? "");
  const output = String(payload.output_summary ?? "");
  const duration = Number(payload.duration_ms);
  const quick = visibleElapsed(duration, false);
  const row = (fields: Omit<ToolRowText, "failed" | "mono"> & { mono?: boolean }): ToolRowText => ({
    mono: true,
    ...fields,
    icon: failed ? "warning-circle" : fields.icon,
    failed,
  });

  switch (tool) {
    case "python_run":
    case "code_run": {
      const language = tool === "code_run" ? LANGUAGE_LABELS[String(reprField(input, "language") ?? "")] ?? "" : "";
      const subject = language ? `${language} 代码` : "实验代码";
      const title = status === "timeout"
        ? `${subject}运行超时（准备修复重试）`
        : failed ? `${subject}执行失败（准备修复重试）` : `已在沙箱执行${language ? " " : ""}${subject}`;
      // 失败时 failure_detail 携带 stderr 尾部（含 traceback）：没有它用户只能看到
      // 「exited with code 1」一行，无从判断崩在哪
      const detail = [
        `状态：${status}`,
        payload.input_summary ? `输入：${input}` : "",
        payload.output_summary ? `输出：${output}` : "",
        payload.failure_detail ? `报错详情：\n${String(payload.failure_detail)}` : "",
      ].filter(Boolean).join("\n");
      return row({ icon: "terminal-window", title, detail, step: "run", elapsedMs: visibleElapsed(duration, true) });
    }
    case "ws_read": {
      const path = reprField(input, "path");
      return row({
        icon: "file-text",
        title: `${path ? `读取 ${path}` : "读取工作区文件"}${failed ? "（失败）" : ""}`,
        detail: failed ? failureText(payload) : reprField(output, "text") ?? "",
        step: "read",
        elapsedMs: quick,
      });
    }
    case "ws_list": {
      const prefix = reprField(input, "prefix");
      const files = failed ? null : reprList(output, "files");
      const count = files && !files.truncated ? `（${files.items.length} 个）` : "";
      return row({
        icon: "folder-open",
        title: `${prefix ? `查看 ${prefix} 下的文件` : "查看工作区文件"}${failed ? "（失败）" : count}`,
        detail: failed
          ? failureText(payload)
          : files ? [...files.items, ...(files.truncated ? ["…"] : [])].join("\n") : "",
        step: "list",
        elapsedMs: quick,
      });
    }
    case "ws_write": {
      const path = reprField(input, "path");
      return row({
        icon: "pencil-simple",
        title: `${path ? `写入 ${path}` : "写入工作区文件"}${failed ? "（失败）" : ""}`,
        detail: failed ? failureText(payload) : "",
        step: "write",
        elapsedMs: quick,
      });
    }
    case "env_probe":
      return row({
        icon: "cpu",
        title: `探测运行环境${failed ? "（失败）" : ""}`,
        detail: failed ? failureText(payload) : output,
        step: "probe",
        elapsedMs: quick,
      });
    case "table_profile": {
      const path = reprField(input, "path");
      return row({
        icon: "table",
        title: `${path ? `生成数据画像：${path}` : "生成数据画像"}${failed ? "（失败）" : ""}`,
        detail: failed ? failureText(payload) : output,
        step: "probe",
        elapsedMs: quick,
      });
    }
    case "knowledge_search": {
      const query = reprField(input, "query");
      return row({
        icon: "magnifying-glass",
        title: `${query ? `检索知识库：${query}` : "检索知识库"}${failed ? "（失败）" : ""}`,
        detail: failed ? failureText(payload) : output,
        step: "search",
        elapsedMs: quick,
      });
    }
    case "knowledge_read": {
      const card = reprField(input, "card_id");
      return row({
        icon: "book-open",
        title: `${card ? `查阅知识卡片 ${card}` : "查阅知识卡片"}${failed ? "（失败）" : ""}`,
        detail: failed ? failureText(payload) : output,
        step: "read",
        elapsedMs: quick,
      });
    }
    default:
      return null;
  }
}

// ── 子代理会话 ──────────────────────────────────────────────────────────────

const SUBAGENT_ROLES: Record<string, string> = {
  reviewer: "审稿子代理",
  sandbox: "沙盒子代理",
  proposer: "方案提议人",
};

const PROPOSER_VIEWS: Record<string, string> = {
  mechanism: "机理建模",
  data_driven: "数据驱动",
  operations_research: "运筹优化",
};

export function subagentRole(kind: string): string {
  return SUBAGENT_ROLES[kind.split(":", 1)[0]] ?? "子代理";
}

function subagentView(ref: SubagentRef): string {
  const suffix = ref.kind.split(":").slice(1).join(":");
  if (PROPOSER_VIEWS[suffix]) return PROPOSER_VIEWS[suffix];
  const quoted = /「([^」]+)」/.exec(ref.goal);
  return quoted ? quoted[1] : suffix;
}

/** 子代理会话组的标题：单个写「角色：任务」，并行的一组写「角色 ×N：各自视角」。 */
export function subagentGroupTitle(spawns: readonly SubagentRef[]): string {
  const first = spawns[0];
  if (!first) return "子代理";
  const role = subagentRole(first.kind);
  if (spawns.length === 1) {
    const goal = first.goal.trim();
    return goal ? `${role}：${goal}` : role;
  }
  const views = spawns.map(subagentView).filter(Boolean);
  return `${role} ×${spawns.length}${views.length ? `：${views.join("、")}` : ""}`;
}

/** 子代理收束状态（ResultEnvelope.status）→ 是否算失败与标题后缀。 */
export function subagentOutcome(status: string): { failed: boolean; label: string } {
  switch (status) {
    case "done":
      return { failed: false, label: "" };
    case "failed":
      return { failed: true, label: "未完成" };
    case "exhausted":
      return { failed: true, label: "预算耗尽" };
    case "timeout":
      return { failed: true, label: "超时" };
    case "cancelled":
      return { failed: true, label: "已取消" };
    default:
      return { failed: status !== "", label: status };
  }
}

// ── 审稿结论与论文阶段进度 ──────────────────────────────────────────────────

const REVIEW_SUBJECTS: Record<string, string> = {
  experiment_review: "实验审稿",
  robustness_review: "稳健性审稿",
  cleaning_review: "清洗审稿",
  paper_figure_review: "补图审稿",
};

const VERDICT_LABELS: Record<string, string> = {
  accept: "通过",
  reject: "不通过",
  static_reject: "静态检查未通过",
};

/** 审稿人（或静态检查）的一轮结论 → 加粗的一句结论 + 审稿人的总结原话；不是审稿事件返回 null。 */
export function reviewText(kind: string, payload: Record<string, unknown>): ReviewText | null {
  const subject = REVIEW_SUBJECTS[kind];
  if (!subject) return null;
  const round = Number(payload.round);
  const verdict = String(payload.verdict ?? "");
  const blockers = Number(payload.blockers) || 0;
  const findings = Number(payload.findings) || 0;
  const counts = [blockers ? `${blockers} 个阻断问题` : "", findings ? `${findings} 条意见` : ""]
    .filter(Boolean)
    .join("、");
  const roundText = Number.isFinite(round) && round > 0 ? `（第 ${round} 轮）` : "";
  const verdictText = VERDICT_LABELS[verdict] ?? (verdict || "已给出意见");
  return {
    heading: `${subject}${roundText}：${verdictText}${counts ? `，${counts}` : ""}。`,
    summary: String(payload.summary ?? "").trim(),
  };
}

/** 论文阶段的进度事件 → 一句叙述；不是这两类事件返回 null。 */
export function paperProgressText(kind: string, payload: Record<string, unknown>): string | null {
  if (kind === "paper_figures") {
    const planned = Number(payload.planned) || 0;
    const rendered = Number(payload.rendered) || 0;
    const missing = Array.isArray(payload.missing) ? payload.missing.length : 0;
    return `论文补图：规划 ${planned} 张，画出 ${rendered} 张${missing ? `，${missing} 张没画成` : ""}。`;
  }
  if (kind === "paper_published") {
    const chapters = Number(payload.chapters) || 0;
    const chars = Number(payload.chars) || 0;
    const findings = Number(payload.audit_findings) || 0;
    return `论文草稿成稿：${chapters} 章，约 ${chars} 字${findings ? `，自查发现 ${findings} 处待核` : ""}。`;
  }
  return null;
}

/** 论文定向回改（图表 / 引用问题按章回改）→ 行标题。 */
export function paperRewriteTitle(payload: Record<string, unknown>): string {
  const index = Number(payload.index) || 0;
  const heading = String(payload.heading ?? "").trim();
  const before = Number(payload.before) || 0;
  const after = Number(payload.after) || 0;
  const verdict = payload.adopted === false ? "（未见改善，交闸门裁定）" : "";
  return `定向回改第 ${index} 章${heading ? `「${heading}」` : ""}：待修问题 ${before} → ${after}${verdict}`;
}

// ── 两级折叠的组标题 ────────────────────────────────────────────────────────

const READ_ONLY_STEPS: ReadonlySet<TraceStep> = new Set(["read", "list", "probe", "search", "note"]);

/** 子代理之外一串连续过程行的组标题：按组里第一个认识的提示词说「在做什么」，
 *  全是工具行时按有没有改动区分「查看工作区」与「执行步骤」。 */
export function processGroupTitle(promptIds: readonly string[], steps: readonly TraceStep[]): string {
  for (const promptId of promptIds) {
    const label = activityLabel(promptId) || thinkingStage(promptId);
    if (label) return label;
  }
  return steps.every(step => READ_ONLY_STEPS.has(step)) ? "查看工作区" : "执行步骤";
}
