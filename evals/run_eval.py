"""RAG 检索评测：用一批标注好的问题，量化「答案有没有被检索回来」。

为什么先只评检索、不评生成：
    检索指标（Recall / MRR）便宜、稳定、可复现——只用到 embedding 和 BM25，
    不调用生成模型，十几条问题几秒就跑完。而「答案质量」要引入 LLM 裁判，
    成本和噪声都高一个量级。先把便宜可靠的数字拿到手，再谈生成质量。

为什么分三组跑：
    纯向量路 / 纯关键词路 / 融合后，各算同一套指标。这张表能直接回答
    「混合检索到底有没有用」，以及「哪一类问题该靠哪一路」。

用法：
    uv run python evals/run_eval.py
    uv run python evals/run_eval.py --top-k 3 --verbose
    uv run python evals/run_eval.py --write-report
"""

from __future__ import annotations

# ---------------------------------------------------------------- 标准库
# argparse 标准库：把 --top-k 3 这样的命令行参数变成 Python 变量。
#           不引入 typer/click，是因为评测脚本只需要几个简单选项。
import argparse
# asyncio 标准库：检索函数（_vector_ranking / search）是 async 的，需要它来驱动。
import asyncio
# json 标准库：读 JSONL。JSONL = 每行一个独立的 JSON 对象，便于逐行 diff。
import json
# statistics 标准库：算平均值。这里只用 mean，不值得为此引入 numpy。
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 第三方
# rich 是终端渲染库（本项目 CLI 也在用）：Table 负责画表格，Console 负责输出。
# 相比手拼字符串，它的好处是自动对齐、能识别中文宽字符、支持颜色。
from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------- 本项目
from agent_template.config import Settings
from agent_template.rag.embeddings import build_embedder
from agent_template.rag.retriever import HybridRetriever
from agent_template.rag.store import VectorStore

console = Console()

# 本文件所在目录：用来找 dataset.jsonl、写 report.md
EVAL_DIR = Path(__file__).resolve().parent

MODES = ("vector", "keyword", "hybrid")
MODE_LABELS = {"vector": "纯向量", "keyword": "纯关键词", "hybrid": "融合(RRF)"}


# ================================================================== 数据集
@dataclass(slots=True)
class Gold:
    """一条标准答案的位置：哪份文档里的哪句话。"""

    source: str
    contains: str  # 关键句片段；用它而不是字符偏移，是为了与切块参数解耦
    occurrence: int = 1  # 同一句话在文档里出现多次时，指定第几次


@dataclass(slots=True)
class Case:
    """一条评测用例。"""

    id: str
    query: str
    gold: list[Gold]
    kind: str = "未分类"  # 问题类型，用于分桶统计
    note: str = ""


def load_cases(path: Path) -> list[Case]:
    """读取 JSONL 数据集。

    用 JSONL 而不是一个大 JSON 数组：加一条用例只会在 diff 里多一行，
    改动范围看得清清楚楚；解析时也能逐行走，不必一次全读进内存。
    """
    cases: list[Case] = []
    text = path.read_text(encoding="utf-8")

    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # 允许空行与 # 注释——JSONL 本身不支持注释，这是我们自己的约定
        if not stripped or stripped.startswith("#"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name} 第 {line_no} 行不是合法 JSON：{exc}") from exc

        cases.append(
            Case(
                id=raw["id"],
                query=raw["query"],
                gold=[Gold(**item) for item in raw["gold"]],
                kind=raw.get("kind", "未分类"),
                note=raw.get("note", ""),
            )
        )

    if not cases:
        raise SystemExit(f"{path} 里没有任何用例")
    return cases


def validate(cases: list[Case], knowledge_dir: Path) -> list[str]:
    """检查每条 gold 锚点都能在源文档里找到，返回问题列表。

    这一步比指标本身还重要：如果 contains 写错了（打错一个字、或者文档后来改过），
    这条用例会永远算「未命中」，而你会以为是检索系统不行——**数字被污染了却看不出来**。
    所以宁可让脚本在开始时就报错。
    """
    problems: list[str] = []

    for case in cases:
        for gold in case.gold:
            path = knowledge_dir / gold.source
            if not path.is_file():
                problems.append(f"[{case.id}] 找不到文档 {gold.source}")
                continue

            document = path.read_text(encoding="utf-8")
            if document.count(gold.contains) < gold.occurrence:
                preview = gold.contains[:24]
                problems.append(
                    f"[{case.id}] {gold.source} 里找不到第 {gold.occurrence} 处「{preview}…」"
                )

    return problems


