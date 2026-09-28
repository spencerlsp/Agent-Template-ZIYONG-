"""文件工具的测试：沙箱边界、截断、目录列举、写入与审批标记。"""

from __future__ import annotations

from pathlib import Path

from agent_template.config import Settings
from agent_template.tools.builtin.fs import (
    make_list_dir,
    make_read_file,
    make_write_file,
)
from agent_template.tools.registry import ToolRegistry


def build_registry(workspace: Path) -> ToolRegistry:
    """按指定工作区根目录装配三个文件工具。

    注册方式刻意和 `register_builtin_tools` 保持一致：读文件和列目录是只读的，
    写文件**不声明** `read_only`（因此需要人工审批）。
    如果这里偷懒全用默认值，下面那条审批标记的断言测的就不是生产行为了。
    """
    settings = Settings(workspace_root=workspace)
    registry = ToolRegistry()
    registry.register(make_read_file(settings), read_only=True)
    registry.register(make_list_dir(settings), read_only=True)
    registry.register(make_write_file(settings))
    return registry


async def test_reads_file_inside_workspace(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("里面的内容", encoding="utf-8")
    registry = build_registry(tmp_path)

    result = await registry.call("read_file", {"path": "note.md"})

    assert "里面的内容" in result


async def test_refuses_to_escape_workspace(tmp_path: Path) -> None:
    """核心安全边界：模型不该能靠 ../ 读到工作区外的文件。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("机密内容", encoding="utf-8")
    registry = build_registry(workspace)

    result = await registry.call("read_file", {"path": "../secret.txt"})

    assert "机密内容" not in result


async def test_missing_file_returns_error_text(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)

    result = await registry.call("read_file", {"path": "nope.md"})

    assert result.startswith(("Error", "错误"))


async def test_long_file_is_truncated(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("头" + "字" * 500, encoding="utf-8")
    registry = build_registry(tmp_path)

    result = await registry.call("read_file", {"path": "big.txt", "max_chars": 10})

    assert result.startswith("头")
    assert len(result) < 100  # 远小于原文 501 字


async def test_list_dir_shows_entries(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    registry = build_registry(tmp_path)

    result = await registry.call("list_dir", {})

    assert "a.md" in result
    assert "sub" in result


async def test_list_dir_rejects_outside_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside").mkdir()
    registry = build_registry(workspace)

    result = await registry.call("list_dir", {"path": "../outside"})

    assert result.startswith(("Error", "错误"))


async def test_write_file_creates_the_file(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)

    result = await registry.call("write_file", {"path": "note.md", "content": "新内容"})

    assert (tmp_path / "note.md").read_text(encoding="utf-8") == "新内容"
    assert "note.md" in result


async def test_write_file_creates_parent_directories(tmp_path: Path) -> None:
    """写 logs/today.md 时父目录还不存在，应当自动建出来。"""
    registry = build_registry(tmp_path)

    await registry.call("write_file", {"path": "logs/today.md", "content": "x"})

    assert (tmp_path / "logs" / "today.md").is_file()


async def test_write_file_refuses_to_escape_workspace(tmp_path: Path) -> None:
    """核心安全边界：写操作同样不能落到工作区之外。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = build_registry(workspace)

    result = await registry.call(
        "write_file", {"path": "../escaped.txt", "content": "不该落盘"}
    )

    assert not (tmp_path / "escaped.txt").exists()   # ← 关键：文件根本没被创建
    assert result.startswith(("Error", "错误"))       # 而且回了错误文本


def test_write_file_is_registered_as_needing_approval(tmp_path: Path) -> None:
    """写文件必须被登记成"有副作用"，否则审批机制不会拦它。

    这条守的是注册时那个"故意不传 read_only"的决定——哪天有人顺手给
    write_file 加上 `read_only=True`，审批会**静默失效**，而不会有任何报错。
    """
    registry = build_registry(tmp_path)

    assert registry.is_parallel_safe("write_file") is False
    assert registry.is_parallel_safe("read_file") is True
