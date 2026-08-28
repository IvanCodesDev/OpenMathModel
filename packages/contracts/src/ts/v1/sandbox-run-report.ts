/* eslint-disable */
/**
 * 本文件由 scripts/generate-ts.mjs 从 schemas/v1 生成，禁止手改。
 * 重新生成：npm run generate --workspace @openmathmodel/contracts
 */

/**
 * 沙盒 Agent 的执行报告（设计 D1.4）：一次「写码→运行→读产物→修复」任务的可审计结论。验收以 assertions 为准（父节点给定的确定性校验），不接受模型自述成功；父节点从 metrics_source_artifact 读真实数字拼 StageOutput（数字冻结纪律的源头）。
 */
export interface SandboxRunReport {
  /**
   * passed=全部断言通过；failed=断言未全过或运行失败（明细见 assertions 与 attempts）。
   */
  status: "passed" | "failed";
  /**
   * 实际执行的运行轮数（R2 修复每轮计一次）。
   */
  attempts: number;
  /**
   * 最终版本代码的 artifact id（可复现入口）。
   */
  final_code_artifact: string;
  /**
   * 本次任务产出的全部 artifact id 列表；无产物为空列表。
   */
  produced_artifacts: string[];
  /**
   * 唯一指标来源的 artifact id（如 metrics.json）；无指标类任务（纯清洗/渲染）为 null。
   */
  metrics_source_artifact: string | null;
  /**
   * 验收断言逐条结果（断言由父节点给定）；空列表 = 父节点未给断言（仅以运行成功为准，须在消费方显式声明）。
   */
  assertions: AssertionResult[];
  /**
   * 本次运行使用的显式随机种子（名称 → 值），可复现性硬要求（§7.3）。
   */
  seeds: {
    [k: string]: number | string;
  };
  env_fingerprint: EnvFingerprint;
  usage: Usage;
}
export interface AssertionResult {
  /**
   * 断言标识（父节点任务卡中的编号）。
   */
  id: string;
  passed: boolean;
  /**
   * 断言的判定说明；失败时必须携带可定位的差异信息。
   */
  detail: string;
}
export interface EnvFingerprint {
  /**
   * 语言运行时（如 python）。
   */
  runtime: string;
  /**
   * 运行时版本号。
   */
  version: string;
  /**
   * 依赖清单的内容哈希；同指纹 + 同种子 + 同数据 = 指标应一致（浮点容差内）。
   */
  deps_hash: string;
}
export interface Usage {
  /**
   * 沙箱运行次数。
   */
  runs: number;
  /**
   * 本任务消耗的 LLM tokens。
   */
  tokens: number;
  duration_ms: number;
}
