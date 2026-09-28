"""重排接口的测试（第一步：接口与离线兜底）。

这一层的价值在于"零行为变化"：默认不重排，所以接进检索层时，
现有指标不会动。等真实重排接上之后，对比才有意义。
"""

from __future__ import annotations
import httpx
import pytest
import json

from agent_template.rag.rerank import (
    NoopReranker,
    OpenAICompatReranker,
    RerankError,
    Reranker,
    build_reranker,
)
from agent_template.config import Settings
from agent_template.rag.rerank import NoopReranker, Reranker, build_reranker
from agent_template.rag.store import ScoredChunk


def make_chunks(count: int) -> list[ScoredChunk]:
    """造一批按分数降序的候选，模拟融合之后的结果。"""
    return [
        ScoredChunk(id=f"c{i}", source="a.md", text=f"片段{i}", score=1.0 - i * 0.1)
        for i in range(count)
    ]


async def test_noop_keeps_order_and_truncates() -> None:
    """不重排时的行为必须可预测：顺序不变，只截前 top_k。"""
    result = await NoopReranker().rerank("随便问", make_chunks(5), top_k=3)

    assert [chunk.id for chunk in result] == ["c0", "c1", "c2"]


async def test_noop_handles_fewer_candidates_than_top_k() -> None:
    """候选比 top_k 少时不能报错——切片天然就是这样，但值得钉住。"""
    result = await NoopReranker().rerank("问", make_chunks(2), top_k=5)

    assert len(result) == 2


def test_default_configuration_is_noop() -> None:
    """默认配置必须产出"不重排"。

    这条守的是"接进检索层时行为不变"这个前提——哪天默认值被改成真实重排，
    所有基于旧基线的对比都会失效，而不会有任何报错。

    注意不能写裸的 `Settings()`：那会读开发机上的 `.env`，本机把
    AGENT_RERANK_PROVIDER 打开后这条就会失败——测试必须与本地配置无关。
    所以分两步：先钉住"代码里的默认值"，再单独验证"none 映射到 Noop"。
    """
    # 字段默认值不受 .env 和环境变量影响，这才是"默认"的真正含义
    assert Settings.model_fields["rerank_provider"].default == "none"

    reranker = build_reranker(Settings(rerank_provider="none"))

    assert isinstance(reranker, NoopReranker)
    assert isinstance(reranker, Reranker)
    assert reranker.name == "none"

def make_reranker(handler, captured: list | None = None) -> OpenAICompatReranker:
    """造一个"发不出真请求"的重排器。

    httpx.MockTransport 让我们完全控制 HTTP 层：既不联网，也能断言
    "我们到底发出去了什么"。
    """

    def wrapped(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(json.loads(request.content))
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return OpenAICompatReranker(
        model="test-reranker", base_url="http://fake/v1", api_key="k", client=client
    )


async def test_reranker_reorders_and_rewrites_scores() -> None:
    """按服务端返回的顺序重排，并把分数换成重排分数。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.55},
                ]
            },
        )

    reranker = make_reranker(handler)
    result = await reranker.rerank("问一句", make_chunks(3), top_k=2)

    assert [chunk.id for chunk in result] == ["c2", "c0"]
    assert result[0].score == 0.91        # 分数语义换成了重排分数
    await reranker.aclose()


async def test_reranker_sends_the_expected_payload() -> None:
    """请求体形状要和各家 rerank 服务商约定的一致。"""
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    reranker = make_reranker(handler, captured)
    await reranker.rerank("素材够不够", make_chunks(3), top_k=2)

    assert captured[0]["model"] == "test-reranker"
    assert captured[0]["query"] == "素材够不够"
    assert captured[0]["documents"] == [chunk.text for chunk in make_chunks(3)]
    assert captured[0]["top_n"] == 2
    await reranker.aclose()


async def test_server_error_raises_rerank_error() -> None:
    """服务端报错时要抛 RerankError，交给检索层决定要不要降级。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="authentication failed")

    reranker = make_reranker(handler)

    with pytest.raises(RerankError):
        await reranker.rerank("问", make_chunks(2), top_k=1)

    await reranker.aclose()


async def test_empty_candidates_skip_the_network() -> None:
    """没有候选时不该白跑一次网络请求。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("候选为空时不该发请求")

    reranker = make_reranker(handler)

    assert await reranker.rerank("问", [], top_k=3) == []

    await reranker.aclose()
