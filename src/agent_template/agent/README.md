# agent —— 主循环与运行时装配

> 把模型、工具、技能、MCP、RAG、记忆串起来的地方。
> 两层分工：**`AgentRuntime` 负责组装与资源生命周期，`AgentLoop` 负责消息流转。**

## 负责什么

- **装配**（`runtime.py`）：按配置构建模型客户端、工具表、技能索引、RAG、MCP、
  记忆、追踪器，并管理它们的关闭顺序。
- **编排**（`loop.py`）：读历史 → 拼提示 → 调模型 → 执行工具 → 回填结果 → 循环，
  直到模型给出最终答案或到达步数上限。
- **输出**（`events.py`）：把过程中的每一步抛成 `AgentEvent`，与展示方式解耦。
- **提示**（`prompts.py`）：拼系统提示（人设 + 技能目录 + 工具清单）。

## 不负责什么

| 你可能会以为它管，但其实不管 | 实际在哪 |
| --- | --- |
| 打印、着色、流式渲染 | `cli.py`（循环只抛事件） |
| 工具的实现与校验 | `tools/` |
| 检索细节 | `rag/`（对循环而言它只是又一个工具） |
| 历史怎么裁剪 | `memory/` |

## 成员清单

### `events.py`

| 名字 | 职责 |
| --- | --- |
| `EventKind` | 八种事件类型：`started` / `reasoning` / `text` / `tool_call` / `tool_result` / `usage` / `error` / `finished` |
| `AgentEvent` | 事件载体。`__str__` 提供一行摘要，CLI 和日志都能直接用 |

### `prompts.py`

| 名字 | 职责 |
| --- | --- |
| `build_system_prompt()` | 拼三段：基础人设（可被 `AGENT_SYSTEM_PROMPT` 覆盖）+ 技能目录 + 工具清单 |

### `approval.py`

| 名字 | 职责 |
| --- | --- |
| `ApprovalDecision` | 审批结果：`APPROVED` / `REJECTED`。**用枚举而不是布尔**——很快会多出超时、记住本次会话这些状态 |
| `ApprovalRequest` | 一次待审批的请求：`call` + `session_id` + `step`。后两个现在用不到，留着是为了将来换成"两阶段"方案时不用重构 |
| `ApprovalHandler` | 决策源的类型：给它一个请求，它给出一个决定 |
| `approve_all()` | 默认决策源：全部批准。**不注入审批时行为与之前完全一致** |
| `rejected_message()` | 被拒绝时回给模型的那段话：说清性质、禁止重试、**堵住绕道**、给出路 |

### `loop.py`

| 名字 | 职责 |
| --- | --- |
| `AgentLoop` | 一轮对话的完整流程 |
| `AgentLoop.run()` | async 生成器，逐步抛事件 |
| `AgentLoop._model_turn()` | 跑一次模型调用（流式或非流式），把增量抛出去、终值写进 holder |
| `AgentLoop._call_tool()` | 执行单个工具调用；单独抽出来是为了能丢进 `asyncio.gather` 并发 |
| `AgentLoop._span()` | 没有配置 tracer 时退化成空上下文，调用处不必写 `if` |
| `AgentLoop.stats` | token 统计摘要 |
| `group_parallel_calls()` | 模块级函数：把同一轮的工具调用按"能不能并发"分批——只读的连续段并成一批，有副作用的单独成批并保序 |

### `runtime.py`

| 名字 | 职责 |
| --- | --- |
| `AgentRuntime` | 装配好的 agent |
| `AgentRuntime.create()` | async 工厂：完成全部装配，返回可用实例 |
| `AgentRuntime.ask()` | 委托给 `AgentLoop.run()`，返回事件流 |
| `AgentRuntime.describe_tools()` | 工具清单，CLI 的 `agent tools` 用它 |
| `AgentRuntime.aclose()` | 按依赖顺序关闭：MCP → RAG → 模型 → 记忆 |

## 类之间的关系

