# obs —— 可观测性

> 回答"这次运行到底发生了什么、慢在哪、花了多少"。
> 刻意做得极简：一个 JSONL 文件 + 一个计数器，接口只有一个上下文管理器。

## 负责什么

- **链路追踪**：记录每个 span 的名字、耗时、状态、属性，追加写入 JSONL。
- **token 计量**：累计输入/输出 token，可选换算成成本。

## 不负责什么

| 你可能会以为它管，但其实不管 | 说明 |
| --- | --- |
| 日志输出与级别 | 由 `cli.py` 的 `setup_logging()` 配置，这里只写 `logger.debug` |
| trace 的可视化 | 没有 UI。文件是 JSONL，可以自己写脚本或喂给现有平台 |
| 采样与限流 | 全部记录。数据量大时需要加（见"后续可做"） |
| 分布式追踪 | 单进程。trace_id 是本地生成的 |

## 成员清单

| 名字 | 文件 | 类型 | 职责 |
| --- | --- | --- | --- |
| `TraceSpan` | `tracing.py` | dataclass | 一次操作的记录：`name` / `trace_id` / `span_id` / `started_at` / `duration_ms` / `status` / `error` / `attributes` |
| `Tracer` | `tracing.py` | 类 | 收集 span，可选追加写入 JSONL |
| `Tracer.span()` | `tracing.py` | 上下文管理器 | **唯一入口**：`with tracer.span("tool.call", tool="read_file"):` |
| `Tracer.summary()` | `tracing.py` | 方法 | 一行摘要：共几个 span、最慢的是哪个、失败几个 |
| `TokenAccountant` | `tracing.py` | 类 | 累计 token 与成本 |
| `TokenAccountant.add()` | `tracing.py` | 方法 | 每次模型调用后累加（输入与输出分开记） |
| `TokenAccountant.cost_usd` | `tracing.py` | 属性 | 按单价换算；**没配单价返回 `None`** |

## 类之间的关系

```
   AgentLoop ──with tracer.span("llm.turn", step=..., stream=...)
             └─with tracer.span("tool.call", tool=...)
                            │
                            ▼
                        Tracer ──┬──► self.spans（内存里留一份，测试可断言）
                                 └──► .agent/traces.jsonl（一行一个 JSON）

   AgentLoop ──accountant.add(prompt_tokens, completion_tokens)
                            │
                            ▼
                     TokenAccountant ──► summary() ──► CLI 收尾打印
```

## 为什么这么设计

**1. 为什么落地格式选 JSONL。**
追加写、断电最多丢最后一行、不会让整个文件不可读；同时能直接 `grep`，
也能喂给任何日志系统。相比一上来接 OpenTelemetry，这层的替换成本极低——
对外接口只有一个 `span()`，换实现不影响任何调用方。

**2. 为什么用同步的上下文管理器。**
span 只记录时间和状态，期间不 `await` 任何东西，所以同步写法就够，而且在同步/异步
代码里都能用。做成 async 版本反而限制使用场景。

**3. 为什么异常照常向外抛。**
`with` 块里出错时，这里记录状态和错误信息，然后**让异常继续往上抛**。
吞掉异常比没有日志更糟——调用方会以为操作成功了。

**4. 为什么用 `perf_counter` 而不是 `time.time`。**
前者单调递增，不受系统时钟调整（NTP 同步、手动改时间）影响，量出来的耗时才是真的。

**5. 为什么不记录正文（prompt 全文、模型回答）。**
这是刻意的克制。日志里混入用户内容，在产品环境里就是隐私问题，而且会让文件迅速膨胀。
需要排查具体内容时，用 `--json` 事件流或会话记忆，那是显式开启的。

**6. 为什么单价是可选的。**
不同供应商、不同模型价差很大，写死反而误导。不填单价就只统计 token，
接口保持一致——**统计口径统一，成本换算按需开启**。

## 已知问题

- `tracing.py` 里的 logger 名字写成了 `"agent.trance"`（`trace` 拼错）。
  功能不受影响，但它导致这一层的日志不在 `agent.trace` 命名空间下，
  按名字过滤日志时会漏掉。**建议顺手改成 `"agent.trace"`。**

## 怎么扩展

### 接 OpenTelemetry

`Tracer.span()` 的形状与 OTel 的 span 很接近，替换点是 `record()` / `_emit()`，
把 span 转成 OTel 的 span 即可。因为外部只依赖 `span()` 这一个入口，
调用方一行都不用改。

```python
from opentelemetry import trace

class OtelTracer:
    def span(self, name: str, **attributes):
        return trace.get_tracer("agent").start_as_current_span(name, attributes=attributes)
```

### 加采样

对话量上来之后，全量记录既占磁盘又没意义。最简单的采样策略：只记录
**失败的 span + 耗时超过阈值的 span + 随机 1%**。

### 加日志轮转

`traces.jsonl` 会一直增长。用 `logging.handlers.RotatingFileHandler` 的思路，
或者干脆按天分文件（`traces-2026-09-27.jsonl`）。

## 后续可做（按性价比排序）

| 优先级 | 事项 | 说明与建议 | 大致工作量 |
| --- | --- | --- | --- |
| 高 | **修掉 logger 名的拼写** | 一行的事，但会影响日志过滤 | 五分钟 |
| 高 | **按会话聚合统计** | 现在只有单次运行的摘要。把 `(session_id, 轮次, tokens, 耗时)` 记下来，就能回答"这个会话一共花了多少" | 半天 |
| 中 | **慢 span 告警** | 某个工具或某轮模型调用超过阈值时明确提示，比事后翻文件管用 | 两小时 |
| 中 | **成本表** | 把常见模型的单价整理进配置，`agent -v` 直接打印本次花销。建议和 `llm/README.md` 里的"真实成本计量"一起做 | 半天 |
| 中 | **trace 可视化** | 写个几十行的脚本把 JSONL 转成瀑布图（HTML），排查多步工具链时非常直观 | 半天 |
| 低 | **日志轮转与保留策略** | 长期运行的必需品 | 两小时 |
| 低 | **接 OpenTelemetry / Langfuse** | 需要与现有观测平台打通时再做。模板阶段保持零依赖更合适 | 一天 |

我的建议：**先修 logger 名、再做"按会话聚合统计"**。前者是明确的缺陷；
后者能回答一个马上就会遇到的问题——"我今天调这个 agent 花了多少 token"。
