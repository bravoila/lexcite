# 中国法律问答与 Book-to-Skill 项目评估

核查日期：2026-09-24。目标：律师/法官可操作的界面、中国模型、案件记忆、可追溯法条引用、尽量降低幻觉、自行部署。

## 结论

没有把任何仓库的“低幻觉”“生产可用”文案视为已验证结论。选型主要依据可见结构、许可和对当前需求的适配性；本次未运行各上游完整系统进行公平横向测评。以下“优先参考”“暂不采用”属于工程判断，不是性能排名。

最终实现独立的法据项目：保留条文级检索和方法流程的思想，用确定性引用检查、案件记忆隔离、人工法库复核补足应用所需控制。未复制上游实现，未采用许可不清晰的代码。

## 逐项评估

| 项目及原始来源 | 本次核查层次 | 可借鉴内容 | 对当前需求的主要缺口 / 不确定性 | 处理决定 |
|---|---|---|---|---|
| [Fan-Luo/Legal-RAG](https://github.com/Fan-Luo/Legal-RAG) | README、LICENSE、`legalrag/pipeline/rag_pipeline.py` | 按法条切分、元数据、混合检索、关联扩展、多模型后端 | 检查到的生成主路径将模型文本和检索结果返回；未在这一路径看到逐结论原文匹配的强制拦截。不能据此断言整个项目没有其他防护。案件记忆与时效维护需进一步补齐 | 优先参考检索结构；独立实现验证层 |
| [Teeeeen/legal_rag](https://github.com/Teeeeen/legal_rag) | README与目录说明；未完成运行核验 | 中文本地模型、法律/案例分库、界面、反思与质量指标 | README的许可段落写“仅供学习与研究使用”，未据此认定具备商业复用许可；原始数据另行获取，当前有效性和自报质量须再验 | 可作研究样例；不并入本项目 |
| [FudanDISC/DISC-LawLLM](https://github.com/FudanDISC/DISC-LawLLM) | README及Apache-2.0 LICENSE | 法律训练数据、评测、检索模块；README列2025年Qwen2.5模型更新 | 模型研究与测评不能代替现行法库、逐条引用校验和业务界面。模型、数据许可须单独核对 | 适合作为后续模型/评测候选 |
| [PKU-YuanGroup/ChatLaw](https://github.com/PKU-YuanGroup/ChatLaw) | README及仓库许可标识 | 法律专用模型、知识图谱和多角色流程研究 | 最新README重点包含ChatLaw2-MoE。旧版13B/33B不是全部现状。AGPL-3.0标识及权重许可需分别确认；论文结果不能证明此应用部署效果 | 可后续接入模型服务，不用作默认应用底座 |
| [pengxiao-song/LaWGPT](https://github.com/pengxiao-song/LaWGPT) | 前次README检索，本次LICENSE | 法律领域训练方法 | GPL-3.0代码许可；法条更新、原文引用与记忆仍需额外应用层 | 不作为降低幻觉的第一步 |
| [malnlda/yd-law-search](https://github.com/malnlda/yd-law-search) | README与SKILL.md | 在线法条搜索、法规/条文详情、有效状态过滤 | 依赖元典API密钥、服务可用性及授权；README写MIT，但本次未取得独立LICENSE文件。不是完全离线法库 | 将来可作为受控检索适配器；本版不依赖 |
| [huangzesen/book-to-skill-distillation](https://github.com/huangzesen/book-to-skill-distillation) | README、MIT LICENSE、validation-checklist | 扫描书整理、任务路由、检查表、法律尽调示例 | 示例和工作流不等于经过独立法律评测。检查清单要求引用忠实，但不自动证明生成规则正确 | 借鉴流程分层；法条保持原文 |
| [virgiliojr94/book-to-skill](https://github.com/virgiliojr94/book-to-skill) | README及MIT LICENSE | 按需加载章节、多文档合并与更新 | 提高文档可用性和上下文效率；不自动解决法规时效、冲突与法律适用 | 可用于未来经人工审定的办案流程模块 |
| [noahnan-max/book-to-skill](https://github.com/noahnan-max/book-to-skill) | README | 来源追溯、分歧保存、知识与技能分层 | README明确表示尚未选择开源许可证；公开可读不代表可再分发代码 | 仅作思想参考，不复用代码 |
| [ECNU-ICALK/AutoSkill4Doc](https://github.com/ECNU-ICALK/AutoSkill/tree/main/AutoSkill4Doc) | 项目及子目录README | 文档到技能、注册与合并、证据关联 | 侧重技能生命周期；不是法律依据库。本次尝试常见LICENSE路径未取得文件，不能据此判断完整许可状态 | 先不增加该框架依赖 |

未把同名fork逐一当作独立方案比较。之前检索结果中的小型项目与镜像，未得到足够核实材料的，不列为推荐基础。

## 为什么采用原文 RAG

本应用的工程目标是：每项法律结论能回到用户复核过的特定版本、特定条文。

1. 原文以一整条为基本检索单位，保留款、项和例外。模型不自行编写法条来源链接。
2. 按案件日期、地区、复核状态选择法源。发现同名法规有效期重叠时先排除并提示处理。
3. 回答按结论分解，要求逐条证据。服务器校验引用ID及连续原文，复核模型再判断支持关系。
4. 记忆记录案件事实；法源保存在独立资料库。历史模型回答不再投入下一轮作为证据。
5. 回答失败后保留可查看的法条，不输出未经检查的生成文本。

这些机制仍不能自动解决：漏检关键司法解释、法条关系与法阶冲突、溯及力/过渡规定、争议裁判观点、复杂事实证明。它们在真实业务测试中必须单独覆盖。

## Skill 后续接入位置

若将来从法律教材蒸馏技能，建议放入“需要核实哪些事实、检索哪些规范、按什么结构撰写”的步骤。每个技能须记录来源版本、适用范围、反例和更新条件，先经人工审定，再加入任务流程。法典完整原文继续保留于独立证据库。

本版没有实现 Book-to-Skill 蒸馏功能，也不需要为使用此应用安装 Claude/Codex Skill。当前已实现的受控RAG路线满足本次二选一路径。

## 模型接口依据

- [阿里云百炼OpenAI兼容Chat](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)：兼容端点及思考参数。
- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode)：JSON模式需明确请求JSON；结构化输出仍须本地验证。
- [Ollama兼容接口](https://docs.ollama.com/api/openai-compatibility)：本地chat completions接口。
- [Ollama Qwen3](https://ollama.com/library/qwen3)：本地模型标签示例。
- [SiliconFlow Chat Completions](https://docs.siliconflow.cn/docs/api/chat-completions-post)：接口及模型可用性说明。

本次没有中国模型API密钥与可运行的本地模型权重，故没有把协议测试写成真实模型实测，也没有给出幻觉率排名。
