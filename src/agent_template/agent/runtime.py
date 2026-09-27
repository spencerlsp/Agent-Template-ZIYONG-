"""运行时装配：把模型、工具、技能、MCP、RAG、记忆、追踪拼成一个能用的 agent。

为什么单独有这一层：
    "怎么组装"和"怎么循环"是两件事。装配要管资源生命周期（MCP 子进程、
    SQLite 连接、HTTP 客户端），循环只管消息流转。分开之后，测试可以只装
    一个假模型和空工具表来跑循环，不必启动 MCP 或 RAG。

一个重要的设计取舍：**RAG 没有索引时不能让 agent 起不来**。
首次 clone 下来的用户还没建索引，这时应该照常能对话，只是没有检索能力，
并在日志里说清楚原因。同理，某个 MCP 服务器起不来只跳过它，不影响其他部分。
"""

from __future__ import annotations

import logging

from agent_template.agent.loop import AgentLoop
from agent_template.config import Settings
from agent_template.llm.base import LLMClient
from agent_template.llm.factory import build_llm
from agent_template.mcp.bridge import register_mcp_tools
from agent_template.mcp.client import MCPManager
from agent_template.memory.store import MemoryStore
from agent_template.obs.tracing import Tracer
from agent_template.rag.pipeline import RagNotReady, RagPipeline
from agent_template.rag.tools import register_rag_tools
from agent_template.skills.loader import SkillsIndex
from agent_template.skills.tools import register_skill_tools
from agent_template.tools.builtin import register_builtin_tools
from agent_template.tools.registry import ToolRegistry

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
        rag: RagPipeline | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.skills = skills
        self.tracer = tracer
        self.mcp = mcp
        self.rag = rag
        self.loop = AgentLoop(
            llm=llm,
            registry=registry,
            memory=memory,
            settings=settings,
            skills=skills,
            tracer=tracer,
        )

    # ------------------------------------------------------------------ 装配
    @classmethod
    async def create(
        cls,
        settings: Settings | None = None,
        *,
        connect_mcp: bool = True,
        enable_rag: bool = True,
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

        # ---- RAG: 没建索引时降级 , 不阻断启动 ----
        rag: RagPipeline | None = None
        rag: RagPipeline | None = None
        if enable_rag:
            candidate = RagPipeline(settings)
            try:
                candidate.ensure_ready()
            except RagNotReady as exc:
                logger.warning("RAG 不可用，已跳过：%s", exc)
                await candidate.aclose()
            else:
                rag = candidate
                register_rag_tools(registry, rag)
                logger.info("RAG 已就绪，索引 %d 个片段", rag.store.count())

         # ---- MCP：远端工具并入同一张表 ----
        mcp: MCPManager | None = None
        if connect_mcp and settings.mcp_servers:
            mcp = MCPManager(settings.mcp_servers)
            connected = await mcp.connect_all()
            if connected:
                await register_mcp_tools(registry, mcp)
                logger.info("MCP 已连接：%s", connected)   

        runtime = cls(
            settings=settings,
            llm=llm,
            registry=registry,
            memory=MemoryStore(settings.memory_path),
            skills=skills,
            tracer=Tracer(settings.trace_path),
            mcp=mcp,
            rag=rag,
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
        if self.rag is not None:
            await self.rag.aclose()
        await self.llm.aclose()
        self.memory.close()