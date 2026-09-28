"""从 traces.jsonl 里聚合 token 用量：按会话、按轮次。

为什么是"扫文件"而不是查数据库：
    用量是只追加、只聚合的遥测数据——不需要 join、不需要更新、不需要事务。
    它和会话存储是两件事：将来记忆换成知识库或别的数据库，这份数据一行都不用改。
    `obs` 只要求一个 `session_id` 字符串当 join key。

它回答什么问题：
    **归因**——哪个会话贵、哪一轮贵、贵在输入还是输出。
    不回答"总共花了多少钱"：那道题的答案在供应商后台（各家单价不同、还一直在变，
    账单还会算上缓存价和折扣）。本地这几个 token 数字的意义在于对齐你自己的行为，
    比如"历史没裁剪导致输入越滚越大"。

用法：
    uv run python scripts/usage.py                 # 全部会话
    uv run python scripts/usage.py --session default
    uv run python scripts/usage.py --runs 20       # 最近 20 轮
"""

from __future__ import annotations

# ---------------------------------------------------------------- 标准库
# argparse：几个选项不值得引入 typer（CLI 才用它）。和 evals/ 里的脚本保持一致。
import argparse
# defaultdict：聚合时的累加器，省得写 if key not in ...
from collections import defaultdict
import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 第三方
# rich 已经在依赖里（CLI 用它渲染），这里直接复用，省得手拼对齐。
from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------- 本项目
from agent_template.config import Settings

console = Console()

# trace 里模型调用的 span 名字。改这个名字会让统计静默归零，
# 所以它和 loop.py 里的字面量是绑在一起的。
LLM_SPAN = "llm.turn"


def load_turns(path: Path) -> tuple[list[dict[str, Any]], int]:
    """读出所有模型调用，返回 (带用量的那些, 缺用量的条数)。

    缺用量是正常情况：有些兼容端点在流式下不返回 usage。这里单独数出来，
    而不是当成 0——0 会被当成"这次不花钱"，缺失则应该被发现。
    """
    if not path.is_file():
        raise SystemExit(f"没有找到 trace 文件：{path}\n先跑一次对话再来统计。")

    turns: list[dict[str, Any]] = []
    missing = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("name") != LLM_SPAN:
            continue
        attributes = row.get("attributes") or {}
        if "prompt_tokens" not in attributes:
            missing += 1
            continue
        turns.append(
            {
                "ts": row.get("ts", ""),
                "session": attributes.get("session", "(无会话)"),
                "run": attributes.get("run", "?"),
                "step": attributes.get("step", "?"),
                "prompt": attributes.get("prompt_tokens", 0),
                "completion": attributes.get("completion_tokens", 0),
                "duration_ms": row.get("duration_ms", 0.0),
                "status": row.get("status", "ok"),
            }
        )
    return turns, missing


def render_sessions(turns: list[dict[str, Any]]) -> None:
    """按会话汇总：谁最贵。"""
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "prompt": 0, "completion": 0, "runs": set()}
    )
    for turn in turns:
        item = stats[turn["session"]]
        item["calls"] += 1
        item["prompt"] += turn["prompt"]
        item["completion"] += turn["completion"]
        item["runs"].add(turn["run"])

    table = Table(title="按会话汇总（token 多的在前）", header_style="bold")
    table.add_column("会话", style="cyan")
    table.add_column("轮次", justify="right")
    table.add_column("调用", justify="right")
    table.add_column("输入", justify="right")
    table.add_column("输出", justify="right")
    table.add_column("合计", justify="right")

    for session, item in sorted(
        stats.items(), key=lambda kv: -(kv[1]["prompt"] + kv[1]["completion"])
    ):
        total = item["prompt"] + item["completion"]
        table.add_row(
            session,
            str(len(item["runs"])),
            str(item["calls"]),
            f"{item['prompt']:,}",
            f"{item['completion']:,}",
            f"[bold]{total:,}[/bold]",
        )
    console.print(table)


def render_runs(turns: list[dict[str, Any]], limit: int) -> None:
    """按轮次汇总：哪一轮贵。

    这是"归因"真正的入口——一轮里模型被调用几次、每次带多少历史，一眼能看出来。
    输入 token 逐轮变大通常意味着历史没裁剪，而不是任务变难了。
    """
    stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"ts": "", "calls": 0, "prompt": 0, "completion": 0, "slowest": 0.0}
    )
    for turn in turns:
        item = stats[(turn["session"], turn["run"])]
        item["ts"] = max(item["ts"], turn["ts"])
        item["calls"] += 1
        item["prompt"] += turn["prompt"]
        item["completion"] += turn["completion"]
        item["slowest"] = max(item["slowest"], turn["duration_ms"])

    table = Table(title=f"最近 {limit} 轮（新的在前）", header_style="bold")
    table.add_column("时间", style="dim")
    table.add_column("会话", style="cyan")
    table.add_column("调用", justify="right")
    table.add_column("输入", justify="right")
    table.add_column("输出", justify="right")
    table.add_column("合计", justify="right")
    table.add_column("最慢一次", justify="right")

    ordered = sorted(stats.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:limit]
    for (session, run), item in ordered:
        total = item["prompt"] + item["completion"]
        table.add_row(
            item["ts"][:19],
            session,
            str(item["calls"]),
            f"{item['prompt']:,}",
            f"{item['completion']:,}",
            f"[bold]{total:,}[/bold]",
            f"{item['slowest']:.0f} ms",
        )
    console.print(table)


def main() -> int:
    parser = argparse.ArgumentParser(description="从 traces.jsonl 聚合 token 用量")
    parser.add_argument(
        "--trace",
        type=Path,
        default=None,
        help="trace 文件路径（默认取配置里的 .agent/traces.jsonl）",
    )
    parser.add_argument("--session", default=None, help="只看某个会话")
    parser.add_argument("--runs", type=int, default=10, help="列出最近多少轮（默认 10）")
    args = parser.parse_args()

    path = args.trace or Settings().trace_path
    turns, missing = load_turns(path)
    if args.session:
        turns = [turn for turn in turns if turn["session"] == args.session]

    console.print(f"trace：{path}")
    console.print(f"模型调用：{len(turns)} 次" + (f"，其中 {missing} 次没有用量字段" if missing else ""))
    if not turns:
        return 0

    console.print()
    render_sessions(turns)
    console.print()
    render_runs(turns, args.runs)
    console.print()
    console.print(
        "[dim]说明：这里统计的是[bold]归因[/bold]（哪个会话、哪一轮贵），"
        "不是账单——各家单价不同且一直在变，账单以供应商后台为准。[/dim]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
