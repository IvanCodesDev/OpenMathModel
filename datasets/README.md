# Datasets

本目录管理数据集的**定义与血缘**，不把大型原始数据直接提交到仓库。

## 分区

- `catalog/`：数据集 manifest、许可、来源、schema、校验和与负责人。
- `samples/`：用于开发和测试的小型、可提交样例。
- `recipes/`：下载、生成、校验、清洗、特征处理和切分配方。
- `raw/`：原始不可变数据，仅本地/对象存储。
- `interim/`：处理中间结果，可随时重建。
- `processed/`：实验输入，必须能由 raw + recipe 重建。

## 推荐 manifest

```yaml
name: SAMPLE_DATASET
version: 1.0.0
source: SOURCE_URI
license: LICENSE_ID
sha256: SHA256
schema: catalog/SAMPLE_DATASET.schema.json
recipe: recipes/SAMPLE_DATASET.py
splits:
  train: OBJECT_URI
  validation: OBJECT_URI
```

规则：`raw/` 永不原地修改；任何处理结果都生成新版本；实验只引用确定版本和校验和。

## 官方赛题采集（Wave A）

首批来源、抓取边界、频率和许可状态集中维护在
[`catalog/source-registry.json`](./catalog/source-registry.json)。任何 Adapter 开始抓取前必须先通过注册表校验和
`robots.txt` 检查；许可未完成人工复核的内容只进入本地不可变原始层，不进入产品索引或再分发包。

```powershell
# 1. 校验来源台账
python datasets/recipes/collect_official_problems.py validate

# 2. 采集 3 个官方站点近 5 年的页面元数据（每个来源最多 25 页）
python datasets/recipes/collect_official_problems.py collect `
  --source all --years 2021:2025 --max-pages 25

# 3. 人工确认来源和许可后，按来源下载公开附件
python datasets/recipes/collect_official_problems.py collect `
  --source SOURCE_ID --years 2021:2025 --max-pages 100 --include-attachments

# 4. 从固定 Git commit 下载 2023–2024 年 A–F 完整赛题与随题附录，
#    按原文顺序提取段落、标题、列表、表格、公式文本和图片
python datasets/recipes/ingest_mathmodel_full_problems.py all

# 5. 将官方站点快照与完整题面规范化为前端赛题库/优秀论文数据
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  datasets/recipes/ingest_full_problem_archives.py all

# 6. 合并所有完整题面和论文索引
python datasets/recipes/build_knowledge_library.py
```

采集结果：

- `raw/objects/<sha256-prefix>/`：内容寻址对象，相同内容只保存一次。
- `raw/snapshots/<source>/<run-id>/manifest.json`：不可变运行清单、HTTP 校验器、来源链接和错误。
- `interim/<source>/http-state.json`：ETag/Last-Modified 与内容哈希状态，可重建。
- `interim/<source>/<run-id>-discovered-links.json`：待规范化的题目与附件候选链接。
- `raw/sources/github/zhanwen-MathModel/source-manifest.json`：17 份题面原文件的 Git blob、SHA-256 和固定 commit 清单。
- `interim/github_zhanwen_mathmodel/full-problems.json`：12 道完整赛题的原序结构化内容块。
- 同一 interim 清单还包含仓库内 2004–2023 年 685 份优秀论文 PDF 的逐篇结构化索引。
- `apps/web/public/problem-assets/<problem-id>/`：从 DOCX 提取并转换为浏览器可显示格式的题面插图。
- `apps/web/public/problem-figures/<problem-id>/`：从 PDF 原题提取的内嵌插图，不使用整页截图替代正文。
- `apps/web/public/problem-files/<problem-id>/`：可点击下载的原题 PDF，以及按赛题归档的随题数据附件包。
- PDF 正文从文本层转换为标题、段落和列表等结构化块；CUMCM 在前端统一显示为“国赛”。
- `apps/web/src/data/knowledge-library.json`：由 Recipe 生成、通过 Schema 校验的前端发布快照。

运行数据均被 `.gitignore` 排除；仓库只提交 Registry、Schema、Recipe 和小型可审查样例。
