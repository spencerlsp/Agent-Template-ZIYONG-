"""切块的测试：长度上限、标题路径、句子对齐、id 稳定性。

这里盯的是几个**不变量**，而不是具体切法。切块参数可以调，但不变量不能破：
  1. 任何片段的最终文本（含标题前缀）都不超过 size
  2. 片段正文里不该出现悬空的标题行
  3. 跨章节不合并
  4. 重叠从句子边界开始
  5. id 只取决于内容与位置
"""

from __future__ import annotations

import pytest

from agent_template.rag.chunkers import _aligned_tail, chunk_document

SIZE = 200
OVERLAP = 40


def paragraphs(count: int, length: int) -> str:
    """造 count 个长度为 length 的段落（段末带句号），用空行隔开。"""
    body = "句" + "字" * (length - 2) + "。"
    return "\n\n".join(body for _ in range(count))


def test_short_document_is_one_chunk() -> None:
    chunks = chunk_document("很短的一段话。", source="a.md", size=SIZE, overlap=OVERLAP)

    assert len(chunks) == 1
    assert "很短的一段话。" in chunks[0].text
    assert chunks[0].index == 0


def test_empty_text_gives_no_chunks() -> None:
    assert chunk_document("   \n\n  ", source="a.md", size=SIZE, overlap=OVERLAP) == []


def test_no_chunk_exceeds_size() -> None:
    """核心不变量：任何片段的最终文本都不超过 size。

    这里刻意用 195 字的段落（size=200）。预算约为 183（要扣掉标题前缀），
    所以 195 落在"预算"与"size"之间——这正是历史上出过问题的区间：
    切单位时按 size 判断、聚合时按预算判断，两边不一致就会产出 212 字的片段。
    换成长度更短的段落，这个用例就抓不住回归了。
    """
    text = "# 标题\n\n" + paragraphs(6, 195)

    chunks = chunk_document(text, source="a.md", size=SIZE, overlap=OVERLAP)

    assert chunks
    assert max(len(chunk.text) for chunk in chunks) <= SIZE


def test_heading_path_is_prefixed() -> None:
    text = "# 顶层\n\n## 子节\n\n" + paragraphs(1, 60)

    chunk = chunk_document(text, source="guide.md", size=SIZE, overlap=OVERLAP)[0]

    assert chunk.heading_path == "顶层 > 子节"
    assert chunk.text.startswith("文档：guide.md ｜ 章节：顶层 > 子节")


def test_no_dangling_heading_inside_body() -> None:
    """标题只该出现在前缀里，不该作为正文的一行被切到片段末尾。"""
    text = "# 一\n\n" + paragraphs(2, 120) + "\n\n## 二\n\n" + paragraphs(2, 120)

    chunks = chunk_document(text, source="a.md", size=SIZE, overlap=OVERLAP)

    for chunk in chunks:
        body = chunk.text.split("\n\n", 1)[1]
        assert not any(line.startswith("#") for line in body.splitlines())


def test_cross_section_chunks_do_not_merge() -> None:
    text = "# 甲\n\n短内容一。\n\n# 乙\n\n短内容二。"

    chunks = chunk_document(text, source="a.md", size=SIZE, overlap=OVERLAP)

    assert len(chunks) == 2
    assert chunks[0].heading_path == "甲"
    assert chunks[1].heading_path == "乙"


def test_long_paragraph_is_hard_split() -> None:
    text = "# 标题\n\n" + "字" * 900

    chunks = chunk_document(text, source="a.md", size=SIZE, overlap=0)

    assert len(chunks) > 1
    assert max(len(chunk.text) for chunk in chunks) <= SIZE


def test_aligned_tail_starts_after_sentence_end() -> None:
    """重叠窗口从句子边界开始，而不是从词中间开始。"""
    assert _aligned_tail("先做检索。再做重排。", 6) == "再做重排。"


def test_aligned_tail_falls_back_when_no_boundary() -> None:
    """窗口里没有句末标点时只能原样截取——总比没有重叠好。"""
    assert _aligned_tail("没有标点的一长串文字", 4) == "长串文字"


def test_aligned_tail_with_zero_overlap_is_empty() -> None:
    assert _aligned_tail("任意内容", 0) == ""


def test_chunk_ids_are_stable_and_content_sensitive() -> None:
    first = chunk_document("一样的内容。", source="a.md", size=SIZE, overlap=OVERLAP)
    again = chunk_document("一样的内容。", source="a.md", size=SIZE, overlap=OVERLAP)
    changed = chunk_document("不一样的内容。", source="a.md", size=SIZE, overlap=OVERLAP)

    assert first[0].id == again[0].id
    assert first[0].id != changed[0].id


def test_start_offset_points_into_source() -> None:
    """offset 是排查"切在哪"的依据，必须能对回原文。"""
    text = "# 标题\n\n第一段内容。\n\n第二段内容。"

    chunk = chunk_document(text, source="a.md", size=SIZE, overlap=OVERLAP)[0]
    body = chunk.text.split("\n\n", 1)[1]

    assert text[chunk.start : chunk.start + len(body)] == body


def test_invalid_size_is_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_document("内容", source="a.md", size=0, overlap=0)