```
             AgentRuntime.create(settings)
                        │ 装配
        ┌───────────────┼───────────────┬──────────────┐
        ▼               ▼               ▼              ▼
     LLMClient    ToolRegistry     MemoryStore      Tracer
                     ▲ ▲ ▲
        ┌────────────┘ │ └────────────┐
     builtin       rag.tools      mcp.bridge
        └───── skills.tools ─────────┘
                        │
                        ▼
                   AgentLoop  ──run()──►  AsyncIterator[AgentEvent]
                        │
                        └── 内部：messages 列表 + 事件抛出
```

一次 `run()` 的时序：

```
读历史 ─► 追加 user 消息 ─► 拼系统提示 ─► messages = [system, *history, user]
   │
   └─► for step in range(max_steps):
           ① _model_turn()  ──► 事件：reasoning* / text* / usage
           ② assistant 消息入 messages 与记忆
           ③ 没有 tool_calls？ ──► finished 事件，结束
           ④ 有？ 按批执行（只读的连续段并发，有副作用的保序）
                ──► 事件：tool_call / tool_result
              结果作为 tool 消息入 messages 与记忆
           ⑤ 回到 ①
```

## 为什么这么设计

**1. 为什么用事件流，而不是在循环里直接 print 或回调。**
同一个循环要服务三种消费者：CLI 要流式打印并区分 stdout/stderr，未来的 Web 端要转成
SSE，测试要断言事件序列。回调能解决一部分问题，但调用方得自己拼装状态；
事件流让**循环保持无状态输出、消费者各自决定怎么呈现**。

**2. 为什么拆成 `AgentRuntime` 和 `AgentLoop` 两层。**
装配要管资源生命周期（子进程、数据库连接、HTTP 客户端），循环只管消息流转。
拆开之后，测试可以只用 `MockLLM` + 空工具表构造一个 `AgentLoop`，
**不必启动 MCP、不必有索引**就能跑完整的循环逻辑。

**3. 为什么 RAG 没索引、MCP 连不上都要降级而不是报错。**
首次 `git clone` 的用户还没建索引；某个 MCP server 可能没装。这些都不该让
"对话"这个基本能力失效。所以：

- RAG 不可用 → 记 warning，跳过，其余照常
- 某个 MCP server 起不来 → 跳过它，其他 server 照常
- 只有模型客户端不可用才算致命（那是核心依赖）

**4. 为什么 assistant 消息必须入历史，哪怕它只有 `tool_calls`。**
这是协议要求：`tool` 结果消息必须是对某条 `tool_calls` 消息的响应。少一条，
下一次请求会被接口拒绝（400），而错误信息不会告诉你"是你忘了存那条空回复"。

**5. 为什么工具失败不中断循环。**
`registry.call()` 从不抛异常，失败会变成 `错误：...` 文本交给模型。这样模型有机会
换参数重试（实测很常见：时区名写错 → 看到错误 → 换一个正确的）。若在这里抛异常，
整个循环就断了，用户看到的是"程序出错"而不是"模型正在自我修正"。

**6. 为什么必须有 `max_steps`。**
模型可能陷入"反复调工具但从不给结论"的状态。没有上限时，这个状态会一直烧 token。
默认 8 步，撞上限时抛 `error` 事件说明情况——**明确失败比无限等待好**。

**7. 为什么模型轮次要写成"生成器 + holder"。**
Python 的 async generator **不能用 return 带出值**，而流式输出又必须边收边抛事件。
所以最终结果通过一个 dict 回传：

```python
holder: dict[str, Any] = {}
async for event in self._model_turn(messages, step, use_stream, holder):
    yield event
message = holder["message"]   # 事件抛完了，终值在这里
```

这是该场景下的标准做法，不是临时凑合。

**8. 为什么系统提示每轮重建。**
技能目录和工具清单会变（新挂了 MCP server、改了技能文件）。把某一版提示存进历史，
下次就会带着过期的工具清单请求——模型会去调一个不存在的能力。

**9. 为什么工具执行要分批，而且还要区分只读。**
规则和实测数据在 [tools/README.md](../tools/README.md) 的"并发执行"一节，这里只补一句
设计分工：**循环负责保住顺序语义，工具负责声明自己能不能并行**。两边职责清楚，
所以将来加写工具时不用回头改循环——最多是那个工具忘记声明 `read_only`，
代价也只是"串行执行，慢一点"。

