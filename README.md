# agent-template

一个**最小但完整**的本地开发 Agent 模板：模型调用、函数调用、技能、MCP、RAG
全部接通并且真能跑，每个子系统都留好了替换点。

它不是一个框架，而是一份**可读、可改、可拆**的参考实现——每个模块都在 100~300 行
之间，注释说明"为什么这么做"，而不是复述代码。

```
Python 3.13+ ｜ uv ｜ MIT ｜ 约 3000 行 Python
```

## 这个模板包含什么

| 能力 | 位置 | 一句话说明 |
| --- | --- | --- |
| 模型调用 | `src/agent_template/llm/` | OpenAI 兼容端点统一适配（OpenAI / DeepSeek / 硅基流动 / Ollama），外加一个离线 mock |
| 函数调用 | `src/agent_template/tools/` | 普通函数 + 类型注解 → 自动生成 JSON Schema，参数校验和超时都在这一层 |
| 技能 | `src/agent_template/skills/` | `SKILL.md` + 渐进式披露：提示里只放目录，正文按需读取 |
| MCP | `src/agent_template/mcp/` | stdio 客户端 + 随模板附带的示例 server，远端工具与本地工具同表 |
| RAG | `src/agent_template/rag/` | 加载 → 结构感知切块 → 向量化 → SQLite 存储 → 向量 + BM25 混合检索（RRF 融合） |
| 主循环 | `src/agent_template/agent/` | 推理/行动循环，事件流输出，会话记忆 |
| 可观测 | `src/agent_template/obs/` | 链路追踪（JSONL）+ token 计量 |
| 命令行 | `src/agent_template/cli.py` | `agent` 命令：对话、单次问答、工具/技能查看、建索引、会话管理 |

## 它刻意不做什么

把边界说清楚，比堆功能更有用：

- **不含业务逻辑**。所有工具和技能都是示例，删掉不影响主链路。
- **不做多用户、权限、审计**。单机单用户是它的设计前提。
- **不自建工具协议**。工具用 OpenAI 的 function calling，远端工具用 MCP，不发明第四套。
- **不做前端**。但事件流是按"可序列化"设计的，接 Web 前端时不需要改核心（见下文）。

## 快速开始

### 前置要求

