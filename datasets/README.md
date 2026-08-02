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
