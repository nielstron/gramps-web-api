"""Provider construction does not perform network calls."""

from gramps_webapi.api.llm.agent import create_agent


def test_openai_compatible_key_and_colon_model_name():
    agent = create_agent(
        model_name="qwen3:8b", base_url="http://ollama:11434/v1", api_key="saved-key"
    )
    assert agent.model.model_name == "qwen3:8b"
    assert agent.model.client.api_key == "saved-key"
    assert str(agent.model.client.base_url) == "http://ollama:11434/v1/"


def test_local_provider_without_key():
    agent = create_agent(model_name="qwen3:8b", base_url="http://ollama:11434/v1")
    assert agent.model.model_name == "qwen3:8b"
    assert agent.model.client.api_key == "not-required"
