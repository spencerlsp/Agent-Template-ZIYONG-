"""主循环的测试：用 mock 模型跑完整流程，全程不需要网络。

这些用例守护的是主循环的**行为契约**：事件顺序、工具是否被真正执行、
消息是否按协议落库、步数上限是否兜住。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import time

from agent_template.agent.loop import AgentLoop, group_parallel_calls
from agent_template.agent.runtime import AgentRuntime
from agent_template.config import Settings
from agent_template.llm.base import Message, ToolCall
from agent_template.llm.mock import MockLLM
from agent_template.memory.store import MemoryStore
from agent_template.tools.registry import ToolRegistry
from agent_template.agent.approval import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
    ApprovalResult,
)


def build_loop(
    tmp_path: Path, scripted: list[Message], *, max_steps: int = 4
) -> tuple[AgentLoop, MemoryStore, list[str]]:
    """造一个只带 ping 工具的循环，模型按脚本回答。"""
    calls: list[str] = []
    registry = ToolRegistry()

    @registry.register
    def ping(target: str) -> str:
        """回显目标文本。"""
        calls.append(target)
        return f"pong:{target}"

    memory = MemoryStore(tmp_path / "memory.sqlite3")
    settings = Settings(
        llm_provider="mock", max_steps=max_steps, stream=True, project_root=tmp_path
    )
    loop = AgentLoop(
        llm=MockLLM(scripted=scripted),
        registry=registry,
        memory=memory,
        settings=settings,
    )
    return loop, memory, calls


async def collect(loop: AgentLoop, session: str = "s1") -> list:
    return [event async for event in loop.run(session, "帮我 ping 一下")]


async def test_tool_call_then_final_answer(tmp_path: Path) -> None:
    loop, memory, calls = build_loop(
        tmp_path,
        [
            Message.assistant(
                tool_calls=[ToolCall(id="c1", name="ping", arguments={"target": "x"})]
            ),
            Message.assistant("已经 ping 过了"),
        ],
    )
    try:
        events = await collect(loop)
        kinds = [event.kind for event in events]

        assert kinds[0] == "started"
        assert "tool_call" in kinds and "tool_result" in kinds
        assert kinds[-1] == "finished"
        assert events[-1].text == "已经 ping 过了"
        # 工具真的被执行了，而不是只发了个事件
        assert calls == ["x"]
        # 按协议落库：user → assistant(tool_calls) → tool → assistant
        assert [message.role for message in memory.history("s1")] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
    finally:
        memory.close()


async def test_answer_without_tool_call(tmp_path: Path) -> None:
    loop, memory, calls = build_loop(tmp_path, [Message.assistant("直接回答")])
    try:
        events = await collect(loop)
        kinds = [event.kind for event in events]

        assert kinds[0] == "started"
        assert kinds[-1] == "finished"
        assert "usage" in kinds  # 用量事件要有
        assert calls == []
        assert [message.role for message in memory.history("s1")] == [
            "user",
            "assistant",
        ]
    finally:
        memory.close()


async def test_max_steps_is_enforced(tmp_path: Path) -> None:
    """模型一直调工具不给结论时，必须在上限处停下并报错。"""
    scripted = [
        Message.assistant(
            tool_calls=[
                ToolCall(id=f"c{index}", name="ping", arguments={"target": str(index)})
            ]
        )
        for index in range(3)
    ]
    loop, memory, calls = build_loop(tmp_path, scripted, max_steps=2)
    try:
        events = await collect(loop)

        assert events[-1].kind == "error"
        assert len(calls) == 2  # 只跑了 2 步
    finally:
        memory.close()


@pytest.mark.parametrize("stream", [True, False])
async def test_stream_and_non_stream_agree(tmp_path: Path, stream: bool) -> None:
    """流式与非流式必须给出同样的最终结果，只是事件粒度不同。"""
    loop, memory, _ = build_loop(tmp_path, [Message.assistant("一样的答案")])
    try:
        events = [event async for event in loop.run("s1", "问一句", stream=stream)]
        finished = [event for event in events if event.kind == "finished"]

        assert finished
        assert finished[0].text == "一样的答案"
    finally:
        memory.close()


async def test_runtime_assembles_offline(tmp_path: Path) -> None:
    """不连 MCP、不启用 RAG 时，运行时也该能装配并跑完一轮。"""
    settings = Settings(
        llm_provider="mock",
        project_root=tmp_path,
        mcp_servers=[],
        embedding_provider="local_hash",
        embedding_dim=64,
    )

    runtime = await AgentRuntime.create(
        settings, connect_mcp=False, enable_rag=False
    )
    try:
        # 5 个内置工具（时间、计算、读、列目录、写）+ load_skill / list_skills
        assert len(runtime.registry) == 7

        kinds = [event.kind async for event in runtime.ask("你好")]

        assert "finished" in kinds
    finally:
        await runtime.aclose()

# ------------------------------------------------- 工具分批（并发执行的前半部分）


def build_mixed_registry() -> ToolRegistry:
    """一个只读工具 + 一个有副作用的工具，用来验证分批规则。"""
    registry = ToolRegistry()

    @registry.register(read_only=True)
    def read_thing(path: str) -> str:
        """读点东西。"""
        return path

    @registry.register  # 不声明就是有副作用（保守默认值）
    def write_thing(path: str) -> str:
        """写点东西。"""
        return path

    return registry


def make_call(name: str, index: int) -> ToolCall:
    return ToolCall(id=f"c{index}", name=name, arguments={})


def test_all_read_only_calls_form_one_batch() -> None:
    """三个只读调用应该只占一批，可以一起跑。"""
    registry = build_mixed_registry()
    calls = [make_call("read_thing", i) for i in range(3)]

    batches = group_parallel_calls(calls, registry)

    assert [len(batch) for batch in batches] == [3]


def test_side_effect_calls_never_merge() -> None:
    """两个写操作不能合并：它们的先后是模型指定的。"""
    registry = build_mixed_registry()
    calls = [make_call("write_thing", i) for i in range(2)]

    batches = group_parallel_calls(calls, registry)

    assert [len(batch) for batch in batches] == [1, 1]


def test_reads_are_split_around_a_write() -> None:
    """核心用例：读、读、写、读、读 → 三批 [2, 1, 2]。

    这是"连续段"规则的意义所在：写操作把两侧的只读调用切开，
    保证"写之后再读"能读到新数据。
    """
    registry = build_mixed_registry()
    calls = [
        make_call("read_thing", 0),
        make_call("read_thing", 1),
        make_call("write_thing", 2),
        make_call("read_thing", 3),
        make_call("read_thing", 4),
    ]

    batches = group_parallel_calls(calls, registry)

    assert [len(batch) for batch in batches] == [2, 1, 2]


def test_batching_never_reorders_calls() -> None:
    """分批只是分组，绝不能改变顺序——顺序是模型给的语义。"""
    registry = build_mixed_registry()
    calls = [
        make_call("read_thing", 0),
        make_call("write_thing", 1),
        make_call("read_thing", 2),
    ]

    batches = group_parallel_calls(calls, registry)

    flattened = [call.id for batch in batches for call in batch]
    assert flattened == [call.id for call in calls]


def test_unknown_tool_is_treated_as_unsafe() -> None:
    """查不到的工具有可能是任何东西，只能单独成批。"""
    registry = build_mixed_registry()
    calls = [
        make_call("read_thing", 0),
        make_call("不存在的工具", 1),
        make_call("read_thing", 2),
    ]

    batches = group_parallel_calls(calls, registry)

    assert [len(batch) for batch in batches] == [1, 1, 1]


def test_empty_calls_produce_no_batches() -> None:
    assert group_parallel_calls([], build_mixed_registry()) == []

# --------------------------------------------- 工具并发执行


def build_loop_with(
    tmp_path: Path,
    registry: ToolRegistry,
    scripted: list[Message],
    *,
    max_steps: int = 4,
    approve: ApprovalHandler | None = None,
) -> tuple[AgentLoop, MemoryStore]:
    """用给定的工具表造一个循环，用于并发相关的测试。"""
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    settings = Settings(
        llm_provider="mock", max_steps=max_steps, stream=True, project_root=tmp_path
    )
    loop = AgentLoop(
        llm=MockLLM(scripted=scripted),
        registry=registry,
        memory=memory,
        settings=settings,
        approve=approve,
    )
    return loop, memory


async def test_same_round_read_only_tools_run_in_parallel(tmp_path: Path) -> None:
    """两个各睡 0.4 秒的只读工具，总耗时应接近 0.4s 而不是 0.8s。

    阈值取 0.7s（并行约 0.4s，串行约 0.8s），留足余量避免机器慢时误报。
    """
    registry = ToolRegistry()

    @registry.register(read_only=True)
    def slow(tag: str) -> str:
        """睡一会儿再返回，用来观察并发。"""
        time.sleep(0.4)
        return f"done:{tag}"

    loop, memory = build_loop_with(
        tmp_path,
        registry,
        [
            Message.assistant(
                tool_calls=[
                    ToolCall(id="c0", name="slow", arguments={"tag": "a"}),
                    ToolCall(id="c1", name="slow", arguments={"tag": "b"}),
                ]
            ),
            Message.assistant("都跑完了"),
        ],
    )

    try:
        started = time.perf_counter()
        await collect(loop)
        elapsed = time.perf_counter() - started
    finally:
        memory.close()

    assert elapsed < 0.7, f"两个 0.4 秒的工具串行花了 {elapsed:.2f}s，说明没有并发"


async def test_side_effect_tools_keep_the_model_order(tmp_path: Path) -> None:
    """同一轮里的写操作必须按模型给的顺序执行。

    这是并发化最容易搞坏的地方：并行跑三个调用虽然快，
    但"写 b" 和 "写 c" 的先后就变得不确定了。
    """
    log: list[str] = []
    registry = ToolRegistry()

    @registry.register(read_only=True)
    def read_thing(tag: str) -> str:
        """读点东西。"""
        log.append(f"read:{tag}")
        return tag

    @registry.register
    def write_thing(tag: str) -> str:
        """写点东西（有副作用，不声明 read_only）。"""
        log.append(f"write:{tag}")
        return tag

    loop, memory = build_loop_with(
        tmp_path,
        registry,
        [
            Message.assistant(
                tool_calls=[
                    ToolCall(id="c0", name="read_thing", arguments={"tag": "a"}),
                    ToolCall(id="c1", name="write_thing", arguments={"tag": "b"}),
                    ToolCall(id="c2", name="write_thing", arguments={"tag": "c"}),
                ]
            ),
            Message.assistant("完成"),
        ],
    )

    try:
        await collect(loop)
        history = memory.history("s1")
    finally:
        memory.close()

    # 执行顺序：读在前，两个写按模型给的顺序
    assert log == ["read:a", "write:b", "write:c"]
    # 回填顺序：tool 消息和 tool_calls 一一对应
    tool_messages = [message for message in history if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["c0", "c1", "c2"]


async def test_batch_events_come_before_their_results(tmp_path: Path) -> None:
    """事件呈现顺序：先看到这一批的全部调用，再看到它们的结果。"""
    registry = ToolRegistry()

    @registry.register(read_only=True)
    def quick(tag: str) -> str:
        """立刻返回。"""
        return tag

    loop, memory = build_loop_with(
        tmp_path,
        registry,
        [
            Message.assistant(
                tool_calls=[
                    ToolCall(id="c0", name="quick", arguments={"tag": "a"}),
                    ToolCall(id="c1", name="quick", arguments={"tag": "b"}),
                ]
            ),
            Message.assistant("完成"),
        ],
    )

    try:
        events = await collect(loop)
    finally:
        memory.close()

    kinds = [event.kind for event in events]
    first = kinds.index("tool_call")
    assert kinds[first : first + 4] == [
        "tool_call",
        "tool_call",
        "tool_result",
        "tool_result",
    ]

# --------------------------------------------- 工具审批（Human in the loop）


def build_approval_case(
    tmp_path: Path,
    approve: ApprovalHandler,
    *,
    read_only: bool = False,
) -> tuple[AgentLoop, MemoryStore, list[str]]:
    """造一个"一个工具 + 一个决策源"的循环，返回 (loop, memory, 执行日志)。"""
    executed: list[str] = []
    registry = ToolRegistry()

    @registry.register(read_only=read_only)
    def demo_tool(path: str) -> str:
        """演示工具：记录自己被真正执行过。"""
        executed.append(path)
        return f"已处理 {path}"

    loop, memory = build_loop_with(
        tmp_path,
        registry,
        [
            Message.assistant(
                tool_calls=[
                    ToolCall(id="c0", name="demo_tool", arguments={"path": "a.txt"})
                ]
            ),
            Message.assistant("完成"),
        ],
        approve=approve,
    )
    return loop, memory, executed


async def test_approved_tool_actually_runs(tmp_path: Path) -> None:
    """批准之后工具必须真的执行——审批不能只是走个形式。"""
    asked: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> ApprovalResult:
        asked.append(request)
        return ApprovalResult(ApprovalDecision.APPROVED)

    loop, memory, executed = build_approval_case(tmp_path, approve)

    try:
        await collect(loop)
    finally:
        memory.close()

    assert [item.call.name for item in asked] == ["demo_tool"]  # 确实问了
    assert asked[0].session_id == "s1"                          # 信息完整
    assert executed == ["a.txt"]                                # 确实执行了


async def test_rejected_tool_never_executes(tmp_path: Path) -> None:
    """拒绝之后副作用绝对不能发生。

    只断言"回复给模型的文案"是不够的——必须证明**工具没被执行**，
    否则就是"嘴上拒绝、身体执行"。
    """
    async def reject(request: ApprovalRequest) -> ApprovalResult:
        return ApprovalResult(ApprovalDecision.REJECTED, reason="这份笔记还在用")

    loop, memory, executed = build_approval_case(tmp_path, reject)

    try:
        await collect(loop)
        history = memory.history("s1")
    finally:
        memory.close()

    assert executed == []                       # ← 关键断言：副作用没发生

    tool_text = next(m.content or "" for m in history if m.role == "tool")
    assert "人工拒绝" in tool_text               # 说清性质
    assert "不要重试" in tool_text               # 禁止重试
    assert "不要换别的方式" in tool_text         # 堵住绕道
    assert "这份笔记还在用" in tool_text         # 用户给的理由要带给模型


async def test_read_only_tool_skips_approval(tmp_path: Path) -> None:
    """只读工具不该弹审批——否则读个文件都要确认，没人受得了。"""
    asked: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> ApprovalResult:
        asked.append(request)
        return ApprovalResult(ApprovalDecision.APPROVED)

    loop, memory, executed = build_approval_case(tmp_path, approve, read_only=True)

    try:
        await collect(loop)
    finally:
        memory.close()

    assert asked == []                  # 一次都没问
    assert executed == ["a.txt"]        # 但仍然正常执行了

async def test_runtime_forwards_the_approval_handler(tmp_path: Path) -> None:
    """走 AgentRuntime（而不是直接构造 AgentLoop）也必须能触发审批。

    这条测试来自一次真实的疏漏：`create(approve=...)` 收下了参数，却忘记往
    `cls(...)` 里传，结果是"以为开了审批，其实一次都没问"——而且完全静默，
    没有任何报错。只测 AgentLoop 抓不到它，必须从 create() 整条链路走一遍。
    """
    asked: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> ApprovalResult:
        asked.append(request)
        return ApprovalResult(ApprovalDecision.REJECTED, reason="接线测试")

    settings = Settings(
        llm_provider="mock",
        project_root=tmp_path,
        mcp_servers=[],
        embedding_provider="local_hash",
        embedding_dim=64,
    )
    runtime = await AgentRuntime.create(
        settings, connect_mcp=False, enable_rag=False, approve=approve
    )
    try:
        # 往运行时自己的工具表里塞一个"有副作用"的工具，
        # 再换上一个会主动调用它的假模型
        @runtime.registry.register
        def write_thing(path: str) -> str:
            """写点东西（有副作用）。"""
            return f"写了 {path}"

        runtime.loop.llm = MockLLM(
            scripted=[
                Message.assistant(
                    tool_calls=[
                        ToolCall(
                            id="c0", name="write_thing", arguments={"path": "a.txt"}
                        )
                    ]
                ),
                Message.assistant("完成"),
            ]
        )

        async for _ in runtime.ask("随便问一句", session_id="approval-wiring"):
            pass
    finally:
        await runtime.aclose()

    assert asked, "审批一次都没触发——说明 create(approve=...) 没把参数传到循环里"


async def test_rejection_without_reason_still_blocks_execution(tmp_path: Path) -> None:
    """没有理由的拒绝，也**必须**阻止执行。

    这是安全底线的回归守卫。曾经写成：

        if not result.approved:
            if result.reason:              # ← 条件错了
                return rejected_message(...)
        # 没理由就掉到这里 → 工具照样执行

    而"回车跳过理由"恰恰是最常见的拒绝路径，所以这个 bug 会让审批在最常用的
    场景下完全失效：嘴上拒绝、身体执行。
    """

    async def reject_without_reason(request: ApprovalRequest) -> ApprovalResult:
        return ApprovalResult(ApprovalDecision.REJECTED)   # 不给理由

    loop, memory, executed = build_approval_case(tmp_path, reject_without_reason)

    try:
        await collect(loop)
    finally:
        memory.close()

    assert executed == [], "没给理由的拒绝被执行了——安全底线失守"
