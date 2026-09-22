"""Filesystem tools, sandboxed to the configured workspace root.
    文件系统工具，沙箱限制在配置的工作目录根路径内。
"""

from __future__ import annotations

from pathlib import Path

from agent_template.config import Settings

def _inside(root: Path, path: str) -> Path:
    """Resolve `path` under `root` , refusing anything that escapes it.
    在根目录`root`下解析路径；拒绝任何逃逸到根目录外的路径。
    """
    # 拼接路径，并解析为真实绝对路径（自动展开../、./）
    candidate = (root / path).resolve()
    # 判断解析后的路径是否在工作目录内，不在则抛出异常
    if not candidate.is_relative_to(root):
        raise ValueError(f"`{path}` is outside the workspace root")
    return candidate


def make_read_file(settings: Settings):
    # 解析并固定沙箱工作区根目录，被内层闭包捕获
    root = settings.resolve(settings.workspace_root)
    def read_file(path: str, max_chars: int = 20000) -> str:
        """读取工作区内UTF-8文本文件，返回文件内容"""
        # 沙箱路径校验，禁止逃逸
        target = _inside(root, path)
        if not target.is_file():
            return f"Error: `{path}` is not a file"
        # 读取文本，无法识别的字符自动替换
        text = target.read_text(encoding="utf-8", errors="replace")
        # 超长截断，避免上下文溢出
        if len(text) > max_chars:
            return text[:max_chars] + f"\n...[truncated, {len(text)} chars total]"
        return text
    return read_file


def make_list_dir(settings: Settings):
    # 固定沙箱根目录
    root = settings.resolve(settings.workspace_root)
    def list_dir(path: str = ".", limit: int = 100) -> str:
        """列出工作区指定路径下的文件和目录"""
        target = _inside(root, path)
        if not target.is_dir():
            return f"Error: `{path}` is not a directory"
        # 排序，最多返回limit条，防止目录内容过多
        entries = sorted(target.iterdir())[:limit]
        # 文件夹名字末尾加 / 区分
        lines = [f"{item.name}/" if item.is_dir() else item.name for item in entries]
        return "\n".join(lines) or "{empty directory}"
    return list_dir