# tools —— 工具层（函数调用）

> 把"普通 Python 函数"变成"模型可以调用的能力"的唯一入口。
> 本地工具、技能工具、RAG 检索、MCP 远端工具，最终都注册到同一张表里。

## 负责什么

- **注册**：从一个函数对象里读出参数类型和 docstring，自动生成 JSON Schema。
- **校验**：模型传来的参数一律先过 pydantic，不合法就拒绝执行。
- **执行**：同步函数丢线程池、异步函数直接 await，统一加超时。
- **容错**：任何失败都转成 `错误：...` 文本返回，**不抛异常**。

## 不负责什么

| 你可能会以为它管，但其实不管 | 实际在哪 |
| --- | --- |
| 决定要不要调工具 | 模型自己决定，`agent/loop.py` 负责执行 |
| 循环调用直到收敛 | `agent/loop.py` |
| 工具的业务实现 | 各 `builtin/` 与 `rag/tools.py`、`mcp/bridge.py` |
| 沙箱与权限 | 由工具自己实现（例如 `fs.py` 的 `_inside()`） |

## 成员清单

| 名字 | 类型 | 职责 |
| --- | --- | --- |
| `Handler` | 类型别名 | `Callable[..., Any]`，一个工具处理函数 |
| `Tool` | dataclass | 一个工具的完整描述：名字、说明、参数模型、处理函数、来源，以及**有没有副作用**（`read_only`，默认 `False`） |
| `Tool.spec()` | 方法 | 转成给模型看的 `ToolSpec`（JSON Schema） |
| `RawArguments` | pydantic 模型 | `extra="allow"` 的兜底参数模型，给"描述已经写好的工具"（MCP）用 |
| `ToolRegistry` | 类 | 工具登记表：注册、列举、调用 |
| `ToolRegistry.register()` | 方法（可作装饰器） | 注册一个函数，自动生成 schema |
| `ToolRegistry.add_described()` | 方法 | 注册"参数 schema 已经现成"的工具——**MCP 的接缝就在这** |
| `ToolRegistry.call()` | async 方法 | 校验 → 执行 → 把结果或错误转成文本 |
| `ToolRegistry.is_parallel_safe()` | 方法 | 这个工具能不能和同一批次里的其他工具并发执行——判据就是 `read_only`；**查不到的工具一律返回 `False`** |
| `args_model_for()` | 函数 | 用 `inspect` + `pydantic.create_model` 从函数签名造参数模型 |
| `first_line()` | 函数 | 取 docstring 第一行当作工具描述 |
| `as_text()` | 函数 | 把任意返回值转成字符串（非字符串走 JSON） |
| `get_current_time` | 内置工具 | 演示"无状态、无依赖"的工具形态 |
| `calculator` | 内置工具 | 用 AST 白名单求值，演示"不用 eval 也能算表达式" |
| `make_read_file` / `make_list_dir` | 工厂函数 | 演示"需要注入配置"的工具形态，含路径沙箱 |
| `register_builtin_tools` | 函数 | 把上面四个装进登记表 |

## 类之间的关系

```
    你的函数（普通 Python 函数）
          │
          │ register()
          ▼
   args_model_for()  ──►  参数模型（pydantic）
          │                    │
          │                    │ model_json_schema()
          ▼                    ▼
        Tool  ──spec()──►  ToolSpec  ──► 模型看到的 JSON Schema
          │
          │ call(name, arguments)
          ▼
   ① model_validate()  参数校验
   ② wait_for(handler)  执行 + 超时
   ③ as_text()          结果转文本
          │
          ▼
   "结果文本" 或 "错误：..."
```

## 为什么这么设计

**1. 为什么用函数签名自动生成 schema，而不是手写。**
手写的 schema 和实现会漂移：改了参数名忘了改 schema，模型就一直在传错参数，
而这种错误**不会报错**，只会让模型反复失败。从签名生成，实现是唯一真相。

**2. 为什么参数一定要过 pydantic。**
模型的参数是不可信输入。它可能给出 `{"path": 123}`、少传必填项、或者多塞一个
不存在的字段。校验层把这些挡在业务代码之外，否则每个工具都要自己写一遍防御代码。

**3. 为什么失败要返回文本，而不是抛异常。**
抛异常会打断整个循环，用户看到的是"程序出错"；返回文本则让**模型看到错在哪**，
它有机会换个参数重试。实测里这条很有效：模型拿到"unknown timezone"之后会换一个
时区名再试。当然，工具**内部实现**该抛就抛——`call()` 会兜住并转成文本。

**4. 为什么同步函数要丢线程池。**
工具里常见阻塞调用（读文件、发 HTTP）。直接在事件循环里跑会卡住整个进程，
`asyncio.to_thread` 让它不阻塞，同时让 `wait_for` 的超时真正生效——
否则超时只能等到阻塞结束才触发，等于没有。

**5. 为什么要有工厂函数这种形态（`make_read_file(settings)`）。**
这是本项目里最容易困惑的一点：**`settings` 不是工具的参数，而是它的环境**。
`make_read_file(settings)` 在**注册时**把配置吃掉，返回的内层 `read_file` 才是工具；
登记表读的是内层函数的签名，所以模型只看到 `path` 和 `max_chars`。

这样做的原因很实际：如果签名写成 `read_file(path, root)`，模型就能自己指定
`root="C:/"` 从而逃出沙箱。**环境由你在注册时冻结，模型只在不敏感的维度上做选择。**

