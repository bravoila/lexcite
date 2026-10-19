# LexCite 1.4.0 验证记录

本轮记录日期：2026-10-19 UTC。对应PRD的P0/P1、R6-S/P/E及N1–N5整改；不扩展P2/P3产品功能。历史[1.3.0记录](VERIFICATION.md)和PRD原审查表保留，不将历史结果改写为本版结果。

**工程回归和隔离部署已通过；真实模型与法律评测的发布门槛尚未完成，不代表生产验收通过。** 全程使用临时数据库、合成案件及一次性容器卷；未调用真实模型或用户密钥，未修改正式案件、法库或`.env`。

## 1. 环境与通过范围

环境为macOS、Python 3.12、Node.js 22.22.2、Chromium 156.0.8078.4及Docker 29.8.2。关键Python依赖包括FastAPI 0.141.1、Uvicorn 0.53.0、HTTPX 0.28.1、Pydantic 2.13.5、pypdf 6.10.0和python-docx 1.2.0。应用仍为单worker、SQLite，无前端构建步骤。

| 检查 | 实际结果 | 边界 |
|---|---|---|
| pytest全集 | **416项通过** | 包括原有回归与本轮新增；有3条既有依赖弃用提示 |
| 独立DOM可靠性 | **15项通过** | 启动错误、版本混用、切案竞态、限次轮询、恢复串行化、转义及配置修复 |
| 真实HTTP的DOM交互 | **23项通过** | 登录、导入、复核、案件、事实、引用、设置及弹窗错误；不是可视浏览器 |
| Chromium完整流程 | **82项通过** | 下表七组；模型、搜索及来源使用夹具，不是法律效果评测 |
| JavaScript / Bash语法 | 通过 | `static/app.js`和`start.sh`；没有实际运行Windows批处理 |
| Docker发布镜像 | 构建成功 | 真实Docker守护进程；Python 3.12、单worker、禁用Uvicorn代理头解析 |
| Compose默认及重排叠加配置 | 语法检查通过 | 不能解释为真实重排权重已加载 |
| 真实Nginx / TLS隔离部署 | **11项通过** | 自签测试证书、真实容器网络与私网DNS、一次性数据卷；零模型调用 |

| Chromium脚本 | 通过数 |
|---|---:|
| `tests/browser_smoke.cjs` | 10 |
| `tests/browser_agent.cjs`（含HTML教程） | 18 |
| `tests/browser_hybrid.cjs` | 11 |
| `tests/browser_layout.cjs` | 14 |
| `tests/browser_memory.cjs` | 11 |
| `tests/browser_sources.cjs` | 9 |
| `tests/browser_core.cjs` | 9 |

浏览器检查均在1.4.0运行，覆盖手机横向溢出、完整引用与导出、实际失败状态、停止/刷新恢复、初始化教程，以及多条历史回答中不安全链接的逐条处理。38项DOM检查与82项Chromium检查分开计数。

## 2. 本轮关键回归

| 需求 | 实际覆盖 |
|---|---|
| 登录与反代 | 伪造XFF、共享来源失败限流、全局非阻塞429、正确密码优先、显式本地恢复、有界地址记录及scrypt并发；早期错误保留安全头 |
| 配置与密钥 | 存储和环境密钥均绑定端点；分析另绑定配置档；省略/`null`保留与显式空串清除分开；无效存量/环境配置可显示并修复 |
| 服务地址 | CGNAT、保留/私网及IPv6映射拒绝；文档化本地服务名和指定回环端口可用；停用服务不解析闲置地址 |
| 拆条与附件 | 原文SHA256/快照不变；Unicode分隔符、零宽字符、全角/混合条号、章节/折行引用、Markdown、多层DOCX、损坏PDF/DOCX及提取上限 |
| 检索与引用 | 多法就近条号绑定及同一校验规则；日期/地区/复核SQL筛选；向量按合格ID批量读取；直接SQL更新使有界缓存失效 |
| 检索性能 | 5000条合成法条的冷词频统计检索断言`< 2秒`通过；不把含导入准备的pytest用时当作检索耗时，也不外推百万级规模 |
| 任务生命周期 | 同步/异步共享任务与幂等机制；案件/事实/设置变更锁；取消事件不逐块访问SQLite；取消先于提交生效；索引入队即取消仍终态化 |
| 模型HTTP | 429/指定5xx最多三次、两种`Retry-After`、可取消退避、JSON/SSE 4MB及错误体16KB；真实HTTP的JSON/SSE阻塞响应体取消/超时四项回归 |
| 实际请求预算 | 六个成功逻辑调用各先失败重试两次，实发恰好18次；第19次请求发出前拒绝；`model_runs.http_attempts`总和为18 |
| 研究结果 | 不把“请分析”当事实答案；当前焦点回退；拒绝伪缺失法源；错误阶段Draft被拒；损坏官方附件保留本地可支持结论 |
| 完整证据 | 点名条文超预算明确记录，不截断原文；后续已纳入的条文不保留假遗漏警告；“仅查法条”保留全部明确请求的完整条文 |
| 前端恢复 | 404终止、500限次退避、悬挂请求超时；恢复不重新提交、与提问串行、404清除过期任务、保留用户新编辑的问题 |

