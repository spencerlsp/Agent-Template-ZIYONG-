"""Agent 运行过程中对外吐出的事件。

为什么专门定义事件类型，而不是在循环里直接 print：
    同一套循环要同时服务 CLI、Web API 和单元测试。循环只负责声明"发生了什么事"，
    "怎么呈现"交给调用方——CLI 打印到终端，API 转成 SSE 推送，测试直接断言事件序列。
    如果循环里写死了 print，这三件事就得改三份代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# 一轮对话中可能出现的事件类型
EventKind = Literal[
    "started",  # 一轮对话开始
    "reasoning",  # 模型的思维链片段（DeepSeek 这类带思考的模型会有）
    "text",  # 最终回答的文本增量（流式）
    "tool_call",  # 模型请求调用某个工具
    "tool_result",  # 工具执行完成
    "usage",  # token 用量
    "error",  # 出错了
    "finished",  # 一轮对话结束
]


@dataclass(slots=True)
class AgentEvent:
    """一次事件。字段按需使用，不相关的一律留默认值。"""

    kind: EventKind
    # 该事件属于循环的第几步(从0开始), 便于定位卡在哪一轮
    step: int = 0
    # 文本增量：kind 为 text / reasoning 时有效
    text: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    tool_result: str = ""
    # 兜底字段：临时附加信息，避免为每个小需求都改这个类
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        """给 CLI 和日志用的一行摘要。"""
        if self.kind == "tool_call":
            return f"[tool] {self.tool_name}({self.tool_arguments})"
        if self.kind == "tool_result":
            return f"[tool] {self.tool_name} -> {len(self.tool_result)} 字符"
        if self.kind == "usage":
            return f"[usage] {self.data}"
        if self.kind in ("text", "reasoning"):
            return self.text
        return f"[{self.kind}] {self.data or self.text}"