- **Python 3.13+**（`pyproject.toml` 里的 `requires-python`；`.python-version` 已固定为 3.13）
- **[uv](https://docs.astral.sh/uv/)**（依赖与虚拟环境管理）

### 四步跑起来

```bash
# 1. 装依赖并创建虚拟环境
uv sync

# 2. 准备配置：复制模板，填入你的 API key
cp .env.example .env
#   AGENT_LLM_API_KEY=sk-...

# 3. 建立知识库索引（把 data/knowledge/ 下的文档切块、向量化、入库）
uv run agent index

# 4. 开始对话
uv run agent
```

单次问答：

```bash
uv run agent ask "RAG 的基本流程分几步？"
```

### 不想配 key？用离线 mock

```bash
AGENT_LLM_PROVIDER=mock uv run agent ask "现在几点？"
```

`mock` 是一个确定性的假模型，会按关键词决定要不要调工具。整条链路（主循环、
工具、技能、MCP、RAG）都能在没有网络、没有 key 的情况下跑通，测试和 CI 靠的就是它。

## 用它开新项目

这个仓库是**起点，不是工作区**。建议每个新项目都从一份干净的副本开始，
而不是在模板里直接改——否则它就不再是模板，下次开新项目没有干净起点。

```bash
# 路线 A（推荐）：GitHub 网页上点 "Use this template"
# 生成的仓库不带模板的提交历史

# 路线 B：本地复制后重开历史
git clone <本仓库地址> my-new-project
cd my-new-project
rm -rf .git && git init -b main    # 丢掉模板历史，作为新项目的起点
```

新项目里通常只需要改两处：

| 改什么 | 在哪 |
| --- | --- |
| 项目名与描述 | `README.md` 标题、`pyproject.toml` 的 `name` / `description` |
| 版权人 | `LICENSE` |

### 哪些是示例内容，可以放心删

模板里有一批**为了演示而存在**的内容。开新项目时第一件事就是替换它们：

| 路径 | 是什么 | 怎么处理 |
| --- | --- | --- |
| `data/knowledge/**` | 19 篇示例知识库（RAG/Agent 工程的实操笔记） | **整体替换**成你自己的文档，再跑 `uv run agent index` |
| `evals/dataset.jsonl` | 60 条示例评测问题，锚定的是上面那批文档 | 跟着语料一起重写；锚点写错会被自检拦住 |
| `skills/example-chat-style/` | 示例技能 | 换成你自己的，或直接删掉 |
| `src/agent_template/mcp/example_server.py` | 示例 MCP server | 可删，同时把 `.env` 里 `AGENT_MCP_SERVERS` 那一项去掉 |
| `src/agent_template/tools/builtin/` | 示例工具（时间、计算、读文件、列目录） | 前两个是纯演示，可删；文件工具通常保留 |
| `.agent/` | 运行态数据（索引、记忆、追踪） | 不在版本库里，删掉会自动重建 |

**其余都是核心，不要删**：`llm/`、`tools/registry.py`、`skills/loader.py`、
`mcp/client.py`、`rag/`、`memory/`、`agent/`、`obs/`、`cli.py`。

### 要不要改包名和命令名

默认包名是 `agent_template`、命令名是 `agent`。多数项目**不用改**——各项目有各自的
`.venv`，同名并不冲突。只有两种情况需要改：要把它做成产品对外分发，
或者要把两个 agent 项目装进同一个 Python 环境。

改名要动三处：目录 `src/agent_template/` → `src/你的包名/`、`pyproject.toml` 里的
`name` 与 `[project.scripts]`、以及所有 `from agent_template ...` 的导入。
`rg -l agent_template` 能一次列出全部需要改的文件。

## 一次提问都发生了什么

这是理解整个项目最快的方式。以 `agent ask "RAG 的基本流程分几步？"` 为例：

1. **CLI 解析参数** → 合成 `Settings`。优先级：命令行参数 > 环境变量 > `.env` > 代码默认值。
2. **装配运行时** `AgentRuntime.create()`：
   - 注册内置工具（时间、计算、读文件、列目录）
   - 扫描 `skills/` 目录，注册 `load_skill` / `list_skills`（**只把技能目录塞进提示，正文不读**）
   - 打开 RAG，校验索引与当前 embedder 是否匹配；**索引不存在就降级跳过，不阻断启动**
   - 连接 MCP 服务器，把远端工具并进同一张表；**某个 server 起不来只跳过它**
   - 最终得到一张包含 9 个工具的表
3. **主循环开始**：读出该会话的历史 → 追加本轮 `user` 消息 → 拼系统提示
   （人设 + 技能目录 + 工具清单）。
4. **调用模型**（流式）：模型先吐思维链，再吐正文，都通过事件抛给 CLI。
5. **模型要求调用工具** → `registry.call()`：
   - 用 pydantic 校验参数（模型可能传错类型）
   - 执行（同步函数丢线程池，带超时）
   - **失败不抛异常，返回 `错误：...` 文本**——让模型看到错在哪、自己纠正
   - 结果作为 `tool` 消息回填，同时写入会话记忆
6. **再次调用模型**，此时它带着工具的真实返回值作答，并在答案里标注来源。
7. **没有 `tool_calls` 了** → 抛 `finished` 事件，这一轮结束。若一直在调工具，
   到 `AGENT_MAX_STEPS` 步仍未收敛则报错退出。
8. **全程记录**：`Tracer` 记下每个 span 的耗时与成败（写 `.agent/traces.jsonl`），
   `TokenAccountant` 累计 token 用量。
9. **CLI 渲染**：回答写 stdout，工具调用/用量/思考摘要写 stderr。
   所以 `agent ask "..." > answer.md` 得到的文件里只有回答。

## 架构

```
                        agent_template.cli
                     （参数、渲染、退出码）
                              │ 消费
                              ▼
        ┌───────────────────────────────────────┐
        │   agent.runtime.AgentRuntime          │  装配一切、管理资源生命周期
        └───────────────────────────────────────┘
             │            │            │
             ▼            ▼            ▼
        agent.loop    tools.registry   memory.store
        （主循环）      （工具表）       （会话历史）
             │            ▲
             │ 调用        │ 注册
             ▼            │
        llm.LLMClient ────┴──── skills / rag / mcp
        （模型适配）              （都只往工具表里加东西）
```

关键设计：**主循环不认识 MCP、RAG、技能**。它们全都通过"往工具表里注册"接入，
所以对模型而言，本地工具和远端工具没有任何区别。

### 各子系统

| 模块 | 入口类型 | 负责 | 替换方式 |
| --- | --- | --- | --- |
| `llm/` | `LLMClient` | 对话、流式、工具调用协议 | 新增一个适配器文件 |
| `tools/` | `ToolRegistry` | 参数校验、超时、错误转文本 | 直接注册新函数 |
| `skills/` | `SkillsIndex` | SKILL.md 解析、按需读取 | 往 `skills/` 放目录 |
| `mcp/` | `MCPManager` | stdio 连接、工具发现与转发 | 改 `AGENT_MCP_SERVERS` |
| `rag/` | `RagPipeline` | 索引与检索 | 换 embedder 或换 `VectorStore` 实现 |
| `memory/` | `MemoryStore` | 会话历史持久化 | 换 SQLite 为任意存储 |
| `agent/` | `AgentLoop` | 推理/行动循环、事件流 | —— |
| `obs/` | `Tracer` | 追踪与计量 | 换成 OpenTelemetry |

> 每个模块都有一份自己的 README，讲清成员职责、类之间的关系、设计取舍，
> 以及**后续可以怎么优化**： [llm](src/agent_template/llm/README.md) ｜
> [tools](src/agent_template/tools/README.md) ｜
> [skills](src/agent_template/skills/README.md) ｜
> [mcp](src/agent_template/mcp/README.md) ｜
> [rag](src/agent_template/rag/README.md) ｜
> [memory](src/agent_template/memory/README.md) ｜
> [agent](src/agent_template/agent/README.md) ｜
> [obs](src/agent_template/obs/README.md)

## 配置

所有配置集中在一个地方：`src/agent_template/config.py` 的 `Settings` 类。
**除了它，代码里没有任何地方直接读环境变量。**

优先级：**命令行参数 > 环境变量 > `.env` 文件 > 代码默认值**。

### 模型相关

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_LLM_PROVIDER` | `openai_compat` | `openai_compat` 或 `mock`（离线） |
| `AGENT_LLM_MODEL` | `deepseek-v4-flash` | 模型名，随供应商而定 |
| `AGENT_LLM_BASE_URL` | `https://api.deepseek.com/v1` | 换成别的供应商就改这里 |
| `AGENT_LLM_API_KEY` | 空 | 密钥，**只放 `.env`** |
| `AGENT_LLM_TEMPERATURE` | `0.2` | 采样温度 |
| `AGENT_LLM_MAX_TOKENS` | `1024` | 单次回复上限 |
| `AGENT_LLM_TIMEOUT_S` | `60` | 请求超时（秒） |
| `AGENT_LLM_MAX_RETRIES` | `2` | 失败重试次数 |

### 主循环

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_MAX_STEPS` | `8` | 一轮对话里最多几次"模型轮次"，防止无限调工具 |
| `AGENT_STREAM` | `true` | 是否流式输出 |
| `AGENT_AGENT_NAME` | `local-agent` | 出现在默认系统提示里的人设名 |
| `AGENT_SYSTEM_PROMPT` | 空 | 覆盖默认系统提示 |

### RAG

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_EMBEDDING_PROVIDER` | `local_hash` | `local_hash`（离线）或 `openai_compat` |
| `AGENT_EMBEDDING_MODEL` | `BAAI/bge-m3` | 远端 embedding 模型名 |
| `AGENT_EMBEDDING_BASE_URL` | `https://api.siliconflow.cn/v1` | embedding 端点 |
| `AGENT_EMBEDDING_API_KEY` | 空 | 可以和 LLM 用不同的 key |
| `AGENT_EMBEDDING_DIM` | `512` | **仅 `local_hash` 使用**；远端模型从响应自动探测维度 |
| `AGENT_CHUNK_SIZE` | `500` | 片段目标长度（字符，含标题前缀） |
| `AGENT_CHUNK_OVERLAP` | `80` | 相邻片段的重叠长度 |
| `AGENT_RAG_TOP_K` | `4` | 最终返回给模型的片段数 |
| `AGENT_RAG_CANDIDATES` | `20` | 每一路检索先取多少候选再融合 |

### 路径与 MCP

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_KNOWLEDGE_DIR` | `data/knowledge` | 源文档目录 |
| `AGENT_SKILLS_DIR` | `skills` | 技能目录 |
| `AGENT_STATE_DIR` | `.agent` | 运行态数据（索引、记忆、追踪），不进 Git |
| `AGENT_WORKSPACE_ROOT` | `.` | 文件工具的沙箱根目录 |
| `AGENT_MCP_SERVERS` | 见下 | JSON 数组，stdio 服务器列表 |

```ini
# 单个 MCP 服务器
AGENT_MCP_SERVERS=[{"name":"example","command":"python","args":["-m","agent_template.mcp.example_server"]}]

# 多个，或给某个 server 单独传环境变量
AGENT_MCP_SERVERS=[{"name":"fs","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","."],"env":{"NODE_NO_WARNINGS":"1"}}]
```

可选项：`enabled`（默认 true）、`env`、`startup_timeout_s`（默认 20）、`call_timeout_s`（默认 60）。

## 使用

### 命令

| 命令 | 作用 |
| --- | --- |
| `agent` | 进入交互式对话（默认命令） |
| `agent chat` | 同上，显式写法 |
| `agent ask "问题"` | 单次问答后退出（多个词会被拼起来，不必加引号） |
| `agent tools` | 列出全部工具，含 MCP 远端工具及其来源 |
| `agent skills` | 列出技能目录（正文由模型按需读取） |
| `agent index` | 重建知识库索引 |
| `agent sessions` | 查看会话列表；`--clear <会话名>` 清空某个会话 |

### 全局选项

> 全局选项要写在**子命令之前**：`agent -v tools`，不是 `agent tools -v`
> （Click 的标准语义）。

| 选项 | 作用 |
| --- | --- |
| `-s, --session <名字>` | 会话名，用于隔离对话历史（默认 `default`） |
| `--no-stream` | 关闭流式输出（CI、日志采集用） |
| `-v, --verbose` | 展开模型的思考过程，并把日志级别调到 INFO |
| `--json` | 事件流以 JSONL 输出到 stdout |
| `--model <名字>` | 覆盖 `AGENT_LLM_MODEL` |
| `--provider <名字>` | 覆盖 `AGENT_LLM_PROVIDER` |
| `--version` | 打印版本 |

### 交互式命令

进入 `agent` 之后可以用这些斜杠命令：

```
/help    查看命令
/tools   列出工具
/skills  列出技能
/stats   查看本次会话的 token 统计
/clear   清空当前会话的历史
/exit    退出
```

### 输出约定

这三条是"能进管道、能被脚本用"的前提：

```bash
# 回答写 stdout，过程写 stderr —— 重定向得到的文件是干净的
uv run agent ask "什么是 MCP" > answer.md

