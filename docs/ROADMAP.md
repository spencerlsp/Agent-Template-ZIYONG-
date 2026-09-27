# 路线图

这份清单汇总自各模块 README 末尾的"后续可做"，按**投入产出比**重新排过序。
每一项都标了出处，细节可以点回去看。

分四波推进：**第一波零成本高收益，第二波提升质量，第三波需要设计，第四波是范式扩展。**

---

## 第一波：零成本高收益

都在半天以内，几乎不需要新设计，做完立刻见效。

| 事项 | 出处 | 为什么现在做 | 工作量 |
| --- | --- | --- | --- |
| 修掉 `obs` 里 logger 名的拼写（`agent.trance` → `agent.trace`） | [obs](../src/agent_template/obs/README.md) | 明确的缺陷，会让日志过滤漏掉这一层 | 五分钟 |
| `llm` 参数校验兜底：`api_key` 必须是 `str` 而非 `SecretStr` | [llm](../src/agent_template/llm/README.md) | 我们因为传了 `SecretStr` 收到过一个毫无头绪的 401，加一行断言就能把这类错误变成启动即报错 | 十分钟 |
| 工具并行执行同一轮的多个调用 | [tools](../src/agent_template/tools/README.md)、[agent](../src/agent_template/agent/README.md) | 模型一轮里要调 3 个互不依赖的工具时，现在得排队等 | 半天 |
| CLI 帮助里说明"全局选项写在子命令之前" | 顶层 | `agent tools -v` 会报错，这是 Click 的语义但不是用户的直觉 | 十分钟 |
| 按会话聚合 token 统计 | [obs](../src/agent_template/obs/README.md) | 马上就会想问"这次会话一共花了多少" | 半天 |

## 第二波：提升答案质量

这一天到两天的投入会直接反映在回答质量上，建议按顺序做。

| 事项 | 出处 | 为什么 | 工作量 |
| --- | --- | --- | --- |
| **检索重排（rerank）** | [rag](../src/agent_template/rag/README.md) | 两路各取 20 个候选后加一层 rerank 模型重新打分，**通常是检索质量提升最大的一步**，比换更大的 embedding 模型还划算 | 一天 |
| **小块检索、大块生成** | [rag](../src/agent_template/rag/README.md) | 检索用小片段保精度，命中后把所属完整章节给模型保上下文。需要给片段记"父块 id" | 一天 |
| 技能触发测试 | [skills](../src/agent_template/skills/README.md) | 技能的失败方式是"模型压根没读它"，静默且难察觉。准备一批"该触发 / 不该触发"的用例，改 `description` 时才有反馈 | 半天 |
| **上下文压缩（对话摘要）** | [agent](../src/agent_template/agent/README.md)、[memory](../src/agent_template/memory/README.md) | 长对话的主要瓶颈：不压缩就只能砍历史，砍了就前言不搭后语 | 一天 |
| 换真实 embedding | 顶层 | `local_hash` 只是离线兜底。换 `BAAI/bge-m3` 之类后，检索质量会有台阶式提升 | 十分钟 + 建索引时间 |
| 工具级统计 | [tools](../src/agent_template/tools/README.md) | 工具一多，"哪个工具模型老用错"是最该看的数据 | 半天 |
| 成本计量：配单价表 | [llm](../src/agent_template/llm/README.md)、[obs](../src/agent_template/obs/README.md) | token 数字有了，但换算成钱才有决策价值 | 半天 |
| 结构化输出（`response_format`） | [llm](../src/agent_template/llm/README.md) | `chat()` 已预留参数没人用，适合"必须返回 JSON"的抽取/分类场景 | 半天 |

## 第三波：工程化

需要设计决策，建议在真实需求出现后再做。

| 事项 | 出处 | 触发条件 | 工作量 |
| --- | --- | --- | --- |
| 增量索引 | [rag](../src/agent_template/rag/README.md) | 文档上百份、重建开始变慢时（现在只要 0.06 秒） | 一天 |
| 多知识库 | [rag](../src/agent_template/rag/README.md) | 需要"不同项目各一套文档"时 | 半天 |
| 可插拔向量库（Qdrant / pgvector） | [rag](../src/agent_template/rag/README.md) | 片段数上十万时 | 一天起 |
| 工具审批与权限 | [tools](../src/agent_template/tools/README.md)、[agent](../src/agent_template/agent/README.md) | **一旦引入写操作就必须做**（写文件、执行命令） | 一天起 |
| MCP 工具名命名空间 | [mcp](../src/agent_template/mcp/README.md) | 挂第二个 MCP server 时必然遇到重名 | 两小时 |
| MCP 的 HTTP 传输与健康检查 | [mcp](../src/agent_template/mcp/README.md) | 需要接远程/共享的 MCP 服务时 | 一天 |
| HTTP + SSE 接口 | [agent](../src/agent_template/agent/README.md)、[cli](../cli.md) | 要接 Web 前端时。事件流已经是可序列化的，只需加一层 | 一天 |
| 查询改写（多路检索式） | [rag](../src/agent_template/rag/README.md) | 用户提问口语化、指代多时 | 半天 |
| 多用户与会话隔离校验 | [memory](../src/agent_template/memory/README.md) | 从单机工具变成服务时 | 两天 |

## 第四波：范式扩展

会改变项目性质的功能，建议先把前三波做扎实。

| 事项 | 出处 | 说明 |
| --- | --- | --- |
| 多 agent 协作 | [agent](../src/agent_template/agent/README.md) | 规划者 + 执行者，或并行探索多方案。注意限制递归深度 |
| 长期记忆 | [memory](../src/agent_template/memory/README.md) | 跨会话记住用户偏好与项目约定。会引入"记错了怎么办"的新问题 |
| 远程技能分发 | [skills](../src/agent_template/skills/README.md) | 概念上类似插件市场，会带来信任与更新问题 |
| 语义切块 | [rag](../src/agent_template/rag/README.md) | 用向量相似度骤降点切分。建库成本翻倍、阈值难调，建议放在 rerank 之后再看 |
| 多模态消息 | [llm](../src/agent_template/llm/README.md) | 支持图片输入需要改 `Message.content` 的类型，会波及所有构造点 |

---

## 如果要我排一个顺序

**先做这三件，性价比排序几乎没有争议：**

1. **修 obs logger 名 + llm 参数校验** —— 加起来二十分钟，都是消除已知隐患。
2. **工具并行执行** —— 半天。模型一轮里要调多个互不依赖的工具时，现在得排队等。
3. **rerank** —— 一天，把答案质量往上抬一个台阶。

**然后按你的实际痛点选：**

- 对话变长就难受 → **上下文压缩**
- 文档多了重建变慢 → **增量索引**
- 想给同事演示或接前端 → **HTTP + SSE**
- 模型老是用错工具 → **工具级统计 + 技能触发测试**（先拿到数据再优化）

**最后提醒一句**：`tests/` 目录下的用例是上面所有这些改动的前提。没有测试，
每次优化都是凭感觉；有了它，你才知道自己是真的改好了，还是只是换了个地方出错。
