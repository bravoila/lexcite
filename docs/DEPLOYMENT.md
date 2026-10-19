# 部署与维护手册

## 1. 选择部署方式

| 场景 | 推荐方式 | 日常使用者需要做什么 |
|---|---|---|
| 个人电脑试用，使用国产API | Docker Desktop + 本项目 | 浏览器登录、配置模型、导入法规 |
| 办公内网，资料不发送云端 | 本项目 + 本地Ollama/vLLM | 浏览器登录；模型和法条均由本地管理员维护 |
| 服务器部署给同一可信团队 | Docker + HTTPS反向代理 | 浏览器访问工作空间，使用共同密码 |

第三种是一个共享工作空间，不包含人员/案件分级权限。多个机构或需隔离案卷的人员不能共用这一个实例；可为独立工作空间部署独立实例和独立数据卷。

当前版本为1.4.0。实际通过的检查和仍待完成的真实服务验收见[本版验证记录](VERIFICATION_1.4.0.md)，不要将协议夹具或启动成功等同于模型效果、GPU部署或法律准确率验收。

## 2. Docker启动

解压进入项目目录，安装并启动Docker。

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=30 lexcite
```

默认地址：http://localhost:8080。Origin 白名单固定允许该地址及
http://127.0.0.1:8080，不再依据请求 Host 自动放行。若浏览器使用其他地址或端口，
请在`.env`中把`LEXCITE_PUBLIC_URL`设为完全匹配的 scheme/host/port；公网部署应使用 HTTPS。
未设置初始密码时，服务就绪后由管理员读取码文件：

```bash
docker compose exec lexcite cat data/setup-code.txt
```

初始化码不写入日志，只用于创建至少12字符的工作空间密码；码文件权限为0600，成功初始化后删除。已初始化或首次通过`LEXCITE_ADMIN_PASSWORD`设定密码时没有此文件，直接登录。读取失败先检查服务是否就绪及当前实例是否已经初始化，不要删除数据卷。

可先复制`.env.example`为`.env`，在其中填写至少12字符的`LEXCITE_ADMIN_PASSWORD`。该变量只用于**首次**初始化，修改它不会覆盖数据库里已有密码。不要把真实`.env`发给别人或提交到git。

Windows双击`start.bat`；macOS/Linux运行`bash start.sh`。首次构建需联网下载依赖，之后原文检索可离线使用。

停止与重启：

```bash
docker compose stop
docker compose start
```

更新代码后重建：

```bash
docker compose up -d --build
```

数据存在名为`lexcite_data`的Compose数据卷。不要使用`docker compose down -v`，它会删除卷中的案件、法库和设置。

## 3. 本地中国模型

### 组合部署：不公开Ollama端口

```bash
docker compose -f compose.yaml -f compose.ollama.yaml up -d --build
docker compose -f compose.yaml -f compose.ollama.yaml exec ollama ollama pull qwen3:8b
```

模型设置：

| 字段 | 值 |
|---|---|
| 服务类型 | 本地 Ollama / 千问 |
| 地址 | `http://ollama:11434/v1` |
| 模型 | `qwen3:8b` |
| 密钥 | 留空 |
| 复核模型 | 留空，或已经拉取的更强模型名 |

CPU组合主要便于验证链路，不承诺可接受的交互速度。Qwen3:8b在Ollama所列权重下载约5.2GB，实际运行还需要上下文缓存和运行内存，不能按文件大小等同计算内存。按目标机器实测选择8B、14B或更大模型，并调整上下文和超时。

语义检索使用独立的向量服务。推荐先拉取：

```bash
docker compose -f compose.yaml -f compose.ollama.yaml exec ollama ollama pull qwen3-embedding:0.6b
```

在“模型与设置 → 语义检索与重排”选择混合模式。向量服务选 Ollama，地址填 `http://ollama:11434`，模型填 `qwen3-embedding:0.6b`。首次可关闭自动索引，保存并测试后点击“建立 / 补齐向量索引”；完成后开启自动索引。也可使用已部署的 `bge-m3`，更换模型后需建立对应索引。

