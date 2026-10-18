# 1.3.0 混合检索说明

## 默认行为与边界

新安装的检索模式默认为 hybrid，但服务初始为“未配置”：不会自动猜测密钥、调用付费服务或下载模型。必须由用户选择向量、重排服务并保存，完成索引后才启用相应路径。未配置、无索引、索引部分覆盖、服务失败均在设置和每次答案记录中明示。

分析/复核继续使用 `llm.py`；向量与重排分别通过 `retrieval_services.py` 配置。两项检索配置互相独立，也不再继承当前聊天服务的密钥。旧版配置仅首次迁移一次。

## 路径

1. 先按正式名称、有效期、地区、人工复核状态和版本冲突筛出可用法条。
2. 多查询 BM25 与可用的向量索引各召回默认40条，明确指定的法规/条号另行保留。向量对最多3个查询批量编码，包括原问题、已收到的事实摘要和可用的研究查询；不使用生成结论作为查询事实。
3. 按法条身份去重，使用 RRF（k=60）合并排名。向量候选不套用跨模型通用的0.40阈值。
4. 默认重排最多60个融合候选；明确指定的条文可额外进入核查。对长条文，取与原问题最相关的两个索引窗口供重排；完整正文仍作为最终候选返回，不以重排分数代替法律支持性判断。
5. 取默认14个候选及明确条号命中，再有限展开同一法规内的明确交叉引用。Agent 继续执行法源覆盖检查、必要的官方补查、完整条文分析、逐字引用校验与模型复核。
6. 官网临时条文只在本次内存中处理，当前走关键词/条号与可选重排，不自动批量向量化整部官网法规；每次记录会明确 `scope=temporary` 和未索引状态。临时条文与法库不能混报向量覆盖。

单字中文与空洞停用词不会作为有效词；关键词正分数是候选信号，不是置信度。小法库不再使用固定BM25总分大于2的门槛，避免精确单词也被拒绝。

## 索引与原文

原始法条保留在 SQLite 的 `articles` 表。`article_headings` 保存新导入正文的显式章节路径。默认1200字符、100字符重叠窗口用于检索，名称、版本、类型、章节、条号作为窗口上下文。

`embedding_states` 记录条文在某输入指纹下的内容哈希和状态；`embedding_chunks` 保存片段和归一化向量。一个条文的全部片段验证完成后才在事务中发布，取消或失败不会暴露半条索引。维度、返回编号、有限数值、零向量均校验。更换模型、维度、地址、版本标记、查询指令、分片规则时使用新指纹；新旧向量不混用。同名模型升级时手工变更版本标记。

索引在后台运行，与研究共用进程内任务锁；已有研究先完成，索引排队。自动索引响应保存配置、导入、复核更名和服务启动；失败条文不会无限自动重试，修正服务后手工补齐。首个服务错误会停止该批任务，保留已完成条文，减少无效收费。UI 的总法条数不等于研究可用数，日期/地区/复核筛选仍在检索时执行。

当前仍用 SQLite JSON 向量和精确余弦扫描，适合当前轻量部署。没有引入 ANN 向量数据库，也没有声称已通过大规模全国法库的并发性能测试。重排不负责认定条文效力、事实真假或结论支持关系。

## 服务预设

| 方式 | 向量地址 / 模型 | 重排地址 / 模型 |
|---|---|---|
| 阿里百炼（北京示例） | `https://dashscope.aliyuncs.com/compatible-mode/v1` / `text-embedding-v4` | `https://dashscope.aliyuncs.com/api/v1` / `qwen3-rerank` |
| 硅基流动 | `https://api.siliconflow.cn/v1` / `Qwen/Qwen3-Embedding-4B` | `https://api.siliconflow.cn/v1` / `Qwen/Qwen3-Reranker-4B` |
| 本地 Compose | `http://ollama:11434` / `qwen3-embedding:0.6b` | `http://reranker:8081/v1` / `Qwen/Qwen3-Reranker-0.6B` |

阿里向量使用兼容 `/embeddings`；北京默认地址为 `https://dashscope.aliyuncs.com/compatible-mode/v1`，也可使用控制台提供的业务空间专属地址。其他地域须使用对应地域的兼容根地址，并确保 API 密钥与模型同地域；多模态向量模型不支持此 OpenAI 兼容接口。阿里 `qwen3-rerank` 使用 DashScope 原生 `/api/v1/services/rerank/text-rerank/text-rerank`（input/parameters 请求体、output.results 返回），重排根地址填写 `/api/v1`。业务空间专属域名也使用相同原生路径。旧配置里的 `/compatible-api/v1` 或聊天兼容根路径会在请求时转换为同域名的 `/api/v1`，不会换地域或自动重试其他服务。硅基/自定义兼容服务使用 `/embeddings` 和 `/rerank`。Ollama 使用 `/api/embed`，设置 `truncate=false` 并使用原生维度。Qwen 预设的查询指令只加给查询，法条窗口不加。

