# 法据 LexCite 1.4.0 产品需求文档(PRD)

| 项 | 内容 |
|---|---|
| 文档版本 | v0.3 |
| 日期 | 2026-10-17 |
| 当前版本 | 1.3.2(已重构 + P0 已修复,见下) |
| 代码基线 | git HEAD `acb2c23 restructure` + 工作区 R3 P0 修复(未提交) |
| 测试基线 | 232 项 pytest 全通过(基线 218 项 + R3 新增 14 条 P0 回归) |
| 审查轮次 | **R1(重构前)**:4 个模块并行审查 + 人工交叉验证;**R2(重构后)**:2 个 agent 复审(研究 Agent 新包 / 工作区改动)+ 逐项 grep 复核;**R3**:P0-1~P0-5 修复并配齐回归测试 |
| 重构说明 | `app/engine.py`、`retrieval.py`、`authorities.py`、`case_state.py` 已删除,逻辑迁入 `app/research_agent/`(runner / retrieval / verification / authorities / context / schemas / prompts)。context.py 与旧 case_state.py 的 12 个函数字节码一致,文档与实现一致,测试未改弱 |

## 1. 背景与目标

1.3.2 已实现混合 RAG、有状态研究循环、引用校验、案件记忆等核心能力,测试与文档质量高。用户已完成一次**结构重构**(engine → research_agent 包),但 R2 复审确认:重构是结构迁移,**绝大多数已知缺陷原样迁入新代码**,并新增少量问题。

**1.4.0 的目标:先把「会出错」的修掉,再把「能上生产」的补上。**

具体分三层:
1. **P0 正确性/安全阻塞项**:修复会让错误法条通过校验、或让攻击者影响全员可用性的缺陷。
2. **P1 安全加固与工程健壮性**:补齐部署安全基线、导入解析可靠性与后台任务状态机。
3. **P2+ 产品能力**:评测体系、多租户、可观测性等面向真实业务的演进。

**非目标(本期不做)**:百万级文书搜索引擎、Book-to-Skill 蒸馏、真实法律准确率承诺、模型微调。

## 2. 优先级总览(R2 复核后)

| 优先级 | 主题 | 需求数 | 已修复 | 说明 |
|---|---|---|---|---|
| P0 | 正确性与安全阻塞项 | 6 | **6(全部)** | R3 完成 P0-1~P0-6,均可发布 |
| P1 | 安全加固 | 7 | 0 | 部署安全基线 |
| P1 | 导入/解析可靠性 | 7 | 0 | 防止条文污染与静默丢失 |
| P1 | 工程健壮性 | 6 | 1(部分) | 任务状态机、性能与韧性 |
| P2 | 评测与可观测 | 5 | 0 | 建立可量化的质量基线 |
| P2 | 产品能力 | 6 | 0 | 检索增强、任务恢复、数据管理 |
| P3 | 多租户与权限 | 5 | 0 | 独立里程碑,跨版本演进 |
| 新增 | 重构引入的问题 | 5 | 0 | R2 发现,见第 10 节 |

---

## 3. P0:正确性与安全阻塞项

### P0-1 登录限流按身份隔离,禁止匿名锁死全员 【已修复】

**现状**:`app/auth.py:48-54` 以 `request.client.host` 为限流键;`app/main.py:122` 传 client.host;`Dockerfile:15` 以 `--no-proxy-headers` 启动。反代部署时所有用户共享代理 IP,任何匿名者连续 10 次错密码,全员(含管理员)10 分钟无法登录,且可反复触发。

**需求**:
- 限流键双层:账号(或账号+IP)+ 全局匿名请求限额。
- 反代场景支持 `--proxy-headers --forwarded-allow-ips`,仅信任已知代理的 XFF。
- 管理员账号免于被全局限额锁死(或提供本地恢复通道)。

**修复**(R3):
- `app/auth.py:login()`:按地址 10 次失败/10 分钟硬限流只封锁被撞的地址;全局匿名失败满 60 次/10 分钟改为 2 秒节流,正确密码仍可登录(只变慢,不拒绝);`127.0.0.1`/`::1` 完全豁免,作为本地恢复通道。
- `app/main.py:client_address()`:`LEXCITE_TRUST_PROXY=1` 时取 XFF 最后一个跳(可信代理实际看到的来源,早段是客户端可伪造的);未开信任时 XFF 整体忽略。Dockerfile 移除 `--no-proxy-headers`(uvicorn 侧不再解析,XFF 统一由应用按信任开关处理);compose.yaml 增加 `LEXCITE_TRUST_PROXY` 与配置说明(代理须追加/覆盖 XFF)。