# JSONL 事件流，每一行一个事件，可直接喂给前端或分析脚本
uv run agent --json ask "一加一等于几"

# 退出码：0 成功 / 1 运行错误 / 2 用法错误 / 130 被中断
uv run agent ask "..." || echo "失败了"
```

`--json` 输出的是 `AgentEvent` 的序列化结果，字段含义见
`src/agent_template/agent/events.py`。**这就是将来前端要消费的契约**——
加一个 SSE 层时不需要改动主循环。

## 扩展指南

### 加一个工具

写一个带类型注解和 docstring 的函数即可，schema 自动生成：

```python
def word_count(text: str) -> int:
    """统计一段文本的字数。"""
    return len(text)

# 在 tools/builtin/__init__.py 的 register_builtin_tools 里注册
registry.register(word_count)
```

**docstring 就是模型看到的工具描述**，它的措辞比 schema 更影响调用质量。
工具应返回错误文本而不是抛异常，这样模型有机会换个参数重试。

### 加一个技能

在 `skills/` 下建目录，放 `SKILL.md`：

```markdown
---
name: sql-review
description: 审查 SQL 时的检查清单——先看索引，再看 NULL 语义，最后看锁范围。
---

# SQL 审查清单

1. ...
```

只有 `name` 和 `description` 会进系统提示，正文等模型调用 `load_skill` 时才读取。
技能适合放"遇到这类任务该怎么做"的流程；原子能力请写成工具。

### 接一个 MCP 服务器

改 `.env` 里的 `AGENT_MCP_SERVERS` 即可，不需要写代码。远端工具会和本地工具
出现在同一张表里，模型看不出区别。若名字冲突，本地工具优先，MCP 的那个会被跳过并记警告。

### 换成真实 embedding

`local_hash` 是离线兜底，召回质量一般。换成真实模型只需改配置：

```ini
AGENT_EMBEDDING_PROVIDER=openai_compat
AGENT_EMBEDDING_MODEL=BAAI/bge-m3
AGENT_EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
AGENT_EMBEDDING_API_KEY=sk-...
```

然后**必须重建索引**：`uv run agent index`。换模型不重建，检索结果会静默全错——
所以索引里记了 embedder 名字和维度，启动时不一致会直接报错并提示重建。

### 换向量库

替换 `rag/store.py` 里的 `VectorStore` 一个类即可，上层只依赖
`add / all_rows / search / count` 这几个方法。几千到几万片段用 SQLite 足够；
再往上考虑 sqlite-vec、faiss 或 Qdrant。

### 换模型供应商

改 `.env` 的三个值（`BASE_URL` / `MODEL` / `API_KEY`）。OpenAI、DeepSeek、
硅基流动、Ollama 都用同一个适配器；如果某家端点有特殊约定，继承
`OpenAICompatClient` 覆盖 `_payload` 或 `_parse_message`。

### 接 Web 前端

主循环产出的是 `AgentEvent` 流，与传输方式无关。加一个 FastAPI 层：

```python
@app.post("/chat")
async def chat(payload: ChatIn):
    async def stream():
        async for event in runtime.ask(payload.question, session_id=payload.session):
            yield f"data: {json.dumps(dataclasses.asdict(event), ensure_ascii=False)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")