切换服务或地址时必须重新提供密钥；密钥只加密存储、不会返回浏览器。云端向量接收法条与查询，云端重排接收查询与候选片段；模型服务的数据去向独立于公开网页搜索服务。

## 本地重排部署

见 `UPGRADE_1.3.0.md` 的三文件 Compose 命令。sidecar 固定加载服务端配置的 Qwen3 Reranker，不能通过请求任意切换模型文件；以 yes/no logits 计算相关性，不调用聊天生成接口模拟重排。默认CPU；服务只开放到 Compose 内部网络。

首次运行需能下载 PyTorch/Transformers 依赖及 Hugging Face 模型。内网离线部署可在可联网机器预先下载完整模型目录，再给容器添加只读挂载（如 `./models/qwen3-reranker:/models/offline:ro`），设置 `RERANK_MODEL_PATH=/models/offline`、`HF_HUB_OFFLINE=1`。请求模型名仍使用 `RERANK_MODEL` 的默认注册名。

如果法据以 Python/uvicorn 直接运行在宿主机，宿主机安装 Docker 后可只启动重排容器：

```bash
docker compose -f compose.reranker.yaml -f compose.reranker.host.yaml up -d --build
```

此时法据中的重排地址使用 `http://127.0.0.1:8081/v1`。新增的 host override 只将端口绑定到本机回环地址；法据也在 Compose 中时继续用 `http://reranker:8081/v1`，无需添加此文件。

GPU 部署可在单独 Compose override 中将构建参数 `TORCH_INDEX_URL` 设置为兼容机器驱动的 PyTorch CUDA wheel 源，给 reranker 配置 GPU 设备预留，并设置 `RERANK_DEVICE=cuda`。默认镜像安装CPU版PyTorch；仅修改设备字段不能让CPU轮子支持CUDA。CPU速度及GPU方案本次均未用真实权重验收。

## 实际检索记录

每轮保存 `retrieval_runs`：scope、configured_mode、embedding/reranker模型、应用/部分覆盖/失败/未配置状态、有效法条数、索引覆盖数、三路候选数、重排数、请求数、返回ID、耗时、降级原因。答案页面和导出会保留实际方法，不把配置为hybrid等同真正调用过embedding。

聊天调用最多18次，embedding与rerank请求另外计数并消耗研究总时限。两项服务独立超时可以在界面设置。未完成索引前的法条仍能参加关键词召回。

## 对照评测

```bash
python scripts/evaluate_retrieval.py --run-live --build-index --output retrieval-evaluation.json
python scripts/evaluate_core.py --run-live --online --output core-evaluation.json
```

前一命令复制法库和相关索引到临时数据库，不复制业务案件。索引缺失时仅在临时副本中构建。对固定法库、问题分别跑 BM25、混合召回、混合+重排，记录真实路径、Recall@20、nDCG@10、耗时和请求次数。法库缺文件时单独报告 source_coverage，不把缺法源隐藏进平均召回率。未真正执行目标路径的结果不能算有效对照。

内置10题是起步检索验收集，包含原彩礼追问、刑法精确条号、合同口语与术语变体；不是经过律师标注的全面golden dataset。正式验收应由法律人员扩展和确认预期法条、例外、时效与版本。后一命令运行真实多轮案件研究，检查已有事实、引用和接口错误；最终法律适用仍需要人工评阅。脚本会实际调用配置的服务，云端可能收费。

官方接口参考（2026-09-27核对）：
- https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api
- https://help.aliyun.com/zh/model-studio/text-rerank-api
- https://api-docs.siliconflow.cn/docs/api/embeddings-post
- https://docs.siliconflow.cn/docs/api/rerank-post
- https://ollama.com/library/qwen3-embedding
- https://github.com/ollama/ollama/releases （Compose 固定0.33.3）
- https://github.com/QwenLM/Qwen3-Embedding
- https://huggingface.co/docs/transformers/v4.56.2/en/model_doc/qwen3 （本地重排只计算末位 logits）
