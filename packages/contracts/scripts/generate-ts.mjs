/**
 * 从 schemas/<version>/*.schema.json 生成 TypeScript 类型（确定性输出）。
 *
 * 用法：
 *   node scripts/generate-ts.mjs           # 生成/覆盖 src/ts/<version>/
 *   node scripts/generate-ts.mjs --check   # 只比对不落盘，不一致时退出码 1（CI 用）
 */
import { mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "json-schema-to-typescript";

const VERSION = "v1";
const pkgRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const schemaDir = path.join(pkgRoot, "schemas", VERSION);
const outDir = path.join(pkgRoot, "src", "ts", VERSION);
const checkMode = process.argv.includes("--check");

const banner = [
  "/* eslint-disable */",
  "/**",
  ` * 本文件由 scripts/generate-ts.mjs 从 schemas/${VERSION} 生成，禁止手改。`,
  " * 重新生成：npm run generate --workspace @openmathmodel/contracts",
  " */",
].join("\n");

const schemaFiles = (await readdir(schemaDir))
  .filter((f) => f.endsWith(".schema.json"))
  .sort();
if (schemaFiles.length === 0) {
  console.error(`no schemas found under ${schemaDir}`);
  process.exit(1);
}

/** @type {Map<string, string>} 文件名 -> 期望内容 */
const outputs = new Map();
/** @type {Array<{key: string, title: string}>} 桶文件显式导出各 Schema 主类型，避免共享 $defs 别名撞名 */
const mainTypes = [];
for (const file of schemaFiles) {
  const key = file.slice(0, -".schema.json".length);
  const schema = JSON.parse(await readFile(path.join(schemaDir, file), "utf8"));
  const title = schema.title ?? key;
  const ts = await compile(schema, title, {
    bannerComment: banner,
    additionalProperties: false,
  });
  outputs.set(`${key}.ts`, ts);
  mainTypes.push({ key, title });
}

const barrel =
  banner +
  "\n\n" +
  mainTypes.map(({ key, title }) => `export type { ${title} } from "./${key}";`).join("\n") +
  "\n";
outputs.set("index.ts", barrel);

if (checkMode) {
  const problems = [];
  for (const [name, expected] of outputs) {
    const target = path.join(outDir, name);
    if (!existsSync(target)) {
      problems.push(`missing: src/ts/${VERSION}/${name}`);
      continue;
    }
    const actual = await readFile(target, "utf8");
    if (actual !== expected) problems.push(`stale: src/ts/${VERSION}/${name}`);
  }
  if (existsSync(outDir)) {
    for (const name of (await readdir(outDir)).sort()) {
      if (!outputs.has(name)) problems.push(`extraneous: src/ts/${VERSION}/${name}`);
    }
  }
  if (problems.length > 0) {
    console.error("CONTRACTS_TS_STALE 生成物与 Schema 不同步，请运行 npm run generate --workspace @openmathmodel/contracts");
    for (const p of problems) console.error(`  - ${p}`);
    process.exit(1);
  }
  console.log(`CONTRACTS_TS_OK {"files":${outputs.size},"version":"${VERSION}"}`);
  process.exit(0);
}

await mkdir(outDir, { recursive: true });
for (const name of (await readdir(outDir)).sort()) {
  if (!outputs.has(name)) await rm(path.join(outDir, name));
}
for (const [name, content] of outputs) {
  await writeFile(path.join(outDir, name), content, "utf8");
}
console.log(`CONTRACTS_TS_GENERATED {"files":${outputs.size},"version":"${VERSION}"}`);
