"""向量化：把文本变成一串数字（向量），让"语义相近"变成"距离相近"。

两个实现：

  * LocalHashEmbedder —— 离线、零依赖、确定性。用哈希技巧把词袋压成定长向量。
    它并不理解语义（"汽车"和"轿车"在它眼里是两回事），但足以让整条 RAG 链路
    在没有 API key、没有网络时跑通并接受测试。

  * OpenAICompatEmbedder —— 走 OpenAI 兼容的 embeddings 接口
    （OpenAI、硅基流动的 BAAI/bge-m3 等），这才是生产环境该用的。
    只要改 .env 里的 AGENT_EMBEDDING_PROVIDER，上层代码一行不动。

为什么最后都做 L2 归一化：
    向量归一化后长度为 1，两个向量的点积就等于余弦相似度。于是检索阶段
    可以用一次矩阵乘法算出全部片段的相似度——又快又简单。这一步必须在
    写入索引之前完成：存的向量和查询向量的尺度不一致时，余弦会被长度干扰。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod

import numpy as np
from openai import AsyncOpenAI

from agent_template.config import Settings

logger = logging.getLogger("agent.rag")

# 匹配"连续的拉丁字母/数字"或"连续的中日韩字符"。
# 这样一次扫描就能把两种文字分开，不用先判断语言。
TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")

def tokenize(text: str) -> list[str]:
    """粗分词：拉丁词整取；中文取单字 + 相邻二字组合。

    为什么要专门处理中文：
        英文靠空格就能切词，中文没有空格。整句当一个 token 的话，
        只有完全一样的句子才能匹配上，召回率极低；只切单字又太碎，
        "检索"和"检查"共享"检"字，会产生大量噪声。
        "单字 + 二元组"是性价比最高的折中：二元组"检索"能精确命中，
        单字则兜住分词边界切错的情况。
    """
    tokens: list[str] = []
    for match in TOKEN_PATTERN.finditer(text.lower()): # 返回**Match 对象迭代器**，可以拿到：匹配文本、起始 / 结束下标、分组等丰富信息；适合大文本（惰性迭代，不一次性把全部结果载入内存）
        piece = match.group(0) # 拿到这次正则匹配出来的**完整整块文本**。`group()` 不带参数等价于 `group(0)`。
        if piece[0].isascii():
            # 拉丁词或数字，整块就是一个 token
            tokens.append(piece)
            continue
        # 中文：先加全部单字，再加所有长度为 2 的相邻组合
        tokens.extend(piece)
        tokens.extend(piece[i : i + 2] for i in range(len(piece) - 1))
    return tokens


class Embedder(ABC):
    """向量化接口。"""

    name: str
    # 向量维度。local_hash 在构造时就确定；远端接口第一次调用后探测得到。
    dim: int

    @abstractmethod
    async def embed(self, texts: list[str]) -> np.ndarray:
        """把若干文本转成 shape=(n, dim) 的 float32 矩阵。"""


    async def aclose(self) -> None:
        """释放资源（HTTP 客户端等）。远端实现需要覆盖它。"""






class LocalHashEmbedder(Embedder):
    """离线兜底实现：符号哈希 + 次线性词频 + L2 归一化。

    原理（哈希技巧，hashing trick）：
        不维护"词 -> 维度下标"的词表，而是直接对词做哈希取模，得到它落在
        哪一维。好处是维度固定、词表可以无限增长而内存不涨；代价是不同词
        可能撞到同一维，也就是哈希碰撞。

    用到的两个技巧都是为了缓解碰撞带来的失真：
        * 符号哈希：由哈希值再取一位当正负号，使碰撞时"有概率互相抵消"，
          而不是一味同向叠加、把无关文本推得越来越近。
        * 次线性词频：权重用 1 + log(次数)。一个词重复 50 次，权重不该是
          重复 1 次的 50 倍——那样文档一长，向量就被高频词带偏了。
    """

    def __init__(self, dim: int = 512) -> None:
        self.name = "local_hash"
        self.dim = dim


    async def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            # 注意形状：(0, dim) 而不是 (0,)，否则下游矩阵乘法会报维度错
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._one(text) for text in texts])

    def _one(self, text: str) -> np.ndarray:
        """把一段文本编码成一个归一化向量。"""
        vector = np.zeros(self.dim, dtype=np.float32)

        # 先统计词频，再统一加权，避免同一个词被反复叠加计算
        counts: dict[str, int] = {}
        for token in tokenize(text):
            counts[token] = counts.get(token, 0) + 1

        for token, count in counts.items():
            # blake2b 取 8 字节：前 3 位左右的熵足够决定维度，最高位拿来定符号
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "little")

            index = value % self.dim
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))  # 次线性词频

        # L2 归一化：让点积直接等于余弦相似度
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector  


class OpenAICompatEmbedder(Embedder):
    """OpenAI 兼容 embeddings 接口的适配器，维度从第一次响应自动探测。"""

    def __init__(
            self,
            *,
            model: str,
            base_url: str,
            api_key: str | None,
            batch_size: int = 32,
            timeout_s: float = 60.0,
    ) -> None:
        self.name = f"openai_compat:{model}" 
        self.model = model

        # 0 表示"还不知道"。写死维度是很常见的坑：换模型（768/1024/1536 维）
        # 后索引和新查询的维度对不上，检索结果会静默地全错。
        # 所以这里让它从真实响应里学，而不是靠配置猜。  
        self.dim = 0
        self.batch_size = batch_size
        self._client = AsyncOpenAI(
            api_key=api_key or "not-needed", base_url=base_url, timeout=timeout_s
        )

    async def embed(self, texts: list[str]) -> np.ndarray:
        vectors: list[list[float]] = []

        # 分批请求：一次塞几千条文本既容易撞到接口的体积上限，
        # 失败重试的代价也大；32 条一批是稳妥的默认值。

        for start in range(0, len(texts), self.batch_size):
            batch = texts[start, start + self.batch_size]
            response = await self._client.embeddings.create(
                model=self.model, input=batch
            )
            # 接口不保证返回顺序与请求顺序一致， 官方建议按index排序后使用
            vectors.extend(
                item.embedding
                for item in sorted(response.data, key=lambda item: item.index)
            )
        if not vectors:
            return np.zeros((0, self.dim or 1), dtype=np.float32)
        matrix = np.asarray(vectors, dtype=np.float32)
        self.dim = int(matrix.shape[1])
        return matrix

    async def aclose(self) -> None:
        await self._client.close()

def build_embedder(settings: Settings) -> Embedder:
    """按配置构造 embedder。切换实现只影响这一个函数。"""
    if settings.embedding_provider == "local_hash":
        return LocalHashEmbedder(dim=settings.embedding_dim)

    if settings.embedding_provider == "openai_compat":
        api_key = (
            settings.embedding_api_key.get_secret_value()
            if settings.embedding_api_key
            else None
        )

        return OpenAICompatEmbedder(
            model=settings.embedding_model,
            api_key=api_key,
            base_url=settings.embedding_base_url,
        )
    raise ValueError(f"未知的 embedding_provider：{settings.embedding_provider}")