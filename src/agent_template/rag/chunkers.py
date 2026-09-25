"""文本分块：把长文档切成"适合检索"的片段。
为什么分块质量直接决定 RAG 的上限：
    向量检索的粒度就是片段的粒度。片段太大，一个向量要代表好几个主题，
    相似度被平均掉，检索变钝；片段太小，一个知识点被拆散，模型拿到的
    上下文不完整，答案就缺斤少两。切分位置切错，后面再好的模型也救不回来。
流程分四个阶段：
    1. 解析（parse_blocks）：扫一遍 Markdown，把正文按空行切成块，并记下每块
       所属的标题路径，形如 "什么是 RAG > 基本流程"。
       —— 标题行不单独成段。它往往只有几个字，单独成片段毫无检索价值；
           绑到它统领的正文上，片段里才带得上"流程""常见坑"这类关键词。
    2. 归一（_to_units）：块要是超过窗口，就按句子切成更小的单位；
       单句仍然超长（长代码块、没有标点的长行）才退化成字符硬切。
    3. 聚合：把连续的单位合并成片段，直到再加一个就会超过预算。
       —— 跨章节既不合并、也不重叠：一个片段只该属于一个章节，
           否则一个向量代表两个主题，检索时互相干扰。
    4. 重叠：收尾时把上一片段的尾部带到下一片段开头，但起点要对齐到句子
       边界（回退到最近的。！？；或换行之后），否则会从"资|料"这种词中间切开。
不变量：**任何片段的最终文本都不超过 size（含标题前缀）**。
size 与 overlap 都按字符计，是配置项 AGENT_CHUNK_SIZE / AGENT_CHUNK_OVERLAP。
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from agent_template.rag.loaders import Document

# Markdown 标题正则：匹配1~6个#，后面跟空格+标题内容
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 代码围栏正则：匹配以空格开头的 ``` 或 ~~~，识别代码块起始/结束标记
FENCE_RE = re.compile(r"^\s*(```|~~~)")
# 句末标点集合，用于句子切分、重叠窗口对齐句子边界
SENTENCE_ENDINGS = "。！？；!?;\n"


@dataclass(slots=True)
class Chunk:
    """可被检索的最小单元，最终存入向量库的对象。"""
    # 稳定唯一标识：内容发生变化id就变化，用于增量索引判断文档片段是否更新
    id: str
    # 文档来源，检索结果返回时用于展示出处
    source: str
    # 当前文档内片段序号，从0开始；不同文档独立计数
    index: int
    # 送入Embedding和LLM的完整文本：【标题前缀 + 正文】
    text: str
    # 正文在原始文档中的字符偏移；注意：前缀是代码动态拼接，不在原文内
    start: int
    # 所属标题路径，用于调试、展示章节信息，例："什么是 RAG > 基本流程"
    heading_path: str = ""


@dataclass(slots=True)
class Block:
    """解析后的正文块：空行分隔的一段正文 + 所属标题路径。
    阶段1 parse_blocks 的输出产物。
    """
    text: str          # 段落正文文本
    heading_path: str  # 该段落归属的标题层级路径
    start: int         # 该段落在原文的起始字符偏移


@dataclass(slots=True)
class _Unit:
    """聚合阶段使用的最小单位（内部私有结构）。
    由Block拆分而来；聚合时以Unit为最小粒度，Unit不可再拆分。
    同一个Unit内的文本，标题路径固定不变。
    """
    text: str          # 单元文本（单段/单句/硬切后的子串）
    start: int         # 该单元在原始文档的起始字符偏移
    heading_path: str  # 所属标题路径，聚合阶段依靠此字段禁止跨章节合并


def parse_blocks(text: str) -> list[Block]:
    """把 Markdown 流式解析成【正文块+标题路径】列表。
    核心逻辑：
    1. 维护标题栈，自动生成层级标题路径，标题行不进入正文，仅修改大纲上下文；
    2. 识别代码围栏，围栏内部的#不会被识别为标题，避免代码注释误解析；
    3. 空行、标题触发flush，将缓冲区的多行文本打包成Block；
    4. 记录原文字符偏移，用于后续溯源。

    Args:
        text: 原始Markdown字符串，换行预先归一为 \n
    Returns:
        list[Block]: 解析完成的正文段落块列表
    """
    blocks: list[Block] = []
    # 标题栈：元素为 (标题层级level, 标题文本)，栈底到栈顶 = 从顶层到子标题
    heading_stack: list[tuple[int, str]] = []
    # 段落缓冲区：缓存还未打包成Block的连续文本行
    # 元素：(本行起始偏移, 本行原始文本)
    paragraph: list[tuple[int, str]] = []
    in_fence = False  # 标记是否处于代码围栏内部，围栏内禁用标题解析
    offset = 0        # 全局字符游标，记录下一行的起始字符位置

    def current_path() -> str:
        """读取当前标题栈，拼接成 A > B > C 格式的标题路径字符串"""
        return " > ".join(title for _, title in heading_stack)

    def flush() -> None:
        """将paragraph缓冲区打包成Block，存入blocks，然后清空缓冲区。
        触发时机：遇到标题 / 空行 / 文档末尾。
        只有缓冲区拼接后非空白，才会生成Block。
        """
        nonlocal paragraph
        if paragraph:
            # 将缓冲区多行用换行拼接，并且整体去除段落首尾空白
            body = "\n".join(line for _, line in paragraph).strip()
            if body:
                blocks.append(
                    Block(
                        text=body,
                        heading_path=current_path(),
                        start=paragraph[0][0],
                    )
                )
        # 无论是否生成Block，清空段落缓冲区，准备收集下一段正文
        paragraph = []

    # 逐行遍历文档，splitlines会剥离每行末尾换行符
    for raw_line in text.splitlines():
        line_start = offset  # 记录当前行的起始偏移快照
        # 更新全局游标：本行字符长度 + 1个换行符(\n)
        offset += len(raw_line) + 1

        # 分支1：匹配代码围栏标记 ``` / ~~~
        if FENCE_RE.match(raw_line):
            in_fence = not in_fence  # 翻转围栏状态，进入/退出代码块
            paragraph.append((line_start, raw_line))  # 围栏标记行本身保留进正文
            continue

        # 围栏外才尝试匹配标题；围栏内直接赋值match=None，不解析标题
        match = None if in_fence else HEADING_RE.match(raw_line)
        if match:
            # 分支2：识别到标题行，先把前面缓存的段落落盘
            flush()
            level = len(match.group(1))  # group1是#串，长度代表标题层级
            title = match.group(2).strip()
            # 弹出同级或更深层级的旧标题，再压入新标题，维护大纲栈
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            continue  # 标题行本身不加入正文缓冲区

        # 分支3：空行，作为段落分隔符，触发flush落盘
        if not raw_line.strip():
            flush()
            continue

        # 分支4：普通正文行，追加到段落缓冲区等待攒满flush
        paragraph.append((line_start, raw_line))

    # 文档遍历结束，兜底flush，处理文档末尾还留在缓冲区的段落
    flush()
    return blocks


def chunk_document(text: str, *, source: str, size: int, overlap: int) -> list[Chunk]:
    """对单份文档执行完整分块流水线：parse_blocks → _to_units → 聚合+句子对齐重叠。

    Args:
        text: 文档原始文本
        source: 文档来源标识，用于Chunk.source
        size: Chunk完整文本最大字符上限（包含标题前缀）
        overlap: 片段之间重叠的目标字符长度

    Returns:
        list[Chunk]: 该文档切分完成的所有检索片段
    """
    if size <= 0:
        raise ValueError("chunk size 必须为正数")
    # overlap上限：不超过size的一半，防止滑动步长过小，片段重复过多
    overlap = max(0, min(overlap, size // 2))
    chunks: list[Chunk] = []
    buffer = ""              # 正在累积的片段正文（不含标题前缀）
    buffer_start = 0         # 当前buffer正文在原文的起始偏移
    buffer_path = ""         # 当前buffer所属标题路径

    # 遍历归一化后的最小聚合单元 _Unit
    for unit in _to_units(parse_blocks(text), size=size, overlap=overlap):
        # 计算标题前缀占用的字符，预留分隔换行\n\n的2个字符
        prefix_len = len(_prefix(source, unit.heading_path)) + 2
        # 预算：buffer最多可以容纳的正文字符上限（保证最终Chunk.text<=size）
        budget = max(1, size - prefix_len)

        # 分支1：进入新章节，标题路径发生变化 → 不跨章节合并，丢弃跨章节重叠
        if buffer and unit.heading_path != buffer_path:
            chunks.append(_make(source, len(chunks), buffer, buffer_start, buffer_path))
            buffer = ""

        # 分支2：追加当前Unit会超出预算上限，先把现有buffer生成Chunk，再生成句子对齐的重叠尾巴
        if buffer and len(buffer) + len(unit.text) + 2 > budget:
            chunks.append(_make(source, len(chunks), buffer, buffer_start, buffer_path))
            # 从buffer尾部截取对齐句子边界的重叠文本
            tail = _aligned_tail(buffer, overlap)
            # 更新buffer起始偏移，偏移跟随tail的起点回退
            buffer_start = buffer_start + len(buffer) - len(tail)
            buffer = tail
            # 校验：尾巴+当前Unit依然超预算，则放弃本次重叠，清空buffer，保证长度约束
            if len(buffer) + len(unit.text) + 2 > budget:
                buffer = ""

        # buffer为空，代表开启全新片段，起点取当前Unit的原文起始位置
        if not buffer:
            buffer_start = unit.start
        buffer_path = unit.heading_path
        # 拼接单元文本，多个单元之间用两个换行分隔
        buffer = f"{buffer}\n\n{unit.text}" if buffer else unit.text

    # 文档遍历结束，处理buffer中剩余未提交的正文
    if buffer.strip():
        chunks.append(_make(source, len(chunks), buffer, buffer_start, buffer_path))
    return chunks


def chunk_documents(
    documents: list[Document], *, size: int, overlap: int
) -> list[Chunk]:
    """批量文档分块入口函数。每份文档独立处理，每个文档内Chunk index从0重新计数。

    Args:
        documents: Document对象列表
        size: Chunk完整文本最大字符上限
        overlap: 片段重叠目标字符长度

    Returns:
        list[Chunk]: 全部文档切分后的Chunk列表
    """
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(
            chunk_document(
                document.text, source=document.source, size=size, overlap=overlap
            )
        )
    return chunks


def _to_units(blocks: list[Block], *, size: int, overlap: int) -> list[_Unit]:
    """【阶段2归一】将Block转换为聚合最小单元_Unit。
    策略：
    1. 短Block直接包装为单个Unit；
    2. 超长Block优先按句子切分；
    3. 单句仍然超长（长代码/无标点单行），退化为字符滑动硬切。

    Args:
        blocks: parse_blocks输出的Block列表
        size: Chunk总字符上限
        overlap: 重叠目标字符长度

    Returns:
        list[_Unit]: 聚合用最小单元列表
    """
    units: list[_Unit] = []
    for block in blocks:
        # 段落长度不超限，直接包装成一个Unit
        if len(block.text) <= size:
            units.append(_Unit(block.text, block.start, block.heading_path))
            continue
        # 超长段落，先拆句子
        for offset, sentence in _split_sentences(block.text):
            # 句子长度合规，直接作为Unit
            if len(sentence) <= size:
                # block.start + offset：句子在原始文档的全局偏移
                units.append(_Unit(sentence, block.start + offset, block.heading_path))
                continue
            # 句子本身超长：滑动窗口硬切
            step = max(1, size - overlap)
            for i in range(0, len(sentence), step):
                units.append(
                    _Unit(
                        text=sentence[i: i + size],
                        start=block.start + offset + i,
                        heading_path=block.heading_path,
                    )
                )
    return units


def _split_sentences(text: str) -> list[tuple[int, str]]:
    """按句末标点切分句子，返回列表：[(句子在Block内的偏移, 句子文本)]
    规则：标点归属前一句；空白句子丢弃。

    Args:
        text: 单个Block的正文文本

    Returns:
        list[tuple[int, str]]: (块内偏移, 句子文本)
    """
    sentences: list[tuple[int, str]] = []
    start = 0
    for index, char in enumerate(text):
        if char not in SENTENCE_ENDINGS:
            continue
        # 截取从start到当前标点（标点包含在内）
        raw = text[start: index + 1]
        piece = raw.strip()
        if piece:
            # 修正偏移：跳过前导空白，保证偏移和原文对齐
            piece_start_in_block = start + len(raw) - len(raw.lstrip())
            sentences.append((piece_start_in_block, piece))
        start = index + 1
    # 处理最后一段没有句末标点的剩余文本
    raw = text[start:]
    piece = raw.strip()
    if piece:
        piece_start_in_block = start + len(raw) - len(raw.lstrip())
        sentences.append((piece_start_in_block, piece))
    return sentences


def _aligned_tail(text: str, overlap: int) -> str:
    """从文本尾部截取overlap长度的重叠片段，并且对齐到句子边界。
    目的：避免下一个片段从词语中间切开；
    逻辑：从overlap起点向后查找第一个句末标点，从标点后开始截取；
    找不到标点时，退化成普通尾部字符截取。

    Args:
        text: 需要截取尾巴的完整buffer文本
        overlap: 目标重叠字符数

    Returns:
        str: 对齐句子边界后的重叠文本
    """
    if overlap <= 0:
        return ""
    # 重叠区域起始位置
    start = max(0, len(text) - overlap)
    # 在重叠窗口内查找句末标点
    for index in range(start, len(text)):
        if text[index] in SENTENCE_ENDINGS:
            candidate = text[index + 1:].lstrip()
            if candidate:
                return candidate
    # 窗口内无句末标点，退化为直接截取尾部overlap字符
    return text[start:]


def _prefix(source: str, heading_path: str) -> str:
    """生成Chunk的文本前缀，附加章节和文档来源信息。
    作用：
    1. 章节关键词进入向量，提升检索召回质量；
    2. LLM读取片段时直接知道所属章节，减少引用错误。
    """
    if heading_path:
        return f"文档：{source} ｜ 章节：{heading_path}"
    return f"文档：{source}"


def _make(source: str, index: int, text: str, start: int, heading_path: str) -> Chunk:
    """组装Chunk对象，拼接前缀+正文，生成稳定id。
    """
    body = text.strip()
    return Chunk(
        id=_chunk_id(source, index, body),
        source=source,
        index=index,
        text=f"{_prefix(source, heading_path)}\n\n{body}",
        start=start,
        heading_path=heading_path,
    )


def _chunk_id(source: str, index: int, text: str) -> str:
    """生成Chunk唯一ID，用于增量索引。
    格式：`source#index:blake2b_6字节hex`
    只对正文body哈希，前缀不参与哈希；内容变化则hash变化。
    """
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=6).hexdigest()
    return f"{source}#{index}:{digest}"
