"""工具登记表的测试：schema 生成、参数校验、错误转文本、超时。"""

from __future__ import annotations

import time

from agent_template.tools.registry import ToolRegistry


def is_error(text: str) -> bool:
    return text.startswith(("Error", "错误"))


async def test_schema_is_generated_from_signature() -> None:
    """参数类型和必填项都从函数签名推导，不需要手写 schema。"""
    registry = ToolRegistry()

    @registry.register
    def add(a: int, b: int = 2) -> int:
        """两数相加。"""
        return a + b

    spec = registry.specs()[0]

    assert spec.name == "add"
    assert spec.description == "两数相加。"
    assert spec.parameters["properties"]["a"]["type"] == "integer"
    # b 有默认值，不该出现在必填列表里
    assert spec.parameters["required"] == ["a"]


async def test_call_passes_validated_arguments() -> None:
    registry = ToolRegistry()

    @registry.register
    def add(a: int, b: int = 2) -> int:
        """两数相加。"""
        return a + b

    assert await registry.call("add", {"a": 1, "b": 3}) == "4"
    assert await registry.call("add", {"a": 5}) == "7"  # 用默认值


async def test_invalid_arguments_become_error_text() -> None:
    """模型的参数不可信：类型不对必须被挡住，且不能抛异常。"""
    registry = ToolRegistry()

    @registry.register
    def add(a: int) -> int:
        """两数相加。"""
        return a

    result = await registry.call("add", {"a": "不是数字"})

    assert is_error(result)


async def test_unknown_tool_lists_available_tools() -> None:
    """报错要能帮模型自我纠正，所以得把可用工具列出来。"""
    registry = ToolRegistry()

    @registry.register
    def known() -> str:
        """一个已知工具。"""
        return "ok"

    result = await registry.call("missing", {})

    assert is_error(result)
    assert "known" in result


async def test_handler_exception_becomes_error_text() -> None:
    """工具内部抛异常由登记表兜住，循环不该被打断。"""
    registry = ToolRegistry()

    @registry.register
    def boom() -> str:
        """总是失败。"""
        raise RuntimeError("内部炸了")

    result = await registry.call("boom", {})

    assert is_error(result)
    assert "内部炸了" in result


async def test_async_handler_is_supported() -> None:
    registry = ToolRegistry()

    @registry.register
    async def async_add(a: int, b: int) -> int:
        """异步相加。"""
        return a + b

    assert await registry.call("async_add", {"a": 2, "b": 3}) == "5"


async def test_non_string_result_is_serialised() -> None:
    registry = ToolRegistry()

    @registry.register
    def stats() -> dict:
        """返回结构化结果。"""
        return {"count": 3}

    assert await registry.call("stats", {}) == '{"count": 3}'


async def test_slow_handler_times_out() -> None:
    """同步工具跑在线程里，所以超时真的能生效。"""
    registry = ToolRegistry(timeout_s=0.05)

    @registry.register
    def slow() -> str:
        """故意很慢。"""
        time.sleep(0.5)
        return "done"

    assert is_error(await registry.call("slow", {}))


async def test_add_described_accepts_arbitrary_arguments() -> None:
    """给 MCP 用的口子：schema 现成，参数按原样收进来。"""
    registry = ToolRegistry()
    received: dict = {}

    async def handler(**kwargs) -> str:
        received.update(kwargs)
        return "ok"

    registry.add_described(
        name="remote",
        description="远端工具",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        handler=handler,
        source="mcp:demo",
    )

    await registry.call("remote", {"x": "1", "y": 2})

    assert received == {"x": "1", "y": 2}
    assert registry.entries()[0].source == "mcp:demo"


async def test_duplicate_registration_overwrites() -> None:
    """同名重复注册以最后一次为准（bridge 层负责先做重名检查）。"""
    registry = ToolRegistry()

    @registry.register(name="dup")
    def first() -> str:
        """第一个。"""
        return "first"

    @registry.register(name="dup")
    def second() -> str:
        """第二个。"""
        return "second"

    assert len(registry) == 1
    assert await registry.call("dup", {}) == "second"
