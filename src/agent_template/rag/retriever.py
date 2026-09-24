"""混合检索：向量相似度 + BM25 关键词，用 RRF 融合两组排名。

为什么必须混合：
    纯向量检索擅长"意思相近"（"怎么让模型少胡说" ≈ "如何抑制幻觉"），
    但对精确匹配很弱——错误码 E1024、函数名 read_file、"第 3.2 条"这类字符串
    在向量空间里几乎没有区分度。纯关键词检索正好相反：字面命中很准，
    换个说法就完全找不到。两者互补，所以成熟做法是各取一批候选，再融合排名。

为什么用 RRF 融合，而不是把两路分数加权相加：
    向量相似度（约 -1~1）和 BM25 分数（0~几十，量纲还取决于语料本身）
    根本不在同一个尺度上。直接加权要先归一化，而归一化方式又得调参，
    换一批语料就要重调。
    RRF（Reciprocal Rank Fusion）只用"名次"不用"分数"：
        总分 = Σ 1 / (k + 名次)，k 通常取 60
    第 1 名和第 2 名的差距是固定的，天然免疫量纲问题，基本不用调参。
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Sequence

import numpy as np

from agent_template.rag.embeddings import Embedder, tokenize
from agent_template.rag.store import ScoredChunk, VectorStore

logger = logging.getLogger("agent.rag")

class BM25:
    """
    Okapi BM25 关键词检索算法
    核心目标：计算【查询文本】和【每一篇文档（这里是RAG的chunk分片）】的关键词匹配分数
    只做字面词语匹配，不理解语义；常和向量检索搭配做混合检索（Hybrid Search）

    超参说明：
        k1: 词频饱和系数，默认1.5。
            同一个词在文档内重复出现，分数不会无限线性上涨，会逐步饱和。
            k1越小，饱和越快；k1越大，词频增加带来的加分越多。常用区间1.2~2.0。
        b: 文档长度归一化系数，默认0.75。
            用来惩罚长文档。长文档天然更容易命中关键词，b用来压制这个优势。
            b=0：完全不做长度惩罚；b=1：完全开启长度归一化。工业界默认0.75。

    BM25总分公式：
        score(D,Q) = Σ_{q∈Q} IDF(q) * [ f(q,D)·(k1+1) / ( f(q,D)+k1·(1-b + b·|D|/avgdl) ) ]
        符号含义：
            Q: 查询（分词后的词列表）
            D: 单篇文档/单个chunk
            q: 查询里面的单个词
            f(q,D): 词q在文档D中的词频（出现多少次）
            |D|: 当前文档D的token数量（文档长度）
            avgdl: 全部文档的平均token长度
            IDF(q): 词q的逆文档频率，衡量词语稀有程度，越稀有IDF越高
    """
    def __init__(
            self,
            corpus_tokens: Sequence[Sequence[str]],
            k1: float = 1.5,
            b: float = 0.75,
    ) -> None:
        self.k1 = k1
        self.b = b
        self.size = len(corpus_tokens)  # 总文档数量，等于chunk总数

        # self.counts：预存每个文档内部的词频Counter
        # 提前统计，避免检索阶段反复count，降低查询耗时
        self.counts = [Counter(tokens) for tokens in corpus_tokens]

        # self.lengths：每个文档的token长度数组
        self.lengths = np.array(
            [len(tokens) for tokens in corpus_tokens], dtype=np.float32
        )
        # avgdl：全部文档的平均token长度，长度归一化要用
        self.avg_length = float(self.lengths.mean()) if self.size else 0.0

        # ========== 计算文档频率 df ==========
        # df(document frequency)：某个词，一共出现在多少【不同文档】里
        # 注意：一篇文档里重复多次的同一个词，df只计数1次，所以用set去重
        document_frequency: Counter[str] = Counter()
        for tokens in corpus_tokens:
            document_frequency.update(set(tokens))

        # ========== IDF 逆文档频率计算 ==========
        # IDF：衡量词语稀有度。词越少见，IDF越大，命中后加分越高
        # 公式：ln( (N - df + 0.5) / (df + 0.5) + 1 )
        # +0.5是平滑项：防止df接近总文档数N时IDF变成负数；
        # 负数会造成：命中该词反而扣分，不符合检索直觉，属于BM25+改进写法
        # +1 是 BM25+ 的写法：避免某个词出现在几乎所有文档时 IDF 变成负数
        # （负数会让"命中更多"反而扣分，明显不合理）
        self.idf = {
            token: math.log((self.size - df + 0.5) / (df + 0.5) + 1.0)
            for token, df in document_frequency.items()
        }


    def scores(self, query_tokens: Sequence[str]) -> np.ndarray:
        """
        计算查询分词与全部文档（chunk）之间的BM25匹配分数
        :param query_tokens: 查询文本分词后的词语序列
        :return: np.ndarray，一维数组，长度等于文档总数；result[i] 代表第i篇文档的BM25总分
                分数越高，代表该文档和当前查询关键词匹配程度越高
        """
        # 初始化所有文档分数为0，数组长度等于文档总数，float32节省内存
        result = np.zeros(self.size, dtype=np.float32)
        # 边界保护：没有任何文档时直接返回全0数组，避免后续除零错误
        if not self.size:
            return result

        # 遍历查询中的每一个词，逐个计算该词对所有文档的得分贡献，最后累加
        for token in query_tokens:
            # 取出该词预计算好的IDF（逆文档频率）；词不在语料词典中则返回None
            idf = self.idf.get(token)
            if idf is None:
                continue  # 这个词在全部知识库都没有出现过，无贡献，直接跳过

            # enumerate遍历所有文档：index是文档下标，counts是当前文档的词频Counter
            for index, counts in enumerate(self.counts):
                # tf: term frequency，当前词在本篇文档内的词频（出现次数）
                tf = counts.get(token, 0)
                if not tf:
                    continue  # 当前文档不包含这个词，该词对本篇文档无分数贡献，跳过

                # ========== 文档长度归一化因子 length_norm ==========
                # 作用：压制长文档天然优势。长文档更容易随机命中关键词，需要扣分
                # 公式： length_norm = 1 - b + b * ( 当前文档长度 / 全部文档平均长度 )
                # self.avg_length or 1.0 防止平均长度为0时触发除零异常
                length_norm = 1.0 - self.b + self.b * (
                    self.lengths[index] / (self.avg_length or 1.0)
                )

                # BM25核心单项分数公式：
                # idf * [ tf*(k1+1) / ( tf + k1 * length_norm ) ]
                # 含义：idf是词稀有度；tf*(k1+1)分子放大词频；分母实现词频饱和，不会无限涨分
                result[index] += idf * tf * (self.k1 + 1.0) / (
                    tf + self.k1 * length_norm
                )
        return result



def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]], k: int = 60
) -> dict[int, float]:
    """把多组"文档下标排名"融合成一张分数表。
    只依赖名次、不依赖原始分数，所以量纲完全不同的两路检索可以放心相加。
    :param rankings: 多组排名列表。每个子列表是一路检索返回的文档下标，按从优到差排序
        例：[[3,1,5,7], [1,3,2,8]] 代表两路召回结果
    :param k: RRF超参数，默认60，控制排名权重衰减速度
    :return: dict[int,float] key=文档chunk下标，value=融合后的RRF总分；分数越高越好
    """
    # RRF 用来合并多路检索结果（比如 BM25 关键词召回 + 向量语义召回），
    # 只看排名位置，不使用原始分数，解决两路分数单位不一样、不能直接相加的问题。
    fused: dict[int, float] = {}
    # 遍历每一路检索结果（例如第一路向量召回，第二路BM25召回）
    for ranking in rankings:
        # rank从1开始计数，index是chunk在语料中的下标
        for rank, index in enumerate(ranking, start=1):
            # 累加RRF分数：1/(k+rank)
            fused[index] = fused.get(index, 0.0) + 1.0 / (k + rank)
    return fused


class HybridRetriever:
    """向量 + BM25 混合检索器。

    懒加载：第一次检索时才把全库读进内存并建 BM25 索引，这样"先构造、
    后建库"的用法不会出错，也不会因为索引暂时为空就崩。
    """

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        *,
        top_k: int = 4,
        candidates: int = 20,
        rrf_k: int = 60,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.top_k = top_k
        # 每路先取这么多候选再融合。取太少会漏掉"另一路排得高"的片段，
        # 取太多则把噪声也带进来。20 是个稳妥的起点。
        self.candidates = candidates
        self.rrf_k = rrf_k

        self._loaded = False
        self._ids: list[str] = []
        self._sources: list[str] = []
        self._texts: list[str] = []
        self._matrix = np.zeros((0, 0), dtype=np.float32)
        self._bm25 = BM25([])

        # 仅供调试与演示：记录最近一次两路检索的排名，别在下游依赖它
        self.last_debug: dict[str, list[str]] = {}

    def _ensure_loaded(self) -> None:
        """把全库读进内存， 并为关键词索引建立好BM25"""
        if self._loaded:
            return

        ids, sources, texts, matrix = self.store.all_rows()
        self._ids, self._sources, self._texts, self._matrix = ids, sources, texts, matrix
        # 关键词检索与向量检索用同一套分词，保证两路看到的词是一致的
        self._bm25 = BM25([tokenize(text) for text in texts])
        self._loaded = True
        logger.debug("检索器已加载 %d 个片段", len(ids))

    async def search(self, query: str, top_k: int | None = None) -> list[ScoredChunk]:
        """检索：两路各取候选，RRF 融合后返回前 top_k 个片段。"""
        self._ensure_loaded()
        limit = top_k or self.top_k
        if not self._ids:
            return []

        vector_ranking = await self._vector_ranking(query)
        keyword_ranking = self._keyword_ranking(query)

        fused = reciprocal_rank_fusion([vector_ranking, keyword_ranking], k=self.rrf_k)
        ordered = sorted(fused.items(), key=lambda item: -item[1])[: limit]

        self.last_debug = {
            "vector": [self._ids[i] for i in vector_ranking[: limit]],
            "keyword": [self._ids[i] for i in keyword_ranking[:limit]],
        }
        return [
            ScoredChunk(
                id=self._ids[index],
                source=self._sources[index],
                text=self._texts[index],
                score=score,
            )
            for index, score in ordered
        ]

    
    async def _vector_ranking(self, query: str) -> list[int]:
        """向量路：把问题也向量化，按余弦相似度排序。"""
        matrix = await self.embedder.embed([query])
        query_vector = matrix[0]

        if query_vector.shape[0] != self._matrix.shape[1]:
            raise ValueError(
                f"查询向量维度 {query_vector.shape[0]} 与索引 {self._matrix.shape[1]} 不一致，"
                "多半是换了 embedding 模型但没重建索引"
            )

        scores = self._matrix @ query_vector
        return [int(index) for index in np.argsort(-scores)[: self.candidates]]


    def _keyword_ranking(self, query: str) -> list[int]:
        """关键词路：BM25 排序。

        只保留分数大于 0 的片段——一个词都没命中的片段拿 0 分，
        把它们塞进候选只会稀释 RRF 的信号。
        """
        scores = self._bm25.scores(tokenize(query))
        order = np.argsort(-scores)
        return [int(i) for i in order if scores[i] > 0][: self.candidates]