本地重排另启独立服务：

```bash
docker compose -f compose.yaml -f compose.ollama.yaml -f compose.reranker.yaml up -d --build
```

重排选“自建 / 其他兼容服务”，地址 `http://reranker:8081/v1`、模型 `Qwen/Qwen3-Reranker-0.6B`，密钥留空。首次启动需下载权重，默认 CPU；离线模型、GPU 改动和故障排查见 [检索配置](RETRIEVAL.md)。未配置或服务失败时保留 BM25/条号检索，并显示实际降级原因。

### NVIDIA GPU

宿主机先装好驱动和NVIDIA Container Toolkit，然后：

```bash
docker compose -f compose.yaml -f compose.ollama.yaml -f compose.gpu.yaml up -d --build
docker compose -f compose.yaml -f compose.ollama.yaml -f compose.gpu.yaml exec ollama ollama pull qwen3:8b
```

Apple Silicon上建议宿主机安装Ollama，以利用Metal；Linux Docker内Ollama默认CPU或NVIDIA方案。

### 已有宿主机Ollama

应用直接运行于宿主机：地址`http://127.0.0.1:11434/v1`。

应用运行于Docker、Ollama在宿主机：地址`http://host.docker.internal:11434/v1`。宿主Ollama必须监听可从Docker网络访问的接口；例如管理员配置`OLLAMA_HOST=0.0.0.0:11434`，同时通过防火墙仅允许Docker/本机访问。**不要向公网暴露无认证的Ollama端口。**不希望配置这些细节时使用上面的组合部署。

本版将 Ollama 示例镜像从0.11.10更新并固定为0.33.3，官方发布页已核实该版本存在，不使用浮动 latest 标签。此镜像和真实向量权重未在交付环境运行，升级后先测试分析与向量服务；已有宿主机 Ollama 也应更新到支持所选 Qwen3 向量模型的版本。32K上下文可减少长法条被模型服务隐式裁剪的风险；服务端仍需监控实际输入长度。应用按字符预算，不包含精确tokenizer预算器。

### 已有vLLM或其他兼容服务器

使用“其他兼容接口”，填已部署服务的`/v1`根地址与实际模型名。服务应支持 chat completions 并能输出符合提示词 schema 的 JSON；不支持接口强制 JSON 的端点可选择“提示词 JSON”模式。不支持结构化输出的模型会被本地验证拦截，不会静默当成正常答案。

### 服务地址边界

允许公网HTTP/HTTPS端点，以及`localhost`、`host.docker.internal`、`ollama`、`reranker`、`searxng`这些明确的本地服务名；字面回环地址`127.0.0.1`/`::1`仅开放11434和8081端口。其他内网、保留、CGNAT/Tailscale地址及其IPv6映射形式被拒绝。部署其他内网模型时，使用上述受信服务名与受控网络映射，不要把内网IP直接填入配置；不能通过放宽全部地址校验解决。

停用的向量/重排服务（`provider=none`）不连接、不解析其闲置地址。存量配置在使用前也会重校验；无效配置会显示“需修正”，请在设置页重新保存，不会继续请求旧私网端点。未解析域名仍可保存，调用前会重校验，但DNS校验与实际建连之间仍非IP固定传输；管理员应限制可配置人员和出口网络。

## 4. 国产云API

界面预设：