**验收标准**:
- [x] 反代部署下,匿名者用错误密码撞库 30 次,不影响正确密码登录。`tests/test_auth.py::test_attacker_flood_does_not_block_other_clients`
- [x] 单账号 10 次失败仍触发该账号 10 分钟限流。`tests/test_auth.py::test_account_limit_still_locks_flooded_address`
- [x] `docker compose logs` 中不出现因 XFF 伪造导致的错误限流。未开信任时代理 XFF 被完全忽略、伪造 XFF 不改变限流键(`test_forged_xff_ignored_without_trusted_proxy`);另覆盖全局节流不锁死(`test_global_anonymous_cap_throttles_but_never_locks`)与本地恢复(`test_loopback_recovery_channel`)。

### P0-2 模型密钥与端点绑定,端点变化即失效 【已修复】

**现状**:`app/llm.py:134-142`。API 请求省略 `api_key` 字段时,旧密文无条件保留;修改 `base_url` 后旧云端密钥仍会发往新端点。前端 `static/app.js:197` 在地址变更时发 `""` 有部分规避,**但直接调 API 仍中招**。检索/搜索服务密钥需同步检查(`app/websearch.py:43-53` 已实现正确模式,可作为参照)。

**需求**:
- 密钥与 (profile, provider, base_url) 三元组绑定;任一变化即清除已存密钥。
- 空字符串与省略字段行为一致(不再区分)。
- 保存前对 base_url 做安全校验(见 P1-2)。

**修复**(R3):`app/llm.py:save_settings()` 与 websearch.py 对齐——三元组(profile, provider, base_url)不变且省略 `api_key` 时才保留旧密文,否则清空;显式 `""` 随时可清除。retrieval_services.py 已先行实现同模式(复查确认)。存量密钥无需迁移:端点不变继续有效,改端点即要求重新输入。

**验收标准**:
- [x] 保存 base_url 为新值且不带 api_key 字段 → 旧密钥不随新端点发送。`tests/test_model_wire.py::test_endpoint_change_drops_stored_key`(直接调 `llm.save_settings`,不经前端)
- [x] 测试连接/研究调用时,`resolved()` 返回的密钥只来自与当前端点匹配的存储项。同上 + `test_unchanged_endpoint_keeps_stored_key`
- [x] 增加回归测试覆盖「改地址后密钥外泄」场景(直接调 API,不经前端)。已覆盖

### P0-3 修正轮检索词不再被截断丢弃 【已修复】

**现状**:`app/research_agent/runner.py:246`(240、291 亦同)。模型修正查询 `plan.queries` 拼接在 needs 展开之后,`[:9]` 截断。needs 累计 ≥8 条时修正查询全部被丢弃,第 2/3 轮只能重复旧检索。

**需求**:
- `plan.queries` 置于 needs 展开之前,或为修正查询单独预留名额。
- 检索查询预算(去重后上限)在日志/进度中可核对。

**修复**(R3):`app/research_agent/runner.py:246` plan.queries 移至 needs 展开之前(focus、事实问题、修正查询、needs 依次去重后取前 9);第 291 行 source_pass 追加逻辑不变,修正查询位置靠前不再被挤掉。检索计划在进度区「检索计划就绪」中原样可见。

**验收标准**:
- [x] needs=8 且 plan.queries=3 时,第 2 轮实际检索包含至少 2 条修正查询。`tests/test_agent.py::test_repair_queries_survive_many_authorities`
- [x] 新增单测:多法源场景下第二轮检索词集合 ≠ 第一轮。同上(8 个合成法源 + 3 条修正查询)

### P0-4 条号精确匹配必须绑定法规主体 【已修复】

**现状**:`app/research_agent/retrieval.py:159,172`。`len(unique_laws)==1` 时,查询中任意「第N条」都成为该法精确命中。仅导入《民法典》时问「刑法第264条」,民法典第 264 条被置顶。现有测试 `tests/test_integrity.py:78-83` 只覆盖「民法典第577条」带库内法名的情形,未覆盖裸条号/未知法名。

