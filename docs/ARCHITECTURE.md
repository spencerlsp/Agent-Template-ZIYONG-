# 架构说明

各模块的 README 讲"我这一层怎么设计的"，这份文档讲**它们怎么拼在一起**。

## 一张图看懂

```
                            ┌──────────────┐
                            │    cli.py    │  参数解析 / 渲染 / 退出码
                            └──────┬───────┘
                                   │ 消费事件流
                                   ▼
┌──────────────────────────────────────────────────────────────┐
│                     agent/runtime.py                         │
│  AgentRuntime.create()：装配一切，管理资源生命周期            │
└───┬──────────────┬──────────────┬──────────────┬─────────────┘
    │              │              │              │
    ▼              ▼              ▼              ▼
┌────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  llm/  │   │  tools/  │   │ memory/  │   │  obs/    │
│模型适配 │   │ 工具登记表│   │ 会话历史 │   │追踪+计量 │
└────────┘   └────▲─────┘   └──────────┘   └──────────┘
                  │ 注册
     ┌────────────┼────────────┬─────────────┐
     │            │            │             │
┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐
│ builtin │ │ skills/  │ │  rag/    │ │    mcp/      │
│ 时间·计算│ │技能目录  │ │混合检索  │ │ 远端工具桥接 │
│ 读写文件 │ │+按需读取 │ │+重排+索引│ │              │
└─────────┘ └──────────┘ └──────────┘ └──────────────┘
   本地能力          └──── 全部经由工具表暴露给模型 ────┘
```

核心结构只有一句话：**主循环不认识技能、RAG、MCP，它们全都是"往工具表里注册"。**

## 依赖方向

依赖是单向的，不允许反向引用：

```
config  ◄──── 所有模块都可以读配置
llm.base ◄─── 所有模块都用它的 Message / ToolSpec / LLMClient
tools.registry ◄─── skills / rag / mcp / builtin 往它里面注册
agent.loop ──► 依赖上面的全部
cli ──► 只依赖 agent.runtime
```

具体规则：

| 规则 | 原因 |
| --- | --- |
| 没有模块直接 `import openai` / `import mcp`，除了各自适配器 | 供应商细节被关在一个文件里 |
| `agent/` 不 import `rag` / `mcp` / `skills` 的具体类型 | 它只认 `ToolRegistry`，所以加能力不用改循环 |
| 除 `config.py` 外没有模块读环境变量 | 配置只有一个真相来源 |
| `rag/` 不认识 `ToolRegistry` | 注册动作由 `runtime` 统一做，RAG 只提供能力 |

## 一次提问的完整链路

```
① cli.py
   解析参数 → Options.build_settings() → Settings
   （优先级：命令行 > 环境变量 > .env > 默认值）
        │
② agent/runtime.py
   AgentRuntime.create(settings):
     llm       = build_llm(settings)
     registry  = ToolRegistry()
     register_builtin_tools(registry, settings)         ← 5 个
     skills    = SkillsIndex.from_dir(...)
     register_skill_tools(registry, skills)             ← +2 个
     rag       = RagPipeline(settings); ensure_ready()  ← 失败则跳过
                  （重排器在这里按配置构造：默认 NoopReranker，等于不重排）
     register_rag_tools(registry, rag)                  ← +1 个
     mcp       = MCPManager(...); connect_all()         ← 失败则跳过
     register_mcp_tools(registry, mcp)                  ← +2 个
     memory, tracer
        │
③ agent/loop.py  AgentLoop.run(session, question)
   system = build_system_prompt(settings, registry, skills)
   history = memory.history(session)        ← 裁剪对齐 user 边界
   memory.append(session, user_message)
   messages = [system, *history, user_message]
        │
        ├─► 循环（最多 AGENT_MAX_STEPS 次）
        │     ④ llm.chat / llm.stream_chat(tools=registry.specs())
        │        → 事件：reasoning* / text* / usage
        │     ⑤ 若模型要求调用工具：
        │        registry.call(name, args)
        │          校验参数 → 执行（超时）→ 结果或错误文本
        │        结果入 messages 与 memory
        │        → 事件：tool_call / tool_result
        │     ⑥ 若没有工具调用 → 事件 finished，结束
        │
⑦ obs/tracing.py
   每次模型轮次与工具调用各记一个 span → .agent/traces.jsonl
   token 累加进 TokenAccountant
        │
⑧ cli.py 按事件渲染
   回答 → stdout；工具/用量/思考摘要 → stderr
```

## 贯穿全局的四条原则

**1. 每层只有一个接缝。**
换模型只改 `llm/factory.py`；换向量库只改 `rag/store.py`；换存储位置只改 `Settings`。
每个模块的 README 里都标出了自己的接缝在哪。

**2. 可选能力失败要降级，核心能力失败才报错。**

