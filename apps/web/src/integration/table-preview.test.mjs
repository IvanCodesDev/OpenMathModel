import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./table-preview.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { delimiterLabel, describeRawData, formatSize, inputRows, isPreviewableName, previewTable, shortHash } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

/** 契约 fixture dataset-profile.2：两个输入（csv 已下发可下载；xlsx 未下发、无哈希、不可下载）。 */
const fixture = JSON.parse(
  await readFile(new URL("../../../../packages/contracts/fixtures/v1/valid/dataset-profile.2.json", import.meta.url), "utf8"),
);
const legacy = JSON.parse(
  await readFile(new URL("../../../../packages/contracts/fixtures/v1/valid/dataset-profile.1.json", import.meta.url), "utf8"),
);

test("describeRawData: inputs → rows with size / hash / download / staged / previewable", () => {
  const view = describeRawData(fixture);
  assert.equal(view.kind, "inputs");
  assert.deepEqual([view.total, view.staged, view.previewable], [2, 1, 1]);
  assert.deepEqual(view.rows, [
    { artifactId: "art_0a1b2c3d4e5f6071", name: "orders.csv", size: "51.3 KB", hash: "7f83b1657ff1…", downloadUrl: "/api/v1/artifacts/art_0a1b2c3d4e5f6071/download", staged: true, previewable: true },
    { artifactId: "art_0a1b2c3d4e5f6072", name: "stations.xlsx", size: "17.9 KB", hash: "—", downloadUrl: null, staged: false, previewable: false },
  ]);
});

test("describeRawData: empty vs absent; previewable needs both a download url and a delimited-text name", () => {
  assert.deepEqual(describeRawData({ ...fixture, inputs: [] }), { kind: "empty" });
  assert.deepEqual(describeRawData(legacy), { kind: "absent" });
  assert.equal(isPreviewableName("a.csv", "text/csv"), true);
  assert.equal(isPreviewableName("a.CSV", "application/vnd.ms-excel"), true, "Windows mimetypes 给 csv 的登记值");
  assert.equal(isPreviewableName("a.tsv", ""), true);
  assert.equal(isPreviewableName("a.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), false);
  assert.equal(isPreviewableName("a.csv", "image/png"), false);
  const rows = inputRows([{ artifact_id: "art_x", name: "raw.csv", media_type: "text/csv", size_bytes: null, sha256: null, download_url: null, staged: false }]);
  assert.equal(rows[0].previewable, false, "不可下载就不请求预览");
  assert.deepEqual([rows[0].size, rows[0].hash], ["—", "—"]);
  assert.deepEqual(inputRows(null), []);
});

test("previewTable: aligns row width to the header and keeps counts", () => {
  const table = previewTable({
    artifact_id: "art_x", name: "orders.csv", media_type: "text/csv", size_bytes: 10, sha256: null,
    encoding: "utf-8-sig", delimiter: ",", columns: ["a", "b", "c"],
    rows: [["1", "2", "3"], ["4", "5"], ["6", "7", "8", "9"]], row_count: 120, truncated: true,
  });
  assert.deepEqual(table.columns, ["a", "b", "c"]);
  assert.deepEqual(table.rows, [["1", "2", "3"], ["4", "5", ""], ["6", "7", "8"]]);
  assert.deepEqual([table.shown, table.rowCount, table.truncated, table.encoding], [3, 120, true, "utf-8-sig"]);
  const headless = previewTable({
    artifact_id: "art_y", name: "x.csv", media_type: "text/csv", size_bytes: 0, sha256: null,
    encoding: "utf-8-sig", delimiter: "\t", columns: [], rows: [["only"]], row_count: null, truncated: false,
  });
  assert.deepEqual(headless.rows, [["only"]]);
  assert.equal(headless.rowCount, null);
});

test("helpers: sizes, hashes, delimiter labels", () => {
  assert.equal(formatSize(0), "0 B");
  assert.equal(formatSize(52480), "51.3 KB");
  assert.equal(formatSize(3 * 1024 * 1024), "3.0 MB");
  assert.equal(formatSize(undefined), "—");
  assert.equal(shortHash("7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069"), "7f83b1657ff1…");
  assert.deepEqual([",", ";", "\t", "|", "~"].map(delimiterLabel), ["逗号", "分号", "制表符", "竖线", "~"]);
});
