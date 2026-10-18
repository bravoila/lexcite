# 升级到 1.0.1（保留原有数据）

本版解决“文件和链接都要填”和“错误被弹窗遮住”两个问题，并补上文件来源在引用中的显示。

## 已经用 Docker 运行

1. 找到原来启动法据的 `lexcite` 文件夹，在这里打开终端。不要改文件夹名或另建一套部署；Compose项目名变化可能连接到一个新的空数据卷。如果之前使用 `-p` 或 `-f` 启动，下列命令继续使用相同参数。
2. 先备份。应用运行时，在原文件夹执行下面两行；如文件名已存在，换一个新名字：

```bash
docker compose exec lexcite python scripts/backup.py --data /app/data --output /app/data/before-1.0.1.zip
docker compose cp lexcite:/app/data/before-1.0.1.zip ./before-1.0.1.zip
```

3. 解压更新包到一个临时位置。将新版 `app`、`static`、`scripts`、`docs`、`tests` 文件夹内的同名文件覆盖到原文件夹。把新版 `README.md`、`package.json`、`package-lock.json` 一并复制过去。此次没有新增Python依赖，无须改Compose或Dockerfile。
4. **保留原来的 `data`、`.env`、模型目录和Docker数据卷。** 文件管理器询问“替换整个文件夹”时，应合并并覆盖代码文件，避免把旧文件夹整体删除。
5. 仍在原文件夹执行：

```bash
docker compose up -d --build
```

使用内置Ollama组合部署时，保持原启动方式，例如：

```bash
docker compose -f compose.yaml -f compose.ollama.yaml up -d --build
```

使用GPU或自定义项目名时，也保留原命令的全部 `-f`、`-p` 参数。不要运行 `docker compose down -v`；`-v` 会删除数据卷。

6. 重新打开法据网页，Windows按 `Ctrl+Shift+R`、Mac按 `Command+Shift+R` 强制刷新。侧栏应显示 **法据 1.0.1**，登录密码、案件、记忆、模型设置和已导入法条仍在。新导入窗口顶部应出现两种来源选项。

如意外出现“初始化工作空间”，先检查是否进入了另一个Compose项目或数据卷，不要立刻重新初始化。

## 用 Python 直接运行

1. 先备份：在原项目目录执行 `python scripts/backup.py --data data --output backups/before-1.0.1.zip`。如果设置过 `LEXCITE_DATA`，将 `--data` 改成实际数据目录。
2. 停止原服务，再按上面第3步覆盖代码文件；保留数据目录、`.env`、虚拟环境和环境变量。
3. 使用原Python虚拟环境和启动命令重新运行，例如：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1
```

4. 强制刷新网页，核对版本号和原有案件。数据库来源字段会自动增加，原法条和历史引用快照保留。

## 新导入方式

| 你手上有什么 | 怎样操作 |
|---|---|
| TXT / MD / 文字型PDF / DOCX | 选“上传文件 / 粘贴正文”→“选择文件”；官网链接可以留空 |
| 已复制的法条文字 | 选“上传文件 / 粘贴正文”→直接粘贴到正文；无需文件和链接 |
| 官方原文网页或附件链接 | 选“读取官网链接”→粘贴地址→“读取链接正文”；无需上传文件 |

读取正文后仍要核对法规名称、版本、地区、日期，预览拆条，导入后人工复核启用。没有官网链接的资料会显示正文来源名称和“未提供官网链接”，不会伪造来源网址。

错误显示在当前弹窗内并持续保留；修改后重试即可。链接读取失败可能与动态页面、访问限制、扫描件、文件格式或页面缺少独立条号有关，可改用文件或粘贴方式。

## 本次验证

44项Python测试、19条网页DOM交互检查通过，包含两种导入、弹窗报错、文件来源引用、旧库迁移及原有问答流程。链接成功场景使用模拟官网响应；未将真实官网连通性、浏览器视觉排版、Docker镜像或真实模型调用标为通过。详细边界见 `VERIFICATION.md`。