**10. 为什么审批要做成"可注入的决策源"，而不是在循环里直接问人。**
因为"谁需要审批"和"谁来问人"是两件事：前者由工具的 `read_only` 决定，
后者取决于你在哪儿跑——终端里是敲键盘，HTTP 里是"挂起 + 恢复"，
WebSocket 里是"另一条消息"。循环只负责 `await self._approve(request)`，
具体怎么问、问谁，由调用方注入。

默认实现是 `approve_all`（全部放行），所以**不注入时行为与之前完全一致**。
新增的能力要能被关掉，否则它会污染所有既有路径和测试。

三种形态的取舍（当前实现第一种）：

| 形态 | 循环里的代码 | 用户必须在场 | 适合 |
| --- | --- | --- | --- |
| 注入回调 | `await self._approve(...)` | 必须 | CLI |
| 两阶段 | 存状态 + `return`，另写 `resume()` | 不必 | HTTP / 异步通知 |
| 长连接 | `await future`（≈ 第一种） | 必须但可稍后 | 实时 Web UI |

**从第一种迁到第三种是换实现，迁到第二种是改控制流**——所以接口里已经带了
`session_id` 和 `step`，将来要换第二种时不用改循环。

## 怎么扩展

### 加一种事件

在 `EventKind` 里加字面量，在 `_render_pretty` 里加一个分支即可。**注意保持
"所有事件都是可 JSON 序列化的"**——这是前端契约成立的前提（`--json` 依赖它）。

### 在循环里挂钩子（中间件风格）

最省事的做法是在 `AgentLoop.run()` 的关键位置加可选回调：

```python
def __init__(self, ..., before_tool: Callable[[ToolCall], None] | None = None):
    self.before_tool = before_tool
```

典型用途：**写操作的人工审批**（发现工具会改数据就暂停等确认）、
敏感参数脱敏、把每次工具调用上报到审计系统。

### 子 agent（子任务委派）

`AgentRuntime` 本身就可以当作一个工具被注册：

```python
async def delegate(task: str) -> str:
    """把子任务交给一个独立的 agent 处理，返回它的结论。"""
    sub = await AgentRuntime.create(settings)
    try:
        ...
    finally:
        await sub.aclose()
```

注意要限制递归深度，否则模型可能让子 agent 继续委派。

### 接 Web 前端

事件流天然可序列化，加一层 FastAPI + SSE 即可（见顶层 README 的"接 Web 前端"）。
**核心代码一行不用改**——这正是把渲染从循环里拆出去的原因。

## 后续可做（按性价比排序）

| 优先级 | 事项 | 说明与建议 | 大致工作量 |
| --- | --- | --- | --- |
| 高 | ~~并行执行同一轮的多个工具调用~~ | **已实现**：`group_parallel_calls()` 按连续段分批，只读并发、有副作用的保序。实测与边界见 [tools/README.md](../tools/README.md) | — |
| 高 | **上下文压缩** | 长时间对话的主要瓶颈。把早期历史摘要成一段，或只保留"做过哪些决定"。配合 `memory/README.md` 里的建议一起做 | 一天 |
| 中 | ~~工具审批中断~~ | **已实现**：`read_only=False` 的工具在执行前过人工审批（`approval.py` 的决策源注入）。CLI 支持终端确认与 `--yes`；非交互式环境默认拒绝 | — |
| 中 | **失败续跑** | 现在模型调用失败就整轮结束。可以保存已完成的步骤，下次从断点继续 | 一天 |
| 中 | **可中断的执行** | 用户在 CLI 里按 Ctrl+C 时，优雅地停在步骤边界而不是抛栈退出 | 半天 |
| 低 | **多 agent 协作** | 规划者 + 执行者、或并行探索多个方案。属于新范式，建议先把单 agent 做扎实 | 三天起 |
| 低 | **成本预算** | 每轮对话设 token 上限，超了就停止并说明。适合放在服务端 | 半天 |

我的建议：**先做"上下文压缩"**。它决定你的 agent 能不能支撑长对话——
现在只要对话一长，历史就会把成本和延迟一起推高。
