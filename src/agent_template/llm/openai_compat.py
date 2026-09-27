"""OpenAI-compatible adapter built on the official `openai` SDK.

Point `base_url` at any OpenAI-compatible endpoint - OpenAI itself, DeepSeek,
SiliconFlow, Ollama, a corporate gateway - and this client works unchanged.

The SDK gives us typed requests, streaming, retries with backoff, and timeouts.
This file's only job is translating between the SDK's objects and our own
models, so that nothing above this layer imports `openai` directly.
"""
# """**基于官方 `openai` SDK 实现的 OpenAI 兼容协议适配器。**
# 把 `base_url` 指向任意兼容 OpenAI 协议的接口地址：原生 OpenAI、DeepSeek、SiliconFlow、Ollama、企业内部网关，这个客户端无需修改代码即可直接使用。

# 官方 SDK 提供了带类型的请求、流式支持、带退避策略的重试、超时能力。
# 本文件的唯一职责：**在 SDK 自带对象和我们自定义领域模型之间做转换**，保证本层以上的所有代码，都不会直接导入 `openai` 包。"""


from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import NOT_GIVEN, APIConnectionError, APIStatusError,  AsyncOpenAI

from agent_template.llm.base import (
    ChatResponse,
    LLMClient,
    LLMError,
    Message,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
)


# Local servers (Ollama, vLLM) ignore the API key, but the SDK refuses to start
# without one, so fall back to a placeholder.
#本地服务（Ollama、vLLM）会忽略 API Key，但是 SDK 如果没有 API Key 就拒绝初始化，因此我们回退使用一个占位符。

_PLACEHOLDER_KEY = "not-needed"


