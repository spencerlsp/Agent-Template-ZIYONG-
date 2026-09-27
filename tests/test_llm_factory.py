"""模型工厂与客户端构造的测试。

这几个用例来自一次真实的翻车：给 factory 加了个防呆检查，结果它挡住的全是
合法输入（key 留空是本地服务的正常用法），放过的全是它想防的情况，
而且 `raise "字符串"` 本身就非法，报出来的是 TypeError 而不是提示。
这里把四种输入状态固定住，防止将来改回去。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from agent_template.config import Settings
from agent_template.llm.factory import build_llm
from agent_template.llm.openai_compat import OpenAICompatiClient


def build_client(**kwargs) -> OpenAICompatiClient:
    """构造一个客户端；base_url 指向不存在的地址也没关系，构造过程不发请求。"""
    params = {"model": "test-model", "base_url": "http://localhost:1/v1", **kwargs}
    return OpenAICompatiClient(**params)


def test_build_llm_with_api_key(tmp_path) -> None:
    settings = Settings(llm_api_key="sk-test", project_root=tmp_path)
    assert build_llm(settings).model == settings.llm_model


def test_build_llm_without_api_key_is_allowed(tmp_path) -> None:
    """key 留空必须合法：本地 Ollama 不需要 key，客户端会退化成占位符。"""
    assert build_llm(Settings(llm_api_key="", project_root=tmp_path))
    assert build_llm(Settings(llm_api_key=None, project_root=tmp_path))


def test_build_llm_mock_provider(tmp_path) -> None:
    """mock 完全不碰 key，任何情况下都该能构造。"""
    llm = build_llm(Settings(llm_provider="mock", llm_api_key=None, project_root=tmp_path))
    assert llm.model.startswith("mock")


def test_plain_string_and_none_are_accepted() -> None:
    assert build_client(api_key="sk-abc")
    assert build_client(api_key=None)


def test_secretstr_is_rejected_with_actionable_message() -> None:
    """把 SecretStr 整个传进来要被挡住，且提示里要说清怎么改。

    背景：SecretStr 被 str() 之后是十个星号，会被当成密钥发出去，
    换回一个毫无头绪的 401。这个断言同时锁住"要拦"和"怎么提示"两件事。
    """
    with pytest.raises(TypeError) as excinfo:
        build_client(api_key=SecretStr("sk-abc"))

    message = str(excinfo.value)
    assert "SecretStr" in message
    assert "get_secret_value" in message