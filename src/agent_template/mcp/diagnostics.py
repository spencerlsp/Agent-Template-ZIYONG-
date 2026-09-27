"""把 MCP 连接失败翻译成人话。

为什么单独一个模块：
    连接失败的原因（命令不存在 / 握手超时 / 服务器自己崩了）对应完全不同的
    排查动作。这一层只做"异常 → 人能看懂的原因 + 可执行的下一步"，
    和连接逻辑分开，既好读也好单独测。

判断标准：**只说"连不上"等于把排查成本推给用户。** 合格的提示要回答两件事——
"发生了什么"和"下一步做什么"，最好再给出可以照抄复现的命令。
"""

from __future__ import annotations

from agent_template.config import MCPServerSettings
from agent_template.mcp.errors import MCPTimeoutError


def root_cause(exc: BaseException | None) -> BaseException | None:
    """顺着 __cause__ 链条挖到最底层的原因。

    我们自己抛的 MCPError 只是一层包装，真正的信息在它包住的异常里
    （FileNotFoundError / TimeoutError / ...）。分类必须看根因，
    否则只能看到一个笼统的"连接失败"，没法给出针对性的建议。

    这也是 client.py 里那句 `raise ... from exc` 不能省的原因——
    没有它，异常链在这里就断了。
    """
    depth = 0
    while exc is not None and exc.__cause__ is not None:
        exc = exc.__cause__
        depth += 1
        if depth > 10:  # 防御：异常链理论上不成环，但别让坏数据转成死循环
            break
    return exc


def manual_command(server: MCPServerSettings) -> str:
    """拼出"手动跑一次"的命令。

    让用户自己跑一遍，错误就原样打在屏幕上了——比任何解释都管用。

    带空格或 shell 特殊字符的参数必须加引号，否则提示里给的命令
    复制出来是跑不通的（`python -c import time; time.sleep(30)` 就是这么失效的）。
    """

    def quote(part: str) -> str:
        if any(char in part for char in ' ;&|()"\''):
            return '"' + part.replace('"', '\\"') + '"'
        return part

    return " ".join(quote(part) for part in [server.command, *server.args])


def exception_chain(exc: BaseException | None) -> list[BaseException]:
    """把异常链摊平成一个列表：从最外层到最里层。

    为什么不直接挖到最底层：我们抛的 `MCPTimeoutError` 本身就是"已经判断过的
    结论"，而它下面还挂着 TimeoutError → CancelledError。只认最底层会把最
    有信息量的那一层丢掉，超时就被误判成"未知错误"——这个坑我们真踩过。
    """
    chain: list[BaseException] = []
    while exc is not None and len(chain) < 10:
        chain.append(exc)
        exc = exc.__cause__
    return chain


def explain_failure(
    exc: BaseException | None, server: MCPServerSettings
) -> tuple[str, str]:
    """把 MCP 连接异常翻译成 (原因, 排查建议)。

    两个返回值都是给人看的：原因说"发生了什么"，建议说"下一步做什么"。
    """
    chain = exception_chain(exc)
    manual = manual_command(server)

    # 从外往里找第一个认得的异常：越靠外层越"了解内情"，因为最外层通常是
    # 我们自己抛的、已经带上下文的那一层。
    timeout_hint = (
        f"手动运行一次看它是不是卡住了：`{manual}`；"
        "如果只是首次启动慢（比如要下载依赖），把该项的 startup_timeout_s 调大。"
    )
    for item in chain:
        # 这个类型是我们自己在 connect() 里判断超时后抛的，原因文案已经写好
        if isinstance(item, MCPTimeoutError):
            return str(item), timeout_hint

        if isinstance(item, FileNotFoundError):
            return (
                f"启动命令 `{server.command}` 不存在",
                f"确认 `{server.command}` 已经安装、并且能直接在终端里运行；"
                f"再检查 AGENT_MCP_SERVERS 里 `{server.name}` 那一项的 command 拼写。",
            )

        if isinstance(item, PermissionError):
            return (
                f"没有权限执行 `{server.command}`",
                "检查这个命令的执行权限，或确认它没有被安全软件拦截。",
            )

        if isinstance(item, TimeoutError):
            return (
                f"启动超时（超过 {server.startup_timeout_s:.0f} 秒未完成握手）",
                timeout_hint,
            )

    # 兜底：进程起来了但立刻退出、协议版本不兼容、服务器自己报错，
    # 都会落到这里。这类问题没法从异常类型精确判断，只能靠手动复现看输出。
    deepest = root_cause(exc)
    detail = (
        f"{type(deepest).__name__}: {deepest}" if deepest is not None else "未知错误"
    )
    return (
        detail,
        f"手动运行一次看它的报错输出：`{manual}`；"
        "再用 `uv run agent -v tools` 查看完整日志。",
    )
