"""Register every built-in tool on a registry."""

from __future__ import annotations

from agent_template.config import Settings
from agent_template.tools.builtin.basic import calculator, get_current_time
from agent_template.tools.builtin.fs import (
    make_list_dir,
    make_read_file,
    make_write_file,
)
from agent_template.tools.registry import ToolRegistry


def register_builtin_tools(registry: ToolRegistry, settings: Settings) -> ToolRegistry:
    """Attach the standard tool set. MCP adds its own later."""
    registry.register(get_current_time, read_only=True)
    registry.register(calculator, read_only=True)
    registry.register(make_read_file(settings), read_only=True)
    registry.register(make_list_dir(settings), read_only=True)
    # 故意不传 read_only：写文件有副作用，注册成"需要人工审批"的工具。
    # 这也是模板里唯一会触发审批的地方——去掉它，审批功能就没有演示入口了。
    registry.register(make_write_file(settings))
    return registry
