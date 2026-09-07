/**
 * 论文页真实图件的纯数据整形（不碰 DOM，node --test 直接断言）：
 * document-draft 契约的 figures（H5 figure_render 第一步——实验 / 检验沙盒真正采集到的
 * 图件，编号固定）→ 正文插图 `![图 N 标题](文件名)` 的 URL 解析器 + 「终稿审计」条的图件计数。
 *
 * 解析只认本次运行的图件：文件名（或 url 的 basename）命中清单且有产物 id 才给出同源的
 * 产物下载链接（Cookie 鉴权、下载即哈希核验）；命不中的一律 null，渲染层保持纯文本——
 * 模型写的任何别的 url 都不会变成 <img>。
 */

import type { DocumentDraft } from "@openmathmodel/contracts";

export type PaperFigure = NonNullable<DocumentDraft["figures"]>[number];

/** 与 stage-outputs 投影 / DeliveryManifest 一致的产物下载路径。 */
export function artifactDownloadUrl(artifactId: string): string {
  return `/api/v1/artifacts/${encodeURIComponent(artifactId)}/download`;
}

function basename(url: string): string {
  return url.replace(/\\/g, "/").replace(/\/+$/, "").split("/").pop()?.split("?")[0] ?? "";
}

/**
 * 图件清单 → renderMarkdown 的 resolveImage：按文件名 / basename 命中、且 artifact_id 非空
 * 才解析；清单为空或缺席 → 恒 null（论文页与聊天气泡一样不出图）。
 */
export function figureImageResolver(
  figures: readonly PaperFigure[] | null | undefined,
): (url: string) => string | null {
  const byName = new Map<string, string>();
  for (const figure of figures ?? []) {
    if (figure.artifact_id && !byName.has(figure.name)) byName.set(figure.name, figure.artifact_id);
  }
  return url => {
    const id = byName.get(url) ?? byName.get(basename(url));
    return id ? artifactDownloadUrl(id) : null;
  };
}

export interface FigureSummary {
  total: number;
  inserted: number;
}

/** 「终稿审计」条的图件计数；清单缺席 / 为空 → null（无图运行的条文案逐字不变）。 */
export function summarizeFigures(
  figures: readonly PaperFigure[] | null | undefined,
): FigureSummary | null {
  if (!figures || figures.length === 0) return null;
  return {
    total: figures.length,
    inserted: figures.filter(figure => figure.inserted).length,
  };
}
