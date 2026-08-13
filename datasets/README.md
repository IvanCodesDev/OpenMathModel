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

# 5. 暂存主办方发布的赛题归档（校验和固定后再解压）。
#    华数杯按场次发布：historical 覆盖 2020–2025，此后每届单独一个包，
#    赛事结束后才收录；省略 --edition 表示处理全部场次。
python datasets/recipes/stage_huashu_cup_archive.py all
python datasets/recipes/stage_mathorcup_archives.py all

#    泰迪杯按题发布单个 PDF 而不是整届压缩包，逐份固定字节数与 SHA-256；
#    2017、2018 详情页已失效，2021 年起迁至 BdRace 平台且题面需注册后下载，
#    这两段缺口记录在来源注册表里，不从镜像补齐。
python datasets/recipes/stage_tipdm_cup_statements.py all

#    2021 年起泰迪杯迁到主办方自建平台，赛题以富文本经公开接口发布。
#    快照全部 21 道并固定校验和；其中 20 道正文被平台截断（省略号或公众号引流），
#    只有未截断的才会进入赛题库。
python datasets/recipes/stage_tipdm_bdrace_statements.py all

#    电工杯 18 届题面全部走 download.jsp 的人机验证，只能采集元数据；
#    该脚本记录官方标题、通知页与稳定文件 id，并拒绝把验证页当作压缩包。
python datasets/recipes/discover_electric_cup.py discover

#    统计建模大赛不出赛题：每届只发布一个主题（2021 年前为选题类别与要求），
#    参赛队自拟题目。官网通知的主题小节即该赛事发布的全部"题面"，
#    逐届快照官网通知并固定校验和，入库时只取主题/选题小节。
python datasets/recipes/stage_tjjmds_notices.py all

# 6. 将官方站点快照与完整题面规范化为前端赛题库/优秀论文数据
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  datasets/recipes/ingest_full_problem_archives.py all

# 7. 合并所有完整题面和论文索引
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
- `datasets/raw/sources/full-problem-archives/tipdm-cup/manifest.json`：泰迪杯 4 届 11 份题面的固定字节数与 SHA-256。
- `datasets/raw/sources/full-problem-archives/tipdm-cup-bdrace/manifest.json`：泰迪杯平台接口的 8 届 21 道快照，逐题标注正文是否完整。
- PDF 之外的富文本题面由 `recipes/pdf_layout.py` 的同级模块 `recipes/html_layout.py` 转成同一套结构化块。
- `datasets/raw/sources/full-problem-archives/electric-cup/manifest.json`：电工杯 18 届 36 道题的官方标题与附件 id；附件本身不可采集，不进入赛题库。
- `datasets/raw/sources/full-problem-archives/tjjmds/manifest.json`：统计建模大赛 8 届官网通知快照与提取出的正文，各届主题在 recipe 中固定并逐次核对。
- PDF 正文从文本层转换为标题、段落和列表等结构化块；CUMCM 在前端统一显示为“国赛”。
- `apps/web/src/data/knowledge-library.json`：由 Recipe 生成、通过 Schema 校验的前端发布快照。

运行数据均被 `.gitignore` 排除；仓库只提交 Registry、Schema、Recipe 和小型可审查样例。
