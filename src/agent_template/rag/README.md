# rag —— 检索增强生成

> 先检索、再生成：把"模型不知道你的私有文档"这件事，变成"临时查资料"。

## 负责什么

分成两条独立链路：

- **索引侧**（离线，`agent index` 触发）：加载文档 → 切块 → 向量化 → 入库。
- **检索侧**（在线，每次提问时）：向量召回 + 关键词召回 → 融合排名 → 拼装上下文。

## 不负责什么

| 你可能会以为它管，但其实不管 | 实际在哪 |
| --- | --- |
| 生成答案 | 生成由 `agent/loop.py` 交给模型完成；这里只提供上下文 |
| 重排（rerank） | 目前没有，接口留好了（见"后续可做"） |
| PDF / Word / Excel 解析 | `loaders.py` 只处理纯文本，扩展点在它身上 |
| 增量索引 | 每次整库重建。当前规模下重建只要几十毫秒，不值得先做增量 |
| 多知识库 / 多租户 | 没有。`VectorStore(path)` 天然支持多条索引，只是没做命名约定 |

## 两条链路

```
索引时（agent index）
  文件 ──loaders──► Document ──chunkers──► Chunk ──embeddings──► 向量
                                                    │
                                                    ▼
                                            store.VectorStore
                                              .agent/index.sqlite3
                                              （chunks + documents + meta）

检索时（提问）
  query ──┬─► embeddings ──► 向量召回 ─┐
          │                            ├─► reciprocal_rank_fusion ─► top_k
          └─► tokenize ────► BM25 召回 ─┘                              │
                                                                       ▼
                                            pipeline.format_context()（带来源，不带分数）
                                                                       │
                                                                       ▼
                                                            rag/tools.py 的
                                                        search_knowledge_base 工具
                                                                       │
                                                                       ▼
                                                                  模型
```

## 成员清单

### `loaders.py` —— 把文件变成 Document

| 名字 | 职责 |
| --- | --- |
| `Document` | 一份待索引文档：`source`（相对路径）、`text`、`path` |
| `iter_documents()` | 递归遍历目录，按后缀白名单产出文档；坏文件跳过记日志，不中断建库 |
| `TEXT_SUFFIXES` | 能当纯文本直接读的后缀集合 |

### `chunkers.py` —— 把文档切成片段（**本模块最关键的 300 行**）

| 名字 | 职责 |
| --- | --- |
| `Chunk` | 片段：`id` / `source` / `index` / `text` / `start` / `heading_path` |
| `Block` | 解析中间产物：一段正文 + 它所属的标题路径 |
| `parse_blocks()` | 扫一遍 Markdown，按空行切块并维护标题栈（`#`→`##`→…），代码围栏内的 `#` 不当标题 |
| `chunk_document()` | 主入口：聚合 → 收尾 → 重叠，产出片段列表 |
| `chunk_documents()` | 批量切块（每份文档的 `index` 从 0 重新计数） |
| `_to_units()` | 把超长块按句子拆成聚合单位；单句仍超长才按字符硬切 |
| `_split_sentences()` | 按句末标点切句 |
| `_aligned_tail()` | 取重叠尾巴，并把起点对齐到句子边界 |
| `_prefix()` | 生成片段前缀：`文档：x.md ｜ 章节：A > B` |
| `_chunk_id()` | 片段 id：来源 + 序号 + 内容哈希 |

### `embeddings.py` —— 文本转向量

| 名字 | 职责 |
| --- | --- |
| `Embedder` | 抽象基类：`embed()` + `aclose()`，`dim` 由实现填充 |
| `LocalHashEmbedder` | 离线兜底：符号哈希 + 次线性词频 + L2 归一化 |
| `OpenAICompatEmbedder` | 走 OpenAI 兼容的 embeddings 接口，**维度从首次响应自动探测** |
| `tokenize()` | 粗分词：拉丁词整取；中文取单字 + 二元组 |
| `build_embedder()` | 按配置构造 |

### `store.py` —— 向量库

| 名字 | 职责 |
| --- | --- |
| `VectorStore` | SQLite 存储：`add` / `all_rows` / `search` / `count` / `meta` / `assert_compatible` |
| `ScoredChunk` | 带分数的检索结果 |
| `EmbeddingMismatch` | 索引与当前 embedder 不匹配时抛出，提示重建 |

### `retriever.py` —— 混合检索

| 名字 | 职责 |
| --- | --- |
| `BM25` | 关键词检索实现（k1=1.5, b=0.75），语料在构造时统计好词频与 IDF |
| `reciprocal_rank_fusion()` | 把多路排名融合成分数：`Σ 1/(k + 名次)` |
| `HybridRetriever` | 两路召回 + 融合，懒加载全库；`last_debug` 留了两路排名供排查 |

### `pipeline.py` / `tools.py` / `indexer.py` —— 对外接口

