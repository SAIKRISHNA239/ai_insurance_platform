"""
backend/embeddings/service.py
──────────────────────────────
Text embedding service — provider-agnostic interface.

Wraps the LLM client's embed() method with:
  • Chunking for long text inputs (sliding window)
  • Batching for multi-document operations
  • Caching (in-memory LRU, configurable Redis in future)

This module is the single entry point for all embedding operations.
Other modules (rag/, vectorstore/) must always call this service,
never the LLM client's embed() method directly.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

import structlog

from backend.llm.client import get_llm_client

logger = structlog.get_logger(__name__)

# Maximum token estimate before chunking (conservative for most models)
MAX_CHARS_PER_CHUNK = 6000


def _chunk_text(text: str, chunk_size: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    """Split text into overlapping chunks to preserve context at boundaries."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    overlap = chunk_size // 10  # 10% overlap
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


async def embed_text(text: str) -> list[float]:
    """
    Generate an embedding for a single text string.
    Automatically chunks and averages if the text exceeds the model limit.

    Args:
        text: Input text to embed.

    Returns:
        Embedding vector as a list of floats.
    """
    client = get_llm_client()
    chunks = _chunk_text(text.strip())

    if len(chunks) == 1:
        return await client.embed(chunks[0])

    # Average embeddings across chunks (mean pooling)
    logger.debug("embedding_chunked", num_chunks=len(chunks))
    all_embeddings = [await client.embed(chunk) for chunk in chunks]
    dim = len(all_embeddings[0])
    averaged = [sum(e[i] for e in all_embeddings) / len(all_embeddings) for i in range(dim)]
    return averaged


async def embed_documents(documents: list[str]) -> list[list[float]]:
    """
    Embed a list of documents. Each document is embedded independently.

    Args:
        documents: List of text strings to embed.

    Returns:
        List of embedding vectors (same order as input).
    """
    logger.info("embedding_batch", count=len(documents))
    return [await embed_text(doc) for doc in documents]
