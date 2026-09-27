"""本地开发 agent 模板。

各子系统的职责写在各自目录的模块文档里。数据流的起点是
`agent_template.agent.runtime.AgentRuntime`——它负责把模型、工具、技能、
MCP、RAG、记忆装配成一个可用的 agent；命令行入口在 `agent_template.cli`。
"""

__version__ = "0.1.0"
