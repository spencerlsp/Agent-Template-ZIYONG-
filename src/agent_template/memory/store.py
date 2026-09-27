"""会话记忆：把对话历史持久化到 SQLite。

为什么需要它：
    模型自己没有记忆，每次请求都得把历史重新发过去。如果历史只存在进程内存里，
    进程一退就没了，用户下次得从头再说一遍。这里用一张表按 session 保存。

为什么存我们自己的 Message 结构：
    它是 llm/base.py 里定义的供应商无关类型。存 JSON 而不是供应商原始格式，
    意味着换模型供应商时历史不用迁移。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from agent_template.llm.base import Message

logger = logging.getLogger("agent.memory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    payload    TEXT NOT NULL,   -- Message 的 JSON 序列化
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


class MemoryStore:
    """按会话存取的对话历史。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def append(self, session_id: str, message: Message) -> None:
        """追加一条消息，并更新会话的活跃时间。"""
        now = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO messages (session_id, role, payload, created_at)"
            " VALUES (?, ?, ?, ?)",
            (session_id, message.role, message.model_dump_json(), now),
        )
        self._conn.execute(
            "INSERT INTO sessions (session_id, updated_at) VALUES (?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET updated_at = excluded.updated_at",
            (session_id, now),
        )
        self._conn.commit()

    def history(self, session_id: str, limit: int | None = 40) -> list[Message]:
        """读回会话历史；limit=None 表示全部读回。

        裁剪时必须落在"安全边界"上，这是最容易踩的坑：
        assistant 的 tool_calls 消息和紧随其后的 tool 结果消息是一对，
        如果按条数硬截，很可能把配对劈开——下一次请求会直接被接口拒绝（400），
        而且报错信息通常指向"消息顺序不合法"，很难联想到是裁剪造成的。
        所以截取之后要把窗口开头推进到一条 user 消息。
        """
        rows = self._conn.execute(
            "SELECT payload FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        messages = [Message.model_validate_json(row[0]) for row in rows]

        if limit is None or len(messages) <= limit:
            return messages

        window = messages[-limit:]
        for index, message in enumerate(window):
            if message.role == "user":
                return window[index:]
        # 窗口里一条 user 都没有（极端情况）：原样返回，总比劈开工具配对好
        return window

    def clear(self, session_id: str) -> int:
        """清空某个会话，返回删除的消息条数。"""
        cursor = self._conn.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
        self._conn.commit()
        return cursor.rowcount

    def sessions(self) -> list[tuple[str, str]]:
        """列出所有会话 (session_id, 最后活跃时间)，最近的在前。"""
        rows = self._conn.execute(
            "SELECT session_id, updated_at FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def count(self, session_id: str) -> int:
        """某个会话的消息条数。"""
        return self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]

    def close(self) -> None:
        self._conn.close()