| 失败 | 处理 |
| --- | --- |
| 索引不存在 / embedder 不匹配 | 记 warning，跳过 RAG，对话照常 |
| 某个 MCP server 起不来 | 跳过它，其他 server 照常 |
| 重排服务不可用（网络 / 鉴权 / 限流） | 记 warning，回退到融合顺序，检索照常返回 |
| 技能文件格式坏 | 跳过该技能 |
| 单个工具执行失败 | 返回错误文本给模型，循环继续 |
| 模型端点不可用 | **这是核心依赖，直接报错退出** |

判断标准：**缺了它还能不能对话**。还能，就降级。

代价是降级会静默发生——重排一直在失败时，表现只是"检索质量退回旧水平"，没有报错。
所以降级路径都必须留下痕迹：检索层打 warning，`evals/bench_search.py` 还会把回退次数
统计出来，大于 0 就提示这一轮的数字不可信。**能做降级，但必须可观测。**

**3. 给模型的输入必须是它解读得了的。**
一个反例：RRF 分数（0.016~0.033）曾被当作"相关度"塞进上下文，模型据此得出
"检索匹配度很低"的结论——它没能力知道这个数字的绝对值没有意义。
开了重排后同一个字段又变成了 cross-encoder 的相关度（0~1，且每条 query 各自标定），
含义随配置而变。凡是模型无法正确解读的信号，都不要给它。

**4. 派生数据可以随时删掉重建。**
`.agent/` 下的索引、记忆、追踪文件都不进版本库。清掉它们只会丢失"对话历史"
和"已建好的索引"，不会丢失任何源数据。这让实验和迁移都变得廉价。

## 扩展点地图

想改什么，直接看这张表：

| 想做的事 | 改哪里 | 大致工作量 |
| --- | --- | --- |
| 接新模型供应商（OpenAI 兼容） | `.env` 三个值 | 五分钟 |
| 接协议不兼容的供应商 | `llm/` 新增适配器 + `factory.py` 加分支 | 半天 |
| 加一个工具 | 写函数 + `registry.register()` | 十分钟 |
| 加一个技能 | `skills/` 下放 `SKILL.md` | 十分钟 |
| 接一个 MCP server | `.env` 的 `AGENT_MCP_SERVERS` | 五分钟 |
| 换成真实 embedding | `.env` + 重建索引 | 十分钟 |
| 换向量库 | 替换 `rag/store.py` 的 `VectorStore` | 半天 |
| 加文档格式（PDF） | `rag/loaders.py` 加一个函数 | 半天 |
| 开启重排 / 换重排模型 | `.env` 的几个 `AGENT_RERANK_*` | 五分钟 |
| 换重排服务商（形状不兼容） | `rag/rerank.py` 加一个 `Reranker` 实现 + `build_reranker()` 分支 | 半天 |
| 加评测用例 / 做检索实验 | `evals/dataset.jsonl` + `uv run python evals/run_eval.py` | 半小时起 |
| 换会话存储 | 替换 `memory/store.py` 的 `MemoryStore` | 半天 |
| 接 Web 前端 | 新增 `api/`，消费同一套 `AgentEvent` | 一天 |
| 换观测平台 | 替换 `obs/tracing.py` 的 `Tracer.record()` | 半天 |

## 评测怎么接进来

`evals/` 不在运行时链路上，它是一把**尺子**，和 `agent/`、`cli/` 并肩而不是套在里面：

```
evals/run_eval.py     ──► 自己构造 embedder + VectorStore + HybridRetriever
                          绕过 AgentLoop，所以不花模型生成的钱
evals/bench_search.py ──► 走 RagPipeline.search()，量的就是生产路径的延迟
```

两处刻意的选择：

1. **质量评测绕过主循环**，直接调检索。一条用例只花一次 embedding + 一次本地 BM25，
   60 条几秒跑完；走完整问答的话，每条都要付一次模型生成。
2. **延迟基准走 `RagPipeline`**，而不是直接调 `HybridRetriever`。因为生产环境调的就是
   pipeline，重排接在哪一层、多绕了几层，都会如实反映在数字里。

改检索代码的闭环是：**跑基线 → 改一处 → 再跑同一张表 → 对比同一列**。
一次改两处，数字动了也说不清是谁的功劳。指标定义与实测基线见
[evals/README.md](../evals/README.md)。

## 依赖与版本约束

| 依赖 | 版本约束 | 为什么要注意 |
| --- | --- | --- |
| `mcp` | `>=2.2.0` | **2.x 与 1.x 不兼容**：`FastMCP` 已改名 `MCPServer`，网上大量示例是 1.x 写法 |
| `openai` | `>=3.16.2` | 只用来发请求；所有类型都被 `llm/base.py` 隔离了，升级 SDK 不影响上层 |
| `pydantic` / `pydantic-settings` | `>=2.13` | v1 与 v2 API 完全不同，本项目全按 v2 写 |
| `tzdata` | `>=2026.4` | Windows 不自带 IANA 时区库，缺了它所有时区查询都会失败 |
| `typer` / `rich` | `>=0.27` / `>=15` | 仅 CLI 使用，核心不依赖 |

Python 版本下限是 **3.13**，用到了 `X | Y` 类型语法、`Path.is_relative_to`、
以及 `asyncio.to_thread` 等特性。
