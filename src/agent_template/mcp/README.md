# mcp —— Model Context Protocol 客户端

> 让"别人写好的工具"和"你自己写的工具"出现在同一张表里，模型看不出区别。

## 负责什么

- **拉起服务**：按配置启动 stdio 子进程，建立管道。
- **握手**：完成 MCP 的 `initialize` 流程，拉取工具清单。
- **桥接**：把远端工具注册进本地 `ToolRegistry`，附带来源标记。
- **转发**：把模型的调用转发过去，把结果（或错误）转成文本。

## 不负责什么

| 你可能会以为它管，但其实不管 | 说明 |
| --- | --- |
| HTTP / SSE 传输 | **只支持 stdio**。要接远程 server 需扩展传输层（见下文） |
| MCP 的 resources / prompts | 只用了 `tools` 这一部分能力 |
| 鉴权与授权 | 子进程以当前用户身份运行，权限就是你的权限 |
| 服务端实现规范 | 附带一个最小示例 server，但它不是教学项目 |

## 成员清单

| 名字 | 文件 | 类型 | 职责 |
| --- | --- | --- | --- |
| `MCPError` | `errors.py` | 异常 | 连接层面的失败：连不上、握手超时。**工具自身的失败不走这里** |
| `MCPTimeoutError` | `errors.py` | 异常 | 启动握手超时。**单独一个类型**，因为超时的排查动作和"命令不存在"完全不同，上层只能靠类型区分 |
| `MCPConnectResult` | `client.py` | dataclass | 一个服务器的连接结果：`ok` / `tools` / `reason`（原因）/ `hint`（建议）/ `cause`（原始异常） |
| `MCPTool` | `client.py` | dataclass | 远端工具的描述：`server` / `name` / `description` / `parameters` |
| `MCPClient` | `client.py` | 类 | 单个服务器的常驻连接：`connect()` / `list_tools()` / `call_tool()` / `aclose()` |
| `MCPManager` | `client.py` | 类 | 多个客户端的集合：`connect_all()` / `all_tools()` / `call()` / `aclose()`。**连接与关闭都是顺序执行**（原因见下文第 7 条） |
| `explain_failure()` | `diagnostics.py` | 函数 | 把异常翻译成 `(原因, 排查建议)`——沿异常链从外往里找第一个认得的类型 |
| `exception_chain()` / `root_cause()` | `diagnostics.py` | 函数 | 摊平异常链 / 挖到最底层原因 |
| `manual_command()` | `diagnostics.py` | 函数 | 拼出"手动跑一次"的复现命令（含引号处理） |
| `_handler_for()` | `bridge.py` | 函数 | 为单个远端工具造一个本地 handler（闭包绑住 server 与工具名） |
| `register_mcp_tools()` | `bridge.py` | async 函数 | 发现工具并注册进登记表，重名跳过 |
| `MCPServer` / `echo` / `add` | `example_server.py` | 示例 server | 随模板附带的零依赖 stdio server |

## 类之间的关系

```
  .env 里的 AGENT_MCP_SERVERS（JSON）
              │
              ▼
        MCPManager ──── connect_all() ────► MCPClient × N
              │                                  │ 拉起子进程
              │                                  │ initialize 握手
              │                                  ▼
              │                            tools/list → MCPTool[]
              │                                  │
              ▼                                  │
     register_mcp_tools(registry, manager) ◄─────┘
              │
              │ add_described(name, description, parameters, handler)
              ▼
        ToolRegistry（与本地工具同表）
              │
              │ 模型调用时
              ▼
     handler(**kwargs) → MCPManager.call(server, name, args) → MCPClient.call_tool()
```

## 为什么这么设计

**1. 为什么默认用 stdio，而不是 HTTP。**
stdio 是"客户端拉起服务端"：你的程序 `subprocess` 起一个进程，通过它的标准输入输出
说话。**没有端口、没有常驻服务、不需要运维**——`git clone` 下来就能跑通整条链路，
这对模板至关重要。HTTP 传输是另一种取舍（多机部署、服务复用），等真有那个需求再加。

**2. 为什么必须用 `AsyncExitStack` 管理生命周期。**
`mcp.Client(...)` 是异步上下文管理器。如果在 `connect()` 里写成 `with`，出了块连接
就被关掉，之后调用工具会失败——而这个错误往往在"第二次调用"才暴露，很难联想到
是生命周期问题。所以这里用 `ExitStack` 手动接管：会话常驻，直到显式 `aclose()`。

配套的另一个坑：**连接失败时也必须把已经建立的资源收干净**。第一版把
`list_tools()` 放在了 `try` 之外，导致握手成功但拉清单失败时子进程泄漏——
你会看到一堆僵尸 python 进程占着管道。现在整段都在 `try` 里，失败即 `aclose()`。

**3. 为什么子进程要继承 `os.environ`。**
MCP server 也是一个 Python 程序，它需要找到解释器、需要能 `import agent_template`。
只传 `settings.env` 会让子进程失去 `PATH` 和虚拟环境信息，症状是
"莫名其妙找不到 python"。所以实际传的是 `{**os.environ, **settings.env}`——
继承当前环境，再叠加服务器自己的配置。

