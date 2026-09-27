"""MCP 失败提示的测试。

两类真实故障对应完全不同的排查动作，这里把它们固定住：
第一类靠一个不存在的命令，第二类靠一个启动后不说话、等握手的进程。

这两个用例**不需要网络、不需要真的 MCP server**，总共两三秒跑完——
失败路径最容易被忽略，但恰好最好测，所以更该测。
"""

from __future__ import annotations

from agent_template.config import MCPServerSettings
from agent_template.mcp import MCPManager
from agent_template.mcp.diagnostics import (
    explain_failure,
    manual_command,
    root_cause,
)
from agent_template.mcp.errors import MCPTimeoutError


async def test_missing_command_is_explained() -> None:
    """命令不存在时，提示要指向具体的配置项，而不是只丢一个异常名。"""
    manager = MCPManager(
        [MCPServerSettings(name="missing", command="definitely-not-exists-xyz")]
    )
    try:
        results = await manager.connect_all()
    finally:
        await manager.aclose()

    assert len(results) == 1
    result = results[0]
    assert result.ok is False
    assert "不存在" in (result.reason or "")
    # 建议要落到"改哪里"，否则用户还得自己去猜
    assert "AGENT_MCP_SERVERS" in (result.hint or "")


async def test_handshake_timeout_is_explained() -> None:
    """进程起来了但不做握手 → 超时，建议里要带可复现的命令。"""
    manager = MCPManager(
        [
            MCPServerSettings(
                name="silent",
                command="python",
                args=["-c", "import time; time.sleep(30)"],
                startup_timeout_s=2,
            )
        ]
    )
    try:
        results = await manager.connect_all()
    finally:
        await manager.aclose()

    result = results[0]
    assert result.ok is False
    assert "超时" in (result.reason or "")
    assert "python" in (result.hint or "")


def test_successful_server_has_no_reason() -> None:
    """成功的服务器不该带 reason/hint，避免调用方把成功也当成警告打印。"""
    from agent_template.mcp import MCPConnectResult

    result = MCPConnectResult(name="ok", ok=True, tools=["echo"])

    assert result.reason is None
    assert result.hint is None
    assert result.cause is None


def test_root_cause_unwraps_the_chain() -> None:
    """分类必须看根因：我们自己的 MCPError 只是包装。"""
    inner = FileNotFoundError("找不到命令")
    try:
        try:
            raise inner
        except FileNotFoundError as exc:
            raise RuntimeError("包装一层") from exc
    except RuntimeError as exc:
        assert root_cause(exc) is inner

    assert root_cause(None) is None
    assert root_cause(ValueError("裸异常")) is not None


def test_timeout_is_classified_by_our_own_type() -> None:
    """超时的分类要认最外层那个"已经判断好的结论"，而不是挖到最底层。

    真实情况是：MCPTimeoutError.__cause__ 是 TimeoutError，再往下是
    CancelledError（wait_for 取消时留下的）。如果分类只看最底层，
    就会把超时报告成"CancelledError: "——这个坑我们真踩过，
    所以这里把整条链子搭出来钉住它。
    """
    import asyncio

    deepest = asyncio.CancelledError()
    middle = TimeoutError()
    middle.__cause__ = deepest
    outer = MCPTimeoutError("启动超时（等待 4.0 秒）")
    outer.__cause__ = middle

    server = MCPServerSettings(name="s", command="python", args=["-c", "sleep(9)"])
    reason, hint = explain_failure(outer, server)

    assert reason == "启动超时（等待 4.0 秒）"
    assert "timeout_s" in hint


def test_manual_command_quotes_arguments_with_spaces() -> None:
    """提示里给的复现命令必须能直接复制粘贴运行。

    带空格/分号的参数不加引号，用户复制出来就是跑不通的——
    `python -c import time; time.sleep(30)` 就是这么失效的。
    """
    server = MCPServerSettings(
        name="s", command="python", args=["-c", "import time; time.sleep(30)"]
    )

    command = manual_command(server)

    assert '"import time; time.sleep(30)"' in command
