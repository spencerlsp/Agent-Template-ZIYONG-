# 路线图

按"投入产出比"分三波。每条都标了出处，细节点回去看模块自己的 README。

## 第一波：补小洞（每条半天以内）

| 事项 | 出处 | 为什么 |
| --- | --- | --- |
| 上下文压缩（对话摘要） | [agent](../src/agent_template/agent/README.md)、[memory](../src/agent_template/memory/README.md) | 长对话的主要瓶颈：不压缩就只能砍历史，砍了就前言不搭后语 |
| 工具级统计 | [tools](../src/agent_template/tools/README.md) | 工具一多，"哪个工具模型老用错"是最该看的数据 |
| 按会话算成本 | [obs](../src/agent_template/obs/README.md)、[llm](../src/agent_template/llm/README.md) | token 已经能按会话/轮次聚合（`scripts/usage.py`），缺的是"换算成钱"——但各家单价不同且一直在变，建议只做**可选**单价，别当默认 |
| 结构化输出（`response_format`） | [llm](../src/agent_template/llm/README.md) | `chat()` 已预留参数没人用，适合"必须返回 JSON"的抽取/分类 |
| 慢 span 告警 | [obs](../src/agent_template/obs/README.md) | 某次工具调用或模型轮次超阈值时当场提示，比事后翻 `traces.jsonl` 管用 |

## 第二波：工程质量（需要设计决策）

| 事项 | 出处 | 触发条件 |
| --- | --- | --- |
| 技能触发测试 | [skills](../src/agent_template/skills/README.md) | 技能的失败方式是"模型压根没读它"，静默且难察觉。准备一批"该触发 / 不该触发"的用例，改 `description` 时才有反馈 |
| HTTP + SSE 接口 | [agent](../src/agent_template/agent/README.md)、[cli](cli.md) | 要接 Web 前端时。事件流已经可序列化，只需加一层 |
| MCP 工具命名空间 | [mcp](../src/agent_template/mcp/README.md) | 挂第二个 MCP server 时必然遇到重名 |
| 静态工具白名单与审计 | [tools](../src/agent_template/tools/README.md) | 运行时审批（问人）已经有了；剩下的是**部署级授权**：启动时限制可用工具集、把每次调用记进审计日志 |

> **检索与评测不在这份清单里**——它们在独立的 ragkit 项目里做（解析、切分、向量化、
> Milvus、混合检索、重排、评估）。本项目只通过 MCP 消费它的工具，
> 所以 RAG 那些优化（增量索引、多知识库、可插拔向量库、查询改写、语义切块）
> 都不会影响这个模板。

## 第三波：范式扩展

会改变项目性质的功能，建议先把前两波做扎实。

| 事项 | 出处 | 说明 |
| --- | --- | --- |
| 多 agent 协作 | [agent](../src/agent_template/agent/README.md) | 规划者 + 执行者，或并行探索多方案。注意限制递归深度 |
| 长期记忆 | [memory](../src/agent_template/memory/README.md) | 跨会话记住用户偏好与项目约定。会引入"记错了怎么办"的新问题 |
| 远程技能分发 | [skills](../src/agent_template/skills/README.md) | 概念上类似插件市场，会带来信任与更新问题 |
| 多模态消息 | [llm](../src/agent_template/llm/README.md) | 支持图片输入需要改 `Message.content` 的类型，会波及所有构造点 |

## 已经做完的（留个记录）

- `obs` logger 名拼写、`llm` 的 `api_key` 类型兜底、CLI 里说明"全局选项写在子命令之前"
- token 按会话 / 按轮次归因（写进 `traces.jsonl` 的 span，配 `scripts/usage.py`）
- 同轮工具并发（只读并发、有副作用保序）、工具审批（Human in the loop）
- MCP 启动失败的明确提示与诊断
- **RAG 整体外移**：本仓库不再实现检索，改由外部 MCP 服务（ragkit）提供
