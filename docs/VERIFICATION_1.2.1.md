# 1.2.1 验证记录

完成于 2026-09-26 UTC。针对研究页排版反馈，使用临时数据库与 Chromium 153.0.8010.0 检查实际页面。模型和官网下载使用明确标识的合成响应。

## 本次执行

| 检查 | 结果 | 范围 |
|---|---|---|
| Chromium 研究页布局 | 14 项通过 | 8 种窗口尺寸、文字不被遮挡、输入框增长与缩回、长标题、可选检索词、引用可操作 |
| Chromium 核心流程 | 9 项通过 | 法源缺口、官方临时证据、引用、四组补充事实、刷新记忆、导出与联网设置 |
| Chromium 导入 | 10 项通过 | 弹窗错误、交叉引用、仅填官网 URL、前后端版本检查 |
| Chromium Agent 与教程 | 17 项通过 | 实际任务进度、刷新恢复、修正、取消、引用、搜索设置、三家 API 标签页、键盘与手机 |
| DOM 交互 | 19 项通过 | 登录、案件、导入复核、记忆、原文查询、引用、设置与错误显示 |
| 后端资源版本检查 | 1 项通过 | 版本化资源 URL、HTML/JS/后端版本及缓存控制 |
| JavaScript 语法检查 | 通过 | `node --check static/app.js` |

本次未重复运行整个后端测试集。1.2.0 的 123 项后端测试及历史验证保留在 [VERIFICATION_1.2.0.md](VERIFICATION_1.2.0.md)，不能当作本次重新执行结果。

## 实际尺寸

单位为 CSS 像素，四舍五入；案件名为 `yunki`，问题框为空，公开检索词折叠，研究已完成。标题高度不含上方全局导航。提问区高度包含输入框、工具栏及说明。窄屏可正常滚动页面，不强制将所有内容挤在一屏。

| 窗口宽×高 | 案件标题 | 问题框 | 整个提问区 | 回答滚动区域 |
|---|---:|---:|---:|---:|
| 1920x1080 | 31 | 36 | 120 | 825 |
| 1440x900 | 31 | 36 | 120 | 645 |
| 1280x720 | 31 | 36 | 120 | 465 |
| 1024x768 | 31 | 36 | 158 | 475 |
| 951x600 | 31 | 36 | 179 | 287 |
| 768x700 | 31 | 36 | 158 | 420 |
| 390x844 | 61 | 36 | 180 | 506 |
| 320x640 | 61 | 36 | 180 | 384 |

- 法源说明的顶部在下拉控件底部之后，命中测试确认文字不被其他元素覆盖；八种尺寸均无横向溢出。
- 长问题会长高至桌面112像素、手机96像素上限，之后框内滚动。25行测试文本完整保留，清空后恢复36像素。原输入长度上限仍为6000字符。
- 宽屏不再因为自动外边距而缩成窄列；桌面回答区随剩余视口高度伸缩，案件记忆单独滚动。
- 研究执行中，进度面板仍可见；更新页面恢复同一任务，引用弹窗与停止按钮仍可操作。
- 教程提供阿里、DeepSeek、硅基流动标签页，打印时三家内容均可见；Ollama 步骤继续保留。

截图与机器结果在 `layout-verification/`。截图内回答是合成测试内容，不是可用于办案的法律结论。

## 复现

先按项目开发依赖说明安装 Python 依赖、Playwright 与 Chromium，再运行：

```bash
python scripts/test_ui.py
python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_layout.cjs python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_core.cjs python scripts/test_browser.py
LEXCITE_BROWSER_SCRIPT=tests/browser_agent.cjs LEXCITE_GUIDE_PATH="$PWD/docs/LexCite_Step_by_Step_Guide.html" python scripts/test_browser.py
python -m pytest -q tests/test_cross_references.py::test_versioned_assets_and_no_cache
node --check static/app.js
```

上述环境变量写法适用于 macOS/Linux。所有浏览器脚本启动独立测试服务，不要指向正式案件库。

## 边界

未操作用户服务器，未构建 Docker 镜像；当前环境没有 Docker 引擎。没有真实模型密钥或 Ollama 权重，因此本次没有重做真实国产模型效果与外部官网连通性验收。此版本改进排版，不以这些界面测试宣称法律准确率提高。

后端定向测试出现一条 Starlette TestClient 对 httpx 的弃用提示，未影响通过。
