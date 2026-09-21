/** Project views share the existing table, data, pagination and delegated actions. */
import { t } from "../i18n/locale";

type ProjectView = "grid" | "list";
const VIEW_KEY = "openmathmodelProjectView";

export function projectPageSize(): number {
  const view = document.querySelector<HTMLElement>(".project-card")?.dataset.projectView ?? savedView();
  return view === "list" ? 10 : 12;
}

function savedView(): ProjectView {
  try { return localStorage.getItem(VIEW_KEY) === "list" ? "list" : "grid"; }
  catch { return "grid"; }
}

function setView(view: ProjectView): void {
  const card = document.querySelector<HTMLElement>(".project-card");
  if (!card) return;
  card.dataset.projectView = view;
  card.querySelectorAll<HTMLButtonElement>("[data-project-view-button]").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.projectViewButton === view));
  });
  try { localStorage.setItem(VIEW_KEY, view); } catch { /* Private sessions still switch views. */ }
  const oldSizeSelect = document.querySelector("[data-page-size-select]");
  if (oldSizeSelect) {
    const label = document.createElement("span");
    label.className = "project-page-size";
    oldSizeSelect.replaceWith(label);
  }
  document.querySelectorAll(".project-page-size").forEach(label => {
    label.textContent = `${projectPageSize()} ${t("条/页")}`;
  });
}

/** Keep the original cell values; card-only labels disappear in table view. */
export function decorateProjectRows(): void {
  document.querySelectorAll<HTMLTableRowElement>(".project-table tbody tr:not(.projects-empty-row)").forEach(row => {
    if (row.dataset.folderReady) return;
    row.dataset.folderReady = "true";
    const cells = row.cells;
    if (cells.length !== 7) return;
    [
      [2, "clock", "更新于"],
      [3, "file-text", "文件"],
      [4, "flask", "实验"],
      [5, "book-open-text", "论文"],
    ].forEach(([index, icon, label]) => {
      const cell = cells[Number(index)];
      const value = document.createElement("span");
      value.className = "project-cell-value";
      // Move rather than copy nodes so translated content keeps its source mapping.
      value.append(...Array.from(cell.childNodes));
      if (index !== 2 && value.textContent?.trim() === "—") {
        cell.classList.add("project-stat-unavailable");
        value.textContent = t("暂无统计");
        cell.title = `${t(String(label))} · ${t("暂无统计")}`;
        cell.setAttribute("aria-label", cell.title);
      }
      const glyph = document.createElement("i");
      glyph.className = `ph ph-${icon} project-card-only`;
      glyph.setAttribute("aria-hidden", "true");
      const caption = document.createElement("span");
      caption.className = "project-card-only project-cell-label";
      caption.textContent = t(String(label));
      cell.replaceChildren(glyph, caption, value);
    });
    const title = cells[0].querySelector("strong");
    if (title) title.setAttribute("title", title.textContent ?? "");
    const subtitle = cells[0].querySelector("span");
    if (subtitle) subtitle.setAttribute("title", subtitle.textContent ?? "");
  });
  sortVisibleRows();
}

function sortVisibleRows(): void {
  const tbody = document.querySelector<HTMLTableSectionElement>(".project-table tbody");
  const sort = document.querySelector<HTMLElement>("[data-project-sort]")?.dataset.projectSort;
  if (!tbody || !sort) return;
  const rows = Array.from(tbody.rows).filter(row => !row.classList.contains("projects-empty-row"));
  rows.sort((a, b) => {
    const cellIndex = sort === "name" ? 0 : 2;
    const text = (row: HTMLTableRowElement) => row.cells[cellIndex].querySelector(cellIndex === 0 ? "strong" : ".project-cell-value")?.textContent ?? "";
    return (sort === "updated-desc" ? -1 : 1) * text(a).localeCompare(text(b), document.documentElement.lang, { numeric: true });
  });
  tbody.append(...rows);
}

