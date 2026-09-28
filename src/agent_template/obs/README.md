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
| `TraceSpan` | `tracing.py` | dataclass | 一次操作的记录：`name` / `trace_id` / `span_id` / `started_at` / `ts` / `duration_ms` / `status` / `error` / `attributes` |
| `Tracer` | `tracing.py` | 类 | 收集 span，拿着公共上下文，可选追加写入 JSONL |
| `Tracer.span()` | `tracing.py` | 上下文管理器 | **唯一入口**：`with tracer.span("tool.call", tool="read_file"):` |
| `Tracer.set_context()` | `tracing.py` | 方法 | 注入"每个 span 都该有"的字段（`session` / `run`），自动并进每次 `span()` 的 attributes |
| `Tracer.summary()` | `tracing.py` | 方法 | 一行摘要：共几个 span、最慢的是哪个、失败几个 |
| `TokenAccountant` | `tracing.py` | 类 | 累计 token 与成本 |
| `TokenAccountant.add()` | `tracing.py` | 方法 | 每次模型调用后累加（输入与输出分开记） |
| `TokenAccountant.cost_usd` | `tracing.py` | 属性 | 按单价换算；**没配单价返回 `None`** |

`TraceSpan` 的字段分两部分：**信封**（`name` / `trace_id` / `span_id` / 两个时间戳 /
耗时 / 状态 / 错误 / attributes）每行都一样，所以按行 grep、按字段聚合都不用特判；
**载荷**放在 `attributes` 里，随 span 类型变化——`llm.turn` 有 `step` 和 token，
`tool.call` 有工具名。

## 类之间的关系

```
   AgentLoop ──with tracer.span("llm.turn", step=..., stream=...)
             └─with tracer.span("tool.call", tool=...)
                            │  每轮的 session / run 由
                            │  tracer.set_context(session=..., run=...) 注入
                            ▼
                        Tracer ──┬──► self.spans（内存里留一份，测试可断言）
                                 └──► .agent/traces.jsonl（一行一个 JSON）

   AgentLoop ──accountant.add(prompt_tokens, completion_tokens)
                            │
                            ▼
                     TokenAccountant ──► summary() ──► CLI 收尾打印
                            ▲
                            └── 同一份数字也写进 llm.turn 的 span.attributes
                                （内存里那份随进程消失，span 那份留在文件里）
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

**7. 为什么 `session` / `run` 由 Tracer 注入，而不是在每个调用点手写。**
`span("llm.turn", session=...)` 这种写法看着更直白，但它把"每个 span 都该有"的字段
交给了每个调用点去记得。漏写的后果是静默的：`tool.call` 少了 `session`，等你按会话
聚合时才发现那一半数据是残的。**公共上下文由 Tracer 统一注入，调用点只写这次调用
独有的东西**——这也是"信封统一、载荷自由"那条结构能守住的原因。

代价是：`Tracer` 是进程内共享的一份状态，前提是**同一时刻只跑一轮对话**（CLI 的用法）。
将来要并发跑多个会话，得换成 `contextvars`，或者把上下文显式传给 `span()`。

**8. 为什么 token 要写进 span，而不是只留在 `TokenAccountant` 里。**
两者寿命不同，分工也不同：

| | 存在哪 | 寿命 | 回答的问题 |
| --- | --- | --- | --- |
| `TokenAccountant` | 进程内存 | 进程退出即归零 | "这次回答用了多少" |
| `llm.turn` 的 span | `.agent/traces.jsonl` | 一直在 | "这个会话、这一轮、哪一步贵" |

这里没有为用量单独建表，是因为用量是**只追加、只聚合**的遥测数据：不需要 join、
不需要更新、不需要事务。相比之下，JSONL 追加写没有迁移成本，也不会和将来的
会话存储（可能换成知识库或别的数据库）产生任何耦合——`obs` 只要求一个
`session_id` 字符串当 join key，不 import memory 的任何类型。

代价是聚合靠扫文件（几千行以内无所谓，再多就用 DuckDB 直接对 JSONL 跑 SQL），
以及文件只增不减，需要时再加轮转（见下文）。

## 怎么扩展

### 看用量（已经能用了）

```bash
uv run python scripts/usage.py              # 按会话 + 最近几轮
uv run python scripts/usage.py --session default --runs 20
```

它读的就是 `traces.jsonl` 里 `llm.turn` 的 span。想加一列自己的指标（比如某个工具的
耗时），在 `Tracer.set_context()` 或对应的 `span(...)` 里补字段，脚本里多读一个键即可——
**不用改存储**。

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
| 高 | **慢 span 告警** | 某个工具或某轮模型调用超过阈值时明确提示，比事后翻文件管用 | 两小时 |
| 中 | **把用量统计做成子命令** | 现在用 `scripts/usage.py`（按会话、按轮次聚合）。常用的话可以提升成 `agent usage`，省得记脚本路径 | 十分钟 |
| 低 | **成本换算** | 各家报价不同且一直在变，写死单价容易误导——账单的真相在供应商后台。要做也只做"可选单价"，别当默认 | 半天 |
| 中 | **trace 可视化** | 写个几十行的脚本把 JSONL 转成瀑布图（HTML），排查多步工具链时非常直观 | 半天 |
| 低 | **日志轮转与保留策略** | 长期运行的必需品 | 两小时 |
| 低 | **接 OpenTelemetry / Langfuse** | 需要与现有观测平台打通时再做。模板阶段保持零依赖更合适 | 一天 |

用量统计已经能用了：`uv run python scripts/usage.py`（见"怎么扩展"一节）。
下一步建议做**慢 span 告警**——它能在出问题的当下提示，而不是事后翻文件。