```

CLI 和前端消费的是同一套事件，渲染逻辑各写各的，核心一行不用改。

## 目录结构

```
.
├─ data/knowledge/          源文档（示例内容，新项目请替换）
├─ skills/                  技能（进 Git）
│  └─ example-chat-style/SKILL.md
├─ evals/                   检索评测：数据集 + 评测脚本
├─ src/agent_template/
│  ├─ config.py             全部配置的唯一入口
│  ├─ cli.py                命令行
│  ├─ agent/                主循环、事件、提示词、运行时装配
│  ├─ llm/                  模型适配 + 离线 mock
│  ├─ tools/                工具登记表 + 内置工具
│  ├─ skills/               SKILL.md 加载与按需读取
│  ├─ mcp/                  MCP 客户端、工具桥接、示例 server
│  ├─ rag/                  加载、切块、向量化、存储、检索、索引
│  ├─ memory/               会话历史
│  └─ obs/                  追踪与 token 计量
├─ tests/                   单元测试与循环测试（离线可跑）
├─ docs/                    架构说明、CLI 说明、路线图
├─ .agent/                  运行态数据（不进 Git，可随时删除重建）
│  ├─ index.sqlite3         向量索引
│  ├─ memory.sqlite3        会话历史
│  └─ traces.jsonl          运行轨迹
├─ .env.example             配置模板（进 Git）
└─ pyproject.toml
```

## 开发中踩到的坑

这些都是实际调试过的问题，写下来省得你再踩一遍：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `agent ask > out.md` 得到的文件在其他工具里是乱码 | Windows 重定向时 Python 用 GBK 写文件 | CLI 启动时把 stdout/stderr 固定为 UTF-8 |
| `ZoneInfo("UTC")` 报 `ZoneInfoNotFoundError` | Windows 不自带 IANA 时区库 | 依赖里加了 `tzdata`，并在工具里做本地时钟降级 |
| `agent tools -v` 报 `No such option: -v` | Click 的全局选项必须在子命令之前 | 用 `agent -v tools` |
| 换了 embedding 模型后检索结果全乱 | 索引与查询向量维度/语义不一致 | 索引里记录 embedder 与维度，启动时校验并提示重建 |
| 模型说"相关度只有 0.03，匹配度很低" | RRF 分数只表示排名，绝对值无意义 | 给模型的上下文里不再显示分数 |
| 片段以"料塞进权重里"开头、以标题结尾 | 字符滑窗切分不尊重文档结构 | 改为结构感知切分 + 标题路径 + 句子边界对齐 |
| 多轮对话报消息顺序非法 | 历史裁剪把 `tool_calls` 与结果拆散了 | 裁剪窗口对齐到 `user` 消息边界 |
| `sqlite3` 报 `uses 1, and there are 5 supplied` | `(value)` 不是元组，字符串被逐字符展开 | 写成 `(value,)`，或改用命名参数 |
| 反复重启后残留 python 子进程 | MCP 子进程随连接创建，异常路径没关闭 | 所有路径都走 `aclose()`（CLI 用 `try/finally`） |
| MCP 关闭时报 `Attempted to exit cancel scope in a different task` | MCP SDK 的 anyio 取消作用域有"任务亲和性"，`asyncio.gather` 会把它拆到不同任务里 | 连接与关闭都改为顺序执行，见 [mcp/README.md](src/agent_template/mcp/README.md) 第 7 条 |

## 开发

```bash
uv run pytest                 # 跑测试
uv run agent -v tools         # 带日志查看装配结果
uv run python -m agent_template.cli --help
```

分支与提交约定（本项目自己就是这么做的）：

1. 每个能力一个分支：`feat/llm-layer`、`feat/rag`、`feat/agent-loop` …
2. 从 `main` 开分支，做完 `git merge --no-ff` 合回 `main` 并删除分支——
   历史里留下清晰的"这一层是作为一个整体落地的"节点。
3. 提交前先 `git status --short`，确认没有多余文件和 `.env`。

## 路线图

- 可插拔的向量库后端（Qdrant / pgvector / sqlite-vec）
- 检索重排（bge-reranker 之类的 rerank 模型）
- 多知识库（按名字隔离多套索引）
- 带权限控制的数据库工具（只读、SQL 白名单）
- OpenAI 兼容的 HTTP 接口，方便直接接现成聊天前端

## 许可

MIT，见 [LICENSE](LICENSE)。
