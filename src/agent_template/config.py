"""The single source of truth for configuration.

Values are read from, in order of precedence:

1. real environment variables, prefixed with ``AGENT_``
2. a ``.env`` file in the project root
3. the defaults declared below

Scalars map straight onto fields; ``AGENT_MCP_SERVERS`` is a JSON array:

    AGENT_MCP_SERVERS=[{"name":"example","command":"python","args":["-m","x"]}]

Nothing else in the codebase reads environment variables directly - import
``Settings`` and pass it down instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# <repo>/src/agent_template/config.py -> <repo>
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MCPServerSettings(BaseModel):
    """一个以 stdio 方式启动的 MCP 服务器。"""

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    startup_timeout_s: float = 20.0
    call_timeout_s: float = 60.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore"
    )

    # --- LLM ---------------------------------------------------------------
    llm_provider: Literal["openai_compat", "mock"] = "openai_compat"
    llm_model: str = "deepseek-v4-flash"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: SecretStr | None = None
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1024
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2

    # --- Agent loop --------------------------------------------------------
    agent_name: str = "local-agent"
    max_steps: int = 8
    stream: bool = True
    system_prompt: str = "" 


    # --- Paths (relative values resolve against the project root) ----------
    project_root: Path = PROJECT_ROOT
    skills_dir: Path = Path("skills")
    state_dir: Path = Path(".agent")
    workspace_root: Path = Path(".")

    # --- MCP ---------------------------------------------------------------
    mcp_servers: list[MCPServerSettings] = Field(
        default_factory=lambda: [
            MCPServerSettings(
                name="example",
                command="python",
                args=["-m", "agent_template.mcp.example_server"],
            )
        ]
    )

    # ------------------------------------------------------------------ paths
    def resolve(self, path: Path) -> Path:
        """absolute path for configured location"""
        return path if path.is_absolute() else self.project_root / path

    @property
    def memory_path(self) -> Path:
        """SQLite file holding conversation history."""
        return self.resolve(self.state_dir) / "memory.sqlite3"

    @property
    def trace_path(self) -> Path:
        """JSONL file that spans are appended to."""
        return self.resolve(self.state_dir) / "traces.jsonl"

    @property
    def effective_system_prompt(self) -> str:
        """The configured prompt, or the default with the agent name filled in."""
        if self.system_prompt.strip():
            return self.system_prompt
        return DEFAULT_SYSTEM_PROMPT.format(agent_name=self.agent_name)


DEFAULT_SYSTEM_PROMPT = """\
You are {agent_name}, a local development agent.

Answer in the language the user writes in. Be concrete and brief.

You have three kinds of capability:

- Tools: call them when you need a fact or an action you cannot produce yourself
  (current time, arithmetic, reading files, searching the knowledge base).
- Skills: short human-written procedures, listed below. When one matches the
  task, call `load_skill` and read it fully before acting.
- Knowledge base: `search_knowledge_base` is provided by an external service
  (another MCP server) and may not always be available. When it is in your tool
  list, call it before answering questions about indexed documents, and name the
  source file you used. If it is missing, say so instead of guessing.

Never invent tool results. If a tool fails, report the failure.
"""
