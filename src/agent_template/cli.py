"""命令行入口：`agent` 命令的全部实现。

设计原则（这些决定了它是个能用的工具，还是个玩具）：

    * **内容走 stdout，过程走 stderr。**
      这样 `agent ask "..." > answer.md` 得到的文件里只有回答，
      工具调用、用量、思考摘要都不会混进去。
    * **退出码有意义**：0 成功、1 运行错误、2 用法错误。自动化流程靠它判断成败。
    * **默认流式**，`--no-stream` 留给 CI 和日志采集。
    * **`--json`** 把事件流按 JSONL 打到 stdout，脚本可以直接消费。
    * **思考过程默认折叠成一行**，`--verbose` 才逐段展开，避免刷屏。
    * **每条路径都走 `runtime.aclose()`**，否则会留下 MCP 子进程。

这一层刻意不做业务：只负责解析参数、调 AgentRuntime、渲染事件。
以后要接 Web 前端，只需新增一个 `api/`（FastAPI + SSE）消费同一个事件流，
CLI 这边一行都不用改——因为两者读的是同一套 AgentEvent。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, AsyncIterator, Coroutine

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from agent_template import __version__
from agent_template.agent.events import AgentEvent
from agent_template.agent.runtime import AgentRuntime
from agent_template.config import Settings
from agent_template.llm.base import LLMError
from agent_template.memory.store import MemoryStore
from agent_template.rag.indexer import build_index
from agent_template.skills.loader import SkillsIndex

# --------------------------------------------------------------- 输出编码
# Windows 上把 stdout 重定向到文件时，Python 默认用**系统区域编码**
# （中文系统是 GBK），于是 `agent ask "..." > answer.md` 写出来的文件，
# 在 Git Bash、编辑器、前端里都按 UTF-8 读——结果全是乱码。
#
# 这里显式统一成 UTF-8：CLI 的输出是给别的程序读的，不能依赖运行环境的
# 区域设置。之前我们踩过一次，这类问题排查起来很费时间，所以在入口处堵死。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")

# 两条流分开：回答给 stdout，过程给 stderr
out = Console(file=sys.stdout, highlight=False)
err = Console(file=sys.stderr, highlight=False, style="dim")


def setup_logging(verbose: bool) -> None:
    """把日志接到 stderr。

    必须显式配置，否则日志会凭空消失：Python 的 root logger 默认级别是
    WARNING、而且没有任何 handler，于是代码里所有 logger.info / logger.debug
    都被静默丢掉——而排查问题时最需要的恰恰是那些信息。

    默认只放行 WARNING 以上（保持安静）；--verbose 打开 INFO，正好把
    "加载了几个技能""RAG 是否就绪""MCP 连上了谁"这类装配信息显示出来。

    日志一律走 stderr：它属于"过程"，不能污染 stdout 里的回答。
    """
    handler = RichHandler(
        console=err,
        show_path=False,
        markup=False,
        rich_tracebacks=False,
    )
    # 只留消息本身，时间戳由 RichHandler 自己加
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    # 先清空：重复添加 handler 会让同一条日志打印两次
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO if verbose else logging.WARNING)

app = typer.Typer(
    add_completion=False,
    # 不加子命令时进入对话，而不是打印帮助——这是最常用的入口
    no_args_is_help=False,
    help="本地开发 agent：对话、工具、技能、MCP、RAG 都在这里。",
)


# --------------------------------------------------------------------- 选项
@dataclass(slots=True)
class Options:
    """全局选项。子命令通过 ctx.obj 拿到它。"""

    session: str = "default"
    stream: bool = True
    verbose: bool = False
    as_json: bool = False
    model: str | None = None
    provider: str | None = None

    def build_settings(self) -> Settings:
        """把命令行参数叠加到配置上。

        优先级：命令行参数 > 环境变量 > .env > 代码默认值。
        pydantic-settings 里构造参数的优先级最高，所以只传**显式给出**的项；
        没给的一律走它原有的解析链，这样环境变量和 .env 依然生效。
        """
        overrides: dict[str, Any] = {}
        if self.model:
            overrides["llm_model"] = self.model
        if self.provider:
            overrides["llm_provider"] = self.provider
        return Settings(**overrides)


def _options(ctx: typer.Context) -> Options:
    """取全局选项。直接调用子命令函数（没经过 callback）时给一份默认值。"""
    if isinstance(ctx.obj, Options):
        return ctx.obj
    return Options()


# ------------------------------------------------------------------- 渲染层
class EventRenderer:
    """把事件流渲染到终端。

    两种模式：
      * 默认（pretty）：回答流式写 stdout；工具调用、用量、错误写 stderr
      * --json：每个事件一行 JSON 写 stdout，给脚本和 CI 消费
    """

    def __init__(self, options: Options) -> None:
        self.options = options
        self._reasoning_chars = 0
        self._answer_chars = 0

    async def render(self, events: AsyncIterator[AgentEvent]) -> str:
        """消费整个事件流，返回回答文本。"""
        answer = ""
        async for event in events:
            if self.options.as_json:
                out.print(json.dumps(dataclasses.asdict(event), ensure_ascii=False))
                if event.kind == "finished":
                    answer = event.text
                continue
            self._render_pretty(event)
            if event.kind == "finished":
                answer = event.text
        return answer

    def _render_pretty(self, event: AgentEvent) -> None:
        if event.kind == "started":
            err.print(
                f"· 会话 {event.data.get('session_id')}｜最多 {event.data.get('max_steps')} 步"
            )
        elif event.kind == "reasoning":
            # 思维链是模型的草稿，默认只累计字数；--verbose 才逐段打出来
            self._reasoning_chars += len(event.text)
            if self.options.verbose:
                err.print(event.text, end="")
        elif event.kind == "text":
            self._answer_chars += len(event.text)
            out.print(event.text, end="")
        elif event.kind == "tool_call":
            arguments = json.dumps(event.tool_arguments, ensure_ascii=False)
            err.print(f"\n→ {event.tool_name}({arguments})")
        elif event.kind == "tool_result":
            preview = event.tool_result.replace("\n", " ")
            suffix = "…" if len(preview) > 80 else ""
            err.print(f"← {preview[:80]}{suffix}")
        elif event.kind == "usage":
            err.print(f"· 用量 {event.data.get('total_tokens')} tokens")
        elif event.kind == "error":
            err.print(f"！{event.text}", style="bold red")
        elif event.kind == "finished" and self._reasoning_chars and not self.options.verbose:
            err.print(f"· 思考 {self._reasoning_chars} 字（--verbose 可展开）")


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """跑一个协程，统一处理 Ctrl+C。

    Ctrl+C 时退出码用 130（128 + SIGINT 的编号 2），这是 shell 惯例，
    脚本能据此区分"被用户打断"和"程序出错"。
    """
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        err.print("\n· 已中断")
        raise typer.Exit(130) from None


# ------------------------------------------------------------------- 对话流
async def _ask(options: Options, question: str) -> int:
    """单次问答。返回退出码。"""
    runtime = await AgentRuntime.create(options.build_settings())
    try:
        renderer = EventRenderer(options)
        await renderer.render(
            runtime.ask(question, session_id=options.session, stream=options.stream)
        )
        if not options.as_json:
            out.print()
            err.print(f"· {runtime.stats}")
        return 0
    except LLMError as exc:
        err.print(f"！模型调用失败：{exc}", style="bold red")
        return 1
    finally:
        # 无论成功、失败还是被 Ctrl+C，都要收掉 MCP 子进程和数据库连接
        await runtime.aclose()


async def _chat(options: Options) -> int:
    """交互式对话。返回退出码。"""
    runtime = await AgentRuntime.create(options.build_settings())
    try:
        renderer = EventRenderer(options)
        out.print(
            Panel.fit(
                f"会话 [bold]{options.session}[/bold]"
                f"｜{len(runtime.registry)} 个工具"
                f"｜RAG {'已就绪' if runtime.rag else '未启用'}\n"
                "输入 /exit 退出，/help 查看全部命令",
                border_style="dim",
            )
        )

        while True:
            try:
                line = Prompt.ask("[bold cyan]你[/bold cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                out.print()
                return 0

            if not line:
                continue
            if line.startswith("/"):
                if not _handle_slash_command(line, options, runtime):
                    return 0
                continue

            try:
                await renderer.render(
                    runtime.ask(line, session_id=options.session, stream=options.stream)
                )
            except LLMError as exc:
                err.print(f"！模型调用失败：{exc}", style="bold red")
            out.print()
            err.print(f"· 本次会话累计：{runtime.stats}")
    finally:
        await runtime.aclose()


def _handle_slash_command(line: str, options: Options, runtime: AgentRuntime) -> bool:
    """处理 / 开头的本地命令。返回 False 表示要退出对话。"""
    command = line[1:].strip().lower()

    if command in {"exit", "quit", "q"}:
        return False
    if command == "help":
        out.print(
            "/exit    退出\n"
            "/tools   列出工具\n"
            "/skills  列出技能\n"
            "/stats   查看 token 统计\n"
            "/clear   清空当前会话的历史"
        )
    elif command == "tools":
        out.print(runtime.describe_tools())
    elif command == "skills":
        out.print(runtime.skills.catalog())
    elif command == "stats":
        out.print(runtime.stats)
    elif command == "clear":
        removed = runtime.memory.clear(options.session)
        out.print(f"已清空 {removed} 条历史（会话 {options.session}）")
    else:
        err.print(f"！未知命令：{line}（输入 /help 查看可用命令）")
    return True


# --------------------------------------------------------------------- 命令
@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    session: str = typer.Option("default", "--session", "-s", help="会话名，用于隔离对话历史"),
    no_stream: bool = typer.Option(False, "--no-stream", help="关闭流式输出（CI、日志采集用）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="展开模型的思考过程"),
    as_json: bool = typer.Option(False, "--json", help="事件流以 JSONL 输出到 stdout"),
    model: str | None = typer.Option(None, "--model", help="覆盖 AGENT_LLM_MODEL"),
    provider: str | None = typer.Option(None, "--provider", help="覆盖 AGENT_LLM_PROVIDER"),
    version: bool = typer.Option(False, "--version", help="打印版本后退出"),
) -> None:
    """不带子命令时直接进入交互式对话。"""
    if version:
        out.print(f"agent-template {__version__}")
        raise typer.Exit()

    setup_logging(verbose)

    ctx.obj = Options(
        session=session,
        stream=not no_stream,
        verbose=verbose,
        as_json=as_json,
        model=model,
        provider=provider,
    )

    # Typer/Click 的惯例：没有子命令时由 callback 决定做什么
    if ctx.invoked_subcommand is None:
        raise typer.Exit(_run(_chat(ctx.obj)))


@app.command()
def chat(ctx: typer.Context) -> None:
    """进入交互式对话。"""
    raise typer.Exit(_run(_chat(_options(ctx))))


@app.command()
def ask(
    ctx: typer.Context,
    question: list[str] = typer.Argument(..., help="要问的问题，不用加引号"),
) -> None:
    """问一个问题，打印回答后退出。"""
    raise typer.Exit(_run(_ask(_options(ctx), " ".join(question))))


@app.command()
def tools(ctx: typer.Context) -> None:
    """列出当前注册的全部工具（含 MCP 远端工具）。"""
    options = _options(ctx)

    async def collect() -> int:
        runtime = await AgentRuntime.create(options.build_settings())
        try:
            table = Table(show_header=True, header_style="bold")
            table.add_column("工具", style="cyan", no_wrap=True)
            table.add_column("来源", style="dim", no_wrap=True)
            table.add_column("说明", overflow="fold")
            for tool in runtime.registry.entries():
                table.add_row(tool.name, tool.source, tool.description)
            out.print(table)
            return 0
        finally:
            await runtime.aclose()

    raise typer.Exit(_run(collect()))


@app.command()
def skills(ctx: typer.Context) -> None:
    """列出可用技能（只列目录，正文由模型按需读取）。"""
    settings = _options(ctx).build_settings()
    index = SkillsIndex.from_dir(settings.resolve(settings.skills_dir))

    if not len(index):
        err.print(
            f"！没有找到任何技能，检查 AGENT_SKILLS_DIR（当前：{settings.resolve(settings.skills_dir)}）",
            style="bold red",
        )
        raise typer.Exit(1)

    table = Table(show_header=True, header_style="bold")
    table.add_column("技能", style="cyan", no_wrap=True)
    table.add_column("说明", overflow="fold")
    table.add_column("位置", style="dim", overflow="fold")
    for name in index.names():
        skill = index.get(name)
        if skill is None:
            continue
        table.add_row(name, skill.description, str(skill.path))
    out.print(table)


@app.command()
def index(ctx: typer.Context) -> None:
    """重建知识库索引（整库重建，随时可以重跑）。"""
    settings = _options(ctx).build_settings()

    try:
        report = _run(build_index(settings))
    except FileNotFoundError as exc:
        err.print(f"！{exc}", style="bold red")
        raise typer.Exit(1) from None
    except LLMError as exc:
        err.print(f"！embedding 调用失败：{exc}", style="bold red")
        raise typer.Exit(1) from None

    table = Table.grid(padding=(0, 2))
    table.add_row("文档数", str(report.documents))
    table.add_row("片段数", str(report.chunks))
    table.add_row("向量维度", str(report.dim))
    table.add_row("embedder", report.embedder)
    table.add_row("索引文件", str(report.index_path))
    table.add_row("耗时", f"{report.elapsed_s}s")
    out.print(table)

    err.print(f"· 每份文档的片段数：{report.per_source}")
    err.print("· 首个片段样例（用于检查切块质量）：")
    err.print("  " + report.sample[:160].replace("\n", "\n  "))


@app.command()
def sessions(
    ctx: typer.Context,
    clear: str | None = typer.Option(None, "--clear", help="清空指定会话的历史记录"),
) -> None:
    """查看会话列表，或清空某个会话。"""
    settings = _options(ctx).build_settings()
    store = MemoryStore(settings.memory_path)
    try:
        if clear:
            removed = store.clear(clear)
            out.print(f"已清空会话 {clear} 的 {removed} 条历史")
            return

        rows = store.sessions()
        if not rows:
            out.print("还没有任何会话记录")
            return

        table = Table(show_header=True, header_style="bold")
        table.add_column("会话", style="cyan")
        table.add_column("消息数", justify="right")
        table.add_column("最后活跃", style="dim")
        for session_id, updated_at in rows:
            table.add_row(session_id, str(store.count(session_id)), updated_at)
        out.print(table)
    finally:
        store.close()


if __name__ == "__main__":
    app()
