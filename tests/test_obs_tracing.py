"""追踪层的测试：公共上下文、落盘格式、token 记账。

这一层守的是"事后能不能算账"：span 写完就没人再看它了，所以字段少一个是
静默的——直到你哪天想聚合"这个会话为什么贵"，才发现数据不全。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_template.agent.loop import AgentLoop
from agent_template.config import Settings
from agent_template.llm.base import Message
from agent_template.llm.mock import MockLLM
from agent_template.memory.store import MemoryStore
from agent_template.obs.tracing import Tracer
from agent_template.tools.registry import ToolRegistry


def test_context_is_merged_into_every_span(tmp_path: Path) -> None:
    """session / run 走 set_context 注入，每个 span 都带上。"""
    tracer = Tracer()
    tracer.set_context(session="s1", run="r1")

    with tracer.span("llm.turn", step=0) as span:
        pass

    assert span.attributes["session"] == "s1"
    assert span.attributes["run"] == "r1"
    assert span.attributes["step"] == 0


def test_call_site_attributes_win_over_context() -> None:
    """同名键以调用点为准，否则上下文会悄悄盖掉本次调用的真实值。"""
    tracer = Tracer(context={"session": "s1", "run": "r1"})

    with tracer.span("tool.call", run="更具体的值") as span:
        pass

    assert span.attributes["run"] == "更具体的值"
    assert span.attributes["session"] == "s1"


def test_span_is_appended_as_a_single_json_line(tmp_path: Path) -> None:
    """每行一个 JSON、时间戳人能读——这是"能直接 grep"的前提。"""
    path = tmp_path / "traces.jsonl"
    tracer = Tracer(path)

    with tracer.span("demo", note="中文也不能乱码"):
        pass

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    row = json.loads(lines[0])
    assert row["name"] == "demo"
    assert row["attributes"]["note"] == "中文也不能乱码"
    # epoch 浮点排序好用，但人看不懂；所以额外留一个 ISO 字符串
    assert row["ts"].startswith("20")
    assert "T" in row["ts"]


async def test_llm_turn_span_records_tokens(tmp_path: Path) -> None:
    """一轮问答结束后，token 必须落在这轮自己的 span 上（并且写进了文件）。"""
    trace_path = tmp_path / "traces.jsonl"
    tracer = Tracer(trace_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    settings = Settings(llm_provider="mock", stream=False, project_root=tmp_path)
    loop = AgentLoop(
        llm=MockLLM(scripted=[Message.assistant("直接回答，不调工具")]),
        registry=ToolRegistry(),
        memory=memory,
        settings=settings,
        tracer=tracer,
    )

    try:
        [event async for event in loop.run("s1", "你好")]
    finally:
        memory.close()

    turns = [span for span in tracer.spans if span.name == "llm.turn"]
    assert len(turns) == 1, "一轮问答该只有一个 llm.turn"

    attributes = turns[0].attributes
    assert attributes["session"] == "s1"
    assert attributes["run"], "run_id 不能是空——否则切不出'这一轮'"
    # MockLLM 固定上报 1 + 1
    assert attributes["prompt_tokens"] == 1
    assert attributes["completion_tokens"] == 1

    # 只有写进文件才算数：内存里那份随进程消失
    written = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert any(row["attributes"].get("prompt_tokens") == 1 for row in written)