export function initializeProjectPresentation(onViewChange: () => void): void {
  const toolbar = document.querySelector<HTMLElement>(".project-search-row");
  if (!toolbar || toolbar.querySelector(".project-view-tools")) return;
  const tools = document.createElement("div");
  tools.className = "project-view-tools";
  tools.innerHTML = `<div class="project-view-switch" role="group" aria-label="${t("项目视图")}">
    <button type="button" data-project-view-button="grid" aria-label="${t("网格视图")}" title="${t("网格视图")}"><i class="ph-fill ph-squares-four" aria-hidden="true"></i></button>
    <button type="button" data-project-view-button="list" aria-label="${t("列表视图")}" title="${t("列表视图")}"><i class="ph ph-list-bullets" aria-hidden="true"></i></button>
  </div><div class="project-sort" data-project-sort="updated-desc" title="${t("对当前页项目排序")}">
    <button type="button" class="project-sort-trigger" aria-haspopup="listbox" aria-expanded="false" aria-controls="project-sort-options" aria-label="${t("对当前页项目排序")}">
      <span data-project-sort-label>${t("最近更新")}</span><i class="ph ph-caret-down" aria-hidden="true"></i>
    </button>
    <div class="project-sort-menu" id="project-sort-options" role="listbox" aria-label="${t("对当前页项目排序")}" hidden>
      ${[["updated-desc", "最近更新", "sort-descending"], ["updated-asc", "最早更新", "sort-ascending"], ["name", "项目名称", "text-aa"]].map(([value, label, glyph]) => `
        <button type="button" role="option" data-project-sort-option="${value}" aria-selected="${value === "updated-desc"}" tabindex="-1"><i class="ph ph-${glyph}" aria-hidden="true"></i><span>${t(label)}</span><i class="ph ph-check" aria-hidden="true"></i></button>`).join("")}
    </div>
  </div>`;
  toolbar.append(tools);
  tools.addEventListener("click", event => {
    const button = (event.target as Element).closest<HTMLElement>("[data-project-view-button]");
    if (button) {
      const view = button.dataset.projectViewButton === "list" ? "list" : "grid";
      if (document.querySelector<HTMLElement>(".project-card")?.dataset.projectView === view) return;
      setView(view);
      onViewChange();
    }
  });
  bindSortMenu(tools);
  setView(savedView());
  decorateProjectRows();
}

function bindSortMenu(tools: HTMLElement): void {
  const host = tools.querySelector<HTMLElement>("[data-project-sort]")!;
  const trigger = host.querySelector<HTMLButtonElement>(".project-sort-trigger")!;
  const menu = host.querySelector<HTMLElement>(".project-sort-menu")!;
  const options = Array.from(menu.querySelectorAll<HTMLButtonElement>("[data-project-sort-option]"));
  const close = (restoreFocus = false) => {
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus();
  };
  const open = (last = false) => {
    menu.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    (last ? options.at(-1) : options.find(option => option.getAttribute("aria-selected") === "true"))?.focus();
  };
  trigger.addEventListener("click", () => menu.hidden ? open() : close());
  trigger.addEventListener("keydown", event => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      open(event.key === "ArrowUp");
    }
  });
  options.forEach(option => option.addEventListener("click", () => {
    host.dataset.projectSort = option.dataset.projectSortOption;
    host.querySelector("[data-project-sort-label]")!.textContent = option.querySelector("span")!.textContent;
    options.forEach(entry => entry.setAttribute("aria-selected", String(entry === option)));
    sortVisibleRows();
    close(true);
  }));
  host.addEventListener("keydown", event => {
    if (menu.hidden) return;
    if (event.key === "Escape") { event.preventDefault(); close(true); }
    if (event.key === "Tab") close();
    if (event.target === trigger) return;
    const index = options.indexOf(document.activeElement as HTMLButtonElement);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
        : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      options[next].focus();
    }
  });
  document.addEventListener("pointerdown", event => {
    if (!host.contains(event.target as Node)) close();
  });
  host.addEventListener("focusout", event => {
    if (!host.contains(event.relatedTarget as Node | null)) close();
  });
}
