"""建立/重建知识库索引。

单独抽成一个模块，是为了让 CLI（`agent index`）和任何脚本共用同一份逻辑。
以前这段代码写在 scripts/rag_index.py 里，那意味着"想建索引就得跑脚本"，
而现在脚本已经退役，索引成了 CLI 的一个子命令。

每次运行都是**整库重建**（不做增量）。以当前的数据规模，重建只要几十毫秒，
比维护增量逻辑划算得多；docs/ROADMAP.md 里记了什么时候该换成增量。
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent_template.config import Settings
from agent_template.rag.chunkers import chunk_document
from agent_template.rag.embeddings import build_embedder
from agent_template.rag.loaders import iter_documents
from agent_template.rag.store import VectorStore


def content_hash(text: str) -> str:
    """文档内容哈希。将来做增量索引时，用它判断这份文件变没变。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class IndexReport:
    """一次建库的结果，交给调用方展示。"""

    documents: int
    chunks: int
    dim: int
    embedder: str
    index_path: Path
    elapsed_s: float
    # 每份文档切出多少片段，用来判断切块参数是否合理
    per_source: dict[str, int] = field(default_factory=dict)
    # 首个片段的开头，用来肉眼检查切块质量
    sample: str = ""


async def build_index(settings: Settings) -> IndexReport:
    """读文档 → 切块 → 向量化 → 写库。

    知识库为空时抛 FileNotFoundError——这是"用法问题"而不是"程序出错"，
    由调用方决定是提示用户还是当成失败。
    """
    started = time.perf_counter()
    knowledge_dir = settings.resolve(settings.knowledge_dir)

    documents = list(iter_documents(knowledge_dir))
    if not documents:
        raise FileNotFoundError(
            f"知识库是空的：{knowledge_dir}\n"
            "往这个目录里放几个 .md / .txt 文件，再重新执行。"
        )

    # 必须一份一份文档地切块：片段序号（index）是"文档内序号"，
    # 如果先把所有文档拼成一个大字符串再切，序号就失去意义了
    chunks = []
    hashes: dict[str, str] = {}
    for document in documents:
        chunks.extend(
            chunk_document(
                document.text,
                source=document.source,
                size=settings.chunk_size,
                overlap=settings.chunk_overlap,
            )
        )
        hashes[document.source] = content_hash(document.text)

    embedder = build_embedder(settings)
    try:
        vectors = await embedder.embed([chunk.text for chunk in chunks])
    finally:
        # 远端 embedder 持有 HTTP 连接，必须释放
        await embedder.aclose()

    store = VectorStore(settings.index_path)
    try:
        store.reset()
        store.add(chunks, vectors)
        # 记下用的是哪个 embedder、多少维，检索时才能校验一致性
        store.set_meta("embedder", embedder.name)
        store.set_meta("dim", str(vectors.shape[1]))

        now = datetime.now().isoformat(timespec="seconds")
        per_source = Counter(chunk.source for chunk in chunks)
        for source, digest in hashes.items():
            store.record_document(
                source=source,
                content_hash=digest,
                chunks=per_source[source],
                indexed_at=now,
            )
    finally:
        store.close()

    return IndexReport(
        documents=len(documents),
        chunks=len(chunks),
        dim=int(vectors.shape[1]),
        embedder=embedder.name,
        index_path=settings.index_path,
        elapsed_s=round(time.perf_counter() - started, 2),
        per_source=dict(per_source),
        sample=chunks[0].text[:160],
    )
