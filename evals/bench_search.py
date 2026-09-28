"""检索延迟基准：把"开不开重排"的代价量出来。

为什么和 run_eval 分成两个脚本：
    run_eval 回答"准不准"（Recall / MRR），本脚本回答"快不快"。两者的采样
    方式是相反的——评测要一次一条、结果可复现；延迟要重复多次取分位数，
    因为网络抖动远大于真实差异。塞进一个脚本只会两边都将就。

为什么必须看分位数，不能只看平均值：
    一次卡住的请求能把均值拉高几十毫秒，而 p50（中位数）才是"多数情况下
    用户等的就是这么久"。所以 mean / p50 / p90 / p95 / max 一起打出来，
    拿结论时优先看 p50。

为什么要数"重排失败回退"的次数：
    重排失败时检索层只 warning 然后回退到融合顺序（见 retriever.search）。
    如果重排一直在挂，你量到的其实是"不重排"的速度，而不是重排的代价——
    数字会好看得离谱还看不出异常。所以这里把回退次数显式打出来，
    大于 0 就说明这一轮的数字不能用来对比。

用法：
    # 基线：不重排（覆盖 .env 里的设置）
    AGENT_RERANK_PROVIDER=none uv run python evals/bench_search.py

    # 开重排，模型和 key 读 .env
    AGENT_RERANK_PROVIDER=openai_compat uv run python evals/bench_search.py

    # 只跑前 10 条、每条重复 3 次，快速对比
    uv run python evals/bench_search.py --limit 10 --repeat 3

两条命令各跑一次，就得到"MRR +0.14 换来 p50 +XXX ms"这句结论。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 标准库
# argparse：命令行参数。和 run_eval 一样，几个选项不值得引入 typer。
import argparse
import asyncio
# json：读 JSONL 问题集（每行一个 JSON 对象）。
import json
# logging：挂一个 handler 拦截"重排失败回退"的 warning。
import logging
# time.perf_counter：单调时钟，不受系统时间调整影响，用来计时最合适。
import time
from pathlib import Path

# ---------------------------------------------------------------- 第三方
# numpy：算分位数。项目里已经在用它，不必为此再引入别的库。
import numpy as np
from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------- 本项目
from agent_template.config import Settings
from agent_template.rag.pipeline import RagNotReady, RagPipeline

console = Console()

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = EVAL_DIR / "dataset.jsonl"

# 检索层的 logger 名字（见 retriever.py / pipeline.py）。回退的 warning 从这里出。
RAG_LOGGER = "agent.rag"


class FallbackCounter(logging.Handler):
    """数"重排失败回退"发生了多少次。

    继承 logging.Handler 而不是自己 try/except：检索层已经做了降级，
    这里只是把它留下的痕迹收集起来——脚本不该为了统计去改被测代码。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def load_queries(path: Path, limit: int | None = None) -> list[str]:
    """只取问题文本。

    和 run_eval.load_cases 读同一份 JSONL，跳过空行和 # 注释的约定也一致；
    但基准不关心标准答案——它不评准不准，只量花多久。
    """
    queries: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        queries.append(json.loads(stripped)["query"])
    return queries[:limit] if limit is not None else queries


def summarise(samples: list[float]) -> dict[str, float]:
    """把一组耗时（秒）压成分位数，单位换回毫秒，免得读的人自己换算。"""
    arr = np.array(samples, dtype=np.float64) * 1000.0
    return {
        "n": float(arr.size),
        "mean": float(arr.mean()),
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
        "p95": float(np.percentile(arr, 95)),
        "max": float(arr.max()),
    }


def describe(settings: Settings) -> str:
    """给这一轮配置起个能写进 README 的名字。"""
    if settings.rerank_provider == "none":
        return "none"
    return f"{settings.rerank_provider}:{settings.rerank_model}"


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    queries = load_queries(args.dataset, args.limit)
    if not queries:
        console.print("[red]问题集是空的[/red]")
        return 1

    pipeline = RagPipeline(settings)
    counter = FallbackCounter()
    logging.getLogger(RAG_LOGGER).addHandler(counter)

    try:
        # 提前自检：索引没建、或者换了 embedding 没重建时，在这里就报错，
        # 而不是给你一份"检索全错所以特别快"的数字
        pipeline.ensure_ready()

        top_k = args.top_k or settings.rag_top_k
        console.print(f"索引：{settings.index_path}")
        console.print(f"问题：{len(queries)} 条 ｜ 每条重复 {args.repeat} 次 ｜ top_k={top_k}")
        console.print(
            f"embedding：{settings.embedding_provider} ｜ "
            f"重排：{describe(settings)}"
            + (
                f"（候选 {settings.rerank_candidates} 条）"
                if settings.rerank_provider != "none"
                else ""
            )
        )

        # 热身：第一次 search 要懒加载全库、建 BM25 索引、初始化 HTTP 连接池，
        # 这几百毫秒是启动成本而不是检索成本，混进统计会把基线抬高一截
        warmup = queries[: max(args.warmup, 0)]
        for query in warmup:
            await pipeline.search(query, top_k=args.top_k)
        if warmup:
            console.print(f"已热身 {len(warmup)} 条（不计入统计）")

        samples: list[float] = []
        for index, query in enumerate(queries, start=1):
            for _ in range(args.repeat):
                started = time.perf_counter()
                await pipeline.search(query, top_k=args.top_k)
                samples.append(time.perf_counter() - started)
            if args.progress_every and index % args.progress_every == 0:
                console.print(f"  已测 {index}/{len(queries)} 条")
    finally:
        await pipeline.aclose()
        logging.getLogger(RAG_LOGGER).removeHandler(counter)

    stats = summarise(samples)
    label = describe(settings)

    table = Table(title="检索延迟", header_style="bold")
    table.add_column("配置", overflow="fold", max_width=34)
    for column in ("样本", "mean", "p50", "p90", "p95", "max"):
        table.add_column(column, justify="right")
    table.add_row(
        label,
        str(int(stats["n"])),
        *[f"{stats[key]:.0f} ms" for key in ("mean", "p50", "p90", "p95", "max")],
    )
    console.print(table)

    console.print(f"重排失败回退：{len(counter.messages)} 次")
    if counter.messages:
        console.print(
            "[yellow]注意：出现回退说明这一轮量的是降级之后的速度，"
            "不能用来和另一组对比[/yellow]"
        )
        console.print(f"  首条原因：{counter.messages[0]}")

    console.print()
    # 这行是留给 README 的：直接贴进去就是一句带数字的权衡说明
    console.print(
        f"[bold][bench][/bold] rerank={label} top_k={top_k} repeat={args.repeat} "
        f"n={len(queries)} | p50={stats['p50']:.0f}ms p95={stats['p95']:.0f}ms "
        f"mean={stats['mean']:.0f}ms"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检索延迟基准：开/不开重排各跑一次，对比 p50"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help="问题集 JSONL（默认 evals/dataset.jsonl，只用到 query 字段）",
    )
    parser.add_argument("--limit", type=int, default=None, help="只用前 N 条问题，快速试跑")
    parser.add_argument(
        "--repeat",
        type=int,
        default=2,
        help="每个问题计时几次（默认 2）。重复是连着的，所以量的是热态延迟",
    )
    parser.add_argument(
        "--warmup", type=int, default=3, help="前 N 条只热身不计时（默认 3）"
    )
    parser.add_argument("--top-k", type=int, default=None, help="覆盖 settings.rag_top_k")
    parser.add_argument(
        "--progress-every", type=int, default=20, help="每测 N 条打印一次进度（0 = 不打印）"
    )
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