| 名字 | 职责 |
| --- | --- |
| `RagPipeline` | 上层唯一入口：`ensure_ready()` / `search()` / `format_context()` / `aclose()` |
| `RagNotReady` | 索引为空或与配置不匹配时的启动前报错 |
| `register_rag_tools()` | 注册 `search_knowledge_base` 工具 |
| `build_index()` | 建索引全流程，返回 `IndexReport`（供 CLI 展示） |
| `IndexReport` | 建库结果：文档数、片段数、维度、耗时、每份文档片段数、首个片段样例 |

## 类之间的关系

```
  Settings
     │
     ├── build_embedder() ──► Embedder（local_hash 或 openai_compat）
     │                              ▲
     ▼                              │ 注入
  VectorStore(.agent/index.sqlite3) │
     ▲                              │
     │ 读写                          │
  HybridRetriever ◄────────────────┘
     │   ├── _vector_ranking()  → matrix @ query_vector
     │   └── _keyword_ranking() → BM25.scores()
     ▼
  RagPipeline ──► register_rag_tools() ──► ToolRegistry
```

`RagPipeline` 是**唯一被上层引用**的类。要换向量库、换检索算法，都在它下面完成，
`agent/` 那一层完全不知道 RAG 内部长什么样。

## 为什么这么设计

**1. 为什么切块要"结构感知 + 标题路径"。**
最初的实现是纯字符滑窗 + 段落聚合，实测出来的片段长这样：

```
片段 A 结尾：……答案还能给出出处。## 基本流程     ← 标题悬空，没有内容
片段 B 开头：料塞进权重里。RAG 用"临时查资料"……  ← 从"资料"中间切开
```

改法是把"标题"和"它统领的正文"绑在一起，并把标题路径拼进片段文本。同一份文档、
同一个提问，改前改后的对比：

| | 改前 | 改后 |
| --- | --- | --- |
| 片段边界 | 悬空标题、半截词 | 完整句子、完整章节 |
| 检索结果 | 只有文件名 | `什么是 RAG > 基本流程` |
| 模型回答 | 引用 `what-is-rag.md` | 引用「什么是 RAG > 基本流程」 |

代价是片段数变多（6 → 11）：**用更多的片段换每个片段的主题纯度**，因为跨章节合并
会让一个向量代表两个主题。

**2. 为什么重叠窗口要对齐句子边界。**
`overlap` 是固定字符数（默认 80），直接从"末尾往前数 80 个字符"截，起点很可能落在
词中间。`_aligned_tail()` 会往后找到第一个句末标点，让重叠部分从完整句子开始。
代价是实际重叠长度可能短于配置值——换来的是干净的片段开头，而**片段开头是它被打分
和展示的门面**。

**3. 为什么必须做混合检索，而不是只用向量。**
向量检索擅长"意思相近"（"怎么让模型少胡说" ≈ "如何抑制幻觉"），但对**精确匹配**
很弱：错误码 `E1024`、函数名 `read_file`、"第 3.2 条"这类字符串在向量空间里几乎
没有区分度。BM25 正好相反。两者互补，所以各取一批候选再融合。

`last_debug` 里保留了两路排名，跑 `agent` 时能直观看到"两路结果确实不完全一样"——
这正是混合检索存在的意义。

**4. 为什么融合用 RRF，而不是把两路分数加权相加。**
向量相似度（约 -1~1）和 BM25 分数（0~几十，量纲还依赖语料）**不在一个尺度上**。
加权求和要先归一化，而归一化方式本身又要调参，换一批语料就得重调。

RRF 只用名次不用分数：`总分 = Σ 1/(60 + 名次)`。第 1 名与第 2 名的差距是固定的，
天然免疫量纲问题，几乎不用调参。这就是为什么这里能在两个完全不同的检索器之间
"直接相加"。

**5. 为什么用 SQLite 存向量、还全量读进内存。**
几万片段以内，矩阵乘法比维护 ANN 索引更快、代码也简单得多；零部署、单文件、
能整个拷走，调试时还能用 `sqlite3` 命令行直接翻内容。真到十万级以上再换
sqlite-vec / faiss / Qdrant——**那时只需要替换 `VectorStore` 一个类**。

**6. 为什么索引里要记 embedder 名字和维度。**
换 embedding 模型却不重建索引，是 RAG 最典型的**静默故障**：维度不同会直接算错，
维度恰好相同则更糟——检索结果全乱却毫无提示。所以 `meta` 表记下这两个值，
`ensure_ready()` 启动时校验，不一致就抛 `EmbeddingMismatch` 并给出重建命令。

**7. 为什么要有 `local_hash` 这种"不聪明"的实现。**
它让整条链路在没有 key、没有网络时也能跑通并可测试。**这不是降级方案，而是
让"链路是否完好"成为一个随时可验证的事实**——用真实 embedding 时，你反而分不清
问题是出在检索质量上还是拼接逻辑上。开发期用 `local_hash`，上真数据前换掉。

