"""CLI 层的报错文案测试。

只钉一件事：**把全局选项写到子命令后面**时，报错里必须带上"该怎么办"。
这是最常见的误用（多数命令行工具允许选项随意摆放），而文档写了不代表用户会看——
出错的那一刻才是提示最有价值的地方。
"""

from __future__ import annotations

from typer.testing import CliRunner

from agent_template.cli import app


def test_global_option_after_subcommand_gets_a_hint() -> None:
    """`agent tools -v` 要报错，而且要告诉用户选项该挪到前面。"""
    result = CliRunner().invoke(app, ["tools", "-v"])

    assert result.exit_code == 2
    assert "No such option: -v" in result.output
    assert "要写在子命令之前" in result.output


def test_global_option_typo_does_not_get_the_placement_hint() -> None:
    """选项拼错（`agent --bogus`）不该提示"挪到子命令前面"——它本来就在正确的位置上。

    这条是为了守住提示的准确性：宁可不提示，也不要给一个误导性的提示。
    """
    result = CliRunner().invoke(app, ["--bogus"])

    assert result.exit_code == 2
    assert "No such option: --bogus" in result.output
    assert "要写在子命令之前" not in result.output
