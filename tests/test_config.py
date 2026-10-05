"""配置层的测试：优先级、路径解析、MCP 的 JSON 解析。"""

from __future__ import annotations

from pathlib import Path

from agent_template.config import Settings


def test_explicit_argument_beats_environment(monkeypatch) -> None:
    """命令行/构造参数优先级最高——CLI 的 --model 靠的就是这条。"""
    monkeypatch.setenv("AGENT_LLM_MODEL", "from-env")

    assert Settings(llm_model="from-arg").llm_model == "from-arg"


def test_environment_beats_dotenv(monkeypatch) -> None:
    """环境变量优先于 .env 文件。"""
    monkeypatch.setenv("AGENT_LLM_MODEL", "env-model")

    assert Settings().llm_model == "env-model"


def test_relative_paths_resolve_against_project_root(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path)

    assert settings.resolve(Path("skills")) == tmp_path / "skills"
    assert settings.memory_path == tmp_path / ".agent" / "memory.sqlite3"
    assert settings.trace_path == tmp_path / ".agent" / "traces.jsonl"


def test_absolute_paths_are_kept_as_is(tmp_path: Path) -> None:
    """绝对路径不该被再拼一次前缀。"""
    settings = Settings(project_root=tmp_path)

    assert settings.resolve(tmp_path / "elsewhere") == tmp_path / "elsewhere"


def test_custom_system_prompt_wins() -> None:
    settings = Settings(system_prompt="你是一个测试助手")

    assert settings.effective_system_prompt == "你是一个测试助手"


def test_default_prompt_contains_agent_name() -> None:
    settings = Settings(agent_name="测试机器人", system_prompt="")

    assert "测试机器人" in settings.effective_system_prompt


def test_mcp_servers_parse_from_json_env(monkeypatch) -> None:
    """AGENT_MCP_SERVERS 是个 JSON 数组，这是最容易配错的一项。"""
    monkeypatch.setenv(
        "AGENT_MCP_SERVERS",
        '[{"name":"demo","command":"python","args":["-m","some.server"]}]',
    )

    servers = Settings().mcp_servers

    assert len(servers) == 1
    assert servers[0].name == "demo"
    assert servers[0].args == ["-m", "some.server"]
