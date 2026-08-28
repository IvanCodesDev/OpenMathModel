/**
 * 任务附件折算成对话上下文（ADR-0010 批次三补全）。
 *
 * 首页新建任务上传的附件保存为项目产物，此前运行页对话（含开场分析）拿不到
 * 它们的内容——模型只看到「见图 1」却永远看不到图。这里读取工作台产物列表，
 * 取服务端权威正文（`GET /artifacts/{id}/text`，含可选 VL，全部在本机解析），
 * 折算成与随消息附件同构的上下文块并入对话。
 *
 * 解析可能很慢（图片/扫描件首次走 VL 需数十秒），因此按「等待预算」工作：
 * 每轮对话只并入已就绪且未并入过的附件，没赶上的自动加入之后的轮次；
 * 服务端按内容寻址缓存正文，重复读取零成本。
 *
 * 开场分析的兜底（用户实测痛点）：服务端正文没赶上等待预算时，退回任务创建
 * 时随 run 交接的「浏览器解析摘录」（见 persistTaskAttachmentExcerpts），保证
 * 首条回复不会对着三个字的任务名说「没收到题面」；权威全文就绪后仍会并入
 * 后续轮次。
 */

export interface TaskAttachmentContext {
  /** 并入用户消息的上下文块；无新增内容时不会产生本对象 */
  block: string;
  /** 消息发送成功后调用：把本次并入的附件标记为已注入，避免重复占上下文 */
  commit: () => void;
}

const PER_ATTACHMENT_CAP = 8000;
const TOTAL_BUDGET = 20000;
/** 开场分析愿意等待解析的上限；之后的轮次只等一小会，赶不上就下轮再补。 */
const FIRST_WAIT_MS = 12_000;
const LATER_WAIT_MS = 2_500;
/** 单个正文请求的兜底超时：VL 首载可达数十秒，给足预算但不无限等。 */
const TEXT_TIMEOUT_MS = 180_000;

const RUN_ID_PATTERN = /^run_[0-9a-f]{32}$/;
const ACTIVE_RUN_KEY = "openmathmodel.activeRunId";
/** 任务创建时交接的浏览器解析摘录（按 run 隔离，sessionStorage）。 */
const EXCERPT_HANDOFF_PREFIX = "openmathmodel.taskAttachmentExcerpts.";
/** 单条摘录与合计的存储预算：与任务草稿的摘要上限（4000/24000）一致。 */
const EXCERPT_HANDOFF_CHARS = 4_000;
const EXCERPT_HANDOFF_TOTAL = 24_000;

interface ArtifactTextPayload {
  artifact_id: string;
  name: string;
  media_type: string;
  status: string;
  engine: string;
  characters: number;
  images?: number | null;
  detail?: string | null;
  text: string;
}

interface Entry {
  id: string;
  name: string;
  promise: Promise<ArtifactTextPayload | null>;
  result?: ArtifactTextPayload | null;
  injected: boolean;
}

interface ExcerptRecord {
  name: string;
  excerpt: string;
  characters?: number;
}

let entriesPromise: Promise<Entry[] | null> | null = null;
let firstCollect = true;
/** 已用摘录顶上的附件名：摘录不重复注入；权威全文就绪后照常并入。 */
const excerptInjectedNames = new Set<string>();

function activeRunId(): string | null {
  const params = new URL(window.location.href).searchParams;
  if (params.get("demo") === "1") return null;
  const fromQuery = params.get("run_id") ?? "";
  if (RUN_ID_PATTERN.test(fromQuery)) return fromQuery;
  try {
    const saved = sessionStorage.getItem(ACTIVE_RUN_KEY) ?? "";
    return RUN_ID_PATTERN.test(saved) ? saved : null;
  } catch {
    return null;
  }
}

/**
 * 任务创建成功后调用（task-start-controller）：把浏览器解析出的正文摘录按
 * run 存进 sessionStorage。运行页的对话（尤其是开场分析）在服务端正文没赶上
 * 等待预算时以它兜底。不做一次性消费：开场失败重试、刷新页面都还要用。
 */
