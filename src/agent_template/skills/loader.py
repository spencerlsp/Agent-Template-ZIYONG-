"""加载 SKILL.md 文件，并逐步把内容交给模型。
Skill（技能）是人工编写的流程文档：描述这个Agent需要稳定执行的任务该怎么做。
只有技能的名称和简介会放进系统提示词；技能正文是按需通过 `load_skill` 工具读取。
这就是这里所说的渐进式披露（progressive disclosure）：技能仓库里可以存放几十个技能，
但不用在每次请求时，把全部技能内容都塞进上下文。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("agent.skills")

FRONTMATTER_FENCE = "---"


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path
    metadata: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        """加载该技能时交给模型的完整文本内容。"""
        return f"# skill: {self.name}\n\n{self.description}\n\n{self.body.strip()}\n"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆分 `---` 包裹的 YAML 头部与正文，返回 (元数据, 正文)。
    头部是可选的。解析失败、没闭合、或者解析出来不是映射时，一律退回成
    "整个文件都是正文"，这样一份写坏格式的技能不会把整个 agent 带崩。
    """
    lines = text.splitlines()
    # 第一行不是 ---，直接没有 frontmatter，全部当作正文
    if not lines or lines[0].strip() != FRONTMATTER_FENCE:
        return {}, text

    # 从第二行开始，寻找结束的 ---
    for index in range(1, len(lines)):
        if lines[index].strip() != FRONTMATTER_FENCE:
            continue
        # 找到了结束分隔线
        raw_header = "\n".join(lines[1:index])
        body = "\n".join(lines[index + 1 :])
        try:
            loaded = yaml.safe_load(raw_header) or {}
        except yaml.YAMLError as exc:
            logger.warning("技能头部 YAML 解析失败，按纯正文处理：%s", exc)
            return {}, text
        # frontmatter 必须是字典（key-value），不能是列表/字符串等
        if not isinstance(loaded, dict):
            logger.warning(
                "技能头部必须是 key: value 映射，实际是 %s", type(loaded).__name__
            )
            return {}, text
        # 把key强制转字符串，返回元数据和正文
        return {str(key): value for key, value in loaded.items()}, body
    # 循环跑完，没有找到闭合的 `---`，视为没有头部
    return {}, text



def load_skill_file(path:  Path) -> Skill | None:
    """读取单个Skill.md; 文件读不到时返回None, 不抛异常"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("技能文件读取失败 %s：%s", path, exc)
        return None

    metadata, body = parse_frontmatter(text)
    name = str(metadata.get("name") or path.parent.name)
    description = str(metadata.get("description") or "").strip() or first_line(body)
    if not description:
        logger.warning("技能 %s 没有描述，模型将无法判断何时该用它", path)

    return Skill(
        name=name,
        description=description,
        body=body,
        path=path,
        metadata=metadata,
    )



def first_line(body: str) -> str:
    """兜底描述：正文里第一行非空、 非标题的文字"""
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


class SkillsIndex:
    """磁盘上所有的技能集合，按名字叫寻址"""

    def __init__(self, skills: list[Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = {skill.name: skill for skill in skills or []}

    @classmethod
    def from_dir(cls, directory: Path) -> "SkillsIndex":
        """递归查找`<directory>/**?SKILL.md`, 技能名取头部里的name， 缺失时用所在目录名"""

        if not directory.is_dir():
            logger.warning("技能目录不存在： %s", directory)
            return cls([])

        skills = [
            skill
            for skill in (load_skill_file(path) for path in sorted(directory.rglob("SKILL.md")))
            if skill is not None
        ]

        if not skills:
            logger.warning("在 %s 下没有找到任何SKILL.md", directory)
        return cls(skills)


    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def catalog(self) -> str:
        """给系统提示用的精简清单： 名字 + 一句话描述。"""
        if not self._skills:
            return "(当前没有安装任何技能)"

        return "\n".join(
            f"- {name}: {self._skills[name].description}" for name in self.names()
        )

    def read(self, name: str) -> str:
        """提取技能正文： 不存在时返回错误文本，交给模型自己决定"""
        skill = self._skills.get(name)
        if skill is None:
            available = "、".join(self.names()) or "无"
            return f"Error: 没有名为 `{name}` 的技能。 可用技能: {available}"

        return skill.render()


    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        return name in self._skills