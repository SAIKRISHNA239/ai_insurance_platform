"""
backend/llm/client.py
──────────────────────
Provider-agnostic LLM client with an abstract base class.

Design: Strategy pattern — the active provider is selected at runtime from
the LLM_PROVIDER environment variable. Add new providers by subclassing
BaseLLMClient and registering in LLM_PROVIDER_REGISTRY.

Supported providers (initial):
  • openai — GPT-4o and compatible models via the OpenAI SDK
  • gemini  — Google Gemini via google-generativeai (stub)
  • bedrock — AWS Bedrock Claude (stub)
"""

from __future__ import annotations

import abc
from functools import lru_cache
from typing import Any

import structlog

from backend.config import get_settings

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Abstract Base
# ─────────────────────────────────────────────────────────────────────────────

class BaseLLMClient(abc.ABC):
    """Abstract interface all LLM provider clients must implement."""

    @abc.abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """
        Generate a completion from the model.

        Args:
            system_prompt: System-level instruction context.
            user_message: The user turn content.
            temperature: Sampling temperature (lower = more deterministic).
            max_tokens: Maximum output tokens.
            response_format: Optional dict to enforce structured output (e.g., {"type": "json_object"}).

        Returns:
            The model's text response as a plain string.
        """
        ...

    @abc.abstractmethod
    async def embed(self, text: str) -> list[float]:
        """
        Generate an embedding vector for the given text.

        Args:
            text: Input text to embed.

        Returns:
            List of floats representing the embedding vector.
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI Provider
# ─────────────────────────────────────────────────────────────────────────────

class OpenAIClient(BaseLLMClient):
    """OpenAI GPT-4o client using the async openai SDK."""

    def __init__(self) -> None:
        # Lazy import — avoids hard dependency if openai not installed
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError("Install 'openai' package: pip install openai")

        settings = get_settings()
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_model
        self._embedding_model = settings.embedding_model

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        response = await self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model=self._embedding_model,
            input=text,
        )
        return response.data[0].embedding


# ─────────────────────────────────────────────────────────────────────────────
# Stub Providers (implement when adding Gemini / Bedrock support)
# ─────────────────────────────────────────────────────────────────────────────

class GeminiClient(BaseLLMClient):
    """Google Gemini provider using the google-genai SDK (v2.x)."""

    def __init__(self) -> None:
        try:
            from google import genai
            from google.genai import types as genai_types  # noqa: F401 – ensure importable
        except ImportError:
            raise ImportError("Install 'google-genai': pip install google-genai")

        settings = get_settings()
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not set in .env")

        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model  = settings.gemini_model

        logger.info("gemini_client_initialized", model=self._model)

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        response_format: dict | None = None,
    ) -> str:
        import asyncio
        from google.genai import types as genai_types

        contents = [
            genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=f"{system_prompt}\n\n{user_message}")],
            )
        ]
        config = genai_types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )

        # google-genai v2 sync → run in thread executor so we don't block the loop
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            ),
        )
        return response.text or ""

    async def stream_complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ):
        """
        Async generator that yields text tokens from a Gemini streaming call.
        Usage:
            async for token in client.stream_complete(sys, usr):
                ...
        """
        import asyncio
        import queue
        import threading
        from google.genai import types as genai_types

        contents = [
            genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=f"{system_prompt}\n\n{user_message}")],
            )
        ]
        config = genai_types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )

        token_queue: queue.Queue[str | None] = queue.Queue()

        def _stream_in_thread():
            try:
                for chunk in self._client.models.generate_content_stream(
                    model=self._model,
                    contents=contents,
                    config=config,
                ):
                    if chunk.text:
                        token_queue.put(chunk.text)
            except Exception as exc:
                token_queue.put(f"__ERROR__:{exc}")
            finally:
                token_queue.put(None)  # sentinel

        thread = threading.Thread(target=_stream_in_thread, daemon=True)
        thread.start()

        while True:
            token = await asyncio.get_event_loop().run_in_executor(None, token_queue.get)
            if token is None:
                break
            if token.startswith("__ERROR__:"):
                raise RuntimeError(token[len("__ERROR__:"):])
            yield token

    async def embed(self, text: str) -> list[float]:
        import asyncio
        from google.genai import types as genai_types

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.models.embed_content(
                model="text-embedding-004",
                contents=text,
                config=genai_types.EmbedContentConfig(output_dimensionality=768),
            ),
        )
        return response.embeddings[0].values



class BedrockClient(BaseLLMClient):
    """AWS Bedrock Claude provider stub."""

    async def complete(self, system_prompt, user_message, temperature=0.2, max_tokens=2048, response_format=None) -> str:
        raise NotImplementedError("BedrockClient is not yet implemented.")

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError("BedrockClient is not yet implemented.")


# ─────────────────────────────────────────────────────────────────────────────
# Provider Registry + Factory
# ─────────────────────────────────────────────────────────────────────────────

LLM_PROVIDER_REGISTRY: dict[str, type[BaseLLMClient]] = {
    "openai": OpenAIClient,
    "gemini": GeminiClient,
    "bedrock": BedrockClient,
}


@lru_cache(maxsize=1)
def get_llm_client() -> BaseLLMClient:
    """
    Return a cached LLM client based on the LLM_PROVIDER setting.
    The singleton is created once per process lifecycle.
    """
    settings = get_settings()
    provider_cls = LLM_PROVIDER_REGISTRY.get(settings.llm_provider)
    if provider_cls is None:
        raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")

    client = provider_cls()
    logger.info("llm_client_initialized", provider=settings.llm_provider)
    return client
