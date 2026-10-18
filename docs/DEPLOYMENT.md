# 部署与维护手册

## 1. 选择部署方式

| 场景 | 推荐方式 | 日常使用者需要做什么 |
|---|---|---|
| 个人电脑试用，使用国产API | Docker Desktop + 本项目 | 浏览器登录、配置模型、导入法规 |
| 办公内网，资料不发送云端 | 本项目 + 本地Ollama/vLLM | 浏览器登录；模型和法条均由本地管理员维护 |
| 服务器部署给同一可信团队 | Docker + HTTPS反向代理 | 浏览器访问工作空间，使用共同密码 |

第三种是一个共享工作空间，不包含人员/案件分级权限。多个机构或需隔离案卷的人员不能共用这一个实例；可为独立工作空间部署独立实例和独立数据卷。

开发时实际运行的是Python服务；本环境没有Docker守护进程，Docker镜像构建和GPU部署没有被本次实测。文件提供的是完整部署配置，第一次目标机安装须按以下步骤验收。

## 2. Docker启动

解压进入项目目录，安装并启动Docker。

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=30 lexcite
```

默认地址：http://localhost:8080。未设置管理员密码时，日志中会给出首次初始化码；仅用一次，随后登录使用自己设置的密码。

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

使用“其他兼容接口”，填`http://模型服务地址:端口/v1`与实际服务模型名。服务应支持 chat completions 并能输出符合提示词 schema 的 JSON；不支持接口强制 JSON 的端点可选择“提示词 JSON”模式。不支持结构化输出的模型会被本地验证拦截，不会静默当成正常答案。

## 4. 国产云API

界面预设：

| 服务 | 示例地址 | 示例模型名称 |
|---|---|---|
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen3-8B` |

服务商可能调整模型名、开通权限、兼容能力与地域端点，务必填账户当前可用值。“测试模型连接”验证的是保存后的地址、认证和结构化输出。它不评估法律专业水平。

分析/复核、向量、重排分别配置，地址、模型和密钥可以不同。分析选择 DeepSeek 时，可单独用阿里/硅基向量与重排，也可用本地 Ollama 向量和本地 Qwen3 重排。阿里 `qwen3-rerank` 使用 DashScope 原生接口，根路径为 `/api/v1`，程序追加 `/services/rerank/text-rerank/text-rerank`；向量根路径为 `/compatible-mode/v1`。请使用与密钥地域一致的域名，配置表见 [检索配置](RETRIEVAL.md)。

研究 Agent 的轮数、总时限及思考模式可在页面配置，chat 调用有18次硬上限；embedding 和 rerank 另外计数并消耗总时限。一次混合检索最多将3个查询批量编码；建库按法条切窗口，每批最多8个片段。云端分析接收研究上下文，云端向量接收法规片段与检索查询，云端重排接收查询和候选片段；部署时分别选择这些资料的接收端点。

模型密钥存于服务器；浏览器只看到“是否已保存”，不会获得明文密钥。切换服务时界面会清除旧服务密钥。

项目根目录 `.env` 按分析模型的本地/API 两种运行方式，以及 Embedding、Reranker、Search 分组列出默认 provider、地址、模型和密钥。模型设置页先选择“本地模型”或“云端 API”，页面会切换到对应默认 provider、地址和模型。Docker Compose 会将这些变量传给服务；直接用 Python 启动时，应用也会从根目录读取 `.env`。只有尚未在页面保存的字段才使用 `.env` 默认值；页面保存的 provider、地址、模型和密钥优先。密钥只在服务器端使用，页面仅显示其来源状态，不会接收明文。`.env` 已加入忽略列表，请勿提交；共享主机上应限制文件权限。

索引在后台运行，每次最多并发4个 embedding 请求、每个请求最多8个片段，并在完整法条向量全部返回后一次性发布。中断或失败时已完成的法条会保留；再次点击“建立 / 补齐向量索引”只处理当前缺失或失败的法条。重排需等待本轮召回候选生成后才能运行，不与其并行，以免改变候选集合。

## 5. 法规导入与更新

支持TXT、MD、文字型PDF及正文段落型DOCX。推荐使用官方TXT或粘贴清洁正文。表格型DOCX、复杂PDF排版和扫描文件请先转为清洁文本再导入；程序不包含OCR引擎。

1.0.1支持“上传文件 / 粘贴正文”和“读取官网链接”两种入口。文件方式的官网链接选填；链接方式先读取官网正文，不再要求上传文件。通用链接支持gov.cn、npc.gov.cn的HTTPS原文网页及可解析附件，下载上限8MB；受限或动态页面可改用文件导入。通用读取不自动保证法规完整或最新。

每部法规一个版本一条记录。正文各条须以独立行的“第X条”开头。预览条数和内容，再核对来源、日期与地区；人工确认后才纳入回答。官网读取民法典的入口具有完整性检查，访问失败时不会导入半份内容。

新版本应新增记录，并给旧版填失效日。已录入原文不提供原地静默修改；发现录错，可删除后重新导入、重新复核。历史答案保留原引用快照。

升级到1.3.0请参考[升级步骤](UPGRADE_1.3.0.md)：沿用原项目目录、Compose项目名和数据卷，保留`.env`及数据目录；首次启动自动增加检索设置和索引表，无须重新初始化工作空间。旧向量会保留但不参与新检索，需在新设置中确认服务并重建索引。

## 6. Python直接运行

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1
```

Windows激活命令为`.venv\Scripts\activate`。默认数据目录`data/`；可通过`LEXCITE_DATA`指定绝对路径。只运行一个worker；当前并发控制是单进程锁。

## 7. HTTPS与内网访问

默认Compose只绑定127.0.0.1，建议用单位已有反向代理提供HTTPS。设置`LEXCITE_PUBLIC_URL=https://实际域名`和`LEXCITE_SECURE_COOKIE=1`。代理需把请求 Host 传给应用。1.1 网页创建后台研究任务后通过短请求轮询状态，不需要让一条 HTTP 请求持续等待整个研究；保留的旧 `/ask` 接口仍是同步长请求，使用它的客户端需自行设置足够超时。始终只运行一个 Uvicorn worker。

示例Nginx位置块（证书及server配置由管理员接入）：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_read_timeout 420s;
    client_max_body_size 22m;
}
```

如提高模型单次超时，代理超时也须相应提高。不要先向公网开放首次初始化页面。该实例只有一个管理员身份，不支持MFA/SSO和细分访问权限。

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
LEXCITE_DATA="$(pwd)/restored-data" python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
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
