# 1.1.1 验证记录

日期：2026-09-25。使用独立临时数据库和合成模型响应，不触碰用户案件数据。

| 检查 | 结果 | 主要覆盖 |
|---|---|---|
| pytest | 98 项通过 | 原有 80 项及新增 18 项；重复追问、历史召回、未知/矛盾/更正、案件隔离、来源核对、HTTP 400、JSON 模式、SSE、思考保存与开关、字符预算 |
| DOM 交互 | 19 项通过 | 登录、案件、记忆、原文问答、引用、独立文件/链接导入和错误显示 |
| Chromium 导入回归 | 10 项通过 | dialog 错误位置、桌面/手机、交叉引用、URL 自动读取、版本不一致 |
| Chromium Agent / 教程 | 17 项通过 | 任务进度、修正循环、取消、刷新恢复、引用、联网线索、三个 API 标签页、键盘、深链接、打印与手机 |
| Chromium 记忆 / 思考 | 11 项通过 | 实时思考、刷新恢复、历史展开、转义、截图场景、原问题延续、HTTP 400、配置保存、手机宽度 |
| 语法 / 依赖 | 通过 | Python compileall、node --check、pip check |

浏览器为 Chromium 153.0.8010.0、Playwright 1.62.1。新增桌面视口 1440×1000、手机 390×844；原 Agent 视口 1280×900。查看了新增桌面/手机截图，思考面板可见，内容独立滚动，无水平溢出。截图和机器检查清单在 `memory-verification/`、`agent-verification/`、`browser-verification/`。截图文字包含明确的合成测试标记，不是真实模型分析。

## 本次问题的回归证据

1. 原问题为“离婚时彩礼能否返还？”，补充“已登记、婚后共同生活、生活困难未知”。让模型替身刻意重复询问截图中的三项，前两项按用户原话去重；第三项通过带原话来源的改写追问核对转回未知。真正未提供的给付日期仍保留为新问题。
2. 接着问“之前不是告诉你了吗”，原研究问题及四条用户陈述仍进入后续调用。超过最近四轮或1500字符的历史中可整理的事实也继续保留。
3. 用户更正、未知转为已知、存在矛盾分别处理；矛盾不能被追问核对器静默删除。其他案件、历史助手回答及思考内容不进入本案用户事实摘要。
4. HTTP 400 参数错误只调用一次模型，保留原问题检索结果并显示“模型调用失败”，展示脱敏供应商错误；不再将该失败解释成“缺少法条”。
5. SSE 的 reasoning_content / reasoning 与最终 content 分离。思考流未结束前已可查看，重载可恢复；关闭展示不保存新思考，没有字段就标明没有，达到上限明确标记截断。
6. 思考内容中的 HTML 作为文本显示。无论思考说了什么，最终法律结论仍要通过引用原文与模型支持性校验，思考不能作为法源或自动确认事实。
7. 千问默认/开启思考的自动格式模式不附加 response_format；严格模式保留参数，兼容模式仅以提示词要求 JSON。所有模式仍进行本地结构验证。

## 复现

```bash
pip install -r requirements-dev.txt
python -m pytest -q
npm ci
python scripts/test_ui.py
```

可选真实浏览器测试（Node.js、Playwright、Chromium）：

```bash
npm install --no-save --package-lock=false playwright
npx playwright install chromium
python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_agent.cjs \
LEXCITE_GUIDE_PATH="$PWD/docs/LexCite_Step_by_Step_Guide.html" \
LEXCITE_BROWSER_OUTPUT=test-results/agent \
python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_memory.cjs \
LEXCITE_BROWSER_OUTPUT=test-results/memory \
python scripts/test_browser.py
```

环境变量语法适用于 macOS/Linux。脚本启动临时测试服务；生产入口仍为 `app.main:app`，不是测试替身。

## 实测边界

模型和官网响应使用合成测试替身；实际执行了浏览器、API、任务线程、SQLite、解析、引用校验和页面布局。未使用真实国产模型 API 密钥、未加载 Ollama 权重、没有 Docker 引擎，未实测镜像构建和目标服务器部署。本次无法仅从截图确定用户 HTTP 400 的具体厂商原因，新版提供错误详情和兼容选项以便定位。

没有真实法律业务评测集或幻觉率统计；重复问题的复杂语义识别和引用支持性判断仍可能出错。用户陈述摘要按启发式整理，未覆盖段数会提示，关键事实建议手工确认。模型接口返回的思考不等于完整内部计算，不能补回旧版未保存的内容。

依赖测试有一条 Starlette 关于 httpx TestClient 的弃用提示；未影响结果。历史验证记录保留在 `VERIFICATION_1.1.0.md` 和 `VERIFICATION_1.0.2.md`，不视为新的实测结论。
