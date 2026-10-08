import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { URL } from "node:url";
import ts from "typescript";

const source = await readFile(new URL("./paper-document.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { SOFT_BREAK, TABLE_CAPTION, allowInlineMathBreaks, insertSoftBreaks, normalizeInlines, stripControlChars, tableLayout, unbreakableWidth } = await import(
  `data:text/javascript;charset=utf-8,${encodeURIComponent(outputText)}`
);

const text = (value, marks = {}) => ({ kind: "text", text: value, marks });
const cell = (value, header = false) => ({ inlines: [text(value)], header });
const shown = value => value.replaceAll(SOFT_BREAK, "|");

test("long latin tokens get break points after separators; short tokens and plain numbers stay intact", () => {
  const separatorsOnly = value => shown(insertSoftBreaks(value, { letters: false }));
  assert.equal(
    separatorsOnly("MLP Adam metrics.test_acc_iris_mlp_adam=0.99166667 低于 logreg"),
    "MLP Adam metrics.|test_|acc_|iris_|mlp_|adam=|0.99166667 低于 logreg",
    "小数点后接数字不断，字母前的点、下划线、等号后可断",
  );
  assert.equal(insertSoftBreaks("误差 5.9e-11、范数 0.99166667"), "误差 5.9e-11、范数 0.99166667", "短数字不拆");
  assert.equal(shown(insertSoftBreaks("误差 5.900580024587043e-11、")), "误差 5.9005800245|87043e-11、", "16 字以上连续没有断点的片段每 12 字给一个断点");
  assert.equal(separatorsOnly("哈希 0123456789abcdef0123456789abcdef"), "哈希 0123456789ab|cdef01234567|89abcdef");
  assert.equal(
    separatorsOnly("metrics.grad_check_max_normwise_rel=2.1052011315375754e-10"),
    "metrics.|grad_|check_|max_|normwise_|rel=|2.1052011315|375754e-10",
    "分隔符切完后仍过长的片段同样补断点",
  );
  assert.equal(separatorsOnly("https://example.org/a/b/c"), "https:|/|/|example.|org/|a/|b/|c");
  assert.equal(insertSoftBreaks("中文段落，没有长串。"), "中文段落，没有长串。");
});

test("inside long tokens letters may break too, keeping at least two letters on each side; digits never split", () => {
  assert.equal(
    shown(insertSoftBreaks("MLP Adam metrics.test_acc_iris_mlp_adam=0.99166667 低于 logreg")),
    "MLP Adam me|t|r|i|cs.|te|st_|acc_|ir|is_|mlp_|ad|am=|0.99166667 低于 logreg",
    "三个字母以内的串不拆；独立的短单词（Adam、logreg）不受影响",
  );
  assert.equal(shown(insertSoftBreaks("gain_over_knn5_pp_synth_circles=31.555555")), "ga|in_|ov|er_|knn5_|pp_|sy|n|th_|ci|r|c|l|es=|31.555555");
  assert.equal(
    shown(insertSoftBreaks("哈希 0123456789abcdef0123456789abcdef")),
    "哈希 0123456789ab|c|d|ef0123456789ab|c|d|ef",
    "字母给了断点后，剩下的数字段不到 16 字就不再补",
  );
});

test("inline math may break after top-level commas only", () => {
  assert.equal(
    allowInlineMathBreaks("\\theta=(W^{(1)},b^{(1)},\\ldots,b^{(L)})"),
    "\\theta=(W^{(1)},\\allowbreak b^{(1)},\\allowbreak \\ldots,\\allowbreak b^{(L)})",
  );
  assert.equal(allowInlineMathBreaks("x_{i,j}+\\text{a, b}"), "x_{i,j}+\\text{a, b}", "花括号里的逗号不动");
  assert.equal(allowInlineMathBreaks("a\\,b,"), "a\\,b,", "\\, 是命令；结尾的逗号后面没东西可断");
  assert.equal(allowInlineMathBreaks("a,\\allowbreak b"), "a,\\allowbreak b", "已有 \\allowbreak 不重复");
});

test("table layout: narrow tables stay centred, wide ones wrap their long text columns", () => {
  const narrow = tableLayout([[cell("方案", true), cell("成本", true)], [cell("A"), cell("低")]]);
  assert.deepEqual([narrow.columns, narrow.headCount, narrow.wrap], [2, 1, [false, false]]);
  const long = "网络待估参数集合，包含所有权重与偏置，后文的前向传播、反向传播与梯度检验均以此为准";
  const wide = tableLayout([[cell("符号", true), cell("含义", true), cell("单位", true)], [cell("θ"), cell(long), cell("无量纲")]]);
  assert.deepEqual(wide.wrap, [false, true, false]);
  const noHeader = tableLayout([[cell("a"), cell("b")]]);
  assert.equal(noHeader.headCount, 0);
});

test("unbreakable width: latin runs without spaces count whole, CJK characters break individually", () => {
  const near = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 1e-9, `${message}：${actual} ≠ ${expected}`);
  near(unbreakableWidth([text("指标 milp_binary_vars_core 偏大")]), 21 * 0.55, "标识符整串");
  near(unbreakableWidth([text("状态与影响")]), 1, "汉字之间可断");
  near(unbreakableWidth([text("第2406小时")]), 4 * 0.55, "汉字与数字之间可断");
  near(unbreakableWidth([text(`data${SOFT_BREAK}_source`)]), 11 * 0.55, "零宽空格不算断点");
  near(unbreakableWidth([text("5\u00a0MW")]), 4 * 0.55, "不换行空格不算断点");
  near(unbreakableWidth([text("ab"), { kind: "break" }, text("cde")]), 3 * 0.55, "换行处断开");
  near(unbreakableWidth([]), 0, "空格子");
});

test("inline normalisation merges same-format text and trims leading / trailing breaks and blanks", () => {
  const out = normalizeInlines([
    { kind: "break" }, text("  前"), text("文"), text("加粗", { bold: true }), { kind: "math", tex: "x" }, text("尾  "), { kind: "break" },
  ]);
  assert.deepEqual(out, [text("前文"), text("加粗", { bold: true }), { kind: "math", tex: "x" }, text("尾")]);
});

test("control characters, U+FFFE and lone surrogates are stripped; tabs, newlines and emoji survive", () => {
  assert.equal(stripControlChars("a\u0001b\u0008c\td\ne\uFFFEf\uD800g😀"), "abc\td\nefg😀");
});

test("table captions are recognised only when they look like 表 N / Table N", () => {
  for (const caption of ["表 1 全文符号说明", "表2：结果", "续表 3 参数", "Table 4 Results", "表 2.1 灵敏度"]) assert.ok(TABLE_CAPTION.test(caption), caption);
  for (const plain of ["表示区域 i 的需求", "表格如下", "Tables are"]) assert.ok(!TABLE_CAPTION.test(plain), plain);
});