| 服务 | 示例地址 | 示例模型名称 |
|---|---|---|
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen3-8B` |

服务商可能调整模型名、开通权限、兼容能力与地域端点，务必填账户当前可用值。“测试模型连接”验证的是保存后的地址、认证和结构化输出。它不评估法律专业水平。

分析/复核、向量、重排分别配置，地址、模型和密钥可以不同。分析选择 DeepSeek 时，可单独用阿里/硅基向量与重排，也可用本地 Ollama 向量和本地 Qwen3 重排。阿里 `qwen3-rerank` 使用 DashScope 原生接口，根路径为 `/api/v1`，程序追加 `/services/rerank/text-rerank/text-rerank`；向量根路径为 `/compatible-mode/v1`。请使用与密钥地域一致的域名，配置表见 [检索配置](RETRIEVAL.md)。

研究 Agent 的轮数、总时限及思考模式可在页面配置，chat有18次**实际HTTP请求**硬上限，重试也计入；embedding和rerank另外计数并消耗总时限。对429/500/502/503/504最多请求三次，遵循`Retry-After`或指数退避，等待可取消，剩余时间不足时不再重试。JSON与SSE均限制4MB；已建立连接的响应体阻塞读取也受取消与墙钟检查中止，DNS/建连阶段仍依赖底层网络超时。

一次混合检索最多将3个查询批量编码；建库按法条切窗口，每批最多8个片段。云端分析接收研究上下文，云端向量接收法规片段与检索查询，云端重排接收查询和候选片段；部署时分别选择这些资料的接收端点。

模型密钥存于服务器；浏览器只看到“是否已保存”，不会获得明文密钥。**数据库密钥与环境密钥均绑定服务商和地址，分析密钥另绑定本地/API配置档。** 端点改变后旧密钥失效；新端点只有与相应环境配置匹配时才可使用环境密钥，否则须重新提供。API保存时省略密钥或传`null`表示同端点保留；显式空字符串表示清除并停用该项环境回退。网页密钥框留空提交`null`，不等于删除密钥。

项目根目录 `.env` 按分析模型的本地/API 两种运行方式，以及 Embedding、Reranker、Search 分组列出默认 provider、地址、模型和密钥。模型设置页先选择“本地模型”或“云端 API”，页面会切换到对应默认 provider、地址和模型。Docker Compose 会将这些变量传给服务；直接用 Python 启动时，应用也会从根目录读取 `.env`。只有尚未在页面保存的字段才使用 `.env` 默认值；页面保存的 provider、地址、模型和密钥优先。密钥只在服务器端使用，页面仅显示其来源状态，不会接收明文。`.env` 已加入忽略列表，请勿提交；共享主机上应限制文件权限。

索引在后台运行，每次最多并发4个 embedding 请求、每个请求最多8个片段，并在完整法条向量全部返回后一次性发布。中断或失败时已完成的法条会保留；再次点击“建立 / 补齐向量索引”只处理当前缺失或失败的法条。重排需等待本轮召回候选生成后才能运行，不与其并行，以免改变候选集合。

## 5. 法规导入与更新

支持TXT、MD、文字型PDF及DOCX。DOCX递归读取段落、嵌套表格、内容控件、文本框，以及首页/奇偶页的页眉页脚；合并单元格与链接部件去重。复杂版式的阅读顺序仍需核对，推荐官方TXT或清洁正文。PDF不含OCR，扫描、加密、异常压缩或过大文本会明确拒绝；页数上限1500、聚合内容流超过50MB时拒绝、抽取文字上限200万字符。内容流在解码后计数，**不是50MB进程内存沙箱**；不可信复杂附件仍须部署级资源隔离。损坏官方附件只标记该条线索读取失败，不抹掉此前可支持的本地分析。

上传请求总量上限20,000,000字节，包含multipart封装；其他变更请求上限8MB，登录/初始化请求上限16KB。官网原文下载上限8MB。代理可以采用下文`20m`上限，但应用仍按十进制字节上限执行，临近上限的文件应先拆分。

1.0.1支持“上传文件 / 粘贴正文”和“读取官网链接”两种入口。文件方式的官网链接选填；链接方式先读取官网正文，不再要求上传文件。通用链接支持gov.cn、npc.gov.cn的HTTPS原文网页及可解析附件，下载上限8MB；受限或动态页面可改用文件导入。通用读取不自动保证法规完整或最新。

每部法规一个版本一条记录。正文各条须以独立行的“第X条”开头。预览条数和内容，再核对来源、日期与地区；人工确认后才纳入回答。官网读取民法典的入口具有完整性检查，访问失败时不会导入半份内容。

新版本应新增记录，并给旧版填失效日。已录入原文不提供原地静默修改；发现录错，可删除后重新导入、重新复核。历史答案保留原引用快照。

1.4.0统一规范解析副本中的BOM、零宽字符和换行；来源快照与SHA256仍基于提交原文。重复内容原子复用既有版本，导入响应保留拆条警告。**旧版可能已吞条、产生虚假条文或漏读附件，升级不会自动改写既有法库。** 应备份后逐份对照原文重导入、复核；不要只重建向量。

升级到1.4.0请参考[升级步骤](UPGRADE_1.4.0.md)：沿用原项目目录、Compose项目名和数据卷，保留`.env`及数据目录；启动自动增加筛选索引和修订号触发器，无须重新初始化工作空间。

## 6. Python直接运行

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1 --no-proxy-headers --forwarded-allow-ips=
```

