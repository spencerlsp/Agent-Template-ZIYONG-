"""把 RAG 检索包装成模型可调用的工具。"""

from __future__ import annotations

from agent_template.tools import ToolRegistry
from agent_template.rag.pipeline import RagPipeline

def register_rag_tools(registry: ToolRegistry, pipeline: RagPipeline) -> ToolRegistry:
    """注册 search_knowledge_base。

    这个 handler 是 async 的——登记表会识别协程函数并 await 它，
    所以不需要为了适配而改成同步版。
    """

    async def search_knowledge_base(query: str, top_k: int = 4) -> str:
        """在本地知识库里检索相关片段。回答关于已索引文档的问题前先调用它，并在答案里标注来源文件名。"""
        chunks = await pipeline.search(query, top_k)
        return pipeline.format_context(chunks)

    registry.register(search_knowledge_base)
    return registry