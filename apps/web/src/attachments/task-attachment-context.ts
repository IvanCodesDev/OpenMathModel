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

let entriesPromise: Promise<Entry[]> | null = null;
let firstCollect = true;

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

async function loadEntries(): Promise<Entry[]> {
  const runId = activeRunId();
  if (!runId) return [];
  const view = await fetchJson<{ artifacts?: WorkspaceArtifact[] }>(
    `/api/v1/task-runs/${encodeURIComponent(runId)}/workspace`,
  );
  // 用户上传的附件没有 producer_node；Agent 产物不在此注入（它们由阶段正文承载）。
  const uploads = (view?.artifacts ?? []).filter(
    artifact => artifact.status === "READY" && !artifact.producer_node,
  );
  return uploads.map(artifact => {
    const entry: Entry = {
      id: artifact.id,
      name: artifact.name,
      injected: false,
      promise: fetchJson<ArtifactTextPayload>(
        `/api/v1/artifacts/${encodeURIComponent(artifact.id)}/text`,
      ),
    };
    void entry.promise.then(result => {
      entry.result = result;
    });
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

/**
 * 取本轮可并入的任务附件上下文。没有新内容（无任务、无附件、都已并入、
 * 都还没解析完）时返回 null；调用方在消息发送成功后执行 commit()。
 */
export async function collectTaskAttachmentContext(): Promise<TaskAttachmentContext | null> {
  entriesPromise ??= loadEntries();
  const entries = await entriesPromise;
  const pending = entries.filter(entry => !entry.injected);
  if (pending.length === 0) return null;

  const waitBudget = firstCollect ? FIRST_WAIT_MS : LATER_WAIT_MS;
  firstCollect = false;
  await Promise.race([
    Promise.allSettled(pending.map(entry => entry.promise)),
    delay(waitBudget),
  ]);

  let budget = TOTAL_BUDGET;
  const sections: string[] = [];
  const included: Entry[] = [];
  let parsing = 0;
  for (const entry of pending) {
    const payload = entry.result;
    if (payload === undefined) {
      parsing += 1; // 还在解析，下一轮再补
      continue;
    }
    if (payload === null) {
      // 读取失败：本轮跳过，下一轮 collect 时重试一次
      entry.promise = fetchJson<ArtifactTextPayload>(
        `/api/v1/artifacts/${encodeURIComponent(entry.id)}/text`,
      );
      entry.result = undefined;
      void entry.promise.then(result => {
        entry.result = result;
      });
      continue;
    }
    const section = sectionOf(payload, budget);
    budget -= Math.min(PER_ATTACHMENT_CAP, budget);
    sections.push(section);
    included.push(entry);
  }

  if (sections.length === 0 && parsing === 0) return null;
  const pieces: string[] = [];
  if (sections.length > 0) {
    pieces.push(`用户为本任务上传了 ${sections.length} 个附件，内容如下：\n\n${sections.join("\n\n")}`);
  }
  if (parsing > 0) {
    pieces.push(
      sections.length > 0
        ? `（另有 ${parsing} 个附件仍在本机解析中，稍后的对话会自动补充其内容）`
        : `（用户为本任务上传了 ${parsing} 个附件，仍在本机解析中，稍后的对话会自动补充其内容）`,
    );
  }
  return {
    block: pieces.join("\n\n"),
    commit: () => {
      for (const entry of included) entry.injected = true;
    },
  };
}

/** 切换任务/页面时重置缓存（与 resetConversation 配套；当前按页面刷新自然重置）。 */
export function resetTaskAttachmentContext(): void {
  entriesPromise = null;
  firstCollect = true;
}
