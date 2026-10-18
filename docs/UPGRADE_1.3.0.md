# 升级到 1.3.0：独立语义检索、重排与索引状态

1. 先停止研究和索引，备份原数据及配置。保留 `.env`、`data`、`secret.key`、原 Compose 项目名和数据卷。
2. 将新包中的代码、静态资源、部署文件和文档更新到原项目目录，不覆盖原数据。启动时自动添加新的索引表，保留案件、法库和历史引用。
3. 原目录执行 `docker compose up -d --build`。组合部署继续使用原有 Compose 参数；原生 Python 部署重启原服务。检查 `/healthz` 和页面版本均为 **1.3.0**，强制刷新浏览器。
4. “模型与设置 → 模型服务”继续负责分析和复核。向下找到新增的“语义检索与重排”，分别选择向量和重排服务，填写各自地址、模型和密钥。两者可与分析服务完全不同。
5. 首次配置时可先关闭“自动建立和补齐索引”，保存后分别测试两个服务，再点击“建立 / 补齐向量索引”。完成后重新开启自动索引并保存。
6. 索引卡片应显示当前模型、已索引/待处理/失败数。失败时先处理卡片内具体错误，再点击补齐；已完成条文不会重复调用。选混合模式，向量服务及索引可用时自动参加召回，重排服务可用时自动排序。
7. 新建问题或重新研究。答案下“本次实际检索”记录真正使用的方式、模型和覆盖数。历史回答不重新生成；历史记录没有此字段时不会伪造检索状态。

## 从旧向量配置迁移

若旧配置填写过 `embedding_model`，首次启动会复制旧服务的地址、模型和密钥到独立向量设置，并关闭自动索引，等待你确认。以后修改分析模型不再影响向量设置。

1.2.x 的 `vectors` 表保留，但新检索不使用这些缺少完整输入指纹的旧向量。首次需要重建。当前指纹包含服务地址、模型、维度、版本标记、查询指令、分片规则；条文或元信息变化也会使对应条文待处理。API 密钥变化不会使索引失效。

本次不需要重新初始化、不需要重新导入现有法规。旧法规没有单独保存章节路径，仍使用已有法规名、版本、类型、条号和完整正文建立索引；新导入文件会记录正文中显式出现的章节标题。

不要运行 `docker compose down -v`。这会删除数据卷。

## 本地完整组合

组合文件同时将 Ollama 镜像从旧0.11.10更新到固定0.33.3。更新保留 `ollama_data` 权重卷；本次未在目标硬件运行镜像，启动后分别测试分析与向量接口。

```bash
docker compose -f compose.yaml -f compose.ollama.yaml -f compose.reranker.yaml up -d --build
docker compose -f compose.yaml -f compose.ollama.yaml -f compose.reranker.yaml exec ollama ollama pull qwen3:8b
docker compose -f compose.yaml -f compose.ollama.yaml -f compose.reranker.yaml exec ollama ollama pull qwen3-embedding:0.6b
```

分析地址 `http://ollama:11434/v1`；向量服务选择 Ollama，根地址 `http://ollama:11434`、模型 `qwen3-embedding:0.6b`；重排服务选择“自建 / 其他兼容服务”，地址 `http://reranker:8081/v1`、模型 `Qwen/Qwen3-Reranker-0.6B`。本地这两个检索服务不需 API 密钥。

重排容器默认 CPU，首次需要下载公开模型权重，可能较慢；模型缓存放在独立卷。使用 `docker compose -f compose.yaml -f compose.ollama.yaml -f compose.reranker.yaml logs --tail=60 reranker` 检查启动。需要 GPU 或离线模型目录时见 `RETRIEVAL.md`。开发环境没有实际拉取这些权重或构建镜像，性能与连通性必须在目标机器验收。