export function persistTaskAttachmentExcerpts(
  runId: string,
  attachments: readonly { name: string; excerpt?: string; characters?: number }[],
): void {
  if (!RUN_ID_PATTERN.test(runId)) return;
  let budget = EXCERPT_HANDOFF_TOTAL;
  const records: ExcerptRecord[] = [];
  for (const attachment of attachments) {
    const excerpt = (attachment.excerpt ?? "").slice(0, Math.max(0, Math.min(EXCERPT_HANDOFF_CHARS, budget)));
    if (!excerpt) continue;
    budget -= excerpt.length;
    records.push({
      name: attachment.name,
      excerpt,
      ...(attachment.characters ? { characters: attachment.characters } : {}),
    });
  }
  if (records.length === 0) return;
  try {
    sessionStorage.setItem(EXCERPT_HANDOFF_PREFIX + runId, JSON.stringify(records));
  } catch {
    // 会话存储不可用时没有兜底摘录，服务端正文仍是权威来源。
  }
}

function readExcerptRecords(runId: string): Map<string, ExcerptRecord> {
  const byName = new Map<string, ExcerptRecord>();
  let raw: string | null = null;
  try {
    raw = sessionStorage.getItem(EXCERPT_HANDOFF_PREFIX + runId);
  } catch {
    return byName;
  }
  if (!raw) return byName;
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return byName;
  }
  if (!Array.isArray(payload)) return byName;
  for (const item of payload) {
    const record = item as ExcerptRecord;
    if (typeof record?.name === "string" && typeof record?.excerpt === "string" && record.excerpt) {
      if (!byName.has(record.name)) byName.set(record.name, record);
    }
  }
  return byName;
}

async function fetchJson<T>(path: string): Promise<T | null> {
  try {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(TEXT_TIMEOUT_MS),
    });
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

interface WorkspaceArtifact {
  id: string;
  name: string;
  status: string;
  producer_node: string | null;
}

function fetchEntryText(entry: Entry): void {
  entry.result = undefined;
  entry.promise = fetchJson<ArtifactTextPayload>(
    `/api/v1/artifacts/${encodeURIComponent(entry.id)}/text`,
  );
  void entry.promise.then(result => {
    entry.result = result;
  });
}

/** 返回 null = 工作台读取失败（与「确实没有附件」区分，失败不缓存）。 */
async function loadEntries(): Promise<Entry[] | null> {
  const runId = activeRunId();
  if (!runId) return [];
  const view = await fetchJson<{ artifacts?: WorkspaceArtifact[] }>(
    `/api/v1/task-runs/${encodeURIComponent(runId)}/workspace`,
  );
  if (view === null) return null;
  // 用户上传的附件没有 producer_node；Agent 产物不在此注入（它们由阶段正文承载）。
  const uploads = (view.artifacts ?? []).filter(
    artifact => artifact.status === "READY" && !artifact.producer_node,
  );
  return uploads.map(artifact => {
    const entry: Entry = {
      id: artifact.id,
      name: artifact.name,
      injected: false,
      promise: Promise.resolve(null),
    };
    fetchEntryText(entry);
    return entry;
  });
}

function delay(ms: number): Promise<void> {
  return new Promise(resolveDone => window.setTimeout(resolveDone, ms));
}

function sectionOf(payload: ArtifactTextPayload, budget: number): string {
  const meta: string[] = [];
  if (payload.characters) meta.push(`${payload.characters.toLocaleString("zh-CN")} 字`);
  if (payload.images) meta.push(`${payload.images} 张图`);
  if (payload.engine && payload.engine !== "none") meta.push(`本机解析：${payload.engine}`);
  const body = payload.text.trim().slice(0, Math.max(0, Math.min(PER_ATTACHMENT_CAP, budget)))
    || `（未能提取文本${payload.detail ? `：${payload.detail}` : ""}）`;
  return `【任务附件：${payload.name}】${meta.length ? `（${meta.join("，")}）` : ""}\n${body}`;
}

function excerptSectionOf(record: ExcerptRecord, budget: number): string {
  const body = record.excerpt.slice(0, Math.max(0, Math.min(PER_ATTACHMENT_CAP, budget)));
  const meta = record.characters
    ? `全文约 ${record.characters.toLocaleString("zh-CN")} 字，以下为开头摘录，`
    : "";
  return `【任务附件：${record.name}】（浏览器初步解析：${meta}完整正文稍后自动并入）\n${body}`;
}

