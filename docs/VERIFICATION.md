# 1.3.0 验证记录

以下为2026-09-27应用验证记录。

完成于2026-09-27 UTC。使用临时数据库、合成模型响应和本机 HTTP 测试端点，没有调用用户的真实密钥或修改正式案件。

| 检查 | 结果 | 范围 |
|---|---|---|
| pytest 全集 | **184 项通过** | 保留原146项，新增38项混合召回、服务协议、索引及评测隔离检查 |
| Chromium 混合检索 | 11 项通过 | 独立配置、密钥不回显、服务测试、后台索引、实际模型展示、完整引用、导出、失败与手机 |
| Chromium 核心流程 | 9 项通过 | 离线/在线、引用、用户四项补充事实、记忆持久化与导出 |
| Chromium 紧凑布局 | 14 项通过 | 8种窗口尺寸、输入增长、选项说明、长标题、引用 |
| Chromium 法源修复 | 8 项通过 | 缺搜索配置、无搜索密钥的刑法目录、名称与版本、导出及手机 |
| Chromium Agent / 教程 | 17 项通过 | 研究进度、修正、停止、刷新、API标签页、键盘、打印和手机 |
| DOM 交互 | 19 项通过 | 登录、案件、导入复核、记忆、引用与弹窗错误 |
| 新教程章节 | 已截图目视检查 | 1440px电脑、390px手机，API地址和本地路线无页面横向溢出 |
| 本地重排请求边界 | 4 项拒绝检查通过 | 错误模型、过长输入、空列表、服务占用；没有加载权重 |
| Python编译 / JS语法 | 通过 | 应用、脚本、重排服务编译；`node --check static/app.js` |

浏览器为Chromium 153.0.8010.0；59项浏览器检查与19项DOM检查全部通过。合成模型仍通过真实 HTTP、数据库、后台任务及浏览器流程。机器结果和新界面截图在 `hybrid-verification/`。pytest有一条Starlette/httpx弃用提示，不影响通过。

## 本次重点检查

- “怎么帮人看娃”的合成向量可召回不含原词的照护条文；BM25无命中时密集召回仍工作。该用例验证数据路径，不代表真实语义模型效果。
- 日期、地区、复核、版本冲突先过滤；过期条文和其他模型指纹的旧向量不参加语义召回。明确法名与条号不会被重排低分挤掉。
- 长条文可切窗口检索，最终返回完整原文。中途失败或取消不会将半条索引发布。更改模型、维度、版本、条文元信息触发重新索引；失败不自动无限重试。
- 后台索引使用共享任务锁，覆盖研究占用时排队、取消、重启恢复、自动导入/复核触发、同一次失败后手工补齐。
- 阿里 embedding、阿里 qwen3-rerank 的原生 `/api/v1/services/rerank/text-rerank/text-rerank`、硅基兼容协议、Ollama `/api/embed` 都有 HTTP 请求/响应契约测试。重排还覆盖旧兼容根地址转换和 404 路径提示；模拟协议测试不等于真实账户联通验证。对重复编号、缺项、非有限数、维度异常、超大响应、超时和 HTTP 错误进行校验。
- 三服务密钥独立；服务或地址改变不携带旧密钥，浏览器不收到明文。错误不回显上游原始响应中的密钥。
- 答案和导出记录真实检索方式、覆盖、候选数及降级原因；在线临时条文单独记录关键词/重排路径，不能冒充已向量化。
- 评测脚本复制法库到临时数据库，不复制业务案件、不写正式索引；三种目标路径实际失败时单独标注，缺法源与召回失败分开统计。

## 实测边界

本次没有真实国产 API 额度，没有下载 Qwen3 向量/重排权重，也没有 Docker 守护进程；没有构建镜像或运行真实GPU/CPU模型。服务适配属于协议与合成端到端验证，不能解释成“真实法律回答已提升多少”。本地重排的实际评分推理和速度仍需目标机器验收。Compose 将 Ollama 示例镜像从0.11.10更新为官方已发布的0.33.3；发布存在性核查不等于镜像运行验收。

内置10题仅为检索验收起点，尚不是经过法律人员标注的完整评测集。它包含合同口语/术语变体、原彩礼补充事实、刑法条号；正式评测必须补齐对应法源并核对参考条文。SQLite JSON向量精确扫描没有进行大规模法库或多人并发压测。

法源引用校验、支持性复核及版本检查继续执行。提高语义召回不能补出未导入的法规，也不能保证法律适用正确。本次没有用真实公网网站重新做批量下载验收。

## 复现功能检查

安装项目开发依赖与Playwright/Chromium后，在项目目录运行：

```bash
python -m pytest -q
python scripts/test_ui.py
LEXCITE_BROWSER_SCRIPT=tests/browser_hybrid.cjs python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_core.cjs python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_layout.cjs python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_sources.cjs python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_agent.cjs LEXCITE_GUIDE_PATH="$PWD/docs/LexCite_Step_by_Step_Guide.html" python scripts/test_browser.py
```

## 部署后运行真实服务对照

```bash
python scripts/evaluate_retrieval.py --run-live --build-index --output retrieval-evaluation.json
python scripts/evaluate_core.py --run-live --online --output core-evaluation.json
```

运行前配置对应服务、导入并复核所需法源；这些命令会调用真实服务，可能收费。检索报告包含三组实际路径、法源覆盖、Recall@20、MRR@20、nDCG@10、请求次数和耗时。核心流程报告用于核对多轮事实、追问和引用，仍需法律人员评阅结论。
