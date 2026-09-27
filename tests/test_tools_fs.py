"""文件工具的测试：沙箱边界、截断、目录列举。"""

from __future__ import annotations

from pathlib import Path

from agent_template.config import Settings
from agent_template.tools.builtin.fs import make_list_dir, make_read_file
from agent_template.tools.registry import ToolRegistry


def build_registry(workspace: Path) -> ToolRegistry:
    """按指定工作区根目录装配两个文件工具。"""
    settings = Settings(workspace_root=workspace)
    registry = ToolRegistry()
    registry.register(make_read_file(settings))
    registry.register(make_list_dir(settings))
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
