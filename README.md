# agent-template

A minimal-but-complete template for building a **local development agent**:
LLM calls, function calling, skills, MCP and RAG — wired end to end, with every
subsystem swappable on its own.

> **Status:** under construction. The layout below is the target; capabilities
> are added one commit at a time.

## What it covers

| Capability | Where it lives | Notes |
| --- | --- | --- |
| LLM calls | `src/agent_template/llm/` | OpenAI-compatible HTTP adapter + offline mock |
| Function calling | `src/agent_template/tools/` | Type hints to JSON Schema, auto-registered |
| Skills | `src/agent_template/skills/` | `SKILL.md` with progressive disclosure |
| MCP | `src/agent_template/mcp/` | stdio client + a bundled example server |
| RAG | `src/agent_template/rag/` | load, chunk, embed, store, retrieve |
| Agent loop | `src/agent_template/agent/` | reason/act loop, streaming events, memory |

## Quickstart

```bash
uv sync
cp .env.example .env   # then fill in AGENT_LLM_API_KEY
uv run agent ask "hello"
```

## License

MIT — see [LICENSE](LICENSE).