"""
backend/database/vector_client.py
──────────────────────────────────
Async ChromaDB HTTP client factory.

ChromaDB is used as the semantic retrieval memory layer for:
  • Claims similarity search (find analogous past claims for adjudication)
  • Policy RAG (retrieve relevant policy sections during LLM inference)
  • Knowledge base retrieval for underwriting guidelines

The client is connection-pooled via httpx under the hood by the ChromaDB SDK.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import chromadb
import structlog

from backend.config import get_settings

logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def get_chroma_client() -> Any:
    """
    Return a cached ChromaDB async HTTP client.
    Called once and reused across the application lifespan.
    """
    settings = get_settings()
    client = chromadb.AsyncHttpClient(
        host=settings.chroma_host,
        port=settings.chroma_port,
    )
    logger.info(
        "chromadb_client_created",
        host=settings.chroma_host,
        port=settings.chroma_port,
    )
    return client


async def get_or_create_collection(
    collection_name: str,
    metadata: dict | None = None,
) -> Any:
    """
    Idempotently get or create a ChromaDB collection.

    Args:
        collection_name: Name of the ChromaDB collection.
        metadata: Optional collection-level metadata (e.g., distance function).

    Returns:
        A ChromaDB Collection object ready for upsert/query operations.
    """
    client = get_chroma_client()
    collection = await client.get_or_create_collection(
        name=collection_name,
        metadata=metadata or {"hnsw:space": "cosine"},
    )
    logger.debug("chroma_collection_ready", collection=collection_name)
    return collection


async def get_claims_collection() -> Any:
    """Shorthand for the claims vector collection."""
    settings = get_settings()
    return await get_or_create_collection(settings.chroma_collection_claims)


async def get_policies_collection() -> Any:
    """Shorthand for the policies vector collection."""
    settings = get_settings()
    return await get_or_create_collection(settings.chroma_collection_policies)
