# Prompts

保存有版本、输入 schema、输出 schema 和回归样例的提示模板。业务流程不能只存在于 prompt 文本中。

文件名 `<stage>.<variant>.prompt.md`，frontmatter 的 `id` 即注册表 id。沙盒消费方（清洗 / 实验 / 检验 / 论文补图）按实现语言分 variant：`<stage>.sandbox` 是 Python 卡，`<stage>.sandbox.r` 是 R 卡（ADR-0021）——同阶段、同占位符、同必填输出键，只有正文的语言纪律不同；节点按任务卡 `language` 选 variant。三个出图消费方的卡片都带「出图规范（论文级图件，硬性）」一节：结论先行、统一样式（无头后端 / 300 dpi / 去脊 / 字号下限）、受控配色、不确定性编码、图件统一落 `figures/`。