# ================================================================== 判定
@dataclass(slots=True)
class CaseResult:
    """单条用例在某个模式下的结果。"""

    case: Case
    mode: str
    hits: int  # 命中的 gold 条数
    first_rank: int | None  # 第一个命中的排名（从 1 开始）；None 表示全部未命中


def judge(case: Case, ranked: list[tuple[str, str, int]]) -> tuple[int, int | None]:
    """判定命中：看前 k 个片段里有没有包含每条 gold 的关键句。

    为什么用「关键句是否出现在片段里」而不是「字符区间是否相交」：
      1. 索引里只存了片段文本，没有存偏移（偏移只在切块时短暂存在）；
      2. 这个判定对切块参数完全免疫——片段切大切小、带不带标题前缀都不影响。

    代价是偏悲观：如果一句话正好被切在两个片段中间，两边都不含完整句子，
    会算作未命中。但这类失败恰恰是我们最想暴露的（说明切块切得不好），
    宁可它偏悲观，也不要它偏乐观。

    来源也必须比对：同一句话可能出现在多篇文档里（我们的语料里就有），
    只比文本会把来自错误文档的片段误判成命中，指标会凭空变好。

    ranked 的每一项是 (来源文档, 片段文本, 排名)。
    """
    hits = 0
    ranks: list[int] = []

    for gold in case.gold:
        for source, text, rank in ranked:
            if source == gold.source and gold.contains in text:
                hits += 1
                ranks.append(rank)
                break  # 这条 gold 已经命中，不必再看后面的片段

    return hits, (min(ranks) if ranks else None)


async def run_case(
    case: Case, retriever: HybridRetriever, top_k: int
) -> dict[str, CaseResult]:
    """跑一条用例的三个模式，返回 {模式: 结果}。"""
    # 向量路与关键词路：直接取 retriever 的内部排名。
    # _vector_ranking / _keyword_ranking 名义上是「内部」方法，但评测脚本作为
    # 同仓库的调试工具直接用它们是合理的——不单独看每一路，就无法判断
    # 融合到底是加分还是减分。将来若嫌这个名字难看，把它们提升为公开方法即可。
    vector_ranking = await retriever._vector_ranking(case.query)
    keyword_ranking = retriever._keyword_ranking(case.query)
    # 按行号索引的片段来源与全文，与上面的排名一一对应
    sources = retriever._sources
    texts = retriever._texts

    ranked_vector = [
        (sources[index], texts[index], rank)
        for rank, index in enumerate(vector_ranking[:top_k], start=1)
    ]
    ranked_keyword = [
        (sources[index], texts[index], rank)
        for rank, index in enumerate(keyword_ranking[:top_k], start=1)
    ]

    results: dict[str, CaseResult] = {}

    hits, first_rank = judge(case, ranked_vector)
    results["vector"] = CaseResult(case, "vector", hits, first_rank)

    hits, first_rank = judge(case, ranked_keyword)
    results["keyword"] = CaseResult(case, "keyword", hits, first_rank)

    # 融合路：走正常的对外接口，拿到的是 RRF 融合后的结果
    fused = await retriever.search(case.query, top_k=top_k)
    hits, first_rank = judge(
        case,
        [(hit.source, hit.text, rank) for rank, hit in enumerate(fused, start=1)],
    )
    results["hybrid"] = CaseResult(case, "hybrid", hits, first_rank)

    return results


# ================================================================== 指标
@dataclass(slots=True)
class Metrics:
    """一组结果的汇总指标。"""

    mode: str
    recall: float
    hit_rate: float
    mrr: float
    cases: int
    gold: int


def summarise(mode: str, results: list[CaseResult]) -> Metrics:
    """把一组结果汇总成三个指标。

    Recall@k   = 命中的 gold 数 / gold 总数      —— 答案「有没有被捡回来」
    HitRate@k  = 至少命中一条的用例占比          —— 有多少问题完全没希望
    MRR        = 平均(1 / 首个命中排名)，未命中记 0 —— 命中了，但排得太靠后吗

    为什么先看 Recall 再看 MRR：模型能过滤掉噪声片段，但**捡不回来的片段永远拿不到**。
    Recall 低说明是召回问题（换 embedding / 改切块）；Recall 高但 MRR 低说明是排序问题
    （该上 rerank 了）。两个数字合起来才能指出优化方向。
    """
    gold_total = sum(len(result.case.gold) for result in results)
    hit_total = sum(result.hits for result in results)
    hit_cases = sum(1 for result in results if result.hits > 0)

    mrr = (
        statistics.mean(
            1.0 / result.first_rank if result.first_rank else 0.0
            for result in results
        )
        if results
        else 0.0
    )

    return Metrics(
        mode=mode,
        recall=hit_total / gold_total if gold_total else 0.0,
        hit_rate=hit_cases / len(results) if results else 0.0,
        mrr=mrr,
        cases=len(results),
        gold=gold_total,
    )


