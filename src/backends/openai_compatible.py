"""Small standard-library client for OpenAI-compatible chat-completions APIs."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def api_key_from_env(settings: dict[str, Any]) -> str | None:
    """Read an optional key at request time; configuration never contains it."""
    env_name = settings.get("api_key_env")
    if env_name is None or env_name == "":
        return None
    if not isinstance(env_name, str):
        raise ValueError("api_key_env must be a string or null.")
    value = os.environ.get(env_name)
    if not value:
        raise RuntimeError(f"OpenAI-compatible API key is missing. Set environment variable {env_name}.")
    return value


def chat_completion(
    settings: dict[str, Any], messages: list[dict[str, Any]], *, response_format: dict[str, Any] | None,
    timeout_seconds: int = 3600,
) -> dict[str, Any]:
    """POST /chat/completions without logging credentials or request bodies."""
    base_url = str(settings.get("base_url", "")).rstrip("/")
    model = settings.get("model")
    if not base_url or not model:
        raise ValueError("OpenAI-compatible backend requires base_url and model.")
    body: dict[str, Any] = {
        "model": model, "messages": messages,
        "temperature": settings.get("temperature", 0.1),
        "max_tokens": settings.get("max_tokens", 4096),
    }
    if response_format is not None:
        body["response_format"] = response_format
    headers = {"Content-Type": "application/json"}
    key = api_key_from_env(settings)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{base_url}/chat/completions", data=json.dumps(body).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI-compatible request failed (HTTP {error.code}): {detail[-1000:]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Cannot reach OpenAI-compatible endpoint at {base_url}.") from error
    if not isinstance(raw, dict):
        raise ValueError("OpenAI-compatible endpoint returned an invalid JSON response.")
    return raw
