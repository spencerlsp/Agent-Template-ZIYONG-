"""文档加载：把知识库目录里的文本文件读成 Document 对象。

这一层只做一件事——把磁盘上的文件变成"纯文本 + 可追溯的来源标识"。
后面的切块、向量化、检索都基于 Document，不再直接接触文件系统。

为什么只支持纯文本类后缀：
    解析 PDF、Word、Excel 需要额外的库，而且各自有各自的坑（版式错乱、
    表格丢失、扫描件要 OCR）。这些属于"按需扩展"：新增一个函数、产出
    Document 即可，上层代码一行都不用改。模板先把主链路做扎实、
    把扩展点标清楚，比把所有格式都塞进来更有价值。
"""

from __future__ import annotations

import logging 
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("agent.rag")

# 能当纯文本直接读的后缀。pdf/docx/xlsx 这类二进制格式故意不在列表里。
TEXT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
}

@dataclass(slots=True)
class Document:
    """一份待索引的原始文档。"""

    # source 用"相对知识库根目录的路径"，而不是绝对路径。三个原因：
    #   1. 展示给用户当引用来源时更短、更好读；
    #   2. 换机器、换目录后依然有效，不会失效；
    #   3. 不会把本机用户名之类的无关信息写进索引，方便把索引拷给别人。

    source: str
    text: str
    # 保留绝对路径。方便日志和错误提示指向真是文件；他不进索引
    path: Path



def iter_documents(directory: Path) -> Iterator[Document]:
    """递归遍历目录，产出所有可读的文本文件。

    设计成生成器而不是返回列表：知识库可能很大，边读边处理不必一次性
    把全部原文读进内存。单个文件读失败只跳过并记日志，不打断整个建库过程——
    知识库里混进一个乱码文件，不该导致全库索引失败。
    """

    if not directory.is_dir():
        logger.warning("知识库目录不存在：%s", directory)
        return

    for path in sorted(directory.rglob("*")):
        # 只处理文件，且后缀在白名单里
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 权限不足， 非UTF-8编码都会落到这里
            logger.warning("跳过无法读取的文件 %s：%s", path, exc)
            continue

        if not text.strip():
            logger.debug("跳过空文件 %s", path)
            continue

        # 统一用正斜杠：Windows 上是反斜杠，直接写进索引会让 source 在不同
        # 操作系统上长得不一样，引用来源也就没法跨机器对齐。
        source = str(path.relative_to(directory)).replace("\\", "/")
        yield Document(source=source, text=text, path=path)  