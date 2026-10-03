import json

import requests

from chatspark.runtime.config import settings


def _request(messages, max_tokens, stream):
    if not settings.OPENAI_COMPAT_MODEL:
        raise ValueError("Configure an explicit generation model and endpoint")
    headers = {"Content-Type": "application/json"}
    if settings.OPENAI_COMPAT_API_KEY:
        headers["Authorization"] = "Bearer " + settings.OPENAI_COMPAT_API_KEY
    return requests.post(
        settings.OPENAI_COMPAT_BASE_URL.rstrip("/") + "/chat/completions",
        headers=headers,
        json={
            "model": settings.OPENAI_COMPAT_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": stream,
        },
        timeout=settings.OPENAI_COMPAT_TIMEOUT,
        stream=stream,
    )


def generate(messages, *, max_tokens=1024):
    try:
        with _request(messages, max_tokens, False) as response:
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise ValueError("Invalid generation output")
            return text
    except Exception:
        raise RuntimeError("Generation endpoint failed") from None


def stream_generate(messages, *, max_tokens=1024):
    try:
        with _request(messages, max_tokens, True) as response:
            response.raise_for_status()
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    return
                content = json.loads(data)["choices"][0]["delta"].get("content")
                if content:
                    if not isinstance(content, str):
                        raise ValueError("Invalid stream output")
                    yield content
    except Exception:
        raise RuntimeError("Generation endpoint failed") from None