关于它的原理：哈希技巧把词袋压成定长向量，用**符号哈希**让碰撞有概率互相抵消，
用 **1+log(词频)** 做次线性加权。它不理解语义（"汽车"和"轿车"是两回事），
但对演示和测试足够。

**8. 为什么给模型的上下文里不显示分数。**
RRF 分数只表示相对排名，绝对值没有任何含义——无论检索质量好坏都落在
0.016~0.033 区间。之前把它标成"相关度"，模型真的据此说过"两个文档的相关度分数
都很低，说明知识库检索匹配度一般"。**给模型一个它无法正确解读的信号，等于制造
噪声。** 所以分数留在 `ScoredChunk` 上供 CLI 调试视图用，不进提示词。

## 怎么扩展

### 换 embedding（最常见）

```ini
AGENT_EMBEDDING_PROVIDER=openai_compat
AGENT_EMBEDDING_MODEL=BAAI/bge-m3
AGENT_EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
AGENT_EMBEDDING_API_KEY=sk-...
```

然后**必须重建索引**：`uv run agent index`。

### 换向量库

替换 `VectorStore` 一个类，保持这几个方法的语义：

```
reset() / add(chunks, vectors) / count() / all_rows() / search(vec, top_k)
set_meta() / get_meta() / assert_compatible() / close()
```

`all_rows()` 是给混合检索用的（BM25 需要在内存里统计语料），如果换到大规模后端、
不想全量加载，需要同时改 `HybridRetriever` 的懒加载策略。

### 加文档格式

`loaders.py` 里加一个产出 `Document` 的函数即可，切块之后全都不用改：

```python
def load_pdf(path: Path) -> Document:
    """用 pypdf 抽文本。注意扫描件需要 OCR，那是另一件事。"""
    from pypdf import PdfReader
    text = "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return Document(source=path.name, text=text, path=path)
```

### 调切块参数的方法论

不要凭感觉调。用 `agent index` 打印的首个片段样例 + 几个真实问题做验证：

1. 先用默认值（500/80）建索引，跑 5~10 个你**知道答案在哪个文档里**的问题。
2. 看检索回来的片段是否完整覆盖了答案所在的那一段。
3. 命中不全 → 片段太大（一个向量代表太多内容）→ 调小 `CHUNK_SIZE`。
4. 命中但内容被切断 → `OVERLAP` 太小。
5. 命中的全是无关片段 → 先怀疑 embedder（换成真实模型），再怀疑参数。

## 后续可做（按性价比排序）

| 优先级 | 事项 | 说明与建议 | 大致工作量 |
| --- | --- | --- | --- |
| 高 | **重排（rerank）** | 现在两路各取 20 个候选，RRF 融合后直接取 top 4。中间加一层 rerank 模型（如 bge-reranker）对候选重新打分，是**检索质量提升最大的一步**，通常比换成更大的 embedding 模型还划算。接口位置：`HybridRetriever.search()` 里融合之后 | 一天 |
| 高 | **小块检索、大块生成** | 现在"检索用的片段"和"送给模型的片段"是同一个。拆开之后：用 200 字的小块保证召回精度，命中后把它所属的完整章节给模型保证上下文完整。需要给每个小块记一个"父块 id" | 一天 |
| 中 | **增量索引** | `documents` 表已经记了每份文档的内容哈希，增量所需的数据已经齐了。做法：比对哈希，只重切变化的文件；`chunks` 表按 `source` 删除旧片段再插入 | 一天 |
| 中 | **查询改写** | 用户的问题往往不适合直接检索（口语、指代、多意图）。先让模型改写成 2~3 个检索式再分别召回，能明显提升召回率。注意这多一次模型调用 | 半天 |
| 中 | **多知识库** | 命名约定改成 `.agent/index-<name>.sqlite3`，`agent index --name x` / `--kb x`。适合"不同项目各一套文档" | 半天 |
| 中 | **embedding 并发** | `OpenAICompatEmbedder.embed()` 现在是分批串行。改成 `asyncio.gather` 并发若干批，建库时间能显著缩短 | 两小时 |
| 低 | **PDF / docx 支持** | 见上文扩展方式。注意版式复杂的 PDF 抽出来的文本质量参差不齐 | 一天 |
| 低 | **语义切块** | 用相邻句子的向量相似度骤降处切分。效果可能更好，但建库成本翻倍且阈值难调，建议在 rerank 之后再看 | 一天 |

我的建议顺序：**rerank → 小块检索大块生成 → 增量索引**。前两个直接提升答案质量，
第三个在你文档数量上去之后会变成刚需（现在重建只要 0.06 秒，还不够痛）。
