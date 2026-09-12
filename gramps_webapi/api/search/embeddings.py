"""Functions to compute vector embeddings."""

from threading import RLock
from typing import Callable, List, Optional

import requests
from flask import current_app

from ...ai_config import get_ai_config

_model_lock = RLock()

from ..util import get_logger


def load_model(model_name: str):
    """Load the sentence transformer model.

    Since the model takes time to load and is subsequently cached,
    this can also be used for preloading the model in the flask app.
    """
    logger = get_logger()
    logger.debug("Initializing embedding model.")
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    logger.debug("Done initializing embedding model.")
    return model


def create_remote_embedding_function(
    base_url: str, model_name: str, api_key: Optional[str] = None
) -> Callable[[List[str]], List[List[float]]]:
    """Create an embedding function that calls a remote OpenAI-compatible API.

    Returns a callable with signature (texts: list[str]) -> list[list[float]].
    """
    stripped = base_url.rstrip("/")
    if stripped.endswith("/v1"):
        stripped = stripped[:-3]
    url = f"{stripped}/v1/embeddings"

    def _embed(texts: List[str]) -> List[List[float]]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {"model": model_name, "input": texts}
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()["data"]
        data.sort(key=lambda item: item["index"])
        return [item["embedding"] for item in data]

    return _embed


def get_embedding_function():
    """Lazily load one model per process, replacing it when settings change."""
    config = get_ai_config()
    model = config["VECTOR_EMBEDDING_MODEL"]
    if not config["AI_ENABLED"] or not model:
        raise ValueError("VECTOR_EMBEDDING_MODEL option not set")
    signature = (
        model,
        config["VECTOR_EMBEDDING_BASE_URL"],
        config["VECTOR_EMBEDDING_API_KEY"],
    )
    with _model_lock:
        cached = current_app.extensions.get("gramps_embedding")
        if cached is not None and cached[0] == signature:
            return cached[1], model
        if signature[1]:
            function = create_remote_embedding_function(
                signature[1], model, signature[2]
            )
        else:
            function = load_model(model).encode
        current_app.extensions["gramps_embedding"] = (signature, function)
        return function, model
