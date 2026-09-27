"""MCP 的错误类型。

为什么单独一个模块：client.py 需要 diagnostics 帮忙把异常翻译成人话，
而 diagnostics 又必须认识异常类型才能分类——如果类型定义在 client.py 里，
两者就互相 import，成了循环依赖。把类型抽到最底层，这个环就解开了。

`MCPTimeoutError` 单独一个类型是刻意的：超时的排查动作（看它是不是卡住了、
把超时调大）和"命令不存在"完全不同，而上层**只能靠类型区分**——
这就是 MCPConnectResult 留了 cause 字段的原因。
"""

from __future__ import annotations


class MCPError(RuntimeError):
    """MCP 环节的失败：连不上、握手超时、调用超时。"""


class MCPTimeoutError(MCPError):
    """启动握手超时。

    为什么要和普通的 MCPError 分开：`asyncio.wait_for` 超时时，SDK 内部的
    anyio 取消常常表现为 CancelledError 而不是干净的 TimeoutError，
    所以抛出处会自己判断"等了多久"并抛这个类型——比让上层去猜异常类型可靠。
    """