class OpenAICompatiClient(LLMClient):
    def __init__(
            self,
            *,
            model: str,
            base_url: str,
            api_key: str| None = None,
            timeout_s: float = 60.0,
            max_retries: int = 2,
            default_temperature: float = 0.2,
            default_max_tokens: int = 1024,
            extra_headers: dict[str, str] | None = None,            
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens
        # 防线放在接收方：这个类可能被别处直接构造，只有在这里校验才兜得住所有调用路径。
        # 我们踩过的坑是把 SecretStr 对象整个传了进来——它会被 str() 成 '**********'，
        # 于是十个星号被当成密钥发出去，换来一个毫无头绪的 401。
        # 注意 None 是合法的：本地服务不需要 key，下面会退化成占位符。
        if api_key is not None and not isinstance(api_key, str):
            raise TypeError(
                "api_key 必须是普通字符串；如果它来自 SecretStr，"
                f"请先调用 .get_secret_value()（当前收到 {type(api_key).__name__}）"
            )
        self._client = AsyncOpenAI(
            api_key=api_key or _PLACEHOLDER_KEY,
            base_url=self.base_url,
            timeout=timeout_s,
            max_retries=max_retries,
            default_headers=extra_headers or None,
        )

# ------------------------------------------------------------------ wire
    def _to_wire_message(self, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content or "",
                    }
                )
            elif message.role == "assistant" and message.tool_calls:
                wire.append(
                    {
                        "role": "assistant",
                        # some provider reject null content alongside tool calls
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(
                                        call.arguments, ensure_ascii=False
                                    ),
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
            else:
                wire.append({"role": message.role, "content": message.content or ""})
        return wire


    @staticmethod
    def _parse_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
        if not raw:
            return {}, None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, f"invalid JSON arguments: {exc}"
        if not isinstance(parsed, dict):
            return {}, "arguments must be a JSON object"
        return parsed, None 

    @staticmethod
    def _usage(raw: Any) ->Usage:
        if raw is None:
            return Usage()

        return Usage(
            prompt_tokens=getattr(raw, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(raw, "completion_tokens", 0) or 0,
            total_tokens=getattr(raw, "total_tokens", 0) or 0,
        )       

    @staticmethod
    def _wrap(exc: Exception) -> LLMError:
        """Keep provider errors recognisable without leaking the SDK everywhere."""
        # 保留服务商错误信息，同时不让上层代码到处依赖 SDK 的异常类型
        if isinstance(exc, APIStatusError):
            body = None
            try:
                body = str(exc.response.text)[:2000]
            except Exception:
                body = None
            return LLMError(
                f"LLM request rejected ({exc.status_code}): {exc.message}",
                status_code=exc.status_code,
                body=body,
            )
        if isinstance(exc, APIConnectionError):
            return LLMError(f"could not reach the LLM endpoint: {exc}")
        return LLMError(str(exc))

# --------------------------------------------------------------- requests
    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResponse:
        try:
            completion = await self._client.chat.completions.create(
                model=self.model,
                messages=self._to_wire_message(messages), # type: ignore[arg-type]
                tools = [spec.to_wire() for spec in tools] if tools else NOT_GIVEN,
                temperature=(
                    self.default_temperature if temperature is None else temperature
                ),
                max_tokens=(
                    self.default_max_tokens if max_tokens is None else max_tokens
                ),
                response_format=response_format or NOT_GIVEN,
            )
        except (APIStatusError, APIConnectionError) as exc:
            raise self._wrap(exc) from exc

        choice = completion.choices[0]
        raw_message = choice.message
        tool_calls: list[ToolCall] = []
        for item in raw_message.tool_calls or []:
            arguments, error = self._parse_arguments(item.function.arguments)
            tool_calls.append(
                ToolCall(
                    id=item.id,
                    name=item.function.name,
                    arguments=arguments,
                    parse_error=error,
                )
            )

        message = Message(
            role="assistant",
            content=raw_message.content,
            tool_calls=tool_calls,
            # DeepSeek exposes the thinking stream as `reasoning_content`.
            reasoning=getattr(raw_message, "reasoning_content", None),
        )

        return ChatResponse(
            message=message,
            usage=self._usage(completion.usage),
            finish_reason=choice.finish_reason,
        )


    async def _create_stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        temperature: float | None,
        max_tokens: int | None,
        *,
        include_usage: bool,
    ) -> Any:
        return await self._client.chat.completions.create(
            model=self.model,
            messages=self._to_wire_message(messages),  # type: ignore[arg-type]
            tools=[spec.to_wire() for spec in tools] if tools else NOT_GIVEN,
            temperature=(
                self.default_temperature if temperature is None else temperature
            ),
            max_tokens=self.default_max_tokens if max_tokens is None else max_tokens,
            stream=True,
            stream_options={"include_usage": True} if include_usage else NOT_GIVEN,
            # - `include_usage=True` → 加入 `stream_options` 参数，要求末尾返回 usage
            # - `include_usage=False` → 传`NOT_GIVEN`，**请求体不带这个 key**，而不是传`None`
        )

    
    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        try:
            stream = await self._create_stream(
                messages, tools, temperature, max_tokens, include_usage=True
            )
        except APIStatusError as exc:
            if exc.status_code != 400:
                raise self._wrap(exc) from exc
            # A few gateways reject `stream_options`; retry without usage info.
            stream = await self._create_stream(
                messages, tools, temperature, max_tokens, include_usage=False
            )
        except APIConnectionError as exc:
            raise self._wrap(exc) from exc

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        # Providers stream the tool name first and the arguments in pieces,
        # so buffer by index and assemble once the stream is over.
        tool_buffer: dict[int, dict[str, Any]] = {}
        usage = Usage()

        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = self._usage(chunk.usage)
            for choice in chunk.choices:
                delta = choice.delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield StreamChunk(kind="reasoning", text=reasoning)
                if delta.content:
                    text_parts.append(delta.content)
                    yield StreamChunk(kind="text", text=delta.content)
                for item in delta.tool_calls or []:
                    index = item.index or 0
                    slot = tool_buffer.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if item.id:
                        slot["id"] = item.id
                    if item.function is not None:
                        if item.function.name:
                            slot["name"] = item.function.name
                        if item.function.arguments:
                            slot["arguments"] += item.function.arguments

        tool_calls: list[ToolCall] = []
        for index in sorted(tool_buffer):
            slot = tool_buffer[index]
            arguments, error = self._parse_arguments(slot["arguments"])
            tool_calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=arguments,
                    parse_error=error,
                )
            )

        yield StreamChunk(
            kind="done",
            message=Message(
                role="assistant",
                content="".join(text_parts) or None,
                tool_calls=tool_calls,
                reasoning="".join(reasoning_parts) or None,
            ),
            usage=usage,
        )

    async def aclose(self) -> None:
        await self._client.close()