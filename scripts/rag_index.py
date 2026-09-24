"""建立/重建知识库索引。

    uv run python scripts/rag_index.py

流程：读文档 → 逐份切块 → 批量向量化 → 写库。
每次运行都是整库重建（不做增量），所以随时可以放心重跑。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter
from datetime import datetime

from agent_template.config import Settings
from agent_template.rag import (
    VectorStore,
    build_embedder,
    chunk_document,
    iter_documents,
)


def content_hash(text: str) -> str:
    """文档内容哈希。将来做增量索引时，用它判断"这份文件变没变"。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def main() -> None:
    settings = Settings()
    knowledge_dir = settings.resolve(settings.knowledge_dir)

    started = time.perf_counter()
    documents = list(iter_documents(knowledge_dir))
    if not documents:
        print(f"知识库是空的：{knowledge_dir}")
        print("往这个目录里放几个 .md / .txt 文件，再重跑本脚本。")
        return

    # 必须一份一份文档地切块：片段序号（index）是"文档内序号"，
    # 如果先把所有文档拼成一个大字符串再切，序号就失去意义了。
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
        # 记下用的是哪个 embedder、多少维，读取时才能校验一致性
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

    elapsed = time.perf_counter() - started
    print(f"文档数    ：{len(documents)}")
    print(f"片段数    ：{len(chunks)}")
    print(f"向量维度  ：{vectors.shape[1]}")
    print(f"embedder  ：{embedder.name}")
    print(f"索引文件  ：{settings.index_path}")
    print(f"耗时      ：{elapsed:.2f}s")
    print()
    print("每份文档的片段数：")
    for source, count in per_source.items():
        print(f"  {source}：{count} 段")
    print()
    print("首个片段样例（用于肉眼检查切分质量）：")
    print("  " + chunks[0].text[:160].replace("\n", "\n  "))


if __name__ == "__main__":
    asyncio.run(main())