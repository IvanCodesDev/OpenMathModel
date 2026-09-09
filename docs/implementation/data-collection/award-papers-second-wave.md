# 第二批优秀论文采集记录（国赛 / 泰迪杯 / MathorCup / 亚太赛 / 华数杯）

日期：2026-09-08　工作区：`E:\Projects\opensource\OpenMathModel`

## 采集目标

优秀论文页此前只有研究生赛（华为杯）与美赛两类论文。本批把国赛、统计建模、MathorCup、
亚太赛、泰迪杯、华数杯六个赛事纳入，逐赛事确认「主办方是否公开发布论文全文」，只收公开可得
的部分，缺口如实记录，不用二手转载或付费站点凑数。

## 来源判定

| 赛事 | 主办方是否公开论文全文 | 采用来源 | 论文数 |
|---|---|---|---:|
| 国赛 CUMCM | 否（只在知网竞赛专栏与纸质《优秀论文选》发布） | 固定 commit 的社区仓库快照 `personqianduixue/Math_Model@8783d0d` | 283 |
| 泰迪杯 | 是，官网「优秀作品」逐篇挂全文 PDF | `tdb.tipdm.net/tzsdt/` + `www.tipdm.org/u/cms/www/*.pdf` | 95 |
| MathorCup | 是，主办方在报名平台发布《历年优秀论文》整包 | 赛氪公告 `/c/nd/39400` 的 RAR 附件 | 50 |
| 亚太赛 APMCM | 是，同上，英文赛项与中文赛项各一包 | 赛氪公告 `/c/nd/33722`、`/c/nd/31675` | 24 |
| 华数杯 | 是，同上 | 赛氪公告 `/c/nd/44347` 的 ZIP 附件 | 18 |
| 统计建模 | **否** | —— | 0 |

统计建模大赛的获奖论文由中国统计出版社逐届出版《获奖论文选》并被知网收录，官网 `dstz` 栏目只发
主题通知与获奖名单，没有任何可下载的论文全文。该赛事因此只有赛题（每届主题）在库，缺口记录在
`datasets/catalog/source-registry.json` 的 `tjjmds_official.notes` 与 `datasets/README.md`。

## 采集与解析

```powershell
python datasets/recipes/ingest_cumcm_paper_fulltext.py all
python datasets/recipes/stage_award_paper_bundles.py all
python datasets/recipes/stage_tipdm_award_papers.py all
python datasets/recipes/build_knowledge_library.py
```

关键输出（退出状态 `0`）：

```text
CUMCM_PAPER_TITLE_RATIO 81%
CUMCM_PAPER_FULLTEXT_VERIFY_OK {"target_count": 283, "paper_count": 283, "skipped_count": 0, "with_title": 228, ...}
AWARD_PAPER_BUNDLES_VERIFY_OK {"paper_count": 92, "with_title": 79, "with_keywords": 87, "with_models": 82, "skipped_count": 0, ...}
TIPDM_AWARD_PAPERS_VERIFY_OK {"paper_count": 95, "with_title": 95, "with_award_rank": 52, "with_institution": 62, "with_keywords": 80, ...}
AWARD_PAPERS_APPENDED 470
{"problem_count": 189, "paper_count": 1589, "source_count": 11, "dataset_version": "wave-a-983666fe188c"}
```

- 每个压缩包按公告里的字节数与 SHA-256 固定；每篇 PDF 落盘后再记一次 SHA-256（社区仓库快照记 Git blob 哈希）。
- 封面解析统一走 `datasets/recipes/paper_covers.py`：题名、参赛编号、院校、摘要、关键词、方法标签。
- **不解析姓名**。泰迪杯封面同页印着作品成员与指导老师，国赛社区快照里有以「编号_姓名_姓名」命名的
  文件——前者不读，后者整份跳过，也不进本地路径与发布字段。
- 2019—2020 两届国赛的仓库文件是无文本层的扫描件，读不出题名；这批论文在阅读器里照常可读，
  题名回落到赛题标题，识别率只报不卡（当前 81%）。

## 发布与呈现

- 论文记录新增 `local_pdf_path`（`/paper-files/archive/<赛事>/<年份>/<题组>/<编号>.pdf`）与
  `source_sha256`、`page_count`，schema 同步更新。
- 开发服务器新增 `/paper-files/archive/…` 路由，从 `datasets/raw/sources/paper-archives/` 读取；
  合集类论文没有单篇公网地址，因此该路由只读磁盘、不按需回源，缺文件时明确提示先跑采集脚本。
- 优秀论文页比赛页签固定顺序为 国赛 → 美赛 → 研究生赛，其后按数据出现顺序排 泰迪杯、MathorCup、
  亚太赛、华数杯；奖项页签新增「优秀作品」与泰迪杯的特等/一等/二等奖（企业冠名等完整表述保留在
  `distinctions`）。
- 论文与赛题按 `problem_id` 挂接，仅当赛题库真的收录该题时才挂：国赛论文可追到 1992 年，而完整
  题面从 2015 年起才有。

## 验收

| 项目 | 结果 |
|---|---|
| `npm run check --workspace @openmathmodel/web` | 通过 |
| `npm run build --workspace @openmathmodel/web` | 通过 |
| `python datasets/recipes/collect_official_problems.py validate` | `{"valid": true, "sources": 11}` |
| 论文页可列出条目 | 1,558（新增 470） |
| `/paper-files/archive/…` 抽样 | 六个赛事各取一篇均返回 `200 application/pdf`；越界路径、缺失文件、大小写不符的赛事名均返回 404 |