/**
 * 取本轮可并入的任务附件上下文。没有新内容（无任务、无附件、都已并入、
 * 都还没解析完且没有摘录兜底）时返回 null；调用方在消息发送成功后执行 commit()。
 */
export async function collectTaskAttachmentContext(): Promise<TaskAttachmentContext | null> {
  const runId = activeRunId();
  if (!runId) return null;
  entriesPromise ??= loadEntries();
  const entries = await entriesPromise;
  // 读取失败或列表为空时不缓存：失败要重试（否则整个页面会话都静默丢附件），
  // 空列表也可能是附件对话框稍后补传，下一轮重新拉取的成本只是一个 GET。
  if (entries === null || entries.length === 0) entriesPromise = null;

  const excerpts = readExcerptRecords(runId);
  const waitBudget = firstCollect ? FIRST_WAIT_MS : LATER_WAIT_MS;
  firstCollect = false;

  const pending = (entries ?? []).filter(entry => !entry.injected);
  if (pending.length > 0) {
    await Promise.race([
      Promise.allSettled(pending.map(entry => entry.promise)),
      delay(waitBudget),
    ]);
  }

  let budget = TOTAL_BUDGET;
  const sections: string[] = [];
  const included: Entry[] = [];
  const excerptUsed: string[] = [];
  /** 还没有任何内容可用（服务端解析中且无摘录）的附件名。 */
  const stillParsing: string[] = [];

  const injectExcerptFallback = (name: string): boolean => {
    const record = excerpts.get(name);
    if (!record || excerptInjectedNames.has(name) || budget <= 0) return false;
    sections.push(excerptSectionOf(record, budget));
    budget -= Math.min(PER_ATTACHMENT_CAP, budget);
    excerptUsed.push(name);
    return true;
  };

  for (const entry of pending) {
    const payload = entry.result;
    if (payload === undefined) {
      // 服务端还在解析：有浏览器摘录先顶上，没有就记为解析中、下一轮再补
      if (!injectExcerptFallback(entry.name)) stillParsing.push(entry.name);
      continue;
    }
    if (payload === null) {
      // 读取失败：立即安排重试；本轮同样先用摘录顶上
      fetchEntryText(entry);
      if (!injectExcerptFallback(entry.name)) stillParsing.push(entry.name);
      continue;
    }
    // 摘录已在此前轮次注入且已覆盖全文：不再重复占上下文
    const record = excerpts.get(entry.name);
    if (
      record
      && excerptInjectedNames.has(entry.name)
      && payload.characters <= record.excerpt.length
    ) {
      included.push(entry);
      continue;
    }
    sections.push(sectionOf(payload, budget));
    budget -= Math.min(PER_ATTACHMENT_CAP, budget);
    included.push(entry);
  }

  // 工作台读取失败（entries 为 null）时产物清单都拿不到：直接用交接摘录兜底，
  // 开场分析不至于两手空空。
  if (entries === null) {
    for (const name of excerpts.keys()) injectExcerptFallback(name);
  }

  if (sections.length === 0 && stillParsing.length === 0) return null;
  const pieces: string[] = [];
  if (sections.length > 0) {
    pieces.push(`用户为本任务上传了附件，内容如下：\n\n${sections.join("\n\n")}`);
  }
  if (stillParsing.length > 0) {
    const names = stillParsing.map(name => `「${name}」`).join("、");
    pieces.push(
      sections.length > 0
        ? `（另有 ${stillParsing.length} 个附件 ${names} 仍在解析中，内容稍后自动并入，无需用户重新提供。）`
        : `（用户已为本任务上传 ${stillParsing.length} 个附件：${names}，正文仍在解析中，稍后会自动并入对话。`
          + `请勿因此断言缺少题面或要求用户补充材料；先基于任务描述与文件名说明即将执行的分析思路即可。）`,
    );
  }
  return {
    block: pieces.join("\n\n"),
    commit: () => {
      for (const entry of included) entry.injected = true;
      for (const name of excerptUsed) excerptInjectedNames.add(name);
    },
  };
}

/** 切换任务/页面时重置缓存（与 resetConversation 配套；当前按页面刷新自然重置）。 */
export function resetTaskAttachmentContext(): void {
  entriesPromise = null;
  firstCollect = true;
  excerptInjectedNames.clear();
}
