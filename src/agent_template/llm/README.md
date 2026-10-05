# llm —— 模型调用层

> 把项目的其余部分和"具体用哪家模型"彻底隔开。上面的所有代码（工具、技能、
> MCP、主循环）只认识本模块定义的类型，不认识任何一家供应商的 SDK。

## 负责什么

- **统一的数据结构**：`Message` / `ToolCall` / `ToolSpec` / `Usage` / `ChatResponse` /
  `StreamChunk`，全项目共用一套。
- **真实调用**：把统一结构翻译成 OpenAI 兼容协议的请求，再把响应翻译回来。
- **流式**：把 SSE 分片整理成"文本增量 + 终止块"两段式输出。
- **离线替身**：`MockLLM` 让整条链路在没有 key、没有网络时可跑、可测。

## 不负责什么

边界清楚，才能知道该改哪儿：

| 你可能会以为它管，但其实不管 | 实际在哪 |
| --- | --- |
| 系统提示怎么拼 | `agent/prompts.py` |
| 要不要调工具、调几轮 | `agent/loop.py` |
| token 统计与成本 | `obs/tracing.py` 的 `TokenAccountant` |
| 多供应商路由、降级、负载均衡 | 目前没有，`factory.py` 只做"二选一" |
| 提示词模板与 few-shot | 没有，由调用方组织 `messages` |

## 成员清单

| 名字 | 文件 | 类型 | 职责 |
| --- | --- | --- | --- |
| `Message` | `base.py` | pydantic 模型 | 一条对话消息。用 `Message.user(...)` / `.assistant(...)` / `.tool_result(...)` 构造，比手填 `role` 更不容易错 |
| `ToolCall` | `base.py` | 模型 | 模型发起的一次工具请求：`id` / `name` / 已解析的 `arguments` |
| `ToolSpec` | `base.py` | 模型 | 给模型的工具说明书，`to_wire()` 转成协议格式 |
| `Usage` | `base.py` | 模型 | 输入/输出 token 数 |
| `ChatResponse` | `base.py` | 模型 | 非流式的一次完整回复 |
| `StreamChunk` | `base.py` | 模型 | 流式的一帧：`text` / `reasoning` / `done` 三种 |
| `LLMError` | `base.py` | 异常 | 把各家 SDK 的异常收敛成一种，上层只需要 catch 它 |
| `LLMClient` | `base.py` | 抽象基类 | **这一层的核心契约**：`chat()` / `stream_chat()` / `aclose()` |
| `OpenAICompatiClient` | `openai_compat.py` | 实现 | 真实调用。官方 `openai` SDK + 自定义 `base_url`，通吃 OpenAI / DeepSeek / 硅基流动 / Ollama |
| `MockLLM` | `mock.py` | 实现 | 离线确定性假模型，按关键词决定调哪个工具 |
| `build_llm` | `factory.py` | 函数 | 按 `AGENT_LLM_PROVIDER` 挑实现并注入配置 |

## 类之间的关系

```
            factory.build_llm(settings)
                     │ 返回
                     ▼
              ┌─────────────┐
              │  LLMClient  │  ← 抽象基类，定义三个方法
              └─────────────┘
                 ▲        ▲
                 │        │
   OpenAICompatiClient   MockLLM
     （真实调用）        （离线）
                 │
                 │ 读写
                 ▼
      Message / ToolSpec ──► ChatResponse / StreamChunk
```

一次调用的数据流：

```
调用方 ──[list[Message], list[ToolSpec]]──► client.chat()
                                              │
                          _to_wire_message()  │  统一结构 → 协议格式
                                              ▼
                                        openai SDK
                                              │
                        _parse_arguments()    │  工具参数是 JSON 字符串，这里解析
                                              ▼
调用方 ◄──── ChatResponse(Message, Usage) ─────┘
```

关键点：**工具参数是模型生成的 JSON 字符串**，永远可能不合法。`_parse_arguments`
负责解析并把失败记进 `ToolCall.parse_error`，而不是让异常冒到主循环里。

## 为什么这么设计

**1. 为什么要 `LLMClient` 这层抽象，而不是到处 `import openai`。**
换供应商是必然事件（价格、可用性、私有部署）。有这层之后，换供应商等于改 `.env`
三个值；没有这层，等于全项目搜索替换。

**2. 为什么数据模型自己造，不直接用 SDK 的类型。**
SDK 的类型是"它们的协议"的映射；换成另一家，字段名、结构都会变。自造一套让
`agent/` 以上的代码永远不会因为 SDK 升级而改动。代价是多一层转换——这层转换
恰好也是最该写测试的地方。

**3. 为什么流式设计成"文本增量 + 终止块"。**
流式过程中你只能拿到碎片；但调用方最终需要一条**完整的 assistant 消息**
（尤其要拿到 `tool_calls`，它们是最后才拼齐的）。所以协议定为：先不断 yield
`kind="text"` / `"reasoning"`，最后一定 yield 一个 `kind="done"`，其中带着
组装好的 `Message` 和 `Usage`。调用方因此可以"边显示边收集"，不用自己拼。

