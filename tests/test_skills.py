"""技能层的测试：YAML 头部解析、目录/正文分离、降级处理。"""

from __future__ import annotations

from pathlib import Path

from agent_template.skills.loader import SkillsIndex, parse_frontmatter


def test_parses_folded_description() -> None:
    """多行描述用 YAML 折叠块写——这是换掉手写解析器的直接原因。"""
    text = (
        "---\n"
        "name: demo\n"
        "description: >-\n"
        "  第一行描述\n"
        "  第二行描述\n"
        "---\n"
        "\n"
        "# 正文标题\n"
        "\n"
        "正文内容。\n"
    )

    metadata, body = parse_frontmatter(text)

    assert metadata["name"] == "demo"
    assert metadata["description"] == "第一行描述 第二行描述"
    assert body.strip().startswith("# 正文标题")
    assert "name: demo" not in body


def test_file_without_frontmatter_is_all_body() -> None:
    metadata, body = parse_frontmatter("# 只有正文\n\n内容")

    assert metadata == {}
    assert body.startswith("# 只有正文")


def test_broken_yaml_degrades_to_plain_body() -> None:
    """格式写坏不该让整个 agent 起不来。"""
    text = "---\nname: [未闭合的列表\n---\n正文"

    metadata, body = parse_frontmatter(text)

    assert metadata == {}
    assert body == text


def write_skill(root: Path, name: str, content: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_index_builds_catalog_and_reads_body(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "demo",
        "---\nname: demo\ndescription: 一句话描述\n---\n\n正文内容",
    )

    index = SkillsIndex.from_dir(tmp_path)

    assert index.names() == ["demo"]
    assert "一句话描述" in index.catalog()
    # catalog 里不该出现正文——这正是渐进式披露的意义
    assert "正文内容" not in index.catalog()
    assert "正文内容" in index.read("demo")


def test_missing_description_falls_back_to_first_line(tmp_path: Path) -> None:
    write_skill(tmp_path, "demo", "---\nname: demo\n---\n\n# 兜底描述\n\n正文")

    skill = SkillsIndex.from_dir(tmp_path).get("demo")

    assert skill is not None
    assert skill.description == "兜底描述"


def test_name_falls_back_to_directory_name(tmp_path: Path) -> None:
    write_skill(tmp_path, "from-dir", "---\ndescription: 只有描述\n---\n\n正文")

    assert SkillsIndex.from_dir(tmp_path).names() == ["from-dir"]


def test_unknown_skill_returns_error_text(tmp_path: Path) -> None:
    write_skill(tmp_path, "demo", "---\nname: demo\ndescription: 描述\n---\n\n正文")
    index = SkillsIndex.from_dir(tmp_path)

    result = index.read("missing")

    assert result.startswith(("Error", "错误"))
    assert "demo" in result  # 提示里列出可用技能，模型可自我纠正


def test_missing_directory_is_empty(tmp_path: Path) -> None:
    index = SkillsIndex.from_dir(tmp_path / "not-there")

    assert len(index) == 0
