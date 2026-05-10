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
    ) -> str:
        """
        Generate a completion from the model.

        Args:
            system_prompt: System-level instruction context.
            user_message: The user turn content.
            temperature: Sampling temperature (lower = more deterministic).
            max_tokens: Maximum output tokens.

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
    ) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
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
    """Google Gemini provider stub."""

    async def complete(self, system_prompt, user_message, temperature=0.2, max_tokens=2048) -> str:
        raise NotImplementedError("GeminiClient is not yet implemented.")

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError("GeminiClient is not yet implemented.")


class BedrockClient(BaseLLMClient):
    """AWS Bedrock Claude provider stub."""

    async def complete(self, system_prompt, user_message, temperature=0.2, max_tokens=2048) -> str:
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
