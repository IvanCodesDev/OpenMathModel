/**
 * 图件放大查看（结果页「结果图表」/ 成果页「论文文件」/ 数据页「清洗数据」的缩略图卡，以及 G4 闸门卡片的附图）。
 *
 * 只放大图本身：半透明深底上一张按视口适配的图，没有面板、标题、按钮——编号、图题、来源、下载都在卡片上，这里不重复。
 * 滚轮 / 双指缩放（围绕指针）、拖动平移、双击在「适应视口」与放大之间切换；左右方向键翻同一条里的其他图；
 * Esc 或点图外任意处关闭，焦点回到打开它的缩略图。缩略图与大图是同一个同源产物地址（Cookie 鉴权），直接命中缓存。
 * 弹层挂在 body 上，z-index 压在普通弹框（120）之上、提示条（260）之下。
 */

import { t } from "../i18n/locale";
import type { FigureCard } from "./result-figures";

const LIGHTBOX_CLASS = "figure-lightbox";
/** 图与视口边缘的留白（窄屏收窄）。 */
const VIEWPORT_PADDING = 32;
const VIEWPORT_PADDING_NARROW = 12;
/** 键盘每步的缩放倍率。 */
const ZOOM_STEP = 1.25;
/** 滚轮一格（deltaY=100）≈ 14% 的缩放量；触控板的细碎 delta 按比例平滑。 */
const WHEEL_ZOOM_RATE = 0.0015;
/** 指针位移超过这个像素数才算拖动；没超过的按下 - 松开是点击（点在图外 → 关闭）。 */
const DRAG_THRESHOLD = 4;

/** 滚轮 delta 归一到像素：Firefox 的行模式 / 页模式给的是行数、页数。 */
function wheelPixels(event: WheelEvent): number {
  if (event.deltaMode === 1) return event.deltaY * 16;
  if (event.deltaMode === 2) return event.deltaY * 400;
  return event.deltaY;
}

export function closeFigureLightbox(): void {
  document.querySelector<HTMLElement>(`.${LIGHTBOX_CLASS}`)?.dispatchEvent(new Event("figure-lightbox:close"));
}

/**
 * 打开放大查看：`figures` 是同一条里的全部卡片（没有产物 id 的图没有大图，翻页时跳过），
 * `index` 指向点开的那张；`opener` 是关闭后要把焦点还回去的元素。
 */
