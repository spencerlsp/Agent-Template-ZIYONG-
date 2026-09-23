"""MCP客户端， 链接stdio方式的MCP服务器， 列出并调用他们暴漏的工具
这一层对外只暴露三个动作——连接、列工具、调工具。上层（bridge 与 agent）
不需要知道 JSON-RPC、子进程、握手这些细节，协议部分交给官方 mcp SDK。

版本注意：mcp 2.x 的服务端基类叫 MCPServer（1.x 叫 FastMCP），
客户端高层入口是 mcp.Client。

"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from agent_template.config import MCPServerSettings

logger = logging.getLogger("agent.mcp")

class MCPError(RuntimeError):
    """MCP 环节的失败：连不上、握手超时、调用超时。"""


@dataclass(slots=True)
class MCPTool:
    """远端工具的描述，字段对齐 MCP 的 tools/list 响应。"""

    server: str
    name: str
    description: str
    parameters: dict[str, Any]


class MCPClient:
    """管理到单个 MCP 服务器的常驻连接。
    采用stdio模式：拉起MCP Server作为本地子进程，通过stdin/stdout管道通信。
    连接会长期保持，支持多次调用工具，直到主动close。
    """
    def __init__(self, settings: MCPServerSettings) -> None:
        # 保存当前MCP服务的配置（启动命令、参数、超时、环境变量等）
        self.settings = settings
        # 缓存MCP服务端返回的工具列表
        self.tools: list[MCPTool] = []
        # MCP SDK客户端实例，None代表未连接
        self._client: Client | None = None
        # 异步资源栈：管理子进程、管道等资源生命周期
        # 会话需要跨多个函数调用常驻，不能直接用with语句（with块结束会自动关闭连接）
        self._stack: AsyncExitStack | None = None

    async def connect(self) -> None:
        """拉起子进程，建立管道，完成initialize握手，然后拉一次工具清单。
        执行流程：
        1. 组装MCP子进程启动参数
        2. 通过AsyncExitStack托管MCP Client上下文
        3. 启动MCP Server子进程，建立stdio管道，执行MCP initialize握手协议
        4. 握手成功后，拉取服务端工具列表并缓存
        5. 任何阶段异常，自动清理资源，防止子进程僵尸残留
        """
        # 构造stdio模式MCP服务启动参数对象，仅保存配置，不执行启动
        params = StdioServerParameters(
            command=self.settings.command,       # 启动MCP服务的可执行命令，如python / uvx / node
            args=list(self.settings.args),        # 命令附带的参数列表
            # 环境变量：继承主进程当前环境，再叠加服务自定义环境，同名key以settings.env为准
            # 目的：保证子进程能找到python解释器、正常import agent_template模块
            env={**os.environ, **self.settings.env},
        )
        # 创建异步资源管理栈，用来托管MCP Client这个异步上下文
        stack = AsyncExitStack()
        try:
            # asyncio.wait_for 限制【整个启动握手流程】的最大耗时
            # stack.enter_async_context：
            #   1. 进入Client的异步上下文，内部拉起MCP子进程、绑定stdio管道、发送initialize握手
            #   2. 将Client注册进资源栈，后续aclose时自动执行清理
            # Client第二个参数 read_timeout_seconds：连接成功后，单次调用工具等待响应的读超时
            client = await asyncio.wait_for(
                stack.enter_async_context(
                    Client(params, read_timeout_seconds=self.settings.call_timeout_s)
                ),
                # startup_timeout_s：启动阶段总超时（子进程拉起+管道就绪+initialize握手）
                timeout=self.settings.startup_timeout_s,
            )
            # 连接握手成功，保存资源栈与client实例，供后续call_tool / close使用
            self._stack = stack
            self._client = client
            # 拉工具清单也放在 try 里：这一步失败同样要收掉子进程
            # 调用list_tools接口，拉取服务端所有可用工具，存入self.tools缓存
            await self.list_tools()
        except Exception as exc:
            # 启动握手出现任何异常：超时、命令不存在、握手协议报错等
            # 立刻关闭资源栈，内部会杀死已拉起的子进程，清理管道，避免僵尸进程残留
            await stack.aclose()
            # 包装为自定义MCP异常，from exc保留原始异常栈，方便排查根因
            await stack.aclose()
            self._stack = None
            self._client = None
            raise MCPError(f"连接 MCP 服务器 `{self.settings.name}` 失败：{exc}") from exc



        # 打印日志，记录MCP服务名称与加载到的工具数量
        logger.info(
            "MCP 服务器 `%s` 已连接，暴露 %d 个工具",
            self.settings.name,
            len(self.tools),
        )

    async def list_tools(self) -> list[MCPTool]:
        """调用 tools/list，把响应整流成我们自己的 MCPTool 列表。"""
        if self._client is None:
            raise MCPError(f"MCP 服务器 `{self.settings.name}` 尚未连接")

        result = await self._client.list_tools()
        self.tools = [
            MCPTool(
                server=self.settings.name,
                name=tool.name,
                # description 允许为空， 兜成空串， 别把None传给下游
                description=tool.description or "",
                # 2.x 的字段名是 input_schema（1.x 是 inputSchema）
                parameters=tool.input_schema
                or {"type": "object", "properties": {}},
            )
            for tool in result.tools
        ]
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用 MCP 的 tools/call 接口，把返回的内容块拼接成一段文本返回。
        设计思路：
        - 工具内部业务报错（result.is_error=True）也返回文本，不抛出异常
        - 让大模型能读到错误信息，自行调整参数重试；如果直接抛异常会中断整个Agent循环
        """
        # 检查连接状态：如果client为空，代表MCP服务还未建立连接
        if self._client is None:
            return f"错误：MCP 服务器 `{self.settings.name}` 尚未连接"

        try:
            # 通过MCP客户端调用指定工具，传入工具名和参数
            result = await self._client.call_tool(name, arguments)
        except Exception as exc: # 捕获传输层异常：管道断开、调用超时、通信异常等
            logger.warning("MCP 调用 %s/%s 失败：%s", self.settings.name, name, exc)
            # 异常包装成字符串返回给模型，不向上抛出
            return f"错误：调用 MCP 工具 `{name}` 失败：{exc}"

        # 取出返回的内容块列表，为空则赋值空列表
        blocks = getattr(result, "content", None) or []
        # 遍历内容块，只提取type="text"类型的文本，用换行拼接
        text = "\n".join(
            block.text for block in blocks if getattr(block, "type", None) == "text"
        ).strip()

        # 判断MCP返回标记：is_error代表【工具业务执行失败】（不是通信异常）
        # 例如：参数非法、业务逻辑报错，这属于工具正常返回了错误信息，不是底层管道异常
        if getattr(result, "is_error", False):
            return f"错误：{text or '工具执行失败'}"

        # 如果提取到文本内容，直接返回文本
        if text:
            return text

        # 部分MCP工具不返回text块，直接返回结构化数据，转成json字符串返回
        structured = getattr(result, "structured_content", None)
        if structured:
            return json.dumps(structured, ensure_ascii=False)

        # 兜底：工具返回为空，返回提示字符串
        return "（工具没有返回任何内容）"


    async def aclose(self) -> None:
        """关闭会话与子进程；重复调用应当是安全的。"""
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self._client = None