**需求**:
- 查询点名其他法规(即使该法规未入库)时,禁止无主体条号匹配。
- 仅当查询未点名任何法规、或点名法规与候选同法规时,条号才可精确加分。
- 未入库法规的点名应转为「缺少法源」提示,而非错误召回。

**修复**(R3):`app/research_agent/retrieval.py:rank()` 新增「查询点名的未入库法规」检测(《》显式名称 + 裸名称紧邻条号模式,含法/条例/规定/办法/解释/规则/细则/决定/公约/通则/法典/典等后缀,过滤「本法/有关规定」等指代词);检测到未入库点名时禁止无主体条号精确匹配,并在检索警告中提示「查询点名了未入库法规:…请补录或核对法规名称」;法源缺口仍由模型 missing_authorities 流程转成「待补齐的法律依据」。

**验收标准**:
- [x] 单法库(民法典)下问「刑法第264条」→ 民法典 264 条不得进入精确匹配通道。`tests/test_integrity.py::test_foreign_law_reference_not_exact_in_single_library`
- [x] 新增单测覆盖「点名他法 + 单法库」「裸条号 + 单法库」场景。同上(裸条号「第264条」仍精确命中,`exact` 通道可见)
- [x] 回答显示「待补齐的法律依据:刑法」而非错误引用。检索警告点名「刑法」并要求补录;配合既有 missing_authorities 提示

### P0-5 条号/法规名比较统一规范化(article_key / law_key)【已修复】

**现状**:`app/research_agent/retrieval.py:161,236-238`、`verification.py:20,26-27`。`app/ingest.py:12` 已支持「之1」,但两处正则仍仅认中文数字;跨法引用展开与引用校验仍是原始字符串比较。导致:
- 正文「本法第一百二十三条」与标题「第123条」不一致时,同法引用展开漏召回。
- 「第134条之1」(阿拉伯数字+之)不互认,可误拒亦可误放行。
- 裸名称「民法典第五百七十七条」(无书名号)可引用同号他法条文通过确定性校验。

**需求**:
- 所有条号比较(引用校验、同法展开、`之`后缀)统一经 `ingest.article_key` 规范化;`之` 后缀正则与 `ingest.ARTICLE` 对齐(接受阿拉伯+中文数字)。
- 法规名匹配支持《》剥离后的裸名称,与法库 `law_key` 对齐。

**修复**(R3):
- `retrieval.py`:qnumbers/同法展开的「之」后缀正则改为 `之+NUM`(与 ingest.ARTICLE 对齐);同法展开改为 `article_key` 集合比较。
- `verification.py`:条号校验经 `article_key` 规范化(数字写法不同不再误拒);《》名称经 `law_key` 规范化;新增裸名称绑定——结论中「法名+第N条」紧邻出现时,该 (法名, 条号) 必须存在于该法的引用证据中(runner 传入法库目录法名),否则拒绝。

**验收标准**:
- [x] 标题「第123条」+ 正文「本法第一百二十三条」→ 展开命中。`tests/test_integrity.py::test_cross_reference_expansion_normalized`
- [x] 「第134条之1」与「第134条之一」互认,且与第134条不互认。`test_zhisuffix_arabic_chinese_interop`(检索)+ `test_zhisuffix_verification`(校验)
- [x] 引用「民法典第五百七十七条」→ 只能匹配民法典第 577 条,不匹配他法。`test_bare_law_name_binding`;另 `test_claim_article_numeral_forms_normalized` 覆盖数字写法互认
- [x] 上述场景各有一条回归测试。已各配至少一条

### P0-6 ~~覆盖检查异常时旧结论与未复核新证据一起返回~~ 【已修复】

**现状**:已随重构修复。`app/research_agent/runner.py:216-221,271-274`:新增官方文本或证据 id 变化即清空 claims/citations/verification;异常路径只经 finish 返回当前已复核结论。旧 assess 逻辑已删除。

**验证**:`tests/test_agent.py:46-68` 的「先批准→新增→失败」测试有效。

---

## 4. P1:安全加固