主要回归位于`tests/test_prd_regressions.py`、`test_auth.py`、`test_security.py`、`test_model_wire.py`、`test_import_quality.py`和`tests/ui_reliability.cjs`。原工作流夹具已按Draft/Verdict/Repair重写，不再声称执行不可达的模型CoverageAudit阶段；兼容结果字段仍由本地构造。

### 真实容器检查的具体边界

`scripts/test_deployment.py`使用`.env.example`，不读取真实`.env`；不挂载项目数据，只创建随机命名的自有网络、卷和容器。后端不发布端口，Nginx仅发布随机本机HTTPS端口，并用`$remote_addr`覆盖XFF。

实际覆盖首次初始化、0600码文件/数据库、不向日志泄露初始化码、初始化后删码；TRUST_PROXY关闭时30次、开启时100次伪造地址失败后正确密码仍可登录；真实loopback不默认豁免且Uvicorn忽略伪造XFF；Secure/HttpOnly cookie、HSTS、安全头、小错误体、重启后的案件及原子消息对、版本资源，以及真实私网DNS下的默认停用重排和服务名保存。

脚本只有清理完自己创建的容器、卷和网络后才输出成功JSON。它证明镜像启动和反代边界，**没有运行Ollama、SearXNG或实际重排模型**；把服务名解析到隔离网络地址并保存配置，不能当作对应服务推理成功。

## 3. 复现命令

在项目根目录和Python 3.12开发环境运行：

```bash
python3.12 -m pytest -q
npm ci
npm run test:ui
node --check static/app.js
bash -n start.sh
```

Chromium为可选测试依赖，不是运行应用的依赖。安装后运行全部七组：

```bash
npm install --no-save --package-lock=false --ignore-scripts playwright
npx --no-install playwright install chromium
for suite in smoke agent hybrid layout memory sources core; do
  LEXCITE_BROWSER_SCRIPT="tests/browser_${suite}.cjs" \
    LEXCITE_GUIDE_PATH="$PWD/docs/LexCite_Step_by_Step_Guide.html" \
    python3.12 scripts/test_browser.py || exit 1
done
```

隔离部署检查需要正在运行的本机Docker及OpenSSL；会使用或拉取`nginx:stable-alpine`，生成的自签证书仅用于本次本机测试，不是生产TLS配置：

```bash
docker compose --env-file .env.example config --quiet
docker compose --env-file .env.example -f compose.yaml -f compose.reranker.yaml config --quiet
docker build --quiet -t lexcite:deployment-test .
python3.12 scripts/test_deployment.py --image lexcite:deployment-test
```

可通过`--output /自行选择的目录/deployment-result.json`保留通过项、镜像ID、完成时间和清理结果。该命令不启动正式Compose项目，也不清理其他实例或镜像。测试结束可自行删除专门构建的`lexcite:deployment-test`镜像，不要执行全局prune或删除正式数据卷。

## 4. 尚未完成的发布门槛

| 门槛 | 当前状态 |
|---|---|
| 真实国产聊天、向量、重排API端到端 | 未执行；协议夹具不能证明实际账户权限、供应商兼容性、费用或法律效果 |
| 真实本地Ollama聊天/向量端到端 | 未执行；未在本轮加载真实权重 |
| `compose.reranker.yaml`实际模型组合 | 配置语法和默认启动故障已覆盖；真实权重加载、评分及GPU/CPU速度未验收 |
| 目标单位HTTPS部署 | 本机隔离Nginx/TLS已通过；实际证书、域名、网络隔离与边缘限流仍需部署管理员验收 |
| 法律人员标注保留集 | 未完成；没有真实法律准确率或零幻觉承诺 |
| Windows启动及目标硬件性能 | 未实际执行；Bash语法及本机合成性能检查不能代替 |

真实服务评测入口仍为`scripts/evaluate_retrieval.py`与`scripts/evaluate_core.py`，使用前须配置服务、补齐并人工复核法源，且确认付费调用授权。评测必须保留未参与调试的法律人员标注样本，不能用本轮合成夹具代替。

## 5. 已知技术边界

研究墙钟与取消检查可中止**响应头到达后的阻塞响应体读取**；DNS、建连和响应头阶段仍依赖HTTPX的剩余预算网络超时，未实现独立可强杀的DNS解析器。服务端点还存在已记录的DNS校验与建连之间窗口，建议配合受控出口网络。

PDF的50MB聚合内容流检查发生在单流解码之后，不是50MB内存沙箱。本机pypdf另有其解压保护；复杂附件仍应使用部署资源限制，扫描PDF不含OCR，复杂DOCX阅读顺序仍须核对。

本版不会自动重解析旧法库。R5期间可能已产生的漏条、假条或附件漏读，须备份后对照权威原文重导入并复核，不能仅靠重建向量修复。正确密码优先策略不等于匿名请求无限制，也不能替代代理层连接/速率限制；冷缓存校验资源饱和仍可能短暂429。

迁移、备份与回退步骤见[升级手册](UPGRADE_1.4.0.md)。这些边界和未完成门槛保留，不以工程测试通过宣称生产就绪。
