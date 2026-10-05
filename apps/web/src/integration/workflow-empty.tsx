import { createRoot, type Root } from "react-dom/client";
import { WorkflowScene, type WorkflowSceneKind } from "../components/WorkflowScene";
import type { JellyVariant } from "../components/Jelly";
import { t } from "../i18n/locale";
import "./workflow-empty.css";

type EmptyCopy = { title: string; detail: string; pose: JellyVariant };
const copy: Record<string, EmptyCopy> = {
  "data-report": { title: "让数据先说话", detail: "数据画像就绪后，会在这里展示清单与质量分析。", pose: "glasses" },
  "model-plan": { title: "从问题到思路", detail: "建模方案就绪后，会在这里展示候选模型与实现计划。", pose: "pencil" },
  "experiment-report": { title: "让每个结论都有依据", detail: "实验结果就绪后，会在这里展示指标、验证与结论。", pose: "reader" },
  charts: { title: "等数据画出答案", detail: "还没有结果图表，生成的图件会出现在这里。", pose: "glasses" },
  "results-table": { title: "把结果整理成章", detail: "还没有结果表，实验数据就绪后会出现在这里。", pose: "pencil" },
  "run-log": { title: "记录每一步探索", detail: "还没有日志文件，发布后的运行记录会出现在这里。", pose: "reader" },
  "model-code": { title: "思路即将有迹可循", detail: "还没有模型代码，发布后的代码文件会出现在这里。", pose: "pencil" },
  "raw-data": { title: "从一份数据开始", detail: "原始数据就绪后，会在这里展示文件与数据预览。", pose: "reader" },
  "clean-data": { title: "让数据更清晰", detail: "清洗结果就绪后，会在这里展示处理记录与数据预览。", pose: "glasses" },
  "field-guide": { title: "读懂每一列数据", detail: "字段说明就绪后，会在这里展示名称、含义与单位。", pose: "reader" },
  assumptions: { title: "为推理打好基础", detail: "模型假设就绪后，会在这里展示适用条件与依据。", pose: "glasses" },
  symbols: { title: "用符号表达思路", detail: "符号表就绪后，会在这里展示变量与参数的定义。", pose: "pencil" },
  implementation: { title: "把方案变成步骤", detail: "实现计划就绪后，会在这里展示具体的求解步骤。", pose: "pencil" },
  "final-summary": { title: "让每一步汇成成果", detail: "还没有可交付文件，发布后的阶段成果会汇集在这里。", pose: "glasses" },
  "paper-package": { title: "为论文整理好附件", detail: "还没有论文文件，正文与附图就绪后会出现在这里。", pose: "reader" },
  "data-code-package": { title: "留下可复现的过程", detail: "还没有数据与代码文件，发布后会在这里统一整理。", pose: "pencil" },
  "delivery-record": { title: "记录成果的来处", detail: "还没有交付记录，确认交付后会在这里展示。", pose: "reader" },
  paper: { title: "把探索写成论文", detail: "正文生成后会在这里呈现，也可以现在开始撰写。", pose: "pencil" },
  default: { title: "为下一步留好位置", detail: "这里还没有内容，阶段成果就绪后会自动呈现。", pose: "wave" },
};

let cleanup: (() => void) | undefined;
function sceneKind(key: string): WorkflowSceneKind {
  if (["data-report", "raw-data", "clean-data", "field-guide", "results-table"].includes(key)) return "data";
  if (["model-plan", "assumptions", "symbols", "implementation"].includes(key)) return "model";
  if (key === "experiment-report" || key === "charts") return "experiment";
  if (key === "model-code" || key === "data-code-package") return "code";
  if (key === "run-log" || key === "delivery-record") return "log";
  if (key === "paper" || key === "paper-package") return "paper";
  return "delivery";
}

