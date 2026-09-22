"""provider-neutral conversation primitives

Everything above this module - tools, skills, MCP, RAG, the agent loop - speaks
only these types. Adding a new model provider means adding one adapter file,
not touching the agent.
"""

from __future__ import annotations 

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]



class ToolCall(BaseModel):
    id: str
    name: str
    # providers hand us a JSON string; we parse it here so that no downstream
    # code has to deal with json. loads or half-broken payloads
    # 模型服务商传给我们的是一段 JSON 字符串；我们在这里完成解析，
    # 这样下游代码就不用处理 `json.loads`，也不用面对残缺、非法的载荷。
    # {
    #   "id": "call_001",
    #   "type": "function",
    #   "function": {
    #     "name": "read_file",
    #     "arguments": "{\"path\":\"README.md\"}"
    #   }
    # }
    arguments: dict[str, Any] = Field(default_factory=dict)
    parse_error: str | None = None




class Message(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None

    @classmethod # 工厂方法 设定具体场景用于高频创建固定类型的消息
    def system(cls, content: str) -> "Message":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role="user", content=content)

    @classmethod
    def assistant(
        cls, content: str | None = None, tool_calls: list[ToolCall] | None = None
    ) -> "Message":
        return cls(role="assistant", content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool_result(
        cls, tool_call_id: str, content: str, name: str | None = None
    ) -> "Message":
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)



class ToolSpec(BaseModel):
    """One callable capability, described for the model as JSON Schema"""
    # 一个可调用的能力（工具），使用 JSON Schema 描述给大模型
    # {
    #   "type": "function",
    #   "function": {
    #     "name": "read_file",
    #     "description": "读取本地文本文件",
    #     "parameters": {
    #       "type": "object",
    #       "properties": {
    #         "path": {"type": "string", "description": "文件路径"}
    #       },
    #       "required": ["path"]
    #     }
    #   }
    # }


    name: str
    description: str
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function":{
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            },
        }


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0



class ChatResponse(BaseModel):
    # {
    #   "id": "chatcmpl-yyyy",
    #   "object": "chat.completion",
    #   "created": 1760000001,
    #   "model": "gpt-4o",
    #   "system_fingerprint": "fp_xyz",
    #   "choices": [
    #     {
    #       "index": 0,
    #       "message": {
    #         "role": "assistant",
    #         "content": null,
    #         "tool_calls": [
    #           {
    #             "id": "call_001",
    #             "type": "function",
    #             "function": {
    #               "name": "read_file",
    #               "arguments": "{\"path\":\"README.md\"}"
    #             }
    #           }
    #         ]
    #       },
    #       "logprobs": null,
    #       "finish_reason": "tool_calls"
    #     }
    #   ],
    #   "usage": {
    #     "prompt_tokens": 45,
    #     "completion_tokens": 22,
    #     "total_tokens": 67
    #   }
    # }

    message: Message
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None # 保存从 OpenAI 拿到的原始完整响应字典，原样存下来，专门用于调试、日志、故障排查，业务主循环逻辑一般不读取 raw





class StreamChunk(BaseModel):
    """One increment of a streaming answer.

    kind="text"      -> append `text` to the visible answer
    kind="reasoning" -> append `text` to the model's thinking stream
    kind="done"      -> terminal chunk, carries the assembled Message and usage
    """
    # 流式回答的一段增量分片。**
    # kind="text"：将文本追加到对用户展示的回答内容中**
    # kind="reasoning"：将文本追加到模型的思考流中**
    # kind="done"：结束分片，携带组装完成的消息对象与 Token 用量**

    kind: Literal["text", "reasoning", "done"]
    text: str = ""
    message: Message | None = None
    usage: Usage | None = None


class LLMError(RuntimeError):
    def __init__(
            self, message: str, *, status_code: int | None = None, body: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class LLMClient(ABC): #`ABC` 是 Python 内置的**用来做抽象类的基类**。
    """The single seam every model provider implements"""
    # 所有模型服务商都要实现的统一接口层

    model: str


    @abstractmethod # 被 `@abstractmethod` 装饰的方法，**子类必须重写实现**，否则子类实例化的时候直接报错。
    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """Run one non-streaming turn"""
        # 执行一轮非流式对话



    @abstractmethod
    def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]: # AsyncIterator 异步迭代器
        """Run one streaming turn: text deltas first, then a terminal chunk."""
        # 执行一轮流式对话：先返回文本增量分片，最后返回结束分片


    async def aclose(self) -> None:
        """Release provider resources (HTTP clients, subprocesses)."""
        # 释放服务商相关资源（HTTP 连接、子进程等）