**4. 为什么 mock 要写成一个真正的 `LLMClient`。**
如果 mock 只是测试里的假对象，那它就无法用于演示和手动验证。让它是 `LLMClient`
的正式实现，意味着"整条链路能否离线跑通"是一个**可以随时验证的事实**，
而不是等到 CI 里才发现的问题。

**5. 为什么 `api_key` 用 `SecretStr`——以及那个真实踩过的坑。**
`SecretStr` 让密钥不会意外出现在日志或 `repr()` 里。但它是**包装对象**，
`str(secret)` 得到的是 `'**********'` 而不是明文。我们因此吃过一次 401：
`factory` 里算出了 `api_key = settings.llm_api_key.get_secret_value()`，
却在构造时传了 `settings.llm_api_key` 本身，于是 SDK 把十个星号当密钥发了出去。
**改动这一层时，务必确认传下去的是 `get_secret_value()` 的结果。**

**6. 为什么流式请求带 `stream_options` 且要能降级。**
`stream_options={"include_usage": True}` 能让最后一帧带回 token 用量，否则流式
调用就统计不到消耗。但少数网关会拒绝这个参数并返回 400，所以 `stream_chat` 里
捕获 400 后去掉该参数重试一次——用一次额外的请求，换"在任何兼容端点上都能跑"。

## 怎么扩展

### 接一家 OpenAI 兼容的新供应商

通常不需要写代码，改配置即可：

```ini
AGENT_LLM_BASE_URL=https://new-provider.example/v1
AGENT_LLM_MODEL=their-model-name
AGENT_LLM_API_KEY=sk-...
```

### 接一家协议不兼容的（例如 Anthropic Messages API）

在 `llm/` 下新增一个文件，实现同样的三个方法：

```python
class AnthropicClient(LLMClient):
    """把 Anthropic Messages API 翻译成我们自己的类型。"""

    async def chat(self, messages, *, tools=None, temperature=None,
                   max_tokens=None, response_format=None) -> ChatResponse:
        # 1. _to_wire：Message → {"role": ..., "content": [...]}
        #    注意：Anthropic 的 system 是顶层参数，不是一条消息
        # 2. 调用 SDK，把 tool_use 块转成我们的 ToolCall
        # 3. 把 stop_reason 映射成我们的 finish_reason
        ...

    def stream_chat(self, messages, **kwargs) -> AsyncIterator[StreamChunk]:
        # 逐事件翻译，最后必须 yield 一个 kind="done" 的终止块
        ...
```

然后在 `factory.build_llm` 里加一个分支。**除了这两个文件，其他代码都不用动。**

### 加真正的多供应商路由

想做成"主挂自动切备"，建议包一层而不是改适配器：

```python
class FallbackClient(LLMClient):
    """依次尝试多个客户端，第一个成功即返回。"""

    def __init__(self, clients: list[LLMClient]) -> None:
        self.clients = clients

    async def chat(self, *args, **kwargs) -> ChatResponse:
        last: Exception | None = None
        for client in self.clients:
            try:
                return await client.chat(*args, **kwargs)
            except LLMError as exc:
                last = exc
        raise last
```

它本身也是 `LLMClient`，所以 `factory` 里包一层就能用，上层无感。

## 后续可做（按性价比排序）

| 优先级 | 事项 | 说明与建议 | 大致工作量 |
| --- | --- | --- | --- |
| 高 | **结构化输出** | `chat()` 已经预留 `response_format` 参数但没人用。适合做"必须返回 JSON"的场景（抽取、分类）。加一个 `chat_json(messages, schema)` 便利方法即可 | 半天 |
| 高 | **参数校验兜底** | 在 `__init__` 里断言 `api_key` 是 `str` 而非 `SecretStr`，把那个 401 的坑从"运行时谜题"变成"启动即报错" | 十分钟 |
| 中 | **真实成本计量** | 给 `TokenAccountant` 配上各模型的单价表，`agent -v` 就能打印本次花销 | 半天 |
| 中 | **重试策略细分** | 现在依赖 SDK 的 `max_retries`。可以按错误类型区分：429 退避重试、5xx 重试、4xx 立即失败并给出可读原因 | 半天 |
| 中 | **响应缓存** | 相同 `messages + tools` 直接命中缓存，开发调试时能省掉大量重复请求 | 一天 |
| 低 | **多模态消息** | `Message.content` 现在是 `str`，要支持图片得改成 `str \| list[ContentBlock]`。改动会波及所有构造点，建议等真有需求再做 | 一天 |
| 低 | **Prompt caching** | 部分供应商支持缓存长前缀（如系统提示 + 工具定义）。适合系统提示很长的场景，短提示收益有限 | 一天 |

我的建议顺序是：**先做"参数校验兜底"和"结构化输出"**——它们成本极低、收益直接；
成本计量和缓存等到你真正开始在意钱和速度时再做，那时候你会有更明确的取舍依据。
