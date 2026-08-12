# Wave A 结构化赛题与附件下载验证记录

> **历史证据快照（2026-08-04）。** 本文记录 Wave A 当日输入、命令、输出和哈希；当前数据目录与产品状态请以 [`datasets/README.md`](../../../datasets/README.md) 和[文档总览](../../README.md)为准。

日期：2026-08-04  
工作区：`E:\Projects\opensource\OpenMathModel`

## 修改目标

1. PDF 赛题正文不得以整页截图替代，必须转换为前端可选择、可搜索的标题、段落和列表。
2. 赛题原题及随题数据必须在详情页提供本地点击下载。
3. CUMCM 前端分类统一显示为“国赛”。

## 数据结果

| 项目 | 结果 |
|---|---:|
| 前端完整赛题 | 86 |
| COMAP MCM/ICM | 30 |
| APMCM | 19 |
| 国赛 CUMCM | 25 |
| 研究生赛 | 12 |
| PDF 源页数 | 220 |
| PDF 结构化文本块 | 1,653 |
| PDF 独立插图 | 127 |
| PDF 赛题下载项 | 103 |
| 含附件包的赛题 | 29 |
| 本地下载文件总字节 | 408,329,177 |
| DOCX 原序内容块 | 1,100 |
| DOCX 图片 | 62 |
| PDF 整页截图块 | 0 |

PDF 正文共 365,681 个字符，DOCX 正文共 77,766 个字符。所有 74 道 PDF 赛题都提供本地原题 PDF；有数据文件的题目另提供逐题 ZIP，保留原附件目录和文件名。

## 数据重建与验证

执行：

```powershell
& 'C:\Users\黄一帆\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' datasets\recipes\ingest_full_problem_archives.py all
python datasets\recipes\build_knowledge_library.py
powershell -ExecutionPolicy Bypass -File tools\verify-data-collection.ps1 -RequireRuntimeData
```

关键字面输出（退出状态 `0`）：

```text
FULL_ARCHIVE_PROBLEMS_VERIFY_OK {"problem_count": 74, "comap_count": 30, "apmcm_count": 19, "cumcm_count": 25, "page_count": 220, "text_block_count": 1653, "figure_count": 127, "attachment_count": 103, "download_bytes": 408329177, "content_character_count": 365681}
MATHMODEL_FULL_PROBLEMS_VERIFY_OK {"documents": 17, "problem_statements": 12, "supporting_documents": 5, "bytes": 6005142, "problem_count": 12, "document_count": 17, "content_block_count": 1100, "content_character_count": 77766, "asset_count": 62, "asset_bytes": 5100338, "paper_inventory_count": 685, "commit": "cd5be91735ebf11d5ee52eb170e86a6d07131977"}
{"output": "artifacts\\data-collection-wave-a\\verification-library.json", "problem_count": 86, "paper_count": 906, "source_count": 5, "dataset_version": "wave-a-f6479b485e0b"}
DATA_COLLECTION_VERIFY_OK {"manifests":10,"objects":55,"bytes":1214964,"discovered_links":427,"errors":0,"orphan_objects":0,"full_problem_documents":17,"full_problem_bytes":6005142,"full_problem_pages":220,"full_problem_text_blocks":1653,"full_problem_figures":127,"full_problem_downloads":103,"full_problem_download_bytes":408329177}
KNOWLEDGE_LIBRARY_VERIFY_OK {"problems":86,"papers":906,"sources":5,"version":"wave-a-f6479b485e0b"}
```

验证器逐项检查：

- 86/86 条赛题均为完整题面，来源数量精确匹配 30/19/25/12。
- 74 道 PDF 赛题全部为 `structured_text`，不存在 `page` 截图块。
- 每道 PDF 赛题恰有一个本地原题下载；本地文件字节数和 SHA-256 与数据记录一致。
- CUMCM 的 25 条前端分类全部为“国赛”。
- 前端源码不存在整页截图渲染分支，并包含附件下载组件。
- 结构化知识库可确定性重建，运行清单无采集错误、无孤儿对象。

附件包抽样执行 ZIP CRC 校验：

```text
cumcm-2025-a zip_entries=3 test=None
cumcm-2024-c zip_entries=5 test=None
apmcm-2024-a zip_entries=1 test=None
```

`test=None` 表示 ZIP 内文件 CRC 全部通过。

