"""Persisted AI settings shared by web and task workers."""

import json
import os

from flask import current_app

from .auth import config_get

AI_CONFIG_KEYS = {
    "AI_ENABLED",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "VECTOR_EMBEDDING_MODEL",
    "VECTOR_EMBEDDING_BASE_URL",
    "VECTOR_EMBEDDING_API_KEY",
}


def get_ai_config() -> dict:
    """Read a consistent snapshot, with deployment settings as defaults."""
    config = {key: current_app.config.get(key) for key in AI_CONFIG_KEYS}
    config["AI_ENABLED"] = current_app.config.get("AI_ENABLED", True)
    config["LLM_API_KEY"] = current_app.config.get("LLM_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )
    stored = config_get("AI_SETTINGS")
    if stored is not None:
        config.update(json.loads(stored))
    return config