### P1-1 会话 Cookie 安全默认 【未修复】

**现状**:`app/main.py:124` 仍仅 `LEXCITE_SECURE_COOKIE=="1"` 才设 Secure,compose 默认 0。

**需求**:`LEXCITE_PUBLIC_URL` 为 https 时强制 Secure;以 http 启动且非 loopback 绑定时打印告警;部署文档补充 HSTS 配置。

**验收**:PUBLIC_URL=https 且 LEXCITE_SECURE_COOKIE=0 → Cookie 仍带 Secure;告警可见于日志。

### P1-2 模型/检索/搜索端点 SSRF 防护 【未修复】

**现状**:`app/models.py:96` 仍仅拦 `169.254.169.254` 与 `metadata.google.internal`;127.0.0.1、内网段、IPv6 均放行;「测试连接」可探测内网,`app/llm.py:291` 回显错误 JSON 构成半盲 SSRF。

**需求**:解析 base_url 后拒绝私有/回环/链路本地/元数据网段(含 IPv6 映射);连接钉扎解析后 IP;本地 Ollama 走显式白名单(host.docker.internal / ollama 服务名)。

**验收**:base_url=http://127.0.0.1:9000、http://[::1] 等 → 保存被拒绝并给出原因;白名单内地址可保存并测试。

### P1-3 鉴权前不再缓冲大请求体 【未修复】

**现状**:`app/main.py:53-65` 中间件在鉴权依赖之前读满 22MB body,未登录者可反复 POST 大包耗尽单 worker 内存。

**需求**:鉴权失败时提前断开(401 优先于 body 解析,用 Content-Length 预判);体上限降至合理值(如 8MB);文档建议反代限制并发与 body。

**验收**:未登录 POST 大包 → 401 且不触发完整 body 解析。

### P1-4 数据文件权限收紧 【未修复】

**现状**:`app/db.py:20-28` 数据目录 0755、SQLite 0644;直接 Python 部署且主机多用户时案卷可被他人直读。

**需求**:数据目录 0700、DB/WAL/SHM 0600;启动时自动纠正既有权限;`secret.key` 保持 0600。

**验收**:`ls -l data/` 与 `lexcite.sqlite3` 权限符合;多用户主机上非属主读取被拒。

### P1-5 初始化码不出现在 stdout 【未修复】

**现状**:`app/auth.py:34` 初始化码打印到 stdout,未初始化期间每次重启重印;能读日志者可在管理员之前抢占初始化。

**需求**:初始化码写入一次性文件(已 0600),从日志移除;改为首次部署时提示「初始化码见 data/setup-code.txt」。

**验收**:启动日志不含初始化码;文件读取后流程不变。

### P1-6 Origin 白名单不依赖 Host 头 【未修复】

**现状**:`app/main.py:48` 白名单仍由 `request.base_url`(Host 头)推导(另加 PUBLIC_URL);反代透传任意 Host 时可绕过。

**需求**:白名单以 `LEXCITE_PUBLIC_URL` 为固定来源;Host 头与 PUBLIC_URL 不符时拒绝或忽略 Origin 校验走 SameSite 主防线。

**验收**:伪造 Host 头的跨站请求仍被 SameSite=Strict + 自定义头拦下;测试覆盖。

### P1-7 前端链接渲染仅允许 http/https 【未修复,降级为低危】

**现状**:`static/app.js:108` 搜索结果 URL 经 `esc()` 后直接渲染为 `<a href>`,未校验 scheme。**R2 复核补充**:`app/websearch.py:110-115` 入库前经 official_url 限制 https+gov.cn/npc.gov.cn,**新数据无法注入**;且 CSP 可挡 `javascript:` 执行。剩余风险仅存在于历史数据/库被篡改场景,属防御纵深。

**需求**:渲染前校验 `new URL(...).protocol` 为 http/https;非 http(s) 降级为纯文本。

**验收**:构造 javascript: 协议的搜索结果 → 页面显示为不可点击文本。

---

## 5. P1:导入/解析可靠性(全部未修复,ingest.py 未被重构触及)

