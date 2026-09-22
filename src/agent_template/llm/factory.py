"""Turn settings into a concrete LLM client
    根据配置项实例化一个具体的大模型客户端
"""

from __future__ import annotations # 支持前向类型引用，允许类内部注解使用自身类型

from agent_template.config import Settings
from agent_template.llm.base import LLMClient
from agent_template.llm.mock import MockLLM
from agent_template.llm.openai_compat import OpenAICompatiClient


def build_llm(settings: Settings) -> LLMClient:
    """Pick a provider from AGENT_LLM_PROVIDER"""
    if settings.llm_provider == "mock":
        return MockLLM(model="mock-01")

    if settings.llm_provider == "openai_compat":
        api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
        return OpenAICompatiClient(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=api_key,
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            default_temperature=settings.llm_temperature,
            default_max_tokens=settings.llm_max_tokens,
        )

    raise ValueError(f"unknown llm_provider: {settings.llm_provider}")