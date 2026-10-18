# 1.2.0 验证记录

完成于 2026-09-26 UTC。测试在独立临时数据库运行，不使用用户业务案件或真实供应商密钥。

| 检查 | 结果 | 验证范围 |
|---|---|---|
| pytest | 123 项通过 | 原 98 项适配新覆盖阶段，新增 25 项核心回归 |
| DOM 交互 | 19 项通过 | 登录、导入、复核、案件记忆、原文检索、引用、错误显示 |
| Chromium 导入 | 10 项通过 | 弹窗顶层错误、滚动/手机可见性、交叉引用、只填 URL 读取、版本校验 |
| Chromium Agent / 教程 | 17 项通过 | 修正循环、取消、刷新恢复、引用与导入、设置、三家 API 标签页、键盘、打印、手机 |
| Chromium 记忆 / 思考 | 11 项通过 | 接口思考字段、刷新与历史、原话记忆、未知、HTTP 400、转义、设置 |
| Chromium 新核心流程 | 9 项通过 | 离线法源缺口、官方临时证据、原文引用、完整四组补充事实、刷新持久化、来源导出、联网默认设置、手机宽度 |

以上测试中的模型和外部官网下载均使用显式合成响应；真实执行了 HTTP、任务线程、SQLite、解析器、检索、数据流和浏览器。浏览器为 Chromium 153.0.8010.0。新增截图及机器结果在 `core-verification/`，测试结论均标明为合成数据。

## 针对本次反馈的证据

- 原样输入“是否登记？办了……共同生活一年……生活困难？没有……彩礼用途？没有”，得到四组带来源的陈述；下一轮保留原法律问题，刷新和数据库重新初始化后仍可读到。
- 已明确关联的问答保留字面答案；模型即使将“办了”改写成“未登记”，该错误改写也不会覆盖原问答。
- 未知不会被变成肯定事实；否定矛盾和持续时间冲突保留，明确更正可替换。短答只有能关联具体问题时才使用，单个字不能消除其他待确认问题。
- “未用于抚养子女”不被事实提取器转换成“没有子女”。这是专门的回归保护，复杂任意表达仍需要人工核对。
- 请求不存在法规的某个条号时，不会用民法典的同条号替代。`mingfa` 可通过复核表单补标准法规名称；冲突版本按标准身份阻断。
- 覆盖检查排除无关条文后，不进入草稿，也不把候选显示成引用。缺司法解释进入 `source_gaps`，不会显示成要求用户补充的案件事实。
- 空草稿明确显示尚未生成有依据的结论，不产生“引用校验通过0项”的成功事件。
- 开启补查可将通过检查的官方全文纳入本次证据，再生成、匹配与复核；原文有哈希、读取时间、来源标记和施行条款。不会自动写入正式法库。
- 官网页面名称、文号、日期、条数不符或正文不连续会被排除；未来生效、地区不符、与人工复核版本重叠不会自动覆盖。搜索故障仍保留本地有依据的部分。
- 自动搜索只收到法规名称，没有收到缺口原因里嵌入的案件信息。目录 URL 不需要搜索 API。搜索摘要不能冒充法律依据。
- 有原话来源的语义快照与消息原子持久化；独立真实模型验收脚本在临时库工作，不复制或改写正式业务案件。

## 复现

```bash
pip install -r requirements-dev.txt
python -m pytest -q
npm ci
python scripts/test_ui.py
npm install --no-save --package-lock=false playwright
npx playwright install chromium
python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_core.cjs python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_memory.cjs python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_agent.cjs LEXCITE_GUIDE_PATH="$PWD/docs/LexCite_Step_by_Step_Guide.html" python scripts/test_browser.py
```

环境变量命令适用于 macOS/Linux。测试入口不是生产入口。生产使用 `app.main:app`。

## 尚未验证

- **真实 qwen-plus、DeepSeek、硅基流动或 Ollama 的法律回答效果**：没有可用真实 API 密钥或本地权重，未产生真实模型效果报告，也没有宣称幻觉率下降的百分比。
- **当前运行环境直连法院官网**：尝试两个目录 URL 均因 DNS 解析限制失败，记录在 `core-verification/official_read_smoke.json`。这不证明部署服务器同样不可达；部署后要检查外网 DNS、HTTPS 和官网访问限制。
- **Docker 镜像构建与用户服务器升级**：本环境没有 Docker 引擎，未操作用户部署。提供源码、Docker 配置与保留数据的升级步骤。
- **法律正确率与法规现行有效性**：元信息、原文匹配和模型复核不能替代法律人员核对；目录是少量官方地址，尚不是持续更新的全国法库。

`evaluate_core.py --run-live --online` 可在用户实际模型配置下生成三轮验收报告，会产生真实模型调用；默认自动测试不会执行。具体指标和人工核对项目见 `CORE_RESEARCH.md`。

测试有一条 Starlette TestClient 使用 httpx 的弃用提示，未影响结果。1.0.x / 1.1.x 的历史记录另行保留，不冒充本次实测。
