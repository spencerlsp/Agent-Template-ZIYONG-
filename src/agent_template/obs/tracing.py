"""可观测性：链路追踪与 token 计量。

为什么需要这一层：
    agent 出问题时，"模型答得不对""工具没被调用""检索没召回相关片段"是三种
    完全不同的病，但表面症状都是"答案不对"。有了记录，你才能顺着一次运行看到：
    哪一轮调了哪个工具、各花了多久、失败在哪一步、token 烧在了什么地方。

两个设计取舍：
    * 落地格式用 JSONL（每行一个 JSON）。追加写、中断不损坏、能直接 grep，
      也能直接喂给任何日志系统。比起一上来就接 OpenTelemetry，这层更容易看懂，
      替换成本也更低——对外接口只有一个 `span()` 上下文管理器。
    * span 用**同步**的上下文管理器，不做 async 版本：它只记录时间和状态，
      期间不 await 任何东西，同步写法在同步/异步代码里都能用。

一个刻意的克制：**日志里不写正文**（不记 prompt 全文、不记模型回答），
只记元信息。日志中混入用户内容，在产品环境里就是隐私问题。

span 的字段分两部分：
    * **信封**：name / trace_id / span_id / started_at / ts / duration_ms / status /
      error / attributes —— 每一行都一样，所以按行 grep、按字段聚合都不需要特判；
    * **载荷**：`attributes` 里放这次操作特有的东西（`llm.turn` 有 step 和 token，
      `tool.call` 有工具名）。

像 session / run 这种"每个 span 都该有"的字段走 `set_context()` 注入，不在调用点
手写——手写的地方迟早会漏，而漏了要等聚合时才发现数据不全。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("agent.trace")


@dataclass
class TraceSpan:
    """一次操作记录"""
    name: str
    # 同一次运行共享一个trace_id, 便于把一次对话的所用span穿起来
    trace_id: str = ""
    # 每个span自己的短标识
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)
    # 人看的那个时间。started_at 是 epoch 浮点（1790564685.7482283），
    # 排序和计算好用，grep 和肉眼不友好，所以两个都留着。
    ts: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    duration_ms: float = 0.0
    # ok 或 error
    status: str = "ok"
    error: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


class Tracer:
    """收集一次运行中的所有span, 可选的追加写入JSONL文件。"""
    def __init__(
        self,
        path: Path | None = None,
        trace_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex[:12]
        self.path = path
        # 内存里留一份，方便测试直接断言：文件那份是给人看的
        self.spans: list[TraceSpan] = []
        # 每个 span 都该带的字段（session / run …）。放在这里而不是让每个
        # 调用点自己写：漏一个地方，聚合时那份数据就是残的。
        self.context: dict[str, Any] = dict(context or {})

    def set_context(self, **fields: Any) -> None:
        """更新公共上下文，同名键覆盖。

        知道当前的会话和"这一轮问答"，span 才能被事后聚合出"哪个会话、哪一步贵"。

        注意这是**进程内共享**的状态，前提是同一时刻只跑一轮对话（CLI 的用法）。
        将来要并发跑多个会话，得换成 `contextvars` 或者把上下文显式传给 `span()`。
        """
        self.context.update(fields)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[TraceSpan]:
        """记录一段操作。

        用法：
            with tracer.span("tool.call", tool="read_file"):
                ...

        异常照常向外抛——这里只做记录，不吞异常。吞掉的话，调用方
        就再也拿不到失败信号了，那是比没有日志更糟的事。
        """
        # 公共上下文在前、本次调用的属性在后：同名时以本次调用为准，
        # 这样 `span("tool.call", tool=...)` 里的 tool 不会被上下文里的键盖掉
        span = TraceSpan(
            name=name, trace_id=self.trace_id, attributes={**self.context, **attributes}
        )
        started = time.perf_counter()
        try:
            yield span
        except Exception as exc: # noqa: BLE001 - 记录后原样抛出
            span.status = "error"
            span.error = f"{type(exc).__name__} : {exc}"
            raise
        finally:
            # 用 perf_counter 而不是 time.time：前者单调递增，
            # 不受系统时钟调整影响，测出来的耗时才是真的
            span.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self.spans.append(span)
            self._emit(span)

    def _emit(self, span: TraceSpan) -> None:
        """写一条到日志和文件。"""
        logger.debug(
            "span=%s status=%s duration_ms=%s %s",
            span.name,
            span.status,
            span.duration_ms,
            span.attributes,
        )
        if not self.path:
            return

        # 父目录可能还不存在（首次运行时的 .agent/）
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 每行一个 JSON：追加写、断电最多丢最后一行，不会让整个文件不可读
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(span), ensure_ascii=False) + "\n")

    def summary(self) -> str:
        """一行摘要，CLI 收尾时好用。"""
        if not self.spans:
            return "本次运行没有记录到任何 span"
        slowest = max(self.spans, key=lambda span: span.duration_ms)
        errors = sum(1 for span in self.spans if span.status == "error")
        return (
            f"{len(self.spans)} 个 span，最慢的是 {slowest.name}"
            f"（{slowest.duration_ms}ms），失败 {errors} 个"
        )

class TokenAccountant:
    """累计 token 用量，并可选地换算成钱。

    为什么要算 token：
        agent 的成本几乎全部来自 token，而且和"轮次 × 上下文长度"成正比。
        多轮对话里历史一发不可收拾，不看数字是不会意识到成本涨得有多快的。

    单价是可选的：不同供应商、不同模型的价差很大，写死反而误导。
    不填单价就只统计 token，接口保持一致。
    """

    def __init__(
        self,
        price_in_per_1m: float | None = None,
        price_out_per_1m: float | None = None,
    ) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.price_in_per_1m = price_in_per_1m
        self.price_out_per_1m = price_out_per_1m

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        """累加一次调用的用量。

        输入和输出要分开统计：它们的价格差好几倍，
        只记总量的话，成本估算会偏得离谱。
        """
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float | None:
        """按每百万 token 的单价换算；没配单价时返回 None。"""
        if self.price_in_per_1m is None or self.price_out_per_1m is None:
            return None
        return (
            self.prompt_tokens / 1_000_000 * self.price_in_per_1m
            + self.completion_tokens / 1_000_000 * self.price_out_per_1m
        )

    def summary(self) -> str:
        cost = self.cost_usd
        cost_part = f"，估算成本 ${cost:.6f}" if cost is not None else ""
        return (
            f"{self.calls} 次调用，"
            f"输入 {self.prompt_tokens} + 输出 {self.completion_tokens}"
            f" = {self.total_tokens} tokens{cost_part}"
        )
