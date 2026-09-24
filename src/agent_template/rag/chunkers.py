"""文本分块：把长文档切成"适合检索"的片段。

为什么分块质量直接决定 RAG 的上限：
    向量检索的粒度，就是片段的粒度。片段太大，一个向量要代表好几个主题，
    相似度被平均掉，检索变钝；片段太小，一个知识点被拆散，模型拿到的
    上下文不完整，答案就缺斤少两。切分位置切错，后面再好的模型也救不回来。

这里的策略是"结构感知 + 段落聚合 + 硬切兜底"，分四步：

    1. 先按空行切段。段落是写作者本来就划分好的语义单元，尊重它比按固定
       字符数硬切更不容易把一句话劈成两半。
    2. 贪心合并：把连续的段落并进同一个片段，直到再加一段就会超过 size。
    3. 预留重叠：每次收尾时，把上一片段的尾部 overlap 个字符带到下一片段
       开头。这样跨段的句子、指代（"它"、"上述方法"）不会正好卡在切口上。
    4. 兜底硬切：遇到单段就超过 size 的（长代码块、长表格），退化成滑动
       窗口硬切，否则它永远不会被切开。

size 和 overlap 都按字符算。中文一个汉字算一个字符，比 token 计数直观，
也省掉了为了数 token 引入分词器依赖。这两个值是配置项（AGENT_CHUNK_SIZE /
AGENT_CHUNK_OVERLAP），要按自己文档的特点调，不用改代码。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from agent_template.rag.loaders import Document


@dataclass(slots=True)
class Chunk:
    """可被检索的最小单元"""
    # 稳定唯一标识。带上内容哈希，则内容变了 id 就变——将来做增量索引时，
    # 可以据此判断"这条片段是不是新的"，而不是靠位置猜。
    id: str
    # 来自哪份文档。检索结果要把它展示给用户当出处。
    source: str
    # 在该文档内的序号，从 0 开始。需要按阅读顺序还原上下文时靠它。
    index: int
    # 真正参与向量计算、也真正贴进模型提示的文本。
    text: str
    # 在原文中的字符偏移。排查"这句话到底被切在哪"时非常有用。
    start: int


def chunk_document(text: str, *, source: str, size: int, overlap: int) -> list[Chunk]:
    """把一份文档切成若干片段。"""
    if size <=0:
        raise ValueError("chunk size 必须为正数")

    # overlap 不允许超过窗口的一半。否则滑动步长（size - overlap）会变得极小，
    # 片段原地打转、数量爆炸，检索时全是重复内容。
    overlap = max(0, min(overlap, size//2))

    chunks: list[Chunk] = []
    buffer = "" # 正在攒的片段正文
    buffer_start = 0 #这个片段在原为中的起始偏移

    for start, paragraph in split_paragraphs(text):
        # ---- 情况一: 单个段落就超长(长代码块，长表格) ----
        if len(paragraph) > size:
            # 先把攒着的缓冲区收掉，否则片段顺序会乱
            if buffer:
                chunks.append(_make(source, len(chunks), buffer, buffer_start))
                buffer = ""
            for piece_start, piece in _hard_split(paragraph, size, overlap, start):
                chunks.append(_make(source, len(chunks), piece, piece_start))
            continue


        # ---- 情况二：再加这一段就超了，先收掉缓冲区 ----
        if buffer and len(buffer) + len(paragraph) + 2 > size:
            current = buffer
            chunks.append(_make(source, len(chunks), current, buffer_start))
            # 把当前片段的尾部留到下一片段开头，这就是 overlap
            tail = current[-overlap:] if overlap else ""
            # tail 是从 current 结尾截出来的，所以新片段的起始偏移
            # 要回退 len(tail) 个字符；否则记录的 start 会和实际文本对不上
            buffer_start = buffer_start + len(current) - len(tail)
            buffer = tail

        # ---- 情况三：正常累积 ----
        if not buffer:
            # 新片段从这一段的起点开始（buffer 非空时说明是上面继承来的尾巴，
            # 起点已经在情况二里算好了，不能覆盖）
            buffer_start = start
        buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph

    # 收尾：最后攒着的那段别忘了
    if buffer.strip():
        chunks.append(_make(source, len(chunks), buffer, buffer_start))

    return chunks

def chunk_documents(
    documents: list[Document], *, size: int, overlap: int
) -> list[Chunk]:
    """批量切块。

    每份文档的 index 从 0 重新计数（在 chunk_document 内部基于 len(chunks)
    递增），因此同一文档内的片段编号是连续且可预期的。
    """
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(
            chunk_document(
                document.text, source=document.source, size=size, overlap=overlap
            )
        )
    return chunks


def split_paragraphs(text: str) -> list[tuple[int, str]]:
    """按空行切段，返回 (段落起始偏移, 段落文本)。

    为什么单独返回偏移：切块之后我们只保留了片段文本，一旦发现检索结果
    不对劲（比如恰好切在关键句中间），能靠 offset 回到原文定位问题。
    """
    paragraphs: list[tuple[int, str]] = []
    offset = 0
    for block in text.split("\n\n"):
        stripped = block.strip()
        if stripped:
            paragraphs.append((offset, stripped))
        # +2 是被 split 吃掉的那两个换行（"\n\n"），
        # 不补回来的话后面所有偏移都会越来越偏
        offset += len(block) + 2
    return paragraphs


def _hard_split(
        text: str, size: int, overlap: int, base: int
) -> list[tuple[int, str]]:
    """超长段落的兜底：按滑动窗口硬切。

    步长是 size - overlap，所以相邻窗口之间有 overlap 个字符重叠，
    保证不会被一刀切成两段互不相干的文本。
    """
    step = max(1, size - overlap)
    return [(base + i, text[i: i +size]) for i in range(0, len(text), step)]


def _make(source: str, index: int, text: str, start: int) -> Chunk:
    """组装一个片段， 顺手把首尾空白清掉"""
    clean = text.strip()
    return Chunk(
        id = _chunk_id(source, index, clean),
        source = source,
        index=index,
        text=clean,
        start=start,
    )

def _chunk_id(source: str, index: int, text: str) -> str:
    """生成片段 id：来源 + 序号 + 内容哈希。

    内容哈希用 blake2b（比 md5 快、比 sha1 短），只取 6 字节——这里只需要
    "能区分不同内容"，不需要抗碰撞的强度。
    """
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=6).hexdigest()
    return f"{source}#{index}:{digest}"