async def evaluate(
    cases: list[Case], settings: Settings, top_k: int
) -> dict[str, list[CaseResult]]:
    """对全部用例跑一遍，返回 {模式: 结果列表}。"""
    embedder = build_embedder(settings)
    store = VectorStore(settings.index_path)

    try:
        # 先做一致性自检：换了 embedding 却没重建索引时，这里会直接报错，
        # 而不是给你一份「检索全错」的数字
        store.assert_compatible(embedder.name, embedder.dim or None)

        retriever = HybridRetriever(
            store,
            embedder,
            top_k=settings.rag_top_k,
            candidates=settings.rag_candidates,
        )
        # 触发懒加载：把全库读进内存并建好 BM25。之后 _texts 才可用。
        retriever._ensure_loaded()

        collected: dict[str, list[CaseResult]] = {mode: [] for mode in MODES}
        for case in cases:
            per_mode = await run_case(case, retriever, top_k)
            for mode in MODES:
                collected[mode].append(per_mode[mode])

        return collected
    finally:
        store.close()
        await embedder.aclose()


# ================================================================== 输出
def render_summary(
    metrics: dict[str, Metrics], top_k: int, case_count: int, gold_count: int
) -> None:
    """三路对比总表。"""
    table = Table(
        title=f"检索质量（@top-{top_k}｜{case_count} 条用例 / {gold_count} 条标准答案）",
        header_style="bold",
    )
    table.add_column("模式", style="cyan", no_wrap=True)
    table.add_column(f"Recall@{top_k}", justify="right")
    table.add_column(f"HitRate@{top_k}", justify="right")
    table.add_column("MRR", justify="right")

    for mode in MODES:
        item = metrics[mode]
        table.add_row(
            MODE_LABELS[mode],
            f"{item.recall:.3f}",
            f"{item.hit_rate:.3f}",
            f"{item.mrr:.3f}",
        )

    console.print(table)


def render_by_kind(collected: dict[str, list[CaseResult]], top_k: int) -> None:
    """按问题类型拆分（只看融合模式）。

    平均值容易被个别用例带偏，分桶之后规律才显出来：比如「术语精确」全中、
    「语义改写」全挂，那说明该换 embedding 模型；反过来则是该加强关键词路。
    """
    kinds = sorted({result.case.kind for result in collected["hybrid"]})

    table = Table(title="按问题类型拆分（融合模式）", header_style="bold")
    table.add_column("类型", style="cyan")
    table.add_column("用例数", justify="right")
    table.add_column(f"Recall@{top_k}", justify="right")

    for kind in kinds:
        subset = [r for r in collected["hybrid"] if r.case.kind == kind]
        table.add_row(kind, str(len(subset)), f"{summarise('hybrid', subset).recall:.3f}")

    console.print(table)


def render_details(collected: dict[str, list[CaseResult]]) -> None:
    """逐条结果：命中的显示「√ 条数 @首个命中排名」，未命中显示「×」。"""
    table = Table(title="逐条结果（√ 命中条数 / 标准答案条数 @首次命中排名）", header_style="bold")
    table.add_column("用例", style="cyan", no_wrap=True)
    table.add_column("问题", overflow="fold", max_width=32)
    table.add_column("类型", no_wrap=True)
    for mode in MODES:
        table.add_column(MODE_LABELS[mode], justify="center", no_wrap=True)

    for index in range(len(collected["hybrid"])):
        case = collected["hybrid"][index].case
        row = [case.id, case.query, case.kind]
        for mode in MODES:
            result = collected[mode][index]
            total = len(case.gold)
            mark = "√" if result.hits else "×"
            detail = f"{mark} {result.hits}/{total}"
            if result.first_rank:
                detail += f" @{result.first_rank}"
            row.append(detail)
        table.add_row(*row)

    console.print(table)


