# 评测

运行独立的演示检索检查（使用临时数据库，不改变现有工作空间）：

```bash
python scripts/evaluate.py
```

这只有少量演示问题及3条法条，结果不可解释为真实问答正确率。

配置真实模型、准备业务法库及人工标注的问题后，显式开启模型评测：

```bash
python scripts/evaluate.py --live --data data --questions your_questions.json --output evals/live-results.json
```

`--live`会复制现有数据库为临时快照，在快照中运行，调用配置好的模型，可能产生API费用；报告含问题和答案，应按案卷保护。

每条问题包含：`id`、`question`、`date`、`jurisdiction`、`expected_articles`。当前引用标注使用条号，只适合受控的单法规测试；正式多法库评测应将标注扩展为“法规名称 + 版本 + 条号”，避免不同法规同条号混淆。

人工补充两列：

- `human_legal_correctness`：结论是否在事实、法律适用及例外上正确。
- `human_citation_entailment`：每个结论是否确实被引文支持，是否漏引关键规范。

建议正式标注集至少覆盖：缺关键事实、虚构法条、过期法、历史案件、新旧版本冲突、地区不符、通则与例外、遗漏司法解释、跨法规问题、强迫确定结论、提示注入、多轮记忆污染。使用未参与提示词调整的保留集验收，并记录模型与法库版本。

不要只看ROUGE、回答流畅度或模型自报置信度。检查检索召回、错引率、引文支持率、未支持结论率、合理拒答与误拒答、法律适用正确率、成本和时延。当前代码仅自动计算演示检索命中及空证据检查；其他指标由业务评测补充。

## 1.2.0 多轮核心流程验收

`python scripts/evaluate_core.py --run-live --online --output core-evaluation.json` 在临时数据库运行三轮彩礼场景，不复制业务案件，保留每轮回答、事实来源及引用。它会真实调用已配置模型，可能产生费用。操作及指标边界见 [核心流程](../docs/CORE_RESEARCH.md)。自动回归测试均使用合成响应，不是模型正确率评测。