export function openFigureLightbox(figures: readonly FigureCard[], index: number, opener?: HTMLElement | null): void {
  const shown = figures.filter(figure => figure.imageUrl);
  let current = shown.indexOf(figures[index]);
  if (current < 0) return;
  closeFigureLightbox();

  const root = document.createElement("div");
  root.className = LIGHTBOX_CLASS;
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.tabIndex = -1;
  const img = document.createElement("img");
  img.className = "figure-lightbox-image";
  img.draggable = false;
  img.decoding = "async";
  // 只在图加载失败时出现的一行说明；正常路径上视口里只有图
  const status = document.createElement("p");
  status.className = "figure-lightbox-status";
  status.hidden = true;
  root.append(img, status);

  // ── 缩放 / 平移状态：图片以自然像素尺寸铺在视口左上角，transform 只做 translate + scale ──
  let natW = 0;
  let natH = 0;
  let scale = 1;
  let fitScale = 1;
  let tx = 0;
  let ty = 0;
  let loadToken = 0;

  const viewport = (): { w: number; h: number } => {
    const rect = root.getBoundingClientRect();
    return { w: rect.width, h: rect.height };
  };
  const padding = (): number => (Math.min(window.innerWidth, window.innerHeight) < 720 ? VIEWPORT_PADDING_NARROW : VIEWPORT_PADDING);
  const computeFit = (): number => {
    const { w, h } = viewport();
    const pad = padding();
    if (!natW || !natH || w <= pad * 2 || h <= pad * 2) return 1;
    return Math.min((w - pad * 2) / natW, (h - pad * 2) / natH);
  };
  const minScale = (): number => Math.min(fitScale, 1) * 0.2;
  const maxScale = (): number => Math.max(fitScale, 1) * 8;

  /** 图比视口小的那一轴居中，比视口大的那一轴不许拖出空边。 */
  const clampOffsets = (): void => {
    const { w, h } = viewport();
    const sw = natW * scale;
    const sh = natH * scale;
    tx = sw <= w ? (w - sw) / 2 : Math.min(0, Math.max(w - sw, tx));
    ty = sh <= h ? (h - sh) / 2 : Math.min(0, Math.max(h - sh, ty));
  };
  const apply = (): void => {
    img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    const { w, h } = viewport();
    root.classList.toggle("is-pannable", natW * scale > w + 1 || natH * scale > h + 1);
  };
  /** 围绕视口内某点（默认视口中心）缩放：该点下方的图像内容缩放前后不动。 */
  const setScale = (next: number, anchorX?: number, anchorY?: number): void => {
    if (!natW || !natH) return;
    const clamped = Math.min(maxScale(), Math.max(minScale(), next));
    const { w, h } = viewport();
    const ax = anchorX ?? w / 2;
    const ay = anchorY ?? h / 2;
    const px = (ax - tx) / scale;
    const py = (ay - ty) / scale;
    scale = clamped;
    tx = ax - px * scale;
    ty = ay - py * scale;
    clampOffsets();
    apply();
  };
  const fit = (): void => {
    fitScale = computeFit();
    scale = fitScale;
    clampOffsets();
    apply();
  };
  const atFit = (): boolean => Math.abs(scale - fitScale) < 1e-3;

  // ── 换图 ──
  const show = (nextIndex: number): void => {
    current = Math.min(shown.length - 1, Math.max(0, nextIndex));
    const figure = shown[current];
    const token = ++loadToken;
    root.setAttribute("aria-label", `${t("图")} ${figure.number} · ${figure.caption}`);
    natW = 0;
    natH = 0;
    img.hidden = true;
    status.hidden = true;
    img.alt = `${figure.label} ${figure.caption}`;
    // 命中缓存时 src 赋值后同步就绪、load 事件随后仍会到：用 natW 挡掉第二次
    const ready = (): void => {
      if (token !== loadToken || natW) return;
      natW = img.naturalWidth;
      natH = img.naturalHeight;
      if (!natW || !natH) {
        status.textContent = t("图片加载失败");
        status.hidden = false;
        return;
      }
      img.style.width = `${natW}px`;
      img.style.height = `${natH}px`;
      img.hidden = false;
      fit();
    };
    img.onload = ready;
    img.onerror = () => {
      if (token !== loadToken) return;
      status.textContent = t("图片加载失败");
      status.hidden = false;
    };
    img.src = figure.imageUrl ?? "";
    if (img.complete && img.naturalWidth) ready();
    // 邻图预热：翻页时不等网络
    for (const neighbour of [shown[current - 1], shown[current + 1]]) {
      if (neighbour?.imageUrl) new Image().src = neighbour.imageUrl;
    }
  };

  // ── 交互 ──
  const insideImage = (x: number, y: number): boolean => {
    if (img.hidden) return false;
    const rect = img.getBoundingClientRect();
    return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
  };

  root.addEventListener("wheel", event => {
    event.preventDefault();
    const factor = Math.min(2, Math.max(0.5, Math.exp(-wheelPixels(event) * WHEEL_ZOOM_RATE)));
    setScale(scale * factor, event.clientX, event.clientY);
  }, { passive: false });

  root.addEventListener("dblclick", event => {
    if (!insideImage(event.clientX, event.clientY)) return;
    if (atFit()) setScale(Math.max(1, fitScale * 2), event.clientX, event.clientY);
    else fit();
  });

  // 单指 / 鼠标拖动平移，双指捏合缩放（Pointer Events，不区分输入设备）；
  // 位移不足阈值的按下 - 松开算点击：点在图外关闭，点在图上不动（留给双击）
  const pointers = new Map<number, { x: number; y: number }>();
  let drag: { x: number; y: number; tx: number; ty: number } | null = null;
  let pinch: { distance: number; scale: number } | null = null;
  let gestured = false;
  const pointerDistance = (): number => {
    const [a, b] = [...pointers.values()];
    return Math.hypot(a.x - b.x, a.y - b.y);
  };
  root.addEventListener("pointerdown", event => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    root.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 1) {
      gestured = false;
      drag = { x: event.clientX, y: event.clientY, tx, ty };
    } else if (pointers.size === 2) {
      gestured = true;
      drag = null;
      pinch = { distance: pointerDistance(), scale };
    }
  });
  root.addEventListener("pointermove", event => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const distance = pointerDistance();
      if (pinch.distance > 0) setScale(pinch.scale * (distance / pinch.distance), (a.x + b.x) / 2, (a.y + b.y) / 2);
      return;
    }
    if (drag && pointers.size === 1) {
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!gestured && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
      gestured = true;
      root.classList.add("is-dragging");
      tx = drag.tx + dx;
      ty = drag.ty + dy;
      clampOffsets();
      apply();
    }
  });
  const releasePointer = (event: PointerEvent): void => {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size === 1) {
      const [remaining] = [...pointers.values()];
      drag = { x: remaining.x, y: remaining.y, tx, ty };
    } else if (pointers.size === 0) {
      drag = null;
      root.classList.remove("is-dragging");
    }
  };
  root.addEventListener("pointerup", releasePointer);
  root.addEventListener("pointercancel", releasePointer);

  const onResize = (): void => {
    if (!natW || !natH) return;
    // 没动过缩放就跟着视口重新适配；动过的保持倍率，只把图拉回可见范围
    if (atFit()) fit();
    else {
      fitScale = computeFit();
      clampOffsets();
      apply();
    }
  };
  window.addEventListener("resize", onResize);

  const dispose = (): void => {
    window.removeEventListener("resize", onResize);
    loadToken += 1;
    root.remove();
    if (opener?.isConnected) opener.focus();
  };
  root.addEventListener("figure-lightbox:close", dispose);
  root.addEventListener("click", event => {
    // 拖动 / 捏合松手后的 click 不算点击
    if (gestured) return;
    if (insideImage(event.clientX, event.clientY)) return;
    dispose();
  });
  root.addEventListener("keydown", event => {
    switch (event.key) {
      case "Escape":
        event.stopPropagation();
        dispose();
        break;
      case "Tab":
        // 弹层里没有别的可聚焦元素：焦点留在弹层上，不跑回后面的页面
        break;
      case "ArrowLeft":
        if (shown.length > 1) show(current - 1);
        break;
      case "ArrowRight":
        if (shown.length > 1) show(current + 1);
        break;
      case "+":
      case "=":
        setScale(scale * ZOOM_STEP);
        break;
      case "-":
      case "_":
        setScale(scale / ZOOM_STEP);
        break;
      case "0":
        fit();
        break;
      default:
        return;
    }
    event.preventDefault();
  });

  document.body.append(root);
  show(current);
  root.focus({ preventScroll: true });
}