**6. 为什么 `add_described()` 单独开一个口子。**
MCP 工具的 schema 是远端给的，不是从本地函数签名生成的，走不了 `register()`。
但如果不给它口子，`bridge.py` 就得自己造一套注册逻辑——那就出现了第二张表。
现在两条路都汇进同一个 `Tool` 结构，对模型完全一致。

## 怎么扩展

### 形态一：无状态的纯函数（最常见）

```python
def word_count(text: str) -> int:
    """统计一段文本的字数。"""
    return len(text)

# tools/builtin/__init__.py
registry.register(word_count)
```

**docstring 的第一行就是模型看到的描述**，写清"做什么、返回什么"比写清"怎么实现"重要得多。

### 形态二：需要配置或外部依赖（工厂函数）

```python
def make_search_web(settings: Settings):
    api_key = settings.web_search_api_key  # 注册时读一次

    async def search_web(query: str, limit: int = 5) -> str:
        """在互联网上搜索，返回前若干条结果的标题与链接。"""
        ...

    return search_web

registry.register(make_search_web(settings))
```

要点：**配置在注册时读掉，不要让它出现在内层函数的签名里**。

### 形态三：异步函数

直接用 `async def` 写即可，`call()` 会识别并 await。RAG 的
`search_knowledge_base` 就是这种形态（它要调用 async 的检索）。

### 形态四：schema 已经现成（MCP）

```python
registry.add_described(
    name="echo",
    description="原样返回传入的文本。",
    parameters=tool.input_schema,       # 远端给的 JSON Schema
    handler=handler,                     # async def handler(**kwargs) -> str
    source="mcp:example",                # 标注来源，便于排查
)
```

注意 handler 必须写成 `**kwargs` 形式：登记表会把参数**以关键字形式展开**后调用它。

## 并发执行：能并行什么、代价是什么

同一轮里模型可能一次请求多个工具。循环会把它们**按"能不能并发"分批**执行：

- **连续的只读调用**并成一批，用 `asyncio.gather` 一起跑；
- **有副作用的调用**单独成批，严格按模型给出的顺序执行。

规则实现在 `agent/loop.py` 的 `group_parallel_calls()`，判据来自本模块的 `Tool.read_only`。

### 实测（每个工具耗时 0.6s，一轮里请求 3 个）

| 模型给出的顺序 | 分批 | 耗时 |
| --- | --- | --- |
| 读、读、读 | `[3]` | **0.61s** |
| 读、读、写 | `[2, 1]` | 1.20s |
| 读、写、读 | `[1, 1, 1]` | 1.80s |
| 写、读、读 | `[1, 2]` | 1.20s |

对照：完全串行 1.8s，完全并行 0.6s。上面是实测数字，不是估算。

三条结论：

1. **耗时 = 批数 × 单次耗时**，所以优化的目标函数就是"把批数压到最少"。
2. **写操作像一道墙**：`读、写、读` 里那个写把两侧的读隔开，第二个读必须等写完成。
   这是"保证写之后再读能读到新数据"的代价，不是缺陷。
3. **收益取决于模型给出的顺序**，而我们控制不了顺序：同样三个调用，
   `读、读、写`（1.2s）比 `读、写、读`（1.8s）快 50%。

### 为什么不做更细的依赖分析

理论上可以按"资源冲突"分批：让工具声明自己访问哪些资源（比如
`resource="file:notes.md"`），那么 `读 A、写 B、读 A` 里的两个读就能并行。但没有做，因为：

- 需要每个工具作者标注资源，比标一个布尔值复杂得多，也更容易标错；
- 收益场景很窄——模型很少在同一轮里对同一批资源交替读写；
- **标错的代价很高**：漏标一个依赖就会出现"读到旧数据"，这种问题极难复现。

现在这套"标错最多只是慢一点"是安全得多的默认值。

## 后续可做（按性价比排序）

| 优先级 | 事项 | 说明与建议 | 大致工作量 |
| --- | --- | --- | --- |
| 高 | ~~并行执行同一轮的多个工具调用~~ | **已实现**：`Tool.read_only` + `group_parallel_calls()` 按连续段分批（只读并发、有副作用的保序），实测与边界见上文 | — |
| 高 | **写操作的权限确认** | 目前所有工具都是只读的，所以没做。一旦加入"写文件""执行命令"，必须引入审批机制（CLI 弹确认、API 记录审计）。建议在 `Tool` 上加 `requires_approval: bool`，由 `call()` 统一处理 | 一天起 |
| 中 | **工具级统计** | 每个工具的成功率、平均耗时、被调用次数，接到 `obs/` 里。工具一多，"哪个工具模型老是用错"就成了最该看的数据 | 半天 |
| 中 | **结果截断策略统一** | 现在各工具自己截（`read_file` 的 `max_chars`、CLI 预览的 80 字）。可以统一成 `Tool.max_result_chars`，超长时附一句"已截断，共 N 字" | 两小时 |
| 中 | **按场景分组暴露** | 工具超过 20 个之后，schema 会占掉大量上下文。可以像 MCP 那样按需发现：默认只暴露常用工具，其余通过 `list_tools` / `load_tools` 动态加载 | 一天 |
| 低 | **参数级说明** | 现在描述只取 docstring 首行。可以解析 docstring 里的 `Args:` 段落，把每个参数的说明写进 schema，模型对参数的理解会明显更准 | 半天 |
| 低 | **调用去重与缓存** | 同一轮里模型偶尔会重复调同一个工具同一组参数，可以按 `(name, arguments)` 缓存结果 | 两小时 |

我的建议：**先做"工具级统计"**——它让你在做其他优化时有数据可依。
权限确认等到真的要加写操作时再做，现在做等于给空场景造机制。