| 编号 | 现状(位置不变) | 需求 | 验收要点 |
|---|---|---|---|
| P1-8 | `app/ingest.py:90` 章节行污染上一条正文 | 章节/分编行独立识别并从 body 剔除,记录 heading_path | 导入含章标题文本 → 正文不含章节行 |
| P1-9 | `app/ingest.py:42-57,75-80` 引用启发式吞真实条标题 | 后文为清晰条号模式时优先按条号断;疑似误并边界显式告警 | 「…依照下列规定\n第二条…」拆分正确或阻断告警,不静默合并 |
| P1-10 | `app/ingest.py:118-127` SHA256 不可审计 | 保存压缩原文快照;统一哈希对象;引用卡片提供重算入口 | 卡片 SHA256 可由存档重算一致;文档明确哈希口径 |
| P1-11 | `app/ingest.py:149-160`、`app/main.py:340-347` PDF/DOCX 静默丢失+阻塞事件循环 | 按页空文本比例/耗时拒绝可疑文件;解析移入线程池;DOCX 合并表格/页眉页脚 | 20MB 扫描 PDF → 明确报错;上传期间其他请求不显著延迟 |
| P1-12 | `app/research_agent/authorities.py:223-225`(已迁入新包)阻断仅查 reviewed_at 真值 | 复用 corpus 的 review_days 截止与辖区判定;不满足阻断条件时降级 warning | 本地同名版本复核已过期 → 联网补查不再被误阻断 |
| P1-13 | 重复导入无查重 | 按 (canonical_name, version, jurisdiction) 或 SHA256 查重,返回已有记录 ID | 同正文重复导入 → 返回「已存在法规 X」而非 409 |
| P1-14 | `app/ingest.py:135-141` Markdown 标记破坏拆条 | 提取时剥离行首 `#`、`*`、`-`;补测试 | Markdown 导入拆条数与纯文本一致 |

---

## 6. P1:工程健壮性

### P1-15 索引任务取消竞态终态化 【未修复】

**现状**:`app/indexing.py:198-215`(未重构)。线程启动前 `_stop` 已置位时,worker 直接退出且不更新任务状态,任务永久 queued。

**需求**:worker 的 finally 对非终态任务强制置 interrupted;提交与启动同锁。

**验收**:提交后立即取消 → 任务最终进入 interrupted/failed,不残留 queued。

### P1-16 研究任务取消检查去数据库化 【未修复】

**现状**:`app/jobs.py:100-103` `cancelled()` 每次新建 SQLite 连接;`app/llm.py:238-239` 流式逐行调 control→check→cancelled,长思考流可产生上千次查询。jobs.py 本次重构仅改 import 别名。

**需求**:内存 Event + 限频落库(如 1s 一次)。

**验收**:取消延迟 < 1s;流式期间 SQLite 查询次数与行数无关。

### P1-17 检索全表扫描下推与缓存 【未修复,已迁入新包】

**现状**:`app/research_agent/retrieval.py:50-55,87-107`、`runner.py:267-269,289-292` 每轮多次 `SELECT * FROM laws/articles` 再 Python 过滤。

**需求**:SQL 按 law_id/日期/辖区过滤,单次查询缓存复用。

**验收**:法库 5000 条时单轮检索 SQLite 读取行数明显下降(基线对比)。

### P1-18 LLM 调用退避重试与墙钟预算 【未修复】

**现状**:`app/llm.py:216-289` 单次 httpx 请求,无重试/退避,429/5xx 直接抛 ModelError;SSE 可拖到总时限。

**需求**:限次重试(≤2)并尊重 Retry-After;单次调用受墙钟硬预算约束。

**验收**:模拟 429 + Retry-After: 2 → 自动重试且总时长受控;SSE 挂起 → 到点强制中断。

### P1-19 无新证据时提前结束研究 【部分修复】

**现状**:`app/research_agent/runner.py:275` 已加入 `ids==last_ids` 时的 break,**但仅在证据为空或 Plan=clarify 时生效**;无「无新缺口」判定、未用 collector.attempted,Plan=retrieve 仍跑满轮(测试锁定 3 轮)。

**需求**:按 collector.attempted / 缺口无变化判定无新证据即结束,覆盖 Plan=retrieve 路径。

**验收**:全部缺口已尝试且无新候选 → 第 2 轮结束,不再空转第 3 轮。

### P1-20 模型声明的缺失法源须经服务端判定 【未修复,已迁入新包】