Windows激活命令为`.venv\Scripts\activate`。默认数据目录`data/`；可通过`LEXCITE_DATA`指定绝对路径。只运行一个worker；当前并发控制是单进程锁。

首次初始化直接读取对应目录的`setup-code.txt`，如`cat data/setup-code.txt`；PowerShell使用`Get-Content .\data\setup-code.txt`。初始化完成后不存在该文件是正常行为。

## 7. HTTPS与内网访问

默认Compose只绑定127.0.0.1，建议用单位已有反向代理提供HTTPS。设置`LEXCITE_PUBLIC_URL=https://实际域名`、`LEXCITE_SECURE_COOKIE=1`。仅在可信代理部署中再设置`LEXCITE_TRUST_PROXY=1`，保持`LEXCITE_LOCAL_RECOVERY=0`，修改环境变量后重建/重新创建应用容器。所有启动方式均禁用Uvicorn自带代理头解析，由应用按显式开关处理地址。

网页通过短请求轮询后台任务；旧`/ask`仍为同步长请求，但使用相同任务状态机与变更锁。同步客户端需设置足够超时。轮询持续失败会解除界面等待并保留问题；“恢复查看”只读取原任务，不会重复提交。

示例Nginx HTTPS server内的位置块（证书及域名由管理员接入；HTTP server仅重定向到HTTPS）：

```nginx
add_header Strict-Transport-Security "max-age=31536000" always;

location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_read_timeout 420s;
    client_max_body_size 20m;
}
```

**代理必须覆盖客户端传入的X-Forwarded-For，不得原样透传；这里使用`$remote_addr`而非`$proxy_add_x_forwarded_for`。** 后端端口只能被本机/可信代理访问，不能同时向不可信客户端暴露。多级代理先在受控网关核对真实地址，再由末端覆盖此头；不能直接信任互联网提交的链。未设置TRUST_PROXY时应用忽略XFF；即使代理未设XFF或伪造头包含回环地址，也没有默认回环豁免。

共享工作空间采用**正确密码优先**策略：匿名失败计数不会触发所有人共用的十分钟账号封禁。每来源十次失败进入429限制；全局节流使用429和`Retry-After`，不阻塞线程。地址记录有上限、哈希并发有界；极端资源占用仍可能返回短暂429，不等于账号被锁定。仍应在边缘代理配置连接数与速率限制，不能把应用限流当作抗分布式拒绝服务系统。

如提高单次或总研究超时，同步客户端及代理超时也须相应提高。不要先向公网开放首次初始化页面。工作空间不支持MFA/SSO和人员权限分级。

### 权限与本地恢复

应用创建的数据目录为0700，数据库/WAL/SHM、初始化码和`secret.key`为0600；已有密钥文件打开时也会纠正权限。管理员须保护`.env`和备份：POSIX下使用`chmod 600 .env`，数据卷由UID10001拥有且不共享给其他服务；Windows下用账户/NTFS ACL限制访问。密钥、案卷与备份不能上传到公共工单或提交到Git。

