"""混合检索的测试：BM25 排序、RRF 融合、端到端召回。"""

from __future__ import annotations

from pathlib import Path

from agent_template.rag.chunkers import Chunk
from agent_template.rag.embeddings import LocalHashEmbedder, tokenize
from agent_template.rag.retriever import BM25, HybridRetriever, reciprocal_rank_fusion
from agent_template.rag.store import VectorStore


def test_rrf_rewards_agreement() -> None:
    """两路都排第一的文档，分数必须高于只被一路看好的。"""
    fused = reciprocal_rank_fusion([[0, 1, 2], [0, 2, 1]], k=60)

    assert fused[0] > fused[1]
    assert fused[0] > fused[2]


def test_rrf_depends_only_on_rank() -> None:
    """分数只跟名次有关：换一套下标（相当于换一批语料）结论不变。"""
    left = reciprocal_rank_fusion([[0, 1]], k=60)
    right = reciprocal_rank_fusion([[5, 9]], k=60)

    assert left[0] == right[5]
    assert left[1] == right[9]


def test_bm25_ranks_matching_document_first() -> None:
    corpus = [
        tokenize("向量检索的原理"),
        tokenize("红烧肉的家常做法"),
        tokenize("混合检索怎么融合排名"),
    ]
    bm25 = BM25(corpus)

    scores = bm25.scores(tokenize("混合检索 融合"))

    assert int(scores.argmax()) == 2


def test_bm25_ignores_unknown_terms() -> None:
    """查询词完全不在语料里时，所有分数为 0（不该随机排序出候选）。"""
    bm25 = BM25([tokenize("向量检索"), tokenize("红烧肉")])

    scores = bm25.scores(tokenize("完全不存在的词汇"))

    assert float(scores.max()) == 0.0


async def test_hybrid_search_finds_relevant_chunk(tmp_path: Path) -> None:
    """端到端：真实存库 + 真实两路检索 + 融合。"""
    rows = [
        ("rag.md", 0, "RAG 先从知识库检索相关片段，再把这些片段作为上下文交给模型作答。"),
        ("rag.md", 1, "混合检索把向量召回和关键词召回的结果用 RRF 融合排名。"),
        ("mcp.md", 0, "MCP 是工具接入的标准化协议，客户端可以发现远端工具。"),
        ("cook.md", 0, "红烧肉要先用冰糖炒糖色，再用小火慢炖四十分钟。"),
    ]
    chunks = [
        Chunk(id=f"{source}#{index}", source=source, index=index, text=text, start=0)
        for source, index, text in rows
    ]

    embedder = LocalHashEmbedder(dim=256)
    vectors = await embedder.embed([chunk.text for chunk in chunks])

    store = VectorStore(tmp_path / "index.sqlite3")
    try:
        store.add(chunks, vectors)
        retriever = HybridRetriever(store, embedder, top_k=2, candidates=10)

        hits = await retriever.search("向量召回和关键词召回怎么融合")

        assert hits, "至少应该召回一些片段"
        assert hits[0].id == "rag.md#1"
        # 两路排名都要留下痕迹：否则"混合"就没有意义了
        assert retriever.last_debug["vector"]
        assert retriever.last_debug["keyword"]
    finally:
        store.close()


async def test_search_on_empty_index_returns_nothing(tmp_path: Path) -> None:
    """索引为空时返回空列表，不该抛异常。"""
    embedder = LocalHashEmbedder(dim=64)
    store = VectorStore(tmp_path / "empty.sqlite3")
    try:
        retriever = HybridRetriever(store, embedder)

        assert await retriever.search("任意问题") == []
    finally:
        store.close()