**现状**:`app/research_agent/runner.py:133-137,289`。先 gaps() 再按 law_key 把模型 explicit 项**无条件追加**为 not_retrieved;实际可用(已被 gaps 跳过)的法源也造假缺口并致 partial 状态。

**需求**:explicit 项先经 authority_gaps 判定;仅真正缺失才进入缺口。

**验收**:模型误报已存在法源 → 不产生假缺口、不触发联网读取。

---

## 7. P2:评测与可观测

### P2-1 法律评测集与 CI 门禁

**需求**:
- 标注集扩展为「法规名称+版本+条号」粒度;至少覆盖:缺关键事实、虚构法条、过期法、新旧版本冲突、地区不符、通则与例外、遗漏司法解释、跨法规问题、提示注入、多轮记忆污染(现 `evals/README.md` 已列清单)。
- 建立人工标注保留集(不参与提示词调整),由律师评阅 `human_legal_correctness` 与 `human_citation_entailment`。
- CI 门禁:Recall@20 / MRR@20 / nDCG@10 回归告警;错引率、未支持结论率纳入报告。

**验收**:评测命令一键运行并输出 CI 可解析报告;指标回退自动失败。

### P2-2 调用级遥测

**需求**:记录每次 LLM/向量/重排/搜索调用的耗时、输入输出 token、费用估算、重试次数、http_status,聚合到研究任务与全局仪表。

**验收**:单次研究结束后可查看逐调用账单明细;支持多模型成本对比。

### P2-3 检索质量增强

**需求**:
- 分词归一:法规简称/旧称映射、繁简转换、同义扩展。
- 维度漂移、复核过期、索引覆盖率从被动提示升级为设置页主动告警。

**验收**:简称/繁体查询召回与全称一致(受控用例);告警在保存配置后 1 分钟内出现。

### P2-4 任务恢复

**需求**:服务重启后 interrupted 任务可手动续跑;续跑前展示将发生的模型调用次数与预估费用并二次确认。

**验收**:研究进行中重启 → 恢复后任务显示「已中断,可续跑」;续跑不重复计费已完成的调用。

### P2-5 备份归档校验

**需求**:备份包带校验和与恢复演练说明;恢复前校验完整性。

**验收**:篡改备份文件 → 恢复被拒绝。

---

## 8. P2:产品能力

### P2-6 批量导入与进度

**需求**:多文件批量导入、逐文件状态与失败重试、批量复核操作。

### P2-7 法条变更追踪

**需求**:修订对照视图(新旧版本 diff)、按事件日期自动选版(现仅冲突阻断)、变更通知。

### P2-8 区划层级

**需求**:省-市-区县层级表与跨层级适用规则;现为精确匹配。

### P2-9 复核到期提醒

**需求**:复核到期前 N 天在界面提示;逾期法规置顶于「需重新复核」队列。

### P2-10 检索预算可配置

**需求**:轮数/调用次数/搜索读取上限从硬编码改为按工作空间可配,并展示每次研究将消耗的预算。

---

## 9. P3:多租户与权限(独立里程碑)

**背景**:当前单密码共享工作空间,同一密码可见所有案件(README、ARCHITECTURE 已承认)。

- **P3-1 用户与角色**:独立用户、管理员/资料员/办案人员角色。
- **P3-2 案件权限**:案件归属、可共享范围、跨用户访问控制。
- **P3-3 审计归属**:审计记录操作者身份;登录成功/失败、导出、删除、设置变更全量记录;防篡改(哈希链或只追加存储)+ 查看入口。
- **P3-4 认证增强**:密码修改与轮换、会话列表与远程注销、MFA/SSO 预留。
- **P3-5 存储加密**:案件内容字段级加密(密钥轮换);当前仅 Fernet 保护模型密钥,且密钥与 DB 同卷。

**里程碑建议**:M1 用户+角色+案件权限 → M2 审计 → M3 认证增强 → M4 加密。

---

## 10. 重构引入的新问题(R2 发现)

