"""RAG 流程编排：把索引侧和检索侧拼成一个对外可用的 API。

上层（工具层、Agent 循环）只用这个类，不需要知道切块、向量、BM25 的存在。
"""

from __future__ import annotations

import logging

from agent_template.config import Settings
from agent_template.rag.embeddings import Embedder, build_embedder
from agent_template.rag.store import ScoredChunk, VectorStore
from agent_template.rag.retriever import HybridRetriever


logger = logging.getLogger("agent.rag")


class RagNotReady(RuntimeError):
    """索引还没建好，或者和当前配置对不上。"""


class RagPipeline:
    """一次构造，反复检索。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.embedder: Embedder = build_embedder(settings)
        self.store = VectorStore(settings.index_path)
        self.retriever = HybridRetriever(
            store=self.store,
            embedder=self.embedder,
            top_k=settings.rag_top_k,
            candidates=settings.rag_candidates,
        )

    def ensure_ready(self) -> None:
        """启动前的自检，把"索引没建""embedder 换了"这类问题挡在前面。

        这两件事如果不提前检查，表现是"检索结果莫名其妙"——用户根本
        想不到是索引的问题，排查成本极高。
        """

        if self.store.count() == 0:
            raise RagNotReady(
                f"索引为空（{self.settings.index_path}）。"
                "先建索引：uv run python scripts/rag_index.py"
            )

        # 远端 embedder 在第一次调用前不知道自己的维度，这种情况只比对名字
        dim = self.embedder.dim if self.embedder.dim else None
        self.store.assert_compatible(self.embedder.name, dim)

    async def search(self, query: str, top_k: int | None = None) -> list[ScoredChunk]:
        """检索相关片段。"""
        return await self.retriever.search(query, top_k=top_k)
    
    @staticmethod
    def format_context(chunks: list[ScoredChunk]) -> str:
        """把检索结果拼成能直接塞进提示词的上下文。

        带序号和来源是刻意的：模型看到 `[1] 来源：what-is-rag.md` 才可能
        在答案里引用出处；没有来源标注，它会把这些片段和自己的记忆混在一起，
        你就无法判断答案到底有没有依据。
        """
        if not chunks:
            return "（没有检索到相关片段）"

        blocks = [
            f"[{order}] 来源：{chunk.source}（相关度 {chunk.score:.4f}）\n{chunk.text}"
            for order, chunk in enumerate(chunks, start=1)
        ]
        
        return "\n\n".join(blocks)

    async def aclose(self) -> None:
        self.store.close()
        await self.embedder.aclose()