# 命令行说明

CLI 的目标不是"能跑"，而是**能被当作工具使用**：进管道、进脚本、进 CI。

## 命令一览

```
agent                     进入交互式对话（不带子命令时的默认行为）
agent chat                同上，显式写法
agent ask "问题"          单次问答后退出
agent tools               列出全部工具（含 MCP 远端工具与来源）
agent skills              列出技能目录
agent index               重建知识库索引
agent sessions            查看会话列表
agent sessions --clear X  清空会话 X 的历史
```

`agent ask` 接受多个词，不必加引号：`agent ask 现在 几点` 等价于 `agent ask "现在 几点"`。

## 全局选项

> **全局选项必须写在子命令之前**：`agent -v tools` ✅ ／ `agent tools -v` ❌
> （这是 Click 的标准语义。写错时会报 `No such option: -v`，并在报错里直接提醒你
> 该把选项挪到前面——`agent --help` 的顶部也写了同一条。）

| 选项 | 简写 | 作用 |
| --- | --- | --- |
| `--session` | `-s` | 会话名，隔离对话历史，默认 `default` |
| `--verbose` | `-v` | 展开模型思考过程 + 日志级别调到 INFO |
| `--json` | | 事件流以 JSONL 输出到 stdout |
| `--no-stream` | | 关闭流式输出 |
| `--model` | | 覆盖 `AGENT_LLM_MODEL` |
| `--provider` | | 覆盖 `AGENT_LLM_PROVIDER` |
| `--yes` | `-y` | 自动批准所有需要确认的工具（脚本、CI 用） |
| `--version` | | 打印版本 |

配置优先级：**命令行参数 > 环境变量 > `.env` > 代码默认值**。

## 交互模式

进入 `agent` 后可用斜杠命令（不会被发给模型）：

```
/help     查看命令
/tools    列出工具
/skills   列出技能
/stats    本次会话累计 token
/clear    清空当前会话历史
/exit     退出（Ctrl+C / Ctrl+D 同样可以）
```

## 工具审批（Human in the loop）

只有**有副作用的工具**（`read_only=False`，比如写文件、改数据）会触发审批；
只读工具（时间、计算、读文件、检索）直接执行，不打断你。

触发时长这样：

```
⚠ 需要确认：`delete_note` 要做一次有副作用的操作
   参数：{"path": "重要笔记.md"}
   执行吗？[y/N]
```

四种情形的行为：

| 场景 | 行为 |
| --- | --- |
| 终端里回答 `y` | 执行 |
| 终端里回答 `n` 或直接回车 | 拒绝（**默认是拒绝**，回车不会误批准） |
| 非交互式环境（管道、重定向、CI） | 直接拒绝，并提示可以加 `--yes` |
| 加了 `--yes` | 全部放行，不提问 |

被拒绝时，回给模型的不是"错误"，而是一段明确的话：**这是用户的决定、不是技术故障、
不要重试、也不要换别的方式做同一件事**。最后两句很关键——否则模型会绕道
（写文件被拒 → 去调执行命令），审批就形同虚设。

**一条刻意保留的安全性质**：批准只能来自"真人在终端里敲 y"或"显式加 `--yes`"。

```bash
echo y | uv run agent ask "帮我删掉那个笔记"    # 不会被当成批准
```

因为"检查 stdin 是不是终端"发生在读输入之前，所以管道里的 `y` 直接走拒绝分支。
代价是 CI 里想自动批准必须写 `--yes`，不能用 `yes |` 之类的技巧——这个取舍是故意的：
**批准这个动作不能被脚本用管道伪造**。

## 输出约定

这三条决定了它能不能进管道：

**① 内容与过程分流。**

```bash
uv run agent ask "什么是 MCP" > answer.md
```

`answer.md` 里**只有回答**。工具调用、token 用量、思考摘要全部走 stderr。

**② 编码固定为 UTF-8。**

CLI 启动时会把 stdout/stderr 强制设为 UTF-8，不跟随系统区域设置。
（Windows 上默认用 GBK 写文件，导致重定向出来的文件在其他工具里是乱码——
我们踩过这个坑，所以在这里堵死。）

**③ 退出码有意义。**

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 运行错误（模型调用失败、知识库为空等） |
| 2 | 用法错误（Typer/Click 处理） |
| 130 | 被用户中断（Ctrl+C） |

```bash
uv run agent ask "..." || echo "出错了"
```

## `--json`：事件流

```bash
uv run agent --json ask "一加一等于几"
```

stdout 每行一个 JSON，对应一个 `AgentEvent`。字段定义见
`src/agent_template/agent/events.py`：

```json
{"kind": "started", "step": 0, "text": "", "data": {"session_id": "default", "max_steps": 8}}
{"kind": "text", "step": 0, "text": "1 + 1 = 2", "data": {}}
{"kind": "usage", "step": 0, "text": "", "data": {"total_tokens": 1234}}
{"kind": "finished", "step": 0, "text": "1 + 1 = 2", "data": {}}
```

**这是前端契约**：CLI 和未来的 Web 端消费的是同一套事件，只是渲染方式不同。

## 渲染策略

| 事件 | 去向 | 呈现 |
| --- | --- | --- |
| `text` | stdout | 直接流式输出，不换行不加工 |
| `reasoning` | stderr | 默认只累计字数，结束时显示"思考 N 字"；`-v` 时逐段展开 |
| `tool_call` | stderr | `→ 工具名({"参数": ...})` |
| `tool_result` | stderr | `← 结果前 80 字…` |
| `usage` | stderr | `· 用量 N tokens` |
| `started` / `finished` | stderr | 会话名、步数上限、思考摘要 |
| `error` | stderr | 红色高亮 |

思考过程默认折叠，是因为实测里一次回答可能产生**九十多个**思维链片段——
逐段打印会把真正的回答淹没。

## 日志

| 级别 | 何时出现 |
| --- | --- |
| WARNING（默认） | RAG 不可用、MCP server 连接失败等需要注意但可降级的情况 |
| INFO（`-v`） | 装配结果：加载了几个技能、RAG 索引了多少片段、连上了哪些 MCP server、工具表总数 |
| DEBUG | 目前只有 span 记录；`obs/tracing.py` 里的 `logger.debug` |

日志通过 `setup_logging()` 接到 stderr。**必须显式配置**——Python 的 root logger
默认级别是 WARNING 且没有 handler，不配置的话所有 INFO 日志都会静默丢失。

## 退出时的资源清理

每条路径都包在 `try/finally` 里调用 `runtime.aclose()`：MCP 子进程、RAG 的
SQLite 连接、模型的 HTTP 客户端都会被释放。**Ctrl+C 中断也会走到这里**——
否则会残留 python 子进程占着管道。

用 `tasklist | grep -i python`（Windows）或 `ps aux | grep agent_template` 可以确认。

## 扩展

CLI 是"薄壳"，加功能时优先改下游而不是这里：

| 想加的东西 | 正确做法 |
| --- | --- |
| 新工具 | 注册到 `ToolRegistry`，`agent tools` 自动显示 |
| 新技能 | 放进 `skills/`，`agent skills` 自动显示 |
| 新事件类型 | 在 `agent/events.py` 加，在 `EventRenderer._render_pretty` 加渲染分支 |
| 新子命令 | 在 `cli.py` 加一个 `@app.command()` |
| Web 前端 | **不要**改 CLI，新增 `api/` 消费同一套事件流 |
