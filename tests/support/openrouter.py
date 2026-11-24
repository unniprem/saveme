"""A mocked OpenRouter (httpx.MockTransport) and catalogue entries shaped like the live ones
(GET /api/v1/models, 24 Nov 2026). Paid API calls are never made by the standard suite."""
import asyncio
import json
from collections.abc import Callable

import httpx

KEY = "sk-or-v1-test0123456789"

HAIKU = {
    "id": "anthropic/claude-haiku-5.5", "name": "Anthropic: Claude Haiku 5.5", "context_length": 1_000_000,
    "architecture": {"input_modalities": ["text", "image", "file"], "output_modalities": ["text"]},
    "pricing": {"prompt": "0.0000001", "completion": "0.0000005", "input_cache_read": "0.00000001",
                "overrides": [{"min_prompt_tokens": 100000, "prompt": "0.0000005",
                               "completion": "0.0000025"}]},
    "top_provider": {"context_length": 1_000_000, "max_completion_tokens": 128000, "is_moderated": True},
    "supported_parameters": ["include_reasoning", "max_completion_tokens", "max_tokens", "reasoning",
                             "reasoning_effort", "response_format", "stop", "structured_outputs",
                             "tool_choice", "tools", "verbosity"],
    "reasoning": {"mandatory": False, "supported_efforts": ["max", "xhigh", "high", "medium", "low", "none"],
                  "default_effort": "medium"},
}
JSON_MODE = {
    "id": "example/json-mode", "name": "JSON mode only", "context_length": 131072,
    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    "pricing": {"prompt": "0.0000002", "completion": "0.0000006"},
    "top_provider": {"context_length": 131072, "max_completion_tokens": 8192},
    "supported_parameters": ["max_tokens", "response_format", "temperature"],
}
PLAIN = {
    "id": "example/plain", "name": "Plain text", "context_length": 32768,
    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    "pricing": {"prompt": "0.0000003", "completion": "0.0000009"},
    "top_provider": {"context_length": 32768, "max_completion_tokens": None},
    "supported_parameters": ["max_tokens", "temperature"],
}
ROUTER = {
    "id": "openrouter/auto", "name": "Auto Router", "context_length": 2_000_000,
    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    "pricing": {"prompt": "-1", "completion": "-1"},
    "supported_parameters": ["max_tokens", "response_format", "structured_outputs"],
}
IMAGE_OUT = {
    "id": "example/painter", "name": "Painter", "context_length": 32768,
    "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
    "pricing": {"prompt": "0.000001", "completion": "0.00004"},
    "supported_parameters": ["max_tokens"],
}
CATALOGUE = [HAIKU, JSON_MODE, PLAIN, ROUTER, IMAGE_OUT]


def completion(answer: dict | str, *, prompt_tokens: int = 1200, completion_tokens: int = 80,
               cost: float | None = 0.00016, finish_reason: str = "stop") -> dict:
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
             "total_tokens": prompt_tokens + completion_tokens}
    if cost is not None:
        usage["cost"] = cost
    content = answer if isinstance(answer, str) else json.dumps(answer)
    return {"id": "gen-1", "model": "anthropic/claude-haiku-5.5", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": content}}],
            "usage": usage}


class MockOpenRouter:
    """Serves /models (CATALOGUE), /key, and /chat/completions from `chat`: each item is a dict
    (a 200 JSON body), an httpx.Response, an exception to raise, or a callable taking the
    request. Every request is kept in `requests`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.chat: list = []
        self.catalogue = list(CATALOGUE)
        self.key_status = 200
        self.delay = 0.0  # seconds before any answer
        self.chat_delay = 0.0  # seconds before a chat completion's answer

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/chat/completions")]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": self.catalogue})
        if path.endswith("/key"):
            if self.key_status != 200:
                return httpx.Response(self.key_status, json={"error": {"code": self.key_status,
                                                                       "message": "No auth"}})
            return httpx.Response(200, json={"data": {"label": "sk-or-v1-tes...789", "usage": 1.25,
                                                      "limit": 10, "limit_remaining": 8.75,
                                                      "is_free_tier": False}})
        if path.endswith("/chat/completions"):
            if self.chat_delay:
                await asyncio.sleep(self.chat_delay)
            if not self.chat:
                return httpx.Response(500, json={"error": {"code": 500, "message": "no reply queued"}})
            item = self.chat.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, httpx.Response):
                return item
            if isinstance(item, Callable):
                return item(request)
            return httpx.Response(200, json=item)
        return httpx.Response(404, json={"error": {"code": 404, "message": "Not found"}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)