## 生产构建

命令：

```powershell
cd apps\web
npm run build
```

字面结果（退出状态 `0`）：

```text
69 modules transformed
dist/index.html                  0.74 kB
dist/assets/index-CWgF5Rlg.css 115.90 kB
dist/assets/index-Io0xQMVm.js  2,208.96 kB
build exit 0
```

Vite 仅报告既有的大型 JS chunk 性能提示。

## 浏览器行为验证

真实浏览器加载 `/library/problems`：

```text
rows=86
tabs=[全部赛题, 国赛, 美赛, 亚太赛, 研究生赛, 收藏]
bodyHasOldName=false
bodyHasNewName=true
```

加载 `/library/problems/detail?index=0`（2025 CUMCM A）：

```text
title="题目：烟幕干扰弹的投放策略"
paragraphCount=7 headingCount=6
pageScreenshotCount=0
downloadCount=2
download[0]="/problem-files/cumcm-2025-a/problem.pdf" download=true
download[1]="/problem-files/cumcm-2025-a/attachments.zip" download=true
textSample="2025 年高教社杯全国大学生数学建模竞赛题目……"
```

实际点击原题链接后浏览器产生下载事件：

```text
pdfLinkCount=1 downloadTriggered=true
```

加载 `/library/problems/detail?index=11`（2024 APMCM A）：

```text
paragraphs=11 headings=3 figures=6 screenshots=0
downloads=[/problem-files/apmcm-2024-a/problem.pdf, /problem-files/apmcm-2024-a/attachments.zip]
```

## 回滚验证

更新后的回滚脚本在独立夹具中带 `-IncludeRuntimeData` 实际执行，退出状态 `0`：

```text
Rolled back Wave A files. Runtime data removed: True
{"readme_restored":true,"frontend_restored":true,"styles_restored":true,"problem_assets_removed":true,"problem_pages_removed":true,"problem_figures_removed":true,"problem_files_removed":true,"runtime_removed":true}
```

工作区回滚命令：

```powershell
& .\tools\rollback-data-collection-wave-a.ps1
& .\tools\rollback-data-collection-wave-a.ps1 -IncludeRuntimeData
```

## 交付角色与哈希

| 角色 | 绝对路径 | SHA-256 / 规模 |
|---|---|---|
| PDF 结构化解析和附件打包 | `E:\Projects\opensource\OpenMathModel\datasets\recipes\ingest_full_problem_archives.py` | `67673786d792c1a1573db82bf4001f9ffd37662efa95caa94ce5eeefa066bbee` |
| 前端结构化数据 | `E:\Projects\opensource\OpenMathModel\apps\web\src\data\knowledge-library.json` | `8a8df049b87c3f0f1d1958c1edb1e5b109137ee8c367454525706fed159c55a1` |
| 前端渲染 | `E:\Projects\opensource\OpenMathModel\apps\web\src\legacy\openmathmodel-ui.ts` | `adf3ef13e0831ad39e00c713b108885c1d918ac49e3ef0b786b1a9bada315391` |
| 前端样式 | `E:\Projects\opensource\OpenMathModel\apps\web\src\styles.css` | `c24723b8aba134b220671609edf45c6d52f55689c7f83a619aa68e3580828f18` |
| PDF 独立插图 | `E:\Projects\opensource\OpenMathModel\apps\web\public\problem-figures` | 127 个文件，19,024,968 字节 |
| 本地题目和附件下载 | `E:\Projects\opensource\OpenMathModel\apps\web\public\problem-files` | 103 个文件，408,329,177 字节 |
| Patch | `E:\Projects\opensource\OpenMathModel\docs\implementation\data-collection\wave-a.patch` | `02f0c5f6652d89099dc271cd17d569f8246a27527dc14f64f89485f2a7d072f6` |
| 验证记录 | `E:\Projects\opensource\OpenMathModel\docs\implementation\data-collection\verification-record.md` | 交付时计算 |
| 回滚脚本 | `E:\Projects\opensource\OpenMathModel\tools\rollback-data-collection-wave-a.ps1` | `7df4819894e1187e730726179357d98772495ba6ea74cb55f24629023643cea8` |

Patch 为代码、Schema、结构化 JSON 和文档差异；体积较大的本地下载文件作为独立交付目录保存，由采集 Recipe 可重复生成。