def render_exclusive(collected: dict[str, list[CaseResult]]) -> None:
    """哪些用例只有一路能命中——这是「混合检索有没有用」最直接的证据。

    如果某条用例只有关键词路命中，说明它是靠字面匹配救回来的（向量路没戏）；
    反之则说明向量路覆盖了改写表达。两路各有独家命中，混合检索才算真的有用。
    """
    vector_only: list[str] = []
    keyword_only: list[str] = []
    both_miss: list[str] = []

    for vector, keyword in zip(collected["vector"], collected["keyword"]):
        if vector.hits and not keyword.hits:
            vector_only.append(vector.case.id)
        elif keyword.hits and not vector.hits:
            keyword_only.append(keyword.case.id)
        elif not vector.hits and not keyword.hits:
            both_miss.append(vector.case.id)

    console.print()
    console.print(f"[bold]只有向量路命中[/bold]（{len(vector_only)} 条）：{', '.join(vector_only) or '无'}")
    console.print(f"[bold]只有关键词路命中[/bold]（{len(keyword_only)} 条）：{', '.join(keyword_only) or '无'}")
    console.print(f"[bold yellow]两路都没命中[/bold yellow]（{len(both_miss)} 条）：{', '.join(both_miss) or '无'}")


def write_report(
    path: Path,
    metrics: dict[str, Metrics],
    collected: dict[str, list[CaseResult]],
    top_k: int,
) -> None:
    """把结果写成 Markdown，便于提交进 Git 对比不同版本的数字。"""
    lines: list[str] = [
        "# RAG 检索评测报告",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 用例数：{len(collected['hybrid'])}",
        f"- 标准答案数：{metrics['hybrid'].gold}",
        f"- top-k：{top_k}",
        "",
        f"## 三路对比（@top-{top_k}）",
        "",
        "| 模式 | Recall | HitRate | MRR |",
        "| --- | ---: | ---: | ---: |",
    ]

    for mode in MODES:
        item = metrics[mode]
        lines.append(
            f"| {MODE_LABELS[mode]} | {item.recall:.3f} | {item.hit_rate:.3f} | {item.mrr:.3f} |"
        )

    lines += ["", "## 逐条结果", "", "| 用例 | 问题 | 类型 | 纯向量 | 纯关键词 | 融合 |", "| --- | --- | --- | --- | --- | --- |"]

    for index in range(len(collected["hybrid"])):
        case = collected["hybrid"][index].case
        cells = []
        for mode in MODES:
            result = collected[mode][index]
            mark = "√" if result.hits else "×"
            cells.append(f"{mark} {result.hits}/{len(case.gold)}")
        lines.append(f"| {case.id} | {case.query} | {case.kind} | " + " | ".join(cells) + " |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ================================================================== 入口
def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 检索评测：算 Recall / HitRate / MRR")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=EVAL_DIR / "dataset.jsonl",
        help="数据集路径（JSONL），默认 evals/dataset.jsonl",
    )
    parser.add_argument("--top-k", type=int, default=5, help="只看前 k 个片段，默认 5")
    parser.add_argument("--verbose", action="store_true", help="打印逐条结果")
    parser.add_argument("--write-report", action="store_true", help="把结果写成 evals/report.md")
    args = parser.parse_args()

    settings = Settings()
    knowledge_dir = settings.resolve(settings.knowledge_dir)

    cases = load_cases(args.dataset)
    problems = validate(cases, knowledge_dir)
    if problems:
        console.print("[bold red]数据集校验失败[/bold red]——先修好这些锚点，否则指标不可信：")
        for problem in problems:
            console.print(f"  · {problem}")
        raise SystemExit(1)

    if not settings.index_path.is_file():
        console.print(f"[bold red]索引不存在：{settings.index_path}[/bold red]")
        console.print("先建索引：uv run agent index")
        raise SystemExit(1)

    console.print(
        f"数据集：{args.dataset}（{len(cases)} 条）｜知识库：{knowledge_dir}"
        f"｜索引：{settings.index_path}"
    )
    console.print()

    collected = asyncio.run(evaluate(cases, settings, args.top_k))
    metrics = {mode: summarise(mode, collected[mode]) for mode in MODES}

    render_summary(metrics, args.top_k, len(cases), metrics["hybrid"].gold)
    render_by_kind(collected, args.top_k)
    render_exclusive(collected)

    if args.verbose:
        render_details(collected)

    if args.write_report:
        target = EVAL_DIR / "report.md"
        write_report(target, metrics, collected, args.top_k)
        console.print(f"\n报告已写入：{target}")


if __name__ == "__main__":
    main()