**4. 为什么工具重名要跳过，而不是覆盖。**
覆盖意味着**一个远端 server 可以劫持本地工具**：它只要把自己的工具命名为
`read_file`，模型下次调用就会走到远端去。所以规则是本地优先，重名时跳过并记 warning。
（更彻底的做法是给远端工具统一加前缀，见"后续可做"。）

**5. 为什么桥接的 handler 要写成 `**kwargs`。**
登记表把模型的参数**以关键字形式展开**后调用 handler（见 `tools/registry.py` 里
`RawArguments` 的 `extra="allow"`）。而 MCP 需要的是一个完整 dict，所以：

```python
async def handler(**kwargs: Any) -> str:
    return await manager.call(tool.server, tool.name, kwargs)
```

写成 `handler(arguments: dict)` 是接不上的——这是跨层适配时最容易搞错的一处。

**6. 关于 SDK 版本：2.x 与 1.x 完全不兼容。**
1.x 里服务端基类叫 `FastMCP`，2.x 改名成 `MCPServer`（`mcp.server.mcpserver`），
客户端高层入口是 `mcp.Client`。网上大量示例还是 1.x 写法，直接抄会报
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`。本项目按 2.x 写，
`pyproject.toml` 里锁的是 `mcp>=2.2.0`。

**7. 为什么 `connect_all` 和 `aclose` 都是顺序执行，而不是并发。**
MCP SDK 的 stdio 客户端内部用了 anyio 的**取消作用域**，而取消作用域有"任务亲和性"：
在哪个任务里进入，就必须在同一个任务里退出。`asyncio.gather` 会把每个协程包成独立的
Task，于是 `connect()` 在 Task-A 里建立连接、`aclose()` 在另一个任务里执行，触发：

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

子进程其实还是被杀掉了，但异常会冒出来——更糟的是它很容易被上层的
`return_exceptions=True` 悄悄吞掉，变成"看起来没事、其实没关干净"的状态。
所以这两个方法都改成顺序执行：代价是多个 server 串行启动（总耗时累加），
换来的是干净、可预期的生命周期。

## 怎么扩展

### 接第三方 server（不需要写代码）

```ini
# 文件系统 server（Node 生态）
AGENT_MCP_SERVERS=[{"name":"fs","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","."]}]

# Python 的 fetch server
AGENT_MCP_SERVERS=[{"name":"fetch","command":"uvx","args":["mcp-server-fetch"]}]

# 多个 server 同时挂上
AGENT_MCP_SERVERS=[{"name":"fs","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","."]},{"name":"example","command":"python","args":["-m","agent_template.mcp.example_server"]}]
```

挂完用 `uv run agent tools` 确认工具出现了；`-v` 还能看到连接日志。

### 写自己的 server

`example_server.py` 是最小样板：

```python
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("my-tools")

@mcp.tool()
def query_metrics(name: str, window_minutes: int = 5) -> str:
    """查询指定指标的最近 N 分钟数据。"""
    ...

if __name__ == "__main__":
    mcp.run()   # 默认 stdio
```

docstring 会成为工具描述——和本地工具一样，**描述写得好不好直接决定模型用得对不对**。

### 加 HTTP 传输

`mcp.Client` 除了 `StdioServerParameters` 也接受地址字符串。扩展点在
`MCPServerSettings`：加一个 `url` 字段，`MCPClient.connect()` 里按"有 url 就用 url"
分支即可。上层（`MCPManager`、`bridge`）完全不用改——它们只认 `MCPTool`。

## 后续可做（按性价比排序）

| 优先级 | 事项 | 说明与建议 | 大致工作量 |
| --- | --- | --- | --- |
| 高 | **工具名加命名空间** | 现在重名靠"跳过 + 警告"，多个 server 时很容易撞。改成 `fs__read_file` 这样的前缀，冲突问题从根上消失。代价是模型看到的工具名变长，所以要权衡 | 两小时 |
| 高 | ~~启动失败的可视化提示~~ | **已实现**：`MCPConnectResult` 带 `reason` + `hint`，CLI 在开始对话前打印"哪台服务器没起来、为什么、怎么排查" | — |
| 中 | **健康检查与重连** | 长会话里子进程可能崩。定期 ping，失败则重连并把工具表刷新一遍 | 一天 |
| 中 | **支持 HTTP / SSE 传输** | 接入远程或共享的 MCP 服务时需要 | 一天 |
| 中 | **resources / prompts** | MCP 还有资源（可读数据）和提示模板两类能力。接进来能解锁更多现成 server | 两天 |
| 低 | **工具调用审计** | 记录"哪个 server 的哪个工具被谁调用、参数是什么"，跨进程调用的透明度值得投入 | 半天 |

我的建议：**先做"工具名命名空间"**——挂第二个 server 时一定会遇到重名，
而重名现在只记一条 warning 就被跳过了，和"没配"很难区分。
