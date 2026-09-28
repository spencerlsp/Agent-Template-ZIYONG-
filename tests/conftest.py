"""共享夹具。

这里刻意**不直接使用 `Settings()`**：它会读取项目根目录的 `.env`，
于是测试结果会随开发者的本地配置（模型名、chunk 大小……）变化。
所有夹具都显式传值，并指向 `tmp_path`，不碰真实的 `.agent/` 和 `data/`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_template.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """一份完全受控的配置：离线模型、小型切块参数、隔离的目录。"""
    return Settings(
        llm_provider="mock",
        embedding_provider="local_hash",
        embedding_dim=128,
        chunk_size=200,
        chunk_overlap=40,
        max_steps=4,
        project_root=tmp_path,
        skills_dir=Path("skills"),
        knowledge_dir=Path("data/knowledge"),
        state_dir=Path(".agent"),
        workspace_root=Path("."),
        # 清空默认的示例 MCP 服务器：单元测试不该拉起子进程
        mcp_servers=[],
        # 显式关掉重排：单元测试不该发真实网络请求。不加这行的话，开发机
        # .env 里打开 AGENT_RERANK_PROVIDER 后，用到检索的用例会悄悄去打真接口
        rerank_provider="none",
    )


def is_error(text: str) -> bool:
    """工具返回的是不是错误文本。

    同时接受中英文前缀——错误文案的措辞可能被翻译，但"错误一定以错误开头"
    这个约定不变，测试不该绑死在具体翻译上。
    """
    return text.startswith(("Error", "错误"))