`LEXCITE_LOCAL_RECOVERY=1`只用于临时、直接宿主机维护：禁用TRUST_PROXY，隔离公网入口，从真实loopback连接登录且不附带XFF；用完立即恢复为0。它仅豁免本地失败节流，**仍要求正确工作空间密码**，不是密码重置功能；容器网关、代理转发或XFF派生的回环地址不具备豁免。忘记密码仍由部署管理员离线处理并清理旧会话，不得删除数据库“恢复登录”。

## 8. 备份与恢复

备份文件包含案件文本、法库和解密模型密钥所需文件，应按案卷资料保护。

Python部署：

```bash
python scripts/backup.py --data data --output backups/lexcite-backup.zip
```

Docker：

```bash
docker compose exec lexcite python scripts/backup.py --data /app/data --output /app/data/backup.zip
docker compose cp lexcite:/app/data/backup.zip ./lexcite-backup.zip
```

脚本使用SQLite在线backup机制，备份中清除会话令牌，避免复制登录状态。不覆盖已有同名备份；下一次用新的文件名。

恢复：停止应用，保留旧数据目录备用，选择一个空目录：

```bash
python scripts/restore.py lexcite-backup.zip --data restored-data
LEXCITE_DATA="$(pwd)/restored-data" python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1 --no-proxy-headers --forwarded-allow-ips=
```

Docker恢复时，将恢复出的`lexcite.sqlite3`与`secret.key`放回停止运行的应用的数据卷，并保证UID10001可读写。不要把新数据覆盖到正在运行的数据库上。

## 9. 常见问题

| 问题 | 检查 |
|---|---|
| 总提示依据不足 | 是否导入、是否人工复核、是否超出复核周期、日期和地区是否匹配 |
| 提示版本冲突 | 演示摘录是否与完整版同时存在；旧版失效日是否补录 |
| 只有法条，没有分析 | 是否仍为仅检索模式；模型/JSON输出/复核是否失败；展开过程查看原因 |
| HTTP401/403来自模型 | API Key、地域端点、模型开通权限 |
| Ollama连接失败 | 容器中的localhost不是宿主机；检查地址及监听接口 |
| 模型输出被截断 | 提高输出长度，换更大上下文模型或缩小问题；程序会停止生成结论 |
| 法条没有拆开 | 条号须在独立行开始；去掉目录与重复版本，核查OCR |
| 更新密码环境变量没生效 | 变量仅首次初始化使用。忘记密码由管理员离线重置，清除所有已有sessions |

## 10. 目标机验收

先用演示摘录完成一次导入、复核、查询、引用打开、事实保存、重新登录和导出；再导入完整业务法规，并接入真实模型运行`evals`。本次交付不包含模型权重、第三方法库账户或全量现行法律数据库。

## 联网搜索和模型思考

搜索配置保存在独立加密设置中，不复用模型 API Key。博查填写 `https://api.bochaai.com/v1` 和博查密钥；自建 SearXNG 填根地址，并开启 JSON 格式。网页“测试联网搜索”使用固定公开词测试，可能产生搜索费用。开启“联网补齐官方法源”后，会按缺少的法规名称检索；内置目录可直接读取官网。手工公开检索词另行发送。自动搜索不发送案件全文，云端模型仍接收研究上下文。详见 [配置与边界](AGENT_AND_SEARCH.md)。

模型思考默认遵循供应商行为；DeepSeek 用 `thinking.type`，百炼/硅基用 `enable_thinking`，Ollama/自定义兼容端点用 `reasoning_effort`。百炼的思考/默认模式使用流式读取，程序分开收集最终 JSON 和接口实际返回的思考内容，后者由展示开关控制。JSON 自动模式兼容百炼思考输出，本地格式与引用校验始终保留。模型不支持该参数时请切回默认模式；JSON 不稳定、输出截断和法律判断能力都需在目标模型测试，单次连接测试不等于业务验收。
