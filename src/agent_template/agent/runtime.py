"""运行时装配：把模型、工具、技能、MCP、记忆、追踪拼成一个能用的 agent。

为什么单独有这一层：
    "怎么组装"和"怎么循环"是两件事。装配要管资源生命周期（MCP 子进程、
    SQLite 连接、HTTP 客户端），循环只管消息流转。分开之后，测试可以只装
    一个假模型和空工具表来跑循环，不必启动 MCP。

一个重要的设计取舍：**外部能力不可用时不能让 agent 起不来**。
某个 MCP 服务器（比如提供知识库检索的那个）起不来，只跳过它、记一条失败记录，
对话照常进行。首次 clone 下来的用户什么都还没配，也应该能立刻跑起来。

**这里不再有 RAG 了。** 检索能力现在是一个外部服务（ragkit），通过 MCP 接进来——
它和任何别的 MCP 服务器走同一条路：连接、发现工具、并入工具表。
"""

from __future__ import annotations

import logging

from agent_template.agent.loop import AgentLoop
from agent_template.config import Settings
from agent_template.llm.base import LLMClient
from agent_template.llm.factory import build_llm
from agent_template.mcp.bridge import register_mcp_tools
from agent_template.mcp.client import MCPManager, MCPConnectResult
from agent_template.memory.store import MemoryStore
from agent_template.obs.tracing import Tracer
from agent_template.skills.loader import SkillsIndex
from agent_template.skills.tools import register_skill_tools
from agent_template.tools.builtin import register_builtin_tools
from agent_template.tools.registry import ToolRegistry
from agent_template.agent.approval import ApprovalHandler

logger = logging.getLogger("agent.runtime")

class AgentRuntime:
    """一个装配好的 agent。构造用 `await AgentRuntime.create(settings)`。"""

    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMClient,
        registry: ToolRegistry,
        memory: MemoryStore,
        skills: SkillsIndex,
        tracer: Tracer,
        mcp: MCPManager | None = None,
        mcp_failures: list[MCPConnectResult] | None = None,
        approve: ApprovalHandler | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.skills = skills
        self.tracer = tracer
        self.mcp = mcp
        self.mcp_failures: list[MCPConnectResult] = mcp_failures or []
        self.loop = AgentLoop(
            llm=llm,
            registry=registry,
            memory=memory,
            settings=settings,
            skills=skills,
            tracer=tracer,
            approve=approve,
        )

    # ------------------------------------------------------------------ 装配
    @classmethod
    async def create(
        cls,
        settings: Settings | None = None,
        *,
        connect_mcp: bool = True,
        approve: ApprovalHandler | None = None,
    ) -> "AgentRuntime":
        """按配置装配一个完整的agent"""
        settings = settings or Settings()
        llm = build_llm(settings)
        registry = ToolRegistry()

        # ---- 本地工具 ----
        register_builtin_tools(registry, settings)

        # ---- 技能，只把目录塞进提示，正文等模型按需读取 ----
        skills = SkillsIndex.from_dir(settings.resolve(settings.skills_dir))
        register_skill_tools(registry, skills)
        logger.info("已加载 %d 个技能：%s", len(skills), skills.names())

        # ---- MCP：远端工具并入同一张表；连不上的要留下失败记录 ----
        # 知识库检索也走这条路：ragkit 是一个 MCP 服务器，
        # 它连不上时下面只记一条失败记录，对话照常。
        # mcp 必须先声明：下面那个 if 不成立时（没配 server，或调用方显式
        # connect_mcp=False），构造 runtime 时仍然会用到这个变量。
        mcp: MCPManager | None = None
        mcp_failures: list[MCPConnectResult] = []
        if connect_mcp and settings.mcp_servers:
            mcp = MCPManager(settings.mcp_servers)
            results = await mcp.connect_all()
            connected = [r for r in results if r.ok]
            mcp_failures = [r for r in results if not r.ok]
            if connected:
                await register_mcp_tools(registry, mcp)
                logger.info("MCP 已连接：%s", [r.name for r in connected])


        runtime = cls(
            settings=settings,
            llm=llm,
            registry=registry,
            memory=MemoryStore(settings.memory_path),
            skills=skills,
            tracer=Tracer(settings.trace_path),
            mcp=mcp,
            mcp_failures=mcp_failures,
            approve=approve,
        )
        logger.info("工具表共 %d 个工具", len(runtime.registry))
        return runtime

    # ------------------------------------------------------------------ 使用
    def ask(self, user_input: str, session_id: str = "default", *, stream: bool | None = None):
        """跑一轮对话，返回事件流。"""
        return self.loop.run(session_id, user_input, stream=stream)

    def describe_tools(self) -> str:
        return self.registry.describe()

    @property
    def stats(self) -> str:
        return self.loop.stats      

    
    # ------------------------------------------------------------------ 收尾
    async def aclose(self) -> None:
        """释放所有资源。顺序反过来：先关有子进程的，再关连接。"""
        if self.mcp is not None:
            await self.mcp.aclose()
        await self.llm.aclose()
        self.memory.close()
