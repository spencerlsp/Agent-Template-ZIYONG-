"""把技能清单包装成模型可调用的普通工具"""

from __future__ import annotations

from agent_template.skills.loader import SkillsIndex
from agent_template.tools.registry import ToolRegistry

def register_skill_tools(registry: ToolRegistry, index: SkillsIndex) -> ToolRegistry:
    """像工具登记注册 load_skill 与 list_skills"""

    def load_skill(name: str) -> str:
        """按名称读取技能的完整正文。当任务匹配某个技能时，先调用它读完再动手。"""
        return index.read(name)

    def list_skills() -> str:
        """列出当前可用的技能，以及每个技能的一句话说明。"""
        return index.catalog()   

    registry.register(load_skill, read_only=True)
    registry.register(list_skills, read_only=True)
    return registry