/** Attach only to existing empty slots. React decoration stays outside saved editor HTML. */
export function mountWorkflowEmptyStates(): void {
  cleanup?.();
  const shell = document.querySelector<HTMLElement>("[data-modeling-shell]");
  if (!shell) return;
  const mounts = new Map<HTMLElement, { node: HTMLElement; root: Root }>();
  let disposed = false;
  let scheduled = false;
  const editor = shell.querySelector<HTMLElement>(".editor-page");
  const clearPlaceholder = () => {
    editor?.querySelector(":scope > .editor-placeholder")?.remove();
  };
  editor?.addEventListener("focus", clearPlaceholder);

  function sync() {
    scheduled = false;
    if (disposed) return;
    if (!shell?.isConnected) { dispose(); return; }
    const targets = new Map<HTMLElement, string>();
    shell.querySelectorAll<HTMLElement>("[data-stage-empty]:not(.outline-empty)").forEach(marker => {
      if (marker.parentElement) targets.set(marker.parentElement, marker.closest<HTMLElement>("[data-workspace-panel]")?.dataset.workspacePanel ?? "default");
    });
    shell.querySelectorAll<HTMLElement>("[data-workspace-panel]").forEach(panel => {
      const key = panel.dataset.workspacePanel ?? "";
      if (["raw-data", "clean-data", "field-guide", "assumptions", "symbols", "implementation"].includes(key)
        && [...panel.children].every(child => child.classList.contains("workflow-empty"))) targets.set(panel, key);
    });
    shell.querySelectorAll<HTMLElement>(".deliverables").forEach(list => {
      const rows = list.querySelectorAll(":scope > .deliverable");
      const panel = list.closest<HTMLElement>("[data-workspace-panel]");
      // Populated summaries/figures retain a compact file-list empty state.
      if (rows.length === 1 && rows[0].hasAttribute("data-artifact-empty")
        && !panel?.querySelector(".result-summary > *, .result-figure")) {
        targets.set(list, panel?.dataset.workspacePanel ?? "default");
      }
    });
    if (editor?.querySelector(":scope > .editor-placeholder") && editor.parentElement) targets.set(editor.parentElement, "paper");
    for (const [host, mount] of mounts) {
      if (!targets.has(host) || !host.contains(mount.node)) {
        mount.root.unmount(); mount.node.remove(); host.classList.remove("has-workflow-empty"); mounts.delete(host);
      }
    }
    for (const [host, key] of targets) {
      if (mounts.has(host)) continue;
      const node = document.createElement("div");
      node.className = "workflow-empty";
      const content = copy[key] ?? copy.default;
      host.classList.add("has-workflow-empty");
      if (key === "paper") editor?.before(node); else host.append(node);
      const root = createRoot(node);
      mounts.set(host, { node, root });
      root.render(<>
        <WorkflowScene kind={sceneKind(key)} pose={content.pose} />
        <strong className="workflow-empty-title">{t(content.title)}</strong>
        <p className="workflow-empty-detail">{t(content.detail)}</p>
        {key === "paper" && <button type="button" className="workflow-empty-write" onClick={() => {
          clearPlaceholder();
          queueMicrotask(() => { sync(); editor?.focus(); });
        }}>{t("开始撰写")}</button>}
      </>);
    }
  }
  const observer = new MutationObserver(records => {
    // Mascot animation and translated copy never trigger another slot scan.
    if (records.every(record => (record.target instanceof Element ? record.target : record.target.parentElement)?.closest(".workflow-empty"))) return;
    if (!scheduled) { scheduled = true; queueMicrotask(sync); }
  });
  observer.observe(document.getElementById("root")!, { childList: true, subtree: true });
  function dispose() {
    disposed = true;
    observer.disconnect();
    editor?.removeEventListener("focus", clearPlaceholder);
    window.removeEventListener("pagehide", dispose);
    // activateScreen runs in a React layout effect; unmount after that commit.
    for (const [host, mount] of mounts) {
      host.classList.remove("has-workflow-empty");
      queueMicrotask(() => { mount.root.unmount(); mount.node.remove(); });
    }
    mounts.clear();
  }
  cleanup = dispose;
  window.addEventListener("pagehide", dispose, { once: true });
  queueMicrotask(sync);
}
