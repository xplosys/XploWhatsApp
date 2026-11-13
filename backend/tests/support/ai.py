"""Fake Gemini, OpenAI and DeepSeek APIs answering in each provider's documented shape.

Queue answers per provider with `reply()`, `fail()` or `raw()`; with nothing queued a
provider answers a polite generic reply. Every request is recorded."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

HOSTS = {
    "api.openai.com": "openai",
    "generativelanguage.googleapis.com": "gemini",
    "api.deepseek.com": "deepseek",
}

DIMENSIONS = 1536
STOPWORDS = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "and",
    "or",
    "of",
    "to",
    "for",
    "in",
    "on",
    "at",
    "do",
    "you",
    "your",
    "we",
    "our",
    "i",
    "it",
    "what",
    "how",
    "much",
    "does",
    "can",
    "with",
    "be",
    "by",
    "from",
}


def fake_vector(text: str, dims: int = DIMENSIONS) -> list[float]:
    """Hashed bag of words: texts sharing words get a high cosine similarity, so
    retrieval tests behave like a real embedding model on simple questions."""
    vector = [0.0] * dims
    for word in re.findall(r"[a-z0-9₹]+", text.lower()):
        if word in STOPWORDS:
            continue
        word = word.removesuffix("s") if len(word) > 3 else word
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


MODELS = {
    "openai": ["gpt-test-mini", "gpt-test", "text-embedding-test"],
    "gemini": ["gemini-test-flash", "gemini-test-pro"],
    "deepseek": ["deepseek-test-chat"],
}


@dataclass
class FakeAI:
    queues: dict[str, list[tuple[str, Any]]] = field(default_factory=lambda: {p: [] for p in MODELS})
    requests: list[tuple[str, httpx.Request]] = field(default_factory=list)
    valid_keys: set[str] = field(default_factory=set)  # empty: any key works
    embed_failures: list[tuple[str, int]] = field(default_factory=list)
    embed_dimensions: int | None = None  # override to simulate a model with the wrong size

    def reply(
        self, provider: str, text: str, *, needs_human: bool = False, out_of_scope: bool = False
    ) -> None:
        payload = json.dumps({"reply": text, "needs_human": needs_human, "out_of_scope": out_of_scope})
        self.queues[provider].append(("text", payload))

    def raw(self, provider: str, text: str) -> None:
        self.queues[provider].append(("text", text))

    def fail(self, provider: str, status: int, message: str = "error") -> None:
        self.queues[provider].append(("error", (status, message)))

    def special(self, provider: str, kind: str) -> None:
        """kind: blocked | cut_off"""
        self.queues[provider].append((kind, None))

    def calls(self, provider: str) -> list[dict[str, Any]]:
        return [
            json.loads(r.content)
            for p, r in self.requests
            if p == provider and r.method == "POST" and not _is_embedding(r)
        ]

    def embed_calls(self, provider: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for p, r in self.requests if p == provider and _is_embedding(r)]

    def fail_embedding(self, provider: str, status: int) -> None:
        self.embed_failures.append((provider, status))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ---- request handling --------------------------------------------------------

    def _key(self, provider: str, request: httpx.Request) -> str:
        if provider == "gemini":
            assert "key" not in request.url.params, "API key must not be in the URL"
            return request.headers.get("x-goog-api-key", "")
        return request.headers.get("authorization", "").removeprefix("Bearer ")

    def handler(self, request: httpx.Request) -> httpx.Response:
        provider = HOSTS[request.url.host]
        self.requests.append((provider, request))
        key = self._key(provider, request)
        if self.valid_keys and key not in self.valid_keys:
            if provider == "gemini":
                return httpx.Response(
                    400,
                    json={
                        "error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT"}
                    },
                )
            return httpx.Response(
                401,
                json={"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}},
            )
        if request.method == "GET":
            return self._models(provider)
        body = json.loads(request.content)
        if _is_embedding(request):
            return self._embed(provider, request, body)
        kind, value = (
            self.queues[provider].pop(0)
            if self.queues[provider]
            else (
                "text",
                json.dumps(
                    {"reply": "Thanks for your message!", "needs_human": False, "out_of_scope": False}
                ),
            )
        )
        if kind == "error":
            status, message = value
            return httpx.Response(status, json={"error": {"message": message}})
        return getattr(self, f"_{provider}")(body, kind, value)

    def _embed(self, provider: str, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        for i, (p, status) in enumerate(self.embed_failures):
            if p == provider:
                self.embed_failures.pop(i)
                return httpx.Response(status, json={"error": {"message": f"embedding failed {status}"}})
        dims = self.embed_dimensions
        if provider == "openai":
            assert body["dimensions"] == DIMENSIONS and body["encoding_format"] == "float"
            texts = body["input"]
            data = [
                {"object": "embedding", "index": i, "embedding": fake_vector(t, dims or DIMENSIONS)}
                for i, t in enumerate(texts)
            ]
            tokens = sum(len(t) // 4 + 1 for t in texts)
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": list(reversed(data)),  # order must come from `index`
                    "model": body["model"],
                    "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
                },
            )
        assert provider == "gemini"
        path_model = request.url.path.split("/models/", 1)[1].split(":", 1)[0]
        embeddings, tokens = [], 0
        for item in body["requests"]:
            assert item["model"] == f"models/{path_model}"
            config = item["embedContentConfig"]
            assert config["outputDimensionality"] == DIMENSIONS
            assert config["taskType"] in ("RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT")
            text = item["content"]["parts"][0]["text"]
            embeddings.append({"values": fake_vector(text, dims or DIMENSIONS)})
            tokens += len(text) // 4 + 1
        return httpx.Response(
            200, json={"embeddings": embeddings, "usageMetadata": {"promptTokenCount": tokens}}
        )

    def _models(self, provider: str) -> httpx.Response:
        ids = MODELS[provider]
        if provider == "gemini":
            models = [
                {
                    "name": f"models/{m}",
                    "displayName": m.title(),
                    "inputTokenLimit": 1_000_000,
                    "outputTokenLimit": 65_536,
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                }
                for m in ids
            ]
            models.append(
                {
                    "name": "models/gemini-embedding-test",
                    "displayName": "Gemini Embedding Test",
                    "supportedGenerationMethods": ["embedContent", "countTokens"],
                }
            )
            return httpx.Response(200, json={"models": models})
        return httpx.Response(
            200, json={"object": "list", "data": [{"id": m, "object": "model"} for m in ids]}
        )

    def _openai(self, body: dict[str, Any], kind: str, value: Any) -> httpx.Response:
        assert body["store"] is False and body["text"]["format"]["type"] == "json_schema"
        usage = {
            "input_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 200},
            "output_tokens": 80,
            "total_tokens": 1280,
        }
        if kind == "blocked":
            content = [{"type": "refusal", "refusal": "I can't help with that."}]
            return httpx.Response(
                200,
                json={
                    "id": "resp_1",
                    "status": "completed",
                    "model": body["model"],
                    "output": [{"type": "message", "role": "assistant", "content": content}],
                    "usage": usage,
                },
            )
        if kind == "cut_off":
            return httpx.Response(
                200,
                json={
                    "id": "resp_1",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "model": body["model"],
                    "output": [],
                    "usage": usage,
                },
            )
        output = [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": value, "annotations": []}],
            },
        ]
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": body["model"],
                "output": output,
                "usage": usage,
            },
        )

    def _gemini(self, body: dict[str, Any], kind: str, value: Any) -> httpx.Response:
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert body["generationConfig"]["responseSchema"]["type"] == "OBJECT"
        usage = {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 60,
            "thoughtsTokenCount": 40,
            "totalTokenCount": 1100,
        }
        if kind == "blocked":
            return httpx.Response(
                200, json={"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": usage}
            )
        if kind == "cut_off":
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "MAX_TOKENS"}],
                    "usageMetadata": usage,
                },
            )
        parts = [{"text": "thinking...", "thought": True}, {"text": value}]
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
                "usageMetadata": usage,
                "modelVersion": "gemini-test-flash",
            },
        )

    def _deepseek(self, body: dict[str, Any], kind: str, value: Any) -> httpx.Response:
        assert body["response_format"] == {"type": "json_object"}
        assert "json" in body["messages"][0]["content"].lower()
        usage = {
            "prompt_tokens": 900,
            "completion_tokens": 50,
            "prompt_cache_hit_tokens": 300,
            "prompt_cache_miss_tokens": 600,
        }
        if kind == "blocked":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": ""},
                            "finish_reason": "content_filter",
                        }
                    ],
                    "usage": usage,
                },
            )
        if kind == "cut_off":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": '{"reply": "Hel'},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": usage,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "model": body["model"],
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": value}, "finish_reason": "stop"}
                ],
                "usage": usage,
            },
        )


def _is_embedding(request: httpx.Request) -> bool:
    path = request.url.path
    return path.endswith("/v1/embeddings") or path.endswith(":batchEmbedContents")
