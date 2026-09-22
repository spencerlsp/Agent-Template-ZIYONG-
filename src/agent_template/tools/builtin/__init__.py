"""Register every built-in tool on a registry."""

from __future__ import annotations

from agent_template.config import Settings
from agent_template.tools.builtin.basic import calculator, get_current_time
from agent_template.tools.builtin.fs import make_list_dir, make_read_file
from agent_template.tools.registry import ToolRegistry


def register_builtin_tools(registry: ToolRegistry, settings: Settings) -> ToolRegistry:
    """Attach the standard tool set. RAG and MCP add theirs later."""
    registry.register(get_current_time)
    registry.register(calculator)
    registry.register(make_read_file(settings))
    registry.register(make_list_dir(settings))
    return registry