"""会话记忆的测试：持久化、会话隔离、裁剪边界。"""

from __future__ import annotations

from pathlib import Path

from agent_template.llm.base import Message, ToolCall
from agent_template.memory.store import MemoryStore


def test_append_and_read_back(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        store.append("s1", Message.user("你好"))
        store.append("s1", Message.assistant("你也好"))

        history = store.history("s1")

        assert [message.role for message in history] == ["user", "assistant"]
        assert history[0].content == "你好"
    finally:
        store.close()


def test_sessions_are_isolated(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        store.append("s1", Message.user("会话一"))
        store.append("s2", Message.user("会话二"))

        assert store.count("s1") == 1
        assert store.history("s2")[0].content == "会话二"
        assert sorted(session for session, _ in store.sessions()) == ["s1", "s2"]
    finally:
        store.close()


def test_tool_calls_survive_round_trip(tmp_path: Path) -> None:
    """工具调用与参数是协议的一部分，序列化后必须能原样读回。"""
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        store.append(
            "s1",
            Message.assistant(
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments={"path": "a.md"})
                ]
            ),
        )

        restored = store.history("s1")[0]

        assert restored.content is None
        assert restored.tool_calls[0].name == "read_file"
        assert restored.tool_calls[0].arguments == {"path": "a.md"}
    finally:
        store.close()


def test_clear_removes_only_target_session(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        store.append("s1", Message.user("一"))
        store.append("s2", Message.user("二"))

        removed = store.clear("s1")

        assert removed == 1
        assert store.count("s1") == 0
        assert store.count("s2") == 1
    finally:
        store.close()


def test_trimming_never_splits_tool_pair(tmp_path: Path) -> None:
    """裁剪必须落在 user 边界：tool 结果不能脱离它的 tool_calls。

    这是最隐蔽的一类 bug——裁剪把配对劈开后，下一次请求会被接口拒绝，
    而报错信息不会提示"是你裁剪造成的"。
    """
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        store.append("s1", Message.user("问题一"))
        store.append(
            "s1",
            Message.assistant(
                tool_calls=[ToolCall(id="c1", name="ping", arguments={})]
            ),
        )
        store.append("s1", Message.tool_result("c1", "结果"))
        store.append("s1", Message.assistant("回答一"))
        store.append("s1", Message.user("问题二"))
        store.append("s1", Message.assistant("回答二"))

        window = store.history("s1", limit=5)

        # 窗口开头被推进到 user 消息；中间那对工具调用被整体丢弃
        assert window[0].role == "user"
        assert window[0].content == "问题二"
        assert [message.role for message in window] == ["user", "assistant"]
    finally:
        store.close()


def test_history_returns_everything_under_limit(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        store.append("s1", Message.user("一"))
        store.append("s1", Message.assistant("二"))

        assert len(store.history("s1", limit=40)) == 2
        assert len(store.history("s1", limit=None)) == 2
    finally:
        store.close()
