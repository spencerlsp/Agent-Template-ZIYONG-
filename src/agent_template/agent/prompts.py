"""系统提示的组装。

提示由三块拼成：

    1. 基础人设：config 里的默认提示，或在 .env 里用 AGENT_SYSTEM_PROMPT 覆盖
    2. 技能目录：只有名字和一句话描述——这就是渐进式披露的那一半，
       正文等模型调用 load_skill 时再取
    3. 工具清单：人可读的工具列表

为什么工具清单要写进提示：
    模型能从请求的 tools 参数里看到工具定义，但在提示里再明确一次
    "你有这些工具、遇到事实性问题要先用工具"，能明显提高调用意愿。
    这是实践里反复验证过的：光给 schema，模型常常凭记忆硬答。
"""

from __future__ import annotations

from agent_template.config import Settings
from agent_template.skills.loader import SkillsIndex
from agent_template.tools.registry import ToolRegistry


def build_system_prompt(
        settings: Settings,
        registry: ToolRegistry,
        skills: SkillsIndex | None = None,
) -> str:
    """拼出完整的系统提示"""
    parts = [settings.effective_system_prompt.strip()]

    if skills is not None and len(skills):
        parts.append(f"可用技能（需要时用 load_skill 读取全文）：\n{skills.catalog()}")

    if len(registry):
        parts.append(f"可用工具：\n{registry.describe()}")

    return "\n\n".join(part for part in parts if part)