| 编号 | 严重度 | 位置 | 问题 | 需求 |
|---|---|---|---|---|
| N1 | 中 | tests/test_agent.py:75/99/116、test_core_research.py:193/213/315、test_memory_reasoning.py:72、tests/browser_fixture_app.py:55/67/73 | CoverageAudit 已改为本地构造,不再有独立模型调用,但测试仍保留 `schema is CoverageAudit` 死分支;覆盖步骤增删时这些测试仍绿,失去保护 | 清理死分支,按新流程重写覆盖步骤测试 |
| N2 | 中 | tests/test_integrity.py:78-83 | P0-3/P0-4/P0-5/P1-12/P1-20 均无回归测试;现有测试只覆盖库内法名与中文「之一」 | 每个 P0/P1 修复配套至少一条回归测试(见各节验收标准)。R3:P0-1~P0-5 已配齐 14 条(见各节);P1 待修时补 |
| N3 | 低 | app/research_agent/runner.py:221 | `source_timing` 置于 finally,异常、取消、同轮空 acquire 都会 emit;正常「补查无新增」在进度区显示为警告 | emit 仅限实际新增来源;无新增时标记 skipped |
| N4 | 低 | tests/browser_fixture_app.py:69 | 无默认值的 `next()`;首个离线提问时 evidence 为空抛 StopIteration → 500(旧代码同样会崩,非新回归) | next(..., None) 或按空 evidence 返回缺失法源 |
| N5 | 低 | app/research_agent/schemas.py | 错误阶段 JSON 静默解析为空 Draft,报 no_supported_answer 而非 model_error(迁移保留行为) | 阶段不匹配时显式报模型结构错误 |

**重构质量结论(正面)**:context.py 与旧 case_state.py 函数字节码一致;authorities/retrieval/verification 仅导入层级差异;runner 各 helper 字节码一致;无循环依赖;文档与实现一致;测试未改弱(新增 9 项,memory 测试新增 usage 断言,source fixture 比原先严格)。

---

## 11. 非功能需求

| 项 | 要求 |
|---|---|
| 性能 | 单轮检索 SQLite 读取量较基线下降;5000 条法库单轮 < 2s(参考机) |
| 安全 | 通过 P0/P1 全部验收;新增 SSRF/锁死/密钥外泄回归测试 |
| 可观测 | 调用级遥测(P2-2);任务与索引状态无永久中间态 |
| 兼容 | Python 3.12、Docker Compose 单 worker 不变;SQLite 迁移保持加列式,历史引用快照不改写 |
| 文档 | DEPLOYMENT 补反代/HSTS/权限章节;USER_GUIDE 同步新功能 |

## 12. 发布与验收

1. **回归基线**:pytest 232 项全通过(R3 修复后,含 14 条新增 P0 回归)+ 59 项浏览器检查。
2. **新增测试**:本 PRD 每个「验收标准」至少一条自动化测试;P0 全部有测试(N2)。
3. **真实服务冒烟**:发布前至少完成一次真实国产 API(聊天/向量/重排)+ 本地 Ollama 的端到端验证(1.3.x 验证记录明确未做过,此项为 1.4.0 发布门槛)。
4. **法律评测**:P2-1 保留集跑通并记录指标;不承诺准确率,但报告必须可复现。
5. **安全回归**:SSRF、锁死 DoS、密钥外泄、权限各一项负向测试。

## 13. 风险与边界

- **SSRF 收紧与本地服务冲突**:P1-2 的白名单必须覆盖 host.docker.internal/ollama/reranker 三种既有部署形态,避免修复引入部署回归。
- **密钥绑定迁移**:P0-2 已按「端点不变自动保留、端点变化即清除」实现,存量密钥无需迁移;改端点后需重新输入密钥(前端提示见 P1 文档工作)。
- **法律边界不变**:引用原文匹配与模型复核不保证法律结论正确;评测指标不构成「法律准确率」声明。
- **单进程边界不变**:多租户(P3)落地前仍需单 worker + 线程锁模型,不引入分布式部署。

---

## 附录 A:发现清单(两轮审查,R2 已逐项复核)

