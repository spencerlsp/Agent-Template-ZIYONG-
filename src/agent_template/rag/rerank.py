"""重排：对召回的候选做一次精细排序。

为什么需要它：
    召回阶段的目标是"不漏"，所以会多取一些候选（默认 20 个）；而精排阶段的
    目标是"排得准"。用一次额外调用把真正相关的顶到前面，通常是检索质量提升
    性价比最高的一步。

它和向量检索的本质区别：
    向量检索把问题和片段**分别**编码成向量再算距离，两者之间没有交互；
    重排模型（cross-encoder）把问题和片段**拼在一起**打分，能做词级匹配，
    所以更准。代价是无法预先建索引，只能对少量候选逐个打分——这也是为什么
    它只能放在"召回之后"。

为什么接口只有 `rerank()` 一个方法：
    重排不改变候选集合（不新增、不删除），只改变**顺序**。把这一点写进接口，
    调用方就不用担心候选被换掉。
"""

from __future__ import annotations

import httpx

from abc import ABC, abstractmethod

from agent_template.config import Settings
from agent_template.rag.store import ScoredChunk


class RerankError(RuntimeError):
    """重排调用失败：网络问题、鉴权失败、服务端返回异常。

    为什么抛出来而不是就地降级：**重排器只负责报告自己的失败**，
    "要不要退回不重排"是检索层的决定（它知道当前还剩多少预算、要不要告警）。
    职责分开之后，测试里就能单独验证"失败会抛错"。
    """


class Reranker(ABC):
    """重排器：给一批候选，返回重新排过序的前 top_k 条。"""

    name: str

    @abstractmethod
    async def rerank(
        self, query: str, candidates: list[ScoredChunk], top_k: int
    ) -> list[ScoredChunk]:
        """按与 query 的相关性重排候选，返回前 top_k 条。

        注意 `top_k` 是**参数**而不是对象上的状态：同一个重排器可能被不同
        场景以不同粒度调用（比如评测里想比较 top-3 和 top-5），
        把它放在参数上，这个类就是无状态的。
        """

    async def aclose(self) -> None:
        """释放资源（HTTP 客户端等）。默认什么都不做。"""


class NoopReranker(Reranker):
    """不重排：原样返回前 top_k 条。

    这是**默认实现**，也是"没配重排时行为与之前完全一致"的保证。

    它还有第二个用处：做对照实验。把 top_k 从 4 调到 8 再跑一次评测，就能看出
    "多给模型几条"和"重排"各值多少分——没有这个基线，你无法判断收益来自哪。
    """
    name = "none"

    async def rerank(
            self, query: str, candidates: list[ScoredChunk], top_k: int
    ) -> list[ScoredChunk]:
        return candidates[: top_k]

class OpenAICompatReranker(Reranker):
    """走服务商的 /rerank 端点（硅基流动、Cohere、Jina 都是这个形状）。

    一个容易误解的点：**rerank 不是 OpenAI 兼容协议的一部分**——没有任何
    标准规定 /rerank 长什么样。这里的"兼容"指的是这些服务商**彼此之间**
    用了同一套请求/响应形状：

        POST {base_url}/rerank
        {"model": ..., "query": ..., "documents": [...], "top_n": N}
        → {"results": [{"index": 3, "relevance_score": 0.98}, ...]}

    所以这里直接用 httpx，而不是 openai SDK——SDK 里没有这个方法。
    """

    def __init__(
            self,
            *,
            model: str,
            base_url: str,
            api_key: str,
            timeout_s: float = 30.0,
            client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = f"openai_compat:{model}"
        self.model = model
        self.url = base_url.rstrip("/") + "/rerank"
        self.api_key = api_key
        # 允许注入 client：测试里塞一个 httpx.MockTransport 就能不发真请求，
        # 也不需要网络。自己建的才由自己关（见 aclose）。
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=15.0)
        )
        self._owns_client = client is None

    async def rerank(
            self, query: str, candidates: list[ScoredChunk], top_k: int
    ) -> list[ScoredChunk]:
        if not candidates:
            return [] # 没有候选就别白跑一次网络请求

        payload = {
            "model": self.model,
            "query": query,
            "documents": [chunk.text for chunk in candidates],
            "top_n": min(top_k, len(candidates)),
            # 不让服务端把原文回传： 原文本地就有，白占宽带
            "return_documents": False
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = await self._client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RerankError(f"重排请求失败：{exc}") from exc

        if response.status_code >= 400:
            raise RerankError(
                f"重排请求被拒绝（{response.status_code}）：{response.text[:300]}"
            )

        results = (response.json() or {}).get("results") or []
        reranked: list[ScoredChunk] = []
        for item in results:
            index = item.get("index")
            # 服务端给了越界下标就跳过，不让整个查询崩掉
            if not isinstance(index, int) or not 0 <= index < len(candidates):
                continue
            original = candidates[index]
            reranked.append(
                ScoredChunk(
                    id=original.id,
                    source=original.source,
                    text=original.text,
                    # 注意分数语义变了：从"RRF 融合分"变成了"重排模型的相似度"，
                    # 两者不是一个尺度，别拿来互相比较
                    score=float(item.get("relevance_score", 0.0)),
                )
            )
        return reranked[:top_k]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def build_reranker(settings: Settings) -> Reranker:
    """按配置构造重排器。切换实现只影响这一个函数。"""
    if settings.rerank_provider == "none":
        return NoopReranker()

    if settings.rerank_provider == "openai_compat":
        api_key = (
            settings.rerank_api_key.get_secret_value()
            if settings.rerank_api_key
            else None
        )
        return OpenAICompatReranker(
            model=settings.rerank_model,
            base_url=settings.rerank_base_url,
            api_key=api_key,
        )

    raise ValueError(f"未知的 rerank_provider：{settings.rerank_provider}")