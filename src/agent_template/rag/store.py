"""向量库：一个 SQLite 版极简实现。

为什么先用它：
    零部署（不用起服务）、单文件（能整个拷走）、可以直接用 sqlite3 命令行
    翻看内容。对"几十份文档、几万个片段"这个量级，检索速度完全够用。
    真到十万级以上，再换带 ANN 索引的方案（sqlite-vec、faiss、Qdrant）——
    那时只需要替换本文件里的这个类，上层依赖的只有下面这几个方法。

存储设计：
    * 向量以 float32 原始字节存进 BLOB。SQLite 没有数组类型，转字节最省空间，
      读出来用 np.frombuffer 还原，零拷贝。
    * 检索时把全库读进内存做一次矩阵乘法。数据量小时这比维护索引更快，
      代码也简单得多。真上规模再换成向量库自带的查询。
    * documents 表记录每份文档的内容哈希与片段数。现在建库是全量重建，
      这张表暂时只是"账本"；等要做增量索引时，比对哈希就能跳过没变的文件，
      不需要重新设计表结构。

两个必须知道的约束：
    * 写入前向量必须已经 L2 归一化（见 embeddings.py）。归一化之后点积就是
      余弦相似度，检索端才能用一次矩阵乘法搞定。
    * meta 表记下 embedder 名字和维度。换了 embedding 模型却不重建索引，是
      RAG 最典型的静默故障——向量维度对不上，检索结果全乱却不报错，
      所以这里主动校验并抛出明确的异常。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from agent_template.rag.chunkers import Chunk

logger = logging.getLogger("agent.rag")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id        TEXT PRIMARY KEY,
    source    TEXT NOT NULL,
    idx       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    embedding BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    source       TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    chunks       INTEGER NOT NULL,
    indexed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
""" 

class EmbeddingMismatch(RuntimeError):
    """索引里的向量与当前 embedder 不匹配，需要重建索引。"""


@dataclass(slots=True)
class ScoredChunk:
    """带分数的检索结果"""
    id: str
    source: str
    text: str
    score: float


class VectorStore:
    """把片段和向量存在一个SQLite文件里"""

    def __init__(self, path: Path) -> None:
        self.path = path
        # 父目录可能还不存在（比如首次运行时的.agent/)
        path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(path)
        # executescript 会隐式提交， 所以这里不用再commit
        self._conn.executescript(SCHEMA)
        self._conn.commit()


    # -------------------------------------------------------------写入
    def reset(self) -> None:
        """清空索引。重建时用，不影响documments 表以外的任何东西"""
        self._conn.execute("DELETE FROM chunks")
        self._conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        """写入元信息（embedder 名、向量维度等）。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def record_document(
        self, *, source: str, content_hash: str, chunks: int, indexed_at: str
    ) -> None:
        """登记一份文档的处理结果。将来做增量索引时的依据"""
        self._conn.execute(
            "INSERT OR REPLACE INTO documents (source, content_hash, chunks, indexed_at)"
            "VALUES (?, ?, ?, ?)",
            (source, content_hash, chunks, indexed_at),
        )
        self._conn.commit()

    def add(self, chunks: list[Chunk], vectors: np.ndarray) -> int:
        """批量写入片段与向量， 行数必须一一对应"""
        if len(chunks) != len(vectors):
            raise ValueError(f"片段数 {len(chunks)} 与向量数 {len(vectors)} 不一致") 

        rows = [
            (
                chunk.id,
                chunk.source,
                chunk.index,
                chunk.text,
                # astype 保证字节序和精度一致，跨机器读回来不会错位
                vector.astype(np.float32).tobytes(),
            )
            for chunk, vector in zip(chunks, vectors)
        ]

        self._conn.executemany(
            "INSERT OR REPLACE INTO chunks (id, source, idx, text, embedding)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

# -----------------------------------------------------------------------------读取
    def count(self) -> int:
        """片段总数"""
        return self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def document_count(self) -> int:
        """文档总数。"""
        return self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def sources(self) -> list[str]:
        """索引里出现过的所有来源文件."""
        rows = self._conn.execute(
            "SELECT DISTINCT source FROM chunks ORDER BY source"
        ).fetchall()
        return [row[0] for row in rows]

    def all_row(self) -> tuple[list[str], list[str], list[str], np.ndarray]:
        """把全库读成 (ids, sources, texts, 向量矩阵)。

        数据量小，一次性全量加载换来的是检索逻辑的简单：向量检索是矩阵乘法，
        关键词检索（BM25）需要在内存里统计词频。真上规模时再换成索引查询。
        """
        rows = self._conn.execute(
            "SELECT id, source, text, embedding FROM chunks ORDER BY source, idx"
        ).fetchall()
        if not rows:
            return [], [], [], np.zeros((0, 0), dtype=np.float32)

        ids = [row[0] for row in rows]
        sources = [row[1] for row in rows]
        texts = [row[2] for row in rows]
        matrix = np.vstack([np.frombuffer(row[3], dtype=np.float32) for row in rows])
        return ids, sources, texts, matrix

    def search(self, query_vector: np.ndarray, top_k: int) -> list[ScoredChunk]:
        """向量检索：向量已归一化，点积即余弦相似度。"""
        ids, sources, texts, matrix = self.all_rows()
        if not ids:
            return []

        query = query_vector.astype(np.float32).reshape(-1)

        if query.shape[0] != matrix.shape[1]:
            raise ValueError(
                f"查询向量维度 {query.shape[0]} 与索引维度 {matrix.shape[1]} 不一致，"
                "多半是换了 embedding 模型但没重建索引"
            )

        scores = matrix @ query # 一次矩阵乘法得到全部相似度
        order = np.argsort(-scores)[:top_k]
        return [
            ScoredChunk(
                id=ids[i], source=sources[i], text=texts[i], score=float(scores[i])
            )
            for i in order
        ]

# ------------------------------------------------------------------------------ 校验
    def assert_compatible(self, embedder_name: str, dim: int) -> None:
        """确认索引与当前 embedder 匹配；不匹配就要求重建。

        空库直接放行——还没写过任何东西，谈不上不一致。
        """
        stored_name = self.get_meta("embedder")
        if stored_name is None:
            return

        stored_dim = self.get_meta("dim")
        if stored_name != embedder_name or (stored_dim and int(stored_dim) != dim):
            raise EmbeddingMismatch(
                f"索引是用 `{stored_name}`（{stored_dim} 维）建立的，"
                f"当前配置是 `{embedder_name}`（{dim} 维）。"
                "请重建索引：uv run python scripts/rag_index.py"
            )

    def close(self) -> None:
        self._conn.close()