| # | 严重度 | 位置(R2 当前) | 摘要 | 状态 | 对应需求 |
|---|---|---|---|---|---|
| 1 | 高 | auth.py:48-54, main.py:122, Dockerfile:15 | 反代下登录限流可匿名锁死全员 | **已修复** | P0-1 ✅ |
| 2 | 高 | llm.py:134-142 | 改 base_url 后旧密钥发往新端点 | **已修复** | P0-2 ✅ |
| 3 | 高 | research_agent/runner.py:246 | 修正轮检索词被 [:9] 截断丢弃 | **已修复** | P0-3 ✅ |
| 4 | 高 | research_agent/retrieval.py:159,172 | 单法库任意「第N条」成为精确命中 | **已修复** | P0-4 ✅ |
| 5 | 高 | research_agent/retrieval.py:161,236-238; verification.py:20,26-27 | 条号原始字符串比较;「之」后缀仅中文数字 | **已修复** | P0-5 ✅ |
| 6 | 中 | research_agent/runner.py:216-221,271-274 | 覆盖异常跳过结论清空分支 | **已修复** | P0-6 ✅ |
| 7 | 中 | main.py:124 | Cookie Secure 默认关 | 未修复 | P1-1 |
| 8 | 中 | models.py:96 | 测试连接半盲 SSRF | 未修复 | P1-2 |
| 9 | 中 | main.py:53-65 | 鉴权前缓冲 22MB body | 未修复 | P1-3 |
| 10 | 中 | db.py:20-28 | SQLite 0644,案卷可直读 | 未修复 | P1-4 |
| 11 | 低 | auth.py:34 | 初始化码打印 stdout | 未修复 | P1-5 |
| 12 | 低 | main.py:48 | Origin 白名单依赖 Host 头 | 未修复 | P1-6 |
| 13 | 低 | static/app.js:108 | 链接渲染未校验 scheme | 未修复(上游缓解,降为低) | P1-7 |
| 14 | 中 | ingest.py:90 | 章节行污染条文正文 | 未修复 | P1-8 |
| 15 | 中 | ingest.py:42-57,75-80 | 引用启发式吞真实条标题 | 未修复 | P1-9 |
| 16 | 中 | ingest.py:118-127 | SHA256 不可审计 | 未修复 | P1-10 |
| 17 | 中 | ingest.py:149-160, main.py:340-347 | PDF/DOCX 静默丢失+阻塞事件循环 | 未修复 | P1-11 |
| 18 | 低 | research_agent/authorities.py:223-225 | 联网阻断未套复核周期/辖区 | 未修复 | P1-12 |
| 19 | 低 | ingest.py:117-127, db.py:61 | 重复导入无查重 | 未修复 | P1-13 |
| 20 | 低 | ingest.py:135-141 | Markdown 标记破坏拆条 | 未修复 | P1-14 |
| 21 | 中 | indexing.py:198-215 | 取消竞态任务永久 queued | 未修复 | P1-15 |
| 22 | 中 | jobs.py:100-103, llm.py:238-239 | cancelled() 每行新建 SQLite 连接 | 未修复 | P1-16 |
| 23 | 中 | research_agent/retrieval.py:50-55,87-107 | 每轮多次全表扫描 | 未修复 | P1-17 |
| 24 | 低 | llm.py:216-289 | 无退避重试,SSE 可拖到总时限 | 未修复 | P1-18 |
| 25 | 低 | research_agent/runner.py:275 | 无新证据仍跑满 3 轮 | 部分修复 | P1-19 |
| 26 | 中 | research_agent/runner.py:133-137,289 | 模型误报产生假缺口 | 未修复 | P1-20 |
| 27 | 中 | research_agent/retrieval.py:161 | 「之」后缀仅中文数字 | **已修复**(并入 P0-5) | P0-5 ✅ |
| N1 | 中 | tests/* 多处 | CoverageAudit 测试死分支 | 新增 | §10 N1 |
| N2 | 中 | 全库 | P0 缺陷无回归测试 | 新增 | §10 N2 |
| N3 | 低 | research_agent/runner.py:221 | finally 伪警告 | 新增 | §10 N3 |
| N4 | 低 | tests/browser_fixture_app.py:69 | next() 空 evidence 抛异常 → 500 | 新增 | §10 N4 |
| N5 | 低 | research_agent/schemas.py | 阶段不匹配静默变空 Draft | 新增 | §10 N5 |

**两轮基线**:R1(重构前)209 项通过;R2(重构后 acb2c23)218 项通过。重构为结构迁移,除 P0-6 外未修复已知缺陷;P1-19 部分修复。
