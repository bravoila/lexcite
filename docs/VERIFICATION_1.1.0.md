# 1.1.0 验证记录

记录日期：2026-09-25。使用临时数据库与测试账户；不触碰实际案件数据。

| 检查 | 结果 | 覆盖范围 |
|---|---|---|
| Python pytest | 80 项通过 | 原 60 项回归；新增研究循环、错误引文修正、证据复核失败重试、预算、澄清退出、任务幂等/取消/中断/鉴权、搜索隔离、密钥、协议与思考适配 |
| 网页 DOM 交互 | 19 项通过 | 登录、案件与记忆、原文问答、引用、导入与复核、文件/链接独立导入和错误提示 |
| Chromium 导入回归 | 10 项通过 | 原生 dialog 顶层错误、桌面/手机可见性、换行交叉引用、URL 自动读取、版本混用提示 |
| Chromium Agent / 教程 | 17 项通过 | 实际进度、刷新恢复、失败后修正、答案只存一次、导出轨迹、官网读取及导入、取消、思考/搜索设置、教程切换/键盘/深链接/打印/手机宽度、无 JS 错误 |
| 依赖和语法 | 通过 | Python 3.12；pip check、Python compileall、node --check |

浏览器：Chromium 153.0.8010.0、Playwright 1.62.1。导入桌面 1280×800、Agent/教程桌面 1280×900、教程和导入手机 390×844。截图与检查清单在 `agent-verification/`、`browser-verification/`；已查看研究进度与教程桌面/手机截图，确认进度区在桌面可视区域内，教程标签页与手机布局正常。

## Agent 验证要点

- 第一轮引文不存在于法条时不会展示结论；错误反馈进入第二轮，修正后再次通过引用和支持性双重校验才输出。
- 模型判定不支持的结论会触发新检索；三轮仍不通过则只保留原文。不存在为了提高回答率而跳过校验的分支。
- 任务创建后，模型尚在运行时已能读取真实的计划事件。刷新页面恢复同一个任务；同一 request_id 不重复调用或保存答案。
- 请求停止后不再调用下一阶段，也不保存未完成草稿；服务启动会把遗留运行任务标记为 interrupted。
- 联网只收到独立填写的公开词；没有公开词时不触发搜索。官网候选和搜索摘要没有进入生成的法律 evidence，伪造以网页 URL 作 article_id 会被阻断。
- 博查 Bearer 认证与响应解析、SearXNG JSON 查询均通过 HTTP MockTransport 验证；非官方/伪装域名和重复结果被过滤。
- DeepSeek、Ollama、硅基的显式思考字段以及百炼 SSE 最终内容收集通过模拟协议测试。内部 reasoning_content 不出现在结构化返回或研究记录中。
- 新增总预算耗尽、搜索失败仍能完成本地分析、未登录访问拒绝和补充事实后提前退出测试。

## 如何复现

```bash
pip install -r requirements-dev.txt
python -m pytest -q
npm ci
python scripts/test_ui.py
```

真实浏览器测试另需 Node.js、Playwright 与 Chromium；运行目录是项目根目录：

```bash
npm install --no-save --package-lock=false playwright
npx playwright install chromium
python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_agent.cjs \
LEXCITE_GUIDE_PATH="$PWD/docs/LexCite_Step_by_Step_Guide.html" \
LEXCITE_BROWSER_OUTPUT=test-results/agent \
python scripts/test_browser.py
```

以上环境变量语法用于 macOS/Linux。测试脚本会建立临时服务与数据库并清理，不应将 `browser_fixture_app:app` 用作部署入口；生产使用 `app.main:app`。

## 实测边界

模型和外部网站响应使用明确的测试替身。浏览器操作、任务线程、真实 HTTP API、SQLite、解析、引用校验和页面布局实际运行，但这不证明特定国产模型的法律准确率、真实搜索召回率或目标官网可达性。没有使用用户真实 API 密钥，也没有加载本地模型权重；没有 Docker 引擎，未实测镜像构建、GPU、正式服务器并发或跨浏览器。

本版本没有真实法律业务评测集和幻觉率统计。模型支持性复核仍可能出错；网上查到的材料需人工确认有效期、完整性与版本后才纳入法源。单工作空间、单 worker，不提供独立用户和案件级权限。旧导入排版问题的完整原文件/网址未提供，测试覆盖已知同类型交叉引用与断行样例。

依赖解析产生一条 Starlette 对 httpx TestClient 的弃用提示；当前测试全部通过，未为消除警告引入额外客户端依赖。

1.0.2 的历史验证记录保留在 `VERIFICATION_1.0.2.md`；早期小样本检索和备份恢复记录不视为本次重新实测。
