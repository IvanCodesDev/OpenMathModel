/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

export type RunId = string;
/**
 * UTC ISO-8601，统一以 Z 结尾。
 */
export type Timestamp = string;

/**
 * 论文编辑页正文投影：PAPER_WRITING 阶段真实 LLM 节点的最新成功输出（结构化论文草稿）。version/updated_at 支撑后续论文编辑的版本演进；markdown 产物本体沿 Artifact 下载链路获取。
 */
export interface DocumentDraft {
  run_id: RunId;
  /**
   * 论文标题。
   */
  title: string;
  /**
   * 摘要（问题、方法、核心结果、结论）。
   */
  abstract: string;
  /**
   * 关键词；节点未给出时为空列表。
   */
  keywords: string[];
  /**
   * 章节列表，按论文顺序排列。
   */
  sections: PaperSection[];
  /**
   * 草稿版本号：PAPER_WRITING 阶段每次成功产出递增（重试/重跑产生新版本）。
   */
  version: number;
  updated_at: Timestamp;
  /**
   * 数字冻结清单（H5）：正文数值的合法来源——上游各阶段结构化产出里确定性抽取的「值 + 出处」，不经模型转述。论文节点未产出该字段（2026-09-03 之前的运行、模拟节点）时为 null。可选字段：旧消费者可忽略。
   */
  frozen_numbers?: null | FrozenNumber[];
  /**
   * 终稿审计链的发现（G4 定稿闸门的证据）：数值审计（正文数值 ∈ 冻结清单 ∪ 材料）、图表审计（引用的图须是真实图件、引用的表须有带编号表题的表格）、引用审计（引用标记与参考文献条目须来自已验证的引用库）三条确定性审计顺序过终稿；空数组 = 审计过且 0 违规；未审计（旧运行、模拟节点）为 null。可选字段：旧消费者可忽略。
   */
  audit_findings?: null | AuditFinding[];
  /**
   * 真实图件清单（H5 figure_render 第一步）：本次运行实验 / 检验沙盒真正落盘并采集为 figure 产物的图，按 实验 → 检验 顺序编号（图 N），是正文插图 `![图 N 标题](文件名)` 的唯一合法来源；图表审计据此核对。编辑页用 name → artifact_id 把插图解析成产物下载链接。空数组 = 本次运行没有产出图件；论文节点未产出该字段（2026-09-07 之前的运行、模拟节点）时为 null。可选字段：旧消费者可忽略。
   */
  figures?: null | PaperFigure[];
  /**
   * 本次运行的已验证引用库（H5 refs/ 第一步）：方案阶段从知识库解析到的条目——选中方案引用的先例 + 用户提供的资料，均带记录级出处 URL——按固定顺序编号 [n]，是正文引用标记与「参考文献」章条目的唯一合法来源；引用审计据此核编号与条目正文。空数组 = 本次运行没有可核实的引用条目；论文节点未产出该字段（旧运行、模拟节点）时为 null。可选字段：旧消费者可忽略。
   */
  references?: null | PaperReference[];
}
export interface PaperSection {
  /**
   * 章节标题。
   */
  heading: string;
  /**
   * 正文 Markdown（可含列表与表格）。
   */
  content: string;
}
export interface FrozenNumber {
  /**
   * 清单内稳定编号（如 metrics.rmse、robustness.bootstrap.value），论文与卡片按它引用。
   */
  id: string;
  /**
   * 人可读含义（如「实验指标 rmse」「稳健性检查「bootstrap 稳定性」阈值」）。
   */
  label: string;
  /**
   * 冻结的数值，来自上游阶段的结构化产出（沙盒标记行 / 清洗统计 / 方案文本），原样不改写。
   */
  value: number;
  /**
   * 产出该数值的阶段。
   */
  source_stage: "DATA_PREPARATION" | "MODEL_PLANNING" | "EXPERIMENTING" | "VALIDATING";
  /**
   * 阶段产出内的路径（如 metrics.rmse、robustness.checks[0].threshold、cleaning.rows_before、plans[A].steps[2]）。
   */
  source_path: string;
}
export interface AuditFinding {
  /**
   * 发现所在位置：「第 N 章《…》」或「摘要」。
   */
  scope: string;
  /**
   * 发现类型：unsourced_number 无出处数值（不在冻结清单与材料中）；phantom_figure 图引用 / 插图没有对应的真实图件；phantom_table 表引用在全文找不到带该编号表题的表格；unverified_citation 引用标记或参考文献条目不在已验证的引用库中。消费者须容忍未知取值。
   */
  kind: "unsourced_number" | "phantom_figure" | "phantom_table" | "unverified_citation";
  /**
   * 违规 token 原样取样（最多 8 个）：数值 / 「图 N」「表 N」与插图文件名 / 引用标记（[3]、\cite{key}）。字段名沿用首版（改名即破坏性变更）。
   */
  numbers: string[];
  /**
   * 人可读说明。
   */
  detail: string;
}
export interface PaperFigure {
  /**
   * 全文固定编号 N（「图 N」），按 实验 → 检验 顺序赋予，不因未插入而重编。
   */
  number: number;
  /**
   * 图件文件名（产物 name，仅 basename）；正文插图的 url 就是它。
   */
  name: string;
  /**
   * 对应 Artifact 的 id（沿 /artifacts/{id}/download 取内容）；节点没拿到产物 id 时为 null，编辑页不解析成图。
   */
  artifact_id: null | string;
  /**
   * 画图工程师在终答里给的一句话说明（只挂到真实文件上）；没有说明为空串。
   */
  caption: string;
  /**
   * 产出该图件的阶段：实验 / 检验沙盒顺手画的图，或论文阶段按总编规划、只用本次运行真实数据补画的图（PAPER_WRITING）。
   */
  source_stage: "EXPERIMENTING" | "VALIDATING" | "PAPER_WRITING";
  /**
   * 正文是否已插入该图（任一 `![…](url)` / `<img src>` 的 url 或其文件名命中 name），由节点确定性判定。
   */
  inserted: boolean;
}
export interface PaperReference {
  /**
   * 全文固定编号 n（正文引用标记「[n]」与参考文献条目编号），按 方案引用 → 用户提供 顺序赋予，不因未引用而重编。
   */
  number: number;
  /**
   * 知识库卡片的标题原文；引用审计据此核参考文献章的条目正文。
   */
  title: string;
  /**
   * 参考文献条目正文（不含编号；由卡片元数据确定性生成，全文 / 来源链接为 Markdown 链接），写手须逐字照抄。
   */
  text: string;
  /**
   * 出处链接（论文全文或来源页，仅 http(s)）；卡片没有可用链接时为 null。
   */
  url: null | string;
  /**
   * 条目来源：plan_citation = 选中方案在方案文本里标出处引用的知识库卡片；user_reference = 用户在首页「添加上下文」提供、按标题匹配到知识库的资料。
   */
  source: "plan_citation" | "user_reference";
  /**
   * 知识库卡片 id（problem:… / paper:…）；没有对应卡片时为 null。
   */
  card_id: null | string;
  /**
   * 正文（含摘要）是否引用了该条目（任一 [n] / \cite{key} 标记展开后命中编号或 key），由节点确定性判定。
   */
  cited: boolean;
  /**
   * 稳定的引用 key（`\cite{key}` 与 refs/references.bib 用），由卡片 id 确定性生成；老运行没有为 null。
   */
  key?: null | string;
  /**
   * 记录级验证状态：source_verified = 知识库卡片自带来源 URL；title_matched = 用户资料按标题匹配到知识库；unverified = 两者皆非；老运行没有为 null。
   */
  verification?: null | ("source_verified" | "title_matched" | "unverified");
}