class MCPManager:
    """把多个 MCPClient 收在一起，对上层提供统一入口。
    管理多台MCP服务，负责批量连接、汇总全部工具、路由工具调用、批量关闭连接
    """
    def __init__(self, servers: list[MCPServerSettings]) -> None:
        # 构建字典：key = MCP服务名称，value = MCPClient实例
        # 只实例化 enabled=True 启用的服务器，禁用的直接过滤掉
        self.clients: dict[str, MCPClient] = {
            server.name: MCPClient(server) for server in servers if server.enabled
        }

    async def connect_all(self) -> list[str]:
        """并发连接所有启用的服务器，返回连接成功的服务器名。
        设计目标：单个服务器起不来不能拖垮整个 agent：失败只记 warning 并跳过。
        """
        # 取出所有MCP服务名称列表，用来后面和执行结果一一对应
        names = list(self.clients)

        # asyncio.gather：并发执行多个异步任务
        # return_exceptions=True：任务报错不会直接抛出异常，而是把异常对象作为返回值放到结果列表
        outcomes = await asyncio.gather(
            *(client.connect() for client in self.clients.values()),
            return_exceptions=True,
        )

        connected: list[str] = []
        # 遍历【服务名】和【任务结果】，两两配对
        for name, outcome in zip(names, outcomes):
            # 如果outcome是异常对象，说明这个服务器connect失败
            if isinstance(outcome, BaseException):
                logger.warning("MCP 服务器 `%s` 连接失败，已跳过：%s", name, outcome)
            else:
                # 连接成功，加入成功列表返回给上层
                connected.append(name)
        return connected

    async def all_tools(self) -> list[MCPTool]:
        """汇总所有服务器暴露的工具。
        把每个MCPClient缓存的tools列表扁平合并，返回全部可用工具
        """
        return [tool for client in self.clients.values() for tool in client.tools]

    async def call(self, server: str, name: str, arguments: dict[str, Any]) -> str:
        """按服务器名路由调用；服务器不存在时返回错误文本。
        上层只需要指定目标MCP服务名称、工具名、参数，manager找到对应的client去执行call_tool
        """
        # 根据服务名查找对应的MCPClient实例
        client = self.clients.get(server)
        if client is None:
            return f"错误：没有名为 `{server}` 的 MCP 服务器"
        # 转发调用到对应client的call_tool方法
        return await client.call_tool(name, arguments)

    async def aclose(self) -> None:
        """并发关闭所有连接。
        并发调用每个client的aclose，统一清理所有MCP子进程、管道资源
        return_exceptions=True：某个服务关闭失败不影响其他服务关闭
        """
        await asyncio.gather(
            *(client.aclose() for client in self.clients.values()),
            return_exceptions=True,
        )