# agent-template

> 一个最小但完整的本地开发 Agent 模板：**模型调用 · 函数调用 · 技能 · MCP**。
> 知识库检索**不在这个仓库里**——它是一个外部 MCP 服务（比如 ragkit），
> 所以换一套 RAG 实现不需要动这个项目的任何一行代码。

![Python](https://img.shields.io/badge/python-3.13%2B-blue)
![Tests](https://img.shields.io/badge/tests-87%20passed-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

每个模块都在 100~300 行之间，注释说的是"为什么这么做"，而不是复述代码。
它不是框架，是一份**可读、可改、可拆**的参考实现。

```bash
uv sync
cp .env.example .env        # 填上 AGENT_LLM_API_KEY
uv run agent                # 开始对话
```

不想配 key 也能跑（离线 mock，主链路全部可用）：

```bash
AGENT_LLM_PROVIDER=mock uv run agent ask "现在几点？"
```

## 包含什么

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| 模型调用 | `src/agent_template/llm/` | OpenAI 兼容端点统一适配（OpenAI / DeepSeek / 硅基流动 / Ollama）+ 离线 mock |
| 函数调用 | `src/agent_template/tools/` | 函数 + 类型注解 → 自动生成 JSON Schema；参数校验、超时、错误转文本都在这层 |
| 技能 | `src/agent_template/skills/` | `SKILL.md` + 渐进式披露：提示里只放目录，正文由模型按需读取 |
| MCP | `src/agent_template/mcp/` | stdio 客户端；远端工具与本地工具进同一张表，模型看不出区别 |
| 主循环 | `src/agent_template/agent/` | 推理/行动循环、事件流、会话记忆、同轮工具并发（只读并发 / 有副作用保序） |
| 工具审批 | `src/agent_template/agent/approval.py` | 有副作用的工具执行前停下来问人；只读工具直接放行 |
| 可观测 | `src/agent_template/obs/` | 链路追踪（JSONL）+ token 计量 |
| 命令行 | `src/agent_template/cli.py` | `agent` 命令：对话、单次问答、工具/技能查看、会话管理 |

**刻意不做**：不含业务逻辑（工具和技能都是示例）；不做多用户/权限/审计（单机单用户是前提）；
不自造工具协议（用 OpenAI function calling + MCP，不发明第三套）；不内置 RAG（见下）。

## 接知识库（可选，走 MCP）

检索能力由**外部服务**提供，本项目只消费它暴露的工具。以 `ragkit` 为例——
它自己管解析、切分、向量化、Milvus 和重排，并暴露一个只读工具 `search_knowledge_base`：

**不接也完全能用**：没有知识库服务时，工具表里就是没有 `search_knowledge_base`，
对话、工具、技能照常；接了之后模型才会在回答文档类问题前去检索并标注出处。
换句话说，"要不要知识库"是这个模板的使用者自己决定的事，模板本身不预设答案。

```jsonc
// .env 里的 AGENT_MCP_SERVERS（JSON 数组）
[{"name":"ragkit",
  "command":"D:/path/to/ragkit/.venv/Scripts/python.exe",
  "args":["-m","ragkit.mcp_server"],
  "env":{"MILVUS_DB":"agent_kb"},          // 用哪个 Milvus 库；不填就是 default
  "call_timeout_s":60}]
```

接上之后 `agent tools` 会多出一行，模型就能在回答文档类问题前先检索并标注出处：

```
search_knowledge_base │ mcp:ragkit │ 在知识库里检索相关片段。…
```

**为什么这么拆**：RAG 换代（换向量库、换切分、换重排模型）的频率远高于主循环。
放在进程外，它的依赖（pymilvus、pypdf、docx…）、配置和升级都不再污染这个模板；
它起不来也只是"少了几个工具"，对话照常（启动时会在 stderr 明确提示）。

## 一次提问发生了什么

以 `uv run agent ask "现在几点？"` 为例：

1. **CLI 解析参数** → `Settings`（优先级：命令行 > 环境变量 > `.env` > 默认值）。
2. **装配运行时** `AgentRuntime.create()`：注册内置工具 → 扫描 `skills/` 注册 `load_skill`/`list_skills`
   → 连接 MCP 服务器并把远端工具并进同一张表（**连不上的只跳过并记一条失败记录**）。
3. **主循环**：读会话历史 → 追加本轮 `user` 消息 → 拼系统提示（人设 + 技能目录 + 工具清单）。
4. **调用模型**（默认流式），事件流交给 CLI 渲染。
5. **模型要求调用工具** → `registry.call()`：pydantic 校验参数 → 执行（同步函数进线程池、带超时）
   → **失败不抛异常，返回 `错误：…` 文本**，让模型看到错在哪、自己纠正。
6. **没有工具调用了** → 这一轮结束；一直在调工具就到 `AGENT_MAX_STEPS` 为止。
   全程记 trace（`.agent/traces.jsonl`）、累计 token。

## 架构

```
        cli.py ──消费事件流──▶ agent/runtime.py（装配 + 资源生命周期）
                                     │
         ┌───────────┬───────────────┼───────────────┬───────────┐
         ▼           ▼               ▼               ▼           ▼
       llm/      tools/          memory/          obs/         mcp/
    模型适配     工具登记表       会话历史       追踪+计量   远端工具桥接
```

**主循环不认识技能、MCP、知识库**——它们全都只是"往工具表里注册"，所以加能力不用改循环。

| 模块 | 入口类型 | 替换方式 |
| --- | --- | --- |
| `llm/` | `LLMClient` | 新增一个适配器文件 |
| `tools/` | `ToolRegistry` | 注册新函数 |
| `skills/` | `SkillsIndex` | 往 `skills/` 放目录 |
| `mcp/` | `MCPManager` | 改 `AGENT_MCP_SERVERS` |
| `memory/` | `MemoryStore` | 换掉 SQLite 实现 |
| `obs/` | `Tracer` | 换成 OpenTelemetry |

每个模块都有自己的 README（成员职责、类之间的关系、后续优化方向）：
[llm](src/agent_template/llm/README.md) ｜ [tools](src/agent_template/tools/README.md) ｜
[skills](src/agent_template/skills/README.md) ｜ [mcp](src/agent_template/mcp/README.md) ｜
[memory](src/agent_template/memory/README.md) ｜ [agent](src/agent_template/agent/README.md) ｜
[obs](src/agent_template/obs/README.md)

## 配置

所有配置集中在 `src/agent_template/config.py` 的 `Settings`，**除它以外没有任何地方读环境变量**。
优先级：命令行参数 > 环境变量 > `.env` > 代码默认值。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_LLM_PROVIDER` | `openai_compat` | 或 `mock`（离线） |
| `AGENT_LLM_MODEL` | `deepseek-v4-flash` | |
| `AGENT_LLM_BASE_URL` | `https://api.deepseek.com/v1` | 换供应商只改这里 |
| `AGENT_LLM_API_KEY` | 空 | **只放 `.env`** |
| `AGENT_LLM_TEMPERATURE` / `_MAX_TOKENS` / `_TIMEOUT_S` / `_MAX_RETRIES` | `0.2` / `1024` / `60` / `2` | |
| `AGENT_MAX_STEPS` | `8` | 一轮最多几次模型轮次，防止无限调工具 |
| `AGENT_STREAM` | `true` | 是否流式输出 |
| `AGENT_AGENT_NAME` | `local-agent` | 出现在默认系统提示里的人设名 |
| `AGENT_SYSTEM_PROMPT` | 空 | 覆盖默认系统提示 |
| `AGENT_SKILLS_DIR` | `skills` | 技能目录 |
| `AGENT_STATE_DIR` | `.agent` | 运行态数据（记忆、追踪），不进 Git |
| `AGENT_WORKSPACE_ROOT` | `.` | 文件工具的沙箱根目录 |
| `AGENT_MCP_SERVERS` | 示例 server | JSON 数组，stdio 服务器列表（见上） |

## 命令行

| 命令 | 作用 |
| --- | --- |
| `agent` / `agent chat` | 进入交互式对话 |
| `agent ask "问题"` | 单次问答后退出 |
| `agent tools` | 列出全部工具（含 MCP 远端工具及其来源） |
| `agent skills` | 列出技能目录 |
| `agent sessions` | 查看会话列表；`--clear <会话名>` 清空某个会话 |

全局选项要写在**子命令之前**（Click 的语义）：`agent -v tools` ✅ ／ `agent tools -v` ❌
（写错位置时 CLI 会在报错里直接提醒你）。

| 选项 | 作用 |
| --- | --- |
| `-s, --session <名字>` | 会话名，隔离对话历史（默认 `default`） |
| `--no-stream` | 关闭流式输出 |
| `-v, --verbose` | 展开思考过程，并把日志级别调到 INFO |
| `--json` | 事件流以 JSONL 输出到 stdout |
| `--model` / `--provider` | 覆盖模型配置 |
| `-y, --yes` | 自动批准所有需要确认的工具（脚本、CI 用） |

交互模式下：`/help` `/tools` `/skills` `/stats` `/clear` `/exit`。

输出约定（"能进管道"的前提）：**回答走 stdout，过程走 stderr**，
所以 `uv run agent ask "..." > answer.md` 得到的是干净的回答；
退出码 `0` 成功 / `1` 运行错误 / `2` 用法错误 / `130` 被中断。

## 扩展

**加一个工具**——写个带类型注解和 docstring 的函数并注册，schema 自动生成：

```python
def word_count(text: str) -> int:
    """统计一段文本的字数。"""      # ← 这行就是模型看到的工具描述
    return len(text)

# tools/builtin/__init__.py 里注册；只读工具传 read_only=True（可并发、免审批）
registry.register(word_count, read_only=True)
```

**加一个技能**——`skills/<名字>/SKILL.md`：frontmatter 里只有 `name` 和 `description` 会进提示，
正文等模型调 `load_skill` 时再读。技能放"遇到这类任务该怎么做"，原子能力请写成工具。

**接一个 MCP 服务器**——改 `AGENT_MCP_SERVERS`，不用写代码。远端工具与本地工具同表；
重名时本地优先并记警告。服务端声明 `readOnlyHint` 的工具会被当成只读（可并发、免审批），
没声明的按"有副作用"处理，执行前问人。

**换模型供应商**——改 `.env` 三个值。若有特殊约定，继承 `OpenAICompatClient`
覆盖 `_payload` 或 `_parse_message`。

**接 Web 前端**——主循环产出的是 `AgentEvent` 流，与传输方式无关：

```python
@app.post("/chat")
async def chat(payload: ChatIn):
    async def stream():
        async for event in runtime.ask(payload.question, session_id=payload.session):
            yield f"data: {json.dumps(dataclasses.asdict(event), ensure_ascii=False)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")
```

## 目录结构

```
src/agent_template/
├─ config.py   全部配置的唯一入口        ├─ llm/     模型适配 + 离线 mock
├─ cli.py      命令行                    ├─ tools/   工具登记表 + 内置工具
├─ agent/      主循环、事件、装配         ├─ skills/  SKILL.md 加载与按需读取
├─ mcp/        MCP 客户端 + 示例 server  ├─ memory/  会话历史
└─ obs/        追踪与 token 计量

skills/           技能（进 Git）
scripts/usage.py  从 traces.jsonl 聚合 token 用量（按会话 / 按轮次）
tests/            87 条测试，全部离线
docs/             架构说明、CLI 说明、路线图
.agent/           运行态数据（记忆、追踪），不进 Git
```

## 开发

```bash
uv run pytest -q                 # 87 条测试，不需要网络、不需要 API key
uv run ruff check . && uv run mypy src
uv run agent -v tools            # 带日志看装配结果
uv run python scripts/usage.py   # token 用量归因（哪个会话、哪一轮贵）
```

**测试为什么能离线**：`AGENT_LLM_PROVIDER=mock` 提供确定性假模型，
`httpx.MockTransport` 替换 HTTP 层，工具 / 技能 / MCP 都可以注入替身——
整条链路在 CI 里可复现，不需要 Milvus、不需要 key。

## 设计取舍

- **主循环只认工具表**。技能、MCP、知识库都只是"往表里加东西"，所以加能力不改循环。
- **可选能力失败要降级，但降级必须可见**：某个 MCP 服务器起不来只跳过它、记一条失败记录，
  对话照常；启动时在 stderr 明确提示"哪台没起来、为什么、怎么办"，
  否则用户只会觉得"模型怎么不用这个能力"。
- **给模型的输入必须是它能解读的**：检索回来的相似度分数一律不进提示词——
  RRF 分（0.016~0.033）和余弦相似度（0~1）量纲不同，模型看到小数只会得出错误结论。
- **派生数据可以随时删**：`.agent/` 下的东西都能重建，清掉只丢对话历史。

## 踩过的坑（节选）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `agent ask > out.md` 得到乱码 | Windows 重定向时 Python 用 GBK 写文件 | CLI 启动时把 stdout/stderr 固定为 UTF-8 |
| `agent tools -v` 报 `No such option` | Click 的全局选项必须写在子命令之前 | 报错时直接提示正确写法 |
| 多轮对话报"消息顺序非法" | 历史裁剪把 `tool_calls` 和结果拆散了 | 裁剪窗口对齐到 `user` 消息边界 |
| 残留 python 子进程 | MCP 子进程在异常路径没关 | 所有路径都走 `aclose()` |
| 服务端说工具是只读的，却被要求审批 | 客户端没读 `annotations.readOnlyHint` | 桥接层把它映射进工具表（默认 `False`，没声明就按有副作用处理） |

更多细节见 `docs/`。

## 路线图

- 按会话聚合 token 统计（现在 `scripts/usage.py` 从 trace 文件聚合）
- 工具级统计：哪个工具模型老用错
- 上下文压缩（长对话）
- HTTP + SSE 接口，直接接现成聊天前端
- 慢 span 告警

## 用它开新项目

这个仓库是**起点，不是工作区**：用 GitHub 的 "Use this template"，
或者 `git clone` 之后 `rm -rf .git && git init -b main`。

开新项目时通常只需要改：`README.md` 标题、`pyproject.toml` 的 `name` / `description`、
`LICENSE` 的版权人，以及把 `skills/example-chat-style/`、
`src/agent_template/mcp/example_server.py` 这些示例内容换成你自己的。
包名 `agent_template` 和命令名 `agent` 一般不用改（各项目有各自的 `.venv`）。

## 许可

MIT，见 [LICENSE](LICENSE)。