## 第二轮：补近几届（2026-09-08 追加）

首轮的年份分布有明显缺口——国赛停在 2020、华为杯停在 2023。逐个赛事复核后：

| 缺口 | 复核结果 | 处理 |
|---|---|---|
| 国赛 2021–2024 | 官网仍只发赛题；`cmathc.org.cn` 的 2010–2025 汇总是百度网盘链接（需登录、有提取码），不可自动化 | 取 `yan-fanyu/CUMCM-Paper-And-SourceCode@d58f5678` 的逐题论文，12 篇 |
| 国赛 2025 | 同上，且当届论文尚无任何合集 | 取 5 篇参赛队自公开论文（全国一等奖 1、全国二等奖 1、省级一等奖 3） |
| 华为杯 2024–2025 | 研创网只有获奖名单与一份答辩 PPT（kdocs 单文件）；社区合集 `zhanwen/MathModel` 的 `国赛论文/` 停在 2023 年 | 取 2 篇参赛队自公开论文（全国二等奖 1、华为专项二等奖 1） |
| 泰迪杯 2024–2025（第十二、十三届） | 官网优秀作品栏目 `tdb.tipdm.net/tzsdt/` 最新一条仍是第十一届；`/dsej12/`、`/dssj13/` 两个频道只挂获奖名单 PDF 与颁奖会通知 | **未采集**，官方尚未发布全文 |
| 亚太赛 2025（第十五届） | 赛氪上最新的「历年优秀论文」包（2025-09-17）与中文赛项包（2025-06-06）都只到 2024 年；2026 周期只发了获奖名单，组委会明确当届论文经 QQ 群文件/登录后课程分发 | **未采集**，无公开直链 |

追加执行：

```powershell
python datasets/recipes/ingest_community_award_papers.py all
python datasets/recipes/build_knowledge_library.py
```

```text
COMMUNITY_AWARD_PAPERS_VERIFY_OK {"entry_count": 19, "paper_count": 19, "skipped_count": 0, "with_title": 14, "years": [2021, 2022, 2023, 2024, 2025]}
{"problem_count": 189, "paper_count": 1608, "source_count": 12, "dataset_version": "wave-a-3b46d81a833a"}
```

补齐后的年份分布：国赛 1992–2025（2021–2024 各 3 篇、2025 共 5 篇），华为杯 2004–2025
（2024、2025 各 1 篇）。这 19 篇的 `award` 一律照来源自述登记，来源自己没写明奖项的仓库不收；
`source_url` 固定到 commit，`source_sha256` 随发布数据一起提交。

## 第三轮：修中文论文渲染白页（2026-09-08 追加）

现象：部分论文在阅读器里只显示数字、拉丁字母与公式，中文整片消失。用 pdf.js 在 Node 侧
复现，警告说得很直白：

```text
Warning: loadFont - translateFont failed: "UnknownErrorException:
Ensure that the `cMapUrl` and `cMapPacked` API parameters are provided."
```

LaTeX 排版的中文论文（Fandol 等 CID 字体）取字形要靠预定义 CMap，Word 导出的论文里
未内嵌的 Times/Helvetica 要靠标准字体数据；阅读器此前调 `getDocument({ url })` 时两样都没给，
pdf.js 只能放弃这些字体，于是中文位置留白。两份资源本来就随 `pdfjs-dist` 发布：

- `apps/web/vite.config.ts`：开发期把 `/pdfjs/cmaps/`、`/pdfjs/standard_fonts/` 指向
  `node_modules/pdfjs-dist`（名字不合法直接 404，避免 SPA 兜底把 index.html 当字体喂给 pdf.js）；
  构建期通过 `generateBundle` 把 169 个 `.bcmap` 与 16 个 `.pfb` 原样发到 `dist/pdfjs/`。
- `apps/web/src/legacy/openmathmodel-ui.ts`：`getDocument` 补上 `cMapUrl`、`cMapPacked: true`、
  `standardFontDataUrl`，基址取 `import.meta.env.BASE_URL`。

影响面实测（在 489 篇本地论文里等距抽 62 篇，逐篇跑 `getOperatorList` 统计字体告警）：

```text
{"sampled": 62, "ofTotalStaged": 489, "brokenWithoutCMaps": 37, "stillBrokenWithCMaps": 0}
```

即约六成中文论文此前都会掉字，补上资源后样本内告警清零。

## 已知缺口

1. 统计建模大赛无公开论文全文（见上）。
2. 泰迪杯第三届 C4 的目录条目没有全文链接，记入 `missing`，不补第三方转载；第十二、十三届
   （2024、2025）官方尚未发布优秀作品全文，只有获奖名单。
3. 亚太赛 2025 年（第十五届）的优秀论文未进入任何公开合集，组委会经 QQ 群文件与登录后课程分发。
4. 国赛 2014 年及部分 2013 年论文因文件名含参赛队员姓名被整体跳过。
5. 合集类论文（华数杯 / 亚太赛 / MathorCup）在生产构建下没有单篇公网地址，需先在本地跑采集脚本
   才能内嵌阅读；泰迪杯、国赛与华为杯论文另有主办方或镜像直链可回源。
6. 国赛 2021–2025 与华为杯 2024–2025 的 19 篇来自参赛队自公开仓库，属社区来源
   （`source_status = community_repository_snapshot`），不是组委会发布的官方优秀论